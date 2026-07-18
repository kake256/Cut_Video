"""Reject privacy-sensitive additions before they enter the repository.

The default mode scans only the staged patch, which makes the command suitable
for a pre-commit hook.  ``--working-tree`` scans the complete tracked working
tree delta and untracked source files for a manual, pre-staging check.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


FORBIDDEN_ROOTS = frozenset({"data", "video", "clips", "exports"})
DANGEROUS_SUFFIXES = (
    ".vindex.zip",
    ".zip",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".index",
    ".faiss",
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".webm",
    ".mpeg",
    ".mpg",
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".srt",
    ".vtt",
)
SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".md",
        ".rst",
        ".txt",
        ".json",
        ".toml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
        ".conf",
        ".ps1",
        ".sh",
        ".bat",
        ".cmd",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".html",
        ".css",
        ".scss",
        ".sql",
        ".csv",
    }
)
SOURCE_NAMES = frozenset(
    {
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        ".env.example",
        "dockerfile",
        "license",
        "makefile",
        "readme",
    }
)

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_WINDOWS_USER_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:)?[\\/]+Users[\\/]+(?P<user>[^\\/:\s\"'<>|]+)"
)
_UNIX_HOME_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])/(?:(?:home)|(?:Users))/(?P<user>[^/\s\"'<>|]+)"
)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")
_TOKEN_PATTERNS = (
    ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("OPENAI_API_KEY", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|passwd|secret[_-]?key)\b\s*(?:=|:)\s*([\"'])([^\"']{12,})\1"
)
_BARE_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|passwd|secret[_-]?key)\b\s*=\s*([A-Za-z0-9_./+=:@-]{16,})\s*(?:#.*)?$"
)
_DERIVED_VIDEO_FIXTURE_RE = re.compile(
    r"(?<!\d)\d{8}_\d{6}_[A-Za-z0-9_-]{11}"
    r"\.(?:mp4|mkv|mov|avi|webm)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_PLACEHOLDER_USERS = frozenset(
    {"user", "users", "username", "yourname", "example", "runner", "public"}
)
_PLACEHOLDER_MARKERS = (
    "example",
    "placeholder",
    "replace-me",
    "replace_me",
    "changeme",
    "change-me",
    "dummy",
    "fake",
    "redacted",
    "your_",
    "your-",
    "<",
    ">",
    "${",
    "{{",
    "***",
)


@dataclass(frozen=True)
class AddedLine:
    path: str
    line_number: int
    text: str


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line_number: int
    message: str


def normalize_repo_path(path: str) -> str:
    """Return a stable repository-relative path without touching the disk."""

    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def is_source_file(path: str) -> bool:
    """Whether an untracked file is safe and useful to inspect as text."""

    normalized = normalize_repo_path(path)
    name = PurePosixPath(normalized).name.lower()
    suffix = PurePosixPath(normalized).suffix.lower()
    return suffix in SOURCE_SUFFIXES or name in SOURCE_NAMES


def find_path_issues(path: str) -> list[Finding]:
    """Detect repository paths that should never be published."""

    normalized = normalize_repo_path(path)
    if not normalized:
        return []
    lowered = normalized.lower()
    parts = tuple(part.lower() for part in PurePosixPath(normalized).parts)
    findings: list[Finding] = []

    # These are repository-root runtime directories.  A source path such as
    # ``package/data/schema.py`` is not the private runtime ``data/`` tree.
    forbidden = parts[0] if parts and parts[0] in FORBIDDEN_ROOTS else None
    if forbidden is not None:
        findings.append(
            Finding(
                "FORBIDDEN_PATH",
                normalized,
                0,
                f"private runtime directory '{forbidden}/' must not be committed",
            )
        )

    dangerous = next(
        (suffix for suffix in DANGEROUS_SUFFIXES if lowered.endswith(suffix)), None
    )
    if dangerous is not None:
        findings.append(
            Finding(
                "DANGEROUS_FILE",
                normalized,
                0,
                f"shared archive, index, database, transcript, or media file ({dangerous})",
            )
        )
    return findings


def parse_added_lines(patch: str) -> list[AddedLine]:
    """Extract only added content lines and their new-file line numbers."""

    current_path: str | None = None
    new_line_number: int | None = None
    added: list[AddedLine] = []

    for raw_line in patch.splitlines():
        if raw_line.startswith("+++ "):
            marker = raw_line[4:]
            if marker == "/dev/null":
                current_path = None
            else:
                current_path = normalize_repo_path(
                    marker[2:] if marker.startswith("b/") else marker
                )
            continue

        hunk = _HUNK_RE.match(raw_line)
        if hunk:
            new_line_number = int(hunk.group(1))
            continue

        if current_path is None or new_line_number is None:
            continue
        if raw_line.startswith("+"):
            added.append(AddedLine(current_path, new_line_number, raw_line[1:]))
            new_line_number += 1
        elif raw_line.startswith("-") or raw_line.startswith("\\ No newline"):
            continue
        else:
            new_line_number += 1

    return added


def _is_placeholder_user(user: str) -> bool:
    lowered = user.strip("{}[]()$% ").lower()
    return lowered in _PLACEHOLDER_USERS or not lowered


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return True
    compact = re.sub(r"\s+", "", lowered)
    return not compact or len(set(compact)) <= 2


def find_line_issues(line: AddedLine) -> list[Finding]:
    """Detect sensitive literals in one newly added source line."""

    text = line.text
    findings: list[Finding] = []

    for match in _WINDOWS_USER_PATH_RE.finditer(text):
        if not _is_placeholder_user(match.group("user")):
            findings.append(
                Finding(
                    "LOCAL_USER_PATH",
                    line.path,
                    line.line_number,
                    "literal Windows user-profile path",
                )
            )
            break

    for match in _UNIX_HOME_PATH_RE.finditer(text):
        if not _is_placeholder_user(match.group("user")):
            findings.append(
                Finding(
                    "LOCAL_USER_PATH",
                    line.path,
                    line.line_number,
                    "literal Unix/macOS home path",
                )
            )
            break

    if _PRIVATE_KEY_RE.search(text):
        findings.append(
            Finding(
                "PRIVATE_KEY",
                line.path,
                line.line_number,
                "private-key material",
            )
        )

    for rule, pattern in _TOKEN_PATTERNS:
        if pattern.search(text):
            findings.append(
                Finding(rule, line.path, line.line_number, "credential-like token literal")
            )

    credential = _CREDENTIAL_ASSIGNMENT_RE.search(text)
    if credential and not _looks_like_placeholder(credential.group(2)):
        findings.append(
            Finding(
                "CREDENTIAL",
                line.path,
                line.line_number,
                "non-placeholder credential assignment",
            )
        )

    bare_credential = _BARE_CREDENTIAL_ASSIGNMENT_RE.search(text)
    if bare_credential and not _looks_like_placeholder(bare_credential.group(1)):
        findings.append(
            Finding(
                "CREDENTIAL",
                line.path,
                line.line_number,
                "non-placeholder credential assignment",
            )
        )

    if _DERIVED_VIDEO_FIXTURE_RE.search(text):
        findings.append(
            Finding(
                "DERIVED_VIDEO_FIXTURE",
                line.path,
                line.line_number,
                "video fixture name resembles a real dated download",
            )
        )
    return findings


def scan_changes(paths: Iterable[str], added_lines: Iterable[AddedLine]) -> list[Finding]:
    """Purely scan already-collected paths and added source lines."""

    findings: list[Finding] = []
    for path in paths:
        findings.extend(find_path_issues(path))
    for line in added_lines:
        findings.extend(find_line_issues(line))

    unique = {(item.rule, item.path, item.line_number, item.message): item for item in findings}
    return sorted(
        unique.values(), key=lambda item: (item.path.lower(), item.line_number, item.rule)
    )


def _run_git(repo: Path, arguments: Sequence[str]) -> bytes:
    completed = subprocess.run(
        ["git", "-c", "core.quotePath=false", *arguments],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"git {' '.join(arguments)} failed")
    return completed.stdout


def _tracked_delta_arguments(working_tree: bool) -> list[str]:
    if working_tree:
        return ["diff", "HEAD"]
    return ["diff", "--cached"]


def _collect_tracked_changes(repo: Path, working_tree: bool) -> tuple[list[str], list[AddedLine]]:
    base = _tracked_delta_arguments(working_tree)
    common = ["--no-renames", "--no-ext-diff", "--no-color"]
    patch = _run_git(repo, [*base, *common, "--unified=0", "--"]).decode(
        "utf-8", errors="replace"
    )
    names = _run_git(
        repo, [*base, *common, "--name-only", "--diff-filter=ACMR", "-z", "--"]
    )
    paths = [
        normalize_repo_path(item.decode("utf-8", errors="surrogateescape"))
        for item in names.split(b"\0")
        if item
    ]
    return paths, parse_added_lines(patch)


def _collect_untracked(repo: Path) -> tuple[list[str], list[AddedLine]]:
    output = _run_git(repo, ["ls-files", "--others", "--exclude-standard", "-z"])
    paths = [
        normalize_repo_path(item.decode("utf-8", errors="surrogateescape"))
        for item in output.split(b"\0")
        if item
    ]
    lines: list[AddedLine] = []
    for relative in paths:
        # Private/dangerous paths are reported by name and deliberately never opened.
        if find_path_issues(relative) or not is_source_file(relative):
            continue
        candidate = repo / Path(relative)
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            raise RuntimeError(f"cannot read untracked source file {relative}: {error}") from error
        lines.extend(
            AddedLine(relative, number, text_line)
            for number, text_line in enumerate(text.splitlines(), start=1)
        )
    return paths, lines


def collect_changes(repo: Path, working_tree: bool) -> tuple[list[str], list[AddedLine]]:
    """Collect Git additions; filesystem access is confined to untracked source files."""

    paths, lines = _collect_tracked_changes(repo, working_tree)
    if working_tree:
        untracked_paths, untracked_lines = _collect_untracked(repo)
        paths.extend(untracked_paths)
        lines.extend(untracked_lines)
    return paths, lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check newly added repository content for private data."
    )
    parser.add_argument(
        "--working-tree",
        action="store_true",
        help="scan all tracked changes against HEAD and untracked source files",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    try:
        paths, lines = collect_changes(options.repo.resolve(), options.working_tree)
    except RuntimeError as error:
        print(f"privacy guard could not inspect Git changes: {error}", file=sys.stderr)
        return 2

    findings = scan_changes(paths, lines)
    if not findings:
        scope = "working tree" if options.working_tree else "staged changes"
        print(f"privacy guard: no sensitive additions found in {scope}")
        return 0

    print(f"privacy guard: blocked by {len(findings)} sensitive addition(s)", file=sys.stderr)
    for finding in findings:
        location = finding.path
        if finding.line_number:
            location += f":{finding.line_number}"
        print(f"  {location}: [{finding.rule}] {finding.message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
