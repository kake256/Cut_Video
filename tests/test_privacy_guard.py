import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.check_privacy import (
    AddedLine,
    build_parser,
    find_line_issues,
    find_path_issues,
    is_source_file,
    parse_added_lines,
    scan_changes,
)


class PrivacyGuardTest(unittest.TestCase):
    def rules_for_line(self, text):
        return {item.rule for item in find_line_issues(AddedLine("safe.py", 7, text))}

    def test_default_is_staged_and_working_tree_is_explicit(self):
        self.assertFalse(build_parser().parse_args([]).working_tree)
        self.assertTrue(build_parser().parse_args(["--working-tree"]).working_tree)

    def test_forbidden_runtime_directories_are_rejected(self):
        for path in (
            "data/index.db",
            "video/source.bin",
            "clips/result.bin",
            "exports/shared.bin",
        ):
            with self.subTest(path=path):
                self.assertIn("FORBIDDEN_PATH", {item.rule for item in find_path_issues(path)})

    def test_dangerous_archives_indexes_databases_and_media_are_rejected(self):
        suffixes = ("bundle.vindex.zip", "cache.db", "text.index", "source.mp4")
        for path in suffixes:
            with self.subTest(path=path):
                self.assertIn("DANGEROUS_FILE", {item.rule for item in find_path_issues(path)})

    def test_ordinary_source_paths_are_allowed(self):
        self.assertEqual(find_path_issues("moment_retrieval/search.py"), [])
        self.assertEqual(find_path_issues("docs/design.md"), [])
        self.assertEqual(find_path_issues("package/data/schema.py"), [])

    def test_only_source_like_untracked_files_are_readable_candidates(self):
        self.assertTrue(is_source_file("scripts/check_privacy.py"))
        self.assertTrue(is_source_file(".gitignore"))
        self.assertTrue(is_source_file(".env.example"))
        self.assertFalse(is_source_file("artifact.bin"))
        self.assertFalse(is_source_file("fixture.mp4"))

    def test_patch_parser_returns_only_added_lines_with_new_line_numbers(self):
        patch = (
            "diff --git a/safe.py b/safe.py\n"
            "--- a/safe.py\n"
            "+++ b/safe.py\n"
            "@@ -2,2 +2,3 @@\n"
            " context\n"
            "-removed\n"
            "+first added\n"
            "+second added\n"
        )
        self.assertEqual(
            parse_added_lines(patch),
            [AddedLine("safe.py", 3, "first added"), AddedLine("safe.py", 4, "second added")],
        )

    def test_literal_local_user_paths_are_rejected_but_placeholders_are_allowed(self):
        windows_path = "C:" + "\\Users\\" + "local-person\\project"
        escaped_windows_path = 'path = "C:' + "\\\\Users\\\\" + 'local-person\\\\project"'
        unix_path = "/home/" + "local-person/project"
        self.assertIn("LOCAL_USER_PATH", self.rules_for_line(windows_path))
        self.assertIn("LOCAL_USER_PATH", self.rules_for_line(escaped_windows_path))
        self.assertIn("LOCAL_USER_PATH", self.rules_for_line(unix_path))
        self.assertNotIn("LOCAL_USER_PATH", self.rules_for_line("C:\\Users\\username\\project"))
        self.assertNotIn("LOCAL_USER_PATH", self.rules_for_line("/home/example/project"))

    def test_private_keys_and_typical_tokens_are_rejected(self):
        key_header = "-----BEGIN " + "PRIVATE KEY-----"
        aws_token = "AKIA" + ("Q7" * 8)
        self.assertIn("PRIVATE_KEY", self.rules_for_line(key_header))
        self.assertIn("AWS_ACCESS_KEY", self.rules_for_line(aws_token))

    def test_concrete_credential_assignment_is_rejected_but_placeholder_is_allowed(self):
        quote = chr(34)
        concrete = "api_key = " + quote + "live-value-782913" + quote
        bare = "access_token=" + "live_782913_abcdef"
        self.assertIn("CREDENTIAL", self.rules_for_line(concrete))
        self.assertIn("CREDENTIAL", self.rules_for_line(bare))
        self.assertNotIn("CREDENTIAL", self.rules_for_line('api_key = "replace-me"'))

    def test_real_download_shaped_video_fixture_name_is_rejected(self):
        derived = "2026" + "0705_130555_" + "wIhYikXPQbs" + ".mp4"
        self.assertIn("DERIVED_VIDEO_FIXTURE", self.rules_for_line(derived))
        self.assertNotIn(
            "DERIVED_VIDEO_FIXTURE", self.rules_for_line("synthetic_library_clip.mp4")
        )

    def test_scan_changes_deduplicates_path_and_line_findings(self):
        secret_path = "exports" + "/bundle.vindex.zip"
        findings = scan_changes([secret_path, secret_path], [])
        keys = {(item.rule, item.path, item.line_number) for item in findings}
        self.assertEqual(len(keys), len(findings))
        self.assertEqual({item.rule for item in findings}, {"FORBIDDEN_PATH", "DANGEROUS_FILE"})


class GitignorePrivacyRegressionTest(unittest.TestCase):
    def test_private_and_generated_paths_remain_ignored(self):
        repository = Path(__file__).resolve().parents[1]
        patterns = {
            line.strip()
            for line in (repository / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertTrue(
            {
                "data/",
                "video/",
                "clips/",
                "exports/",
                ".env",
                ".env.*",
                "*.pem",
                "*.key",
                "credentials*.json",
                "*.vindex.zip",
                "benchmark-results/",
            }.issubset(patterns)
        )

        for candidate in (
            "data/index.db",
            "video/synthetic.mp4",
            "clips/result.mp4",
            "exports/shared.vindex.zip",
            "shared.vindex.zip",
            "benchmark-results/synthetic.json",
            ".env",
        ):
            with self.subTest(candidate=candidate):
                ignored = subprocess.run(
                    ["git", "check-ignore", "--no-index", "--quiet", candidate],
                    cwd=repository,
                    check=False,
                )
                self.assertEqual(ignored.returncode, 0)


class PrivacyGuardCliTest(unittest.TestCase):
    guard = Path(__file__).resolve().parents[1] / "scripts" / "check_privacy.py"

    def run_git(self, repository, *arguments):
        return subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

    def run_guard(self, repository, *arguments):
        return subprocess.run(
            [sys.executable, str(self.guard), "--repo", str(repository), *arguments],
            cwd=repository,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

    def make_repository(self, root):
        self.run_git(root, "init", "--quiet")
        self.run_git(root, "config", "user.name", "Privacy Guard Test")
        self.run_git(root, "config", "user.email", "privacy-guard@example.invalid")
        (root / "safe.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.run_git(root, "add", "safe.py")
        self.run_git(root, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "baseline")

    def test_default_cli_scans_staged_additions(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            self.make_repository(repository)
            derived = "2026" + "0705_130555_" + "wIhYikXPQbs" + ".mp4"
            (repository / "safe.py").write_text(
                "VALUE = 1\nFIXTURE = " + repr(derived) + "\n", encoding="utf-8"
            )
            self.run_git(repository, "add", "safe.py")

            result = self.run_guard(repository)

            self.assertEqual(result.returncode, 1)
            self.assertIn("DERIVED_VIDEO_FIXTURE", result.stderr)

    def test_working_tree_cli_adds_unstaged_and_untracked_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            self.make_repository(repository)
            local_path = "C:" + "\\Users\\" + "local-person\\project"
            (repository / "safe.py").write_text(
                "VALUE = 1\nLOCAL_PATH = " + repr(local_path) + "\n", encoding="utf-8"
            )
            key_header = "-----BEGIN " + "PRIVATE KEY-----"
            (repository / "notes.txt").write_text(key_header + "\n", encoding="utf-8")
            (repository / "capture.mp4").write_bytes(b"synthetic")

            staged_only = self.run_guard(repository)
            working_tree = self.run_guard(repository, "--working-tree")

            self.assertEqual(staged_only.returncode, 0)
            self.assertEqual(working_tree.returncode, 1)
            self.assertIn("LOCAL_USER_PATH", working_tree.stderr)
            self.assertIn("PRIVATE_KEY", working_tree.stderr)
            self.assertIn("DANGEROUS_FILE", working_tree.stderr)


if __name__ == "__main__":
    unittest.main()
