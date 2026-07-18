"""Synthetic Phase 0 storage benchmark for comparable local baselines.

The harness deliberately avoids application data, media, models, and network
access.  Every benchmark artifact lives in a marker-protected temporary
directory below a root explicitly supplied by the user.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import math
import os
import random
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence


REPORT_SCHEMA_VERSION = 1
FIXED_SEED = 20_260_717
WORKSPACE_PREFIX = ".cut-video-phase0-benchmark-"
WORKSPACE_MARKER = ".cut-video-phase0-marker.json"
WORKSPACE_KIND = "cut-video-phase0-synthetic-benchmark"
QUERY_TOKEN = "phasezero-target"
CASE_NAMES = (
    "sqlite_selected_text_scan",
    "sqlite_all_text_scan",
    "sequential_write_fsync",
    "sequential_read",
)
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


class BenchmarkError(RuntimeError):
    """A safe, user-facing benchmark failure without local path details."""


class BenchmarkSafetyError(BenchmarkError):
    """Raised when workspace ownership cannot be proven before cleanup."""


@dataclass(frozen=True)
class Profile:
    name: str
    sqlite_rows: int
    video_count: int
    words_per_row: int
    sample_count: int
    io_bytes: int
    io_block_bytes: int


@dataclass(frozen=True)
class RootSpec:
    label: str
    path: Path


@dataclass(frozen=True)
class Workspace:
    root: Path
    path: Path
    token: str


PROFILES: Mapping[str, Profile] = {
    # ``tiny`` exists for deterministic unit/integration tests only.
    "tiny": Profile("tiny", 64, 4, 6, 2, 32 * 1024, 4 * 1024),
    "smoke": Profile("smoke", 2_500, 8, 12, 5, 2 * 1024 * 1024, 256 * 1024),
    # p95 targets in the product specification require at least 20 samples.
    "baseline": Profile(
        "baseline", 100_000, 32, 24, 20, 64 * 1024 * 1024, 1024 * 1024
    ),
}


def _local_identity_values() -> set[str]:
    values = {getpass.getuser().strip(), socket.gethostname().strip()}
    return {value.casefold() for value in values if value}


def parse_root_spec(value: str) -> RootSpec:
    """Parse ``label=root`` without exposing the root in the report."""

    if "=" not in value:
        raise argparse.ArgumentTypeError("root must use the form label=directory")
    label, raw_path = value.split("=", 1)
    if not _LABEL_RE.fullmatch(label):
        raise argparse.ArgumentTypeError(
            "label must be 1-32 ASCII letters, digits, dots, underscores, or hyphens"
        )
    folded_label = label.casefold()
    if any(
        len(identity) >= 3 and identity in folded_label
        for identity in _local_identity_values()
    ):
        raise argparse.ArgumentTypeError("label must not be a local user or host name")
    if not raw_path.strip():
        raise argparse.ArgumentTypeError("root directory is required")

    try:
        path = Path(raw_path).expanduser().resolve(strict=True)
    except OSError as error:
        raise argparse.ArgumentTypeError("root must already exist") from error
    if not path.is_dir():
        raise argparse.ArgumentTypeError("root must be a directory")
    return RootSpec(label=label, path=path)


def _validate_root_specs(root_specs: Sequence[RootSpec]) -> None:
    if not root_specs:
        raise BenchmarkError("at least one root is required")
    labels = [item.label.casefold() for item in root_specs]
    if len(labels) != len(set(labels)):
        raise BenchmarkError("root labels must be unique")
    resolved = [item.path.resolve(strict=True) for item in root_specs]
    if len(resolved) != len(set(resolved)):
        raise BenchmarkError("each label must refer to a different root")


def create_workspace(root: Path) -> Workspace:
    """Create an owned workspace immediately below an existing root."""

    root = root.resolve(strict=True)
    if not root.is_dir():
        raise BenchmarkError("benchmark root is not a directory")
    try:
        path = Path(tempfile.mkdtemp(prefix=WORKSPACE_PREFIX, dir=root))
    except OSError as error:
        raise BenchmarkError("could not create benchmark workspace") from error

    token = uuid.uuid4().hex
    marker = path / WORKSPACE_MARKER
    try:
        with marker.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump({"kind": WORKSPACE_KIND, "token": token}, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        # The directory was created by this call and is still expected to be
        # empty when marker creation failed.  Never recursively remove it.
        try:
            marker.unlink(missing_ok=True)
            path.rmdir()
        except OSError:
            pass
        raise BenchmarkError("could not mark benchmark workspace") from error
    return Workspace(root=root, path=path, token=token)


def remove_workspace(workspace: Workspace) -> None:
    """Remove a workspace only after proving marker, name, and parent root."""

    path = workspace.path
    marker = path / WORKSPACE_MARKER
    try:
        resolved_path = path.resolve(strict=True)
        resolved_root = workspace.root.resolve(strict=True)
        if path.is_symlink() or resolved_path.parent != resolved_root:
            raise BenchmarkSafetyError("workspace is outside the requested root")
        if not resolved_path.name.startswith(WORKSPACE_PREFIX):
            raise BenchmarkSafetyError("workspace name is not owned by this benchmark")
        if marker.is_symlink() or not marker.is_file():
            raise BenchmarkSafetyError("workspace marker is missing")
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except BenchmarkSafetyError:
        raise
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise BenchmarkSafetyError("workspace marker could not be verified") from error

    if payload != {"kind": WORKSPACE_KIND, "token": workspace.token}:
        raise BenchmarkSafetyError("workspace marker does not match this run")
    shutil.rmtree(resolved_path)


@contextmanager
def owned_workspace(root: Path) -> Iterator[Workspace]:
    workspace = create_workspace(root)
    try:
        yield workspace
    finally:
        remove_workspace(workspace)


def _synthetic_text(rng: random.Random, row_number: int, word_count: int) -> str:
    vocabulary = (
        "alpha",
        "bravo",
        "cobalt",
        "delta",
        "ember",
        "forest",
        "galaxy",
        "harbor",
        "island",
        "jigsaw",
        "kernel",
        "lantern",
    )
    words = [rng.choice(vocabulary) for _ in range(word_count)]
    if row_number % 97 == 0:
        # Full-width uppercase deliberately exercises the same NFKC/casefold
        # normalization used by the current text-search path.
        words[word_number(row_number, word_count)] = "ＰＨＡＳＥＺＥＲＯ－ＴＡＲＧＥＴ"
    return " ".join(words)


def word_number(row_number: int, word_count: int) -> int:
    """Return a stable insertion point without relying on Python hash state."""

    return (row_number * 17 + 3) % word_count


def create_sqlite_fixture(path: Path, profile: Profile, seed: int = FIXED_SEED) -> None:
    """Create a deterministic synthetic transcript database inside a workspace."""

    rng = random.Random(seed)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(
            "CREATE TABLE segments ("
            "segment_id INTEGER PRIMARY KEY, "
            "video_id TEXT NOT NULL, "
            "start_ms INTEGER NOT NULL, "
            "end_ms INTEGER NOT NULL, "
            "text TEXT NOT NULL)"
        )
        batch: list[tuple[str, int, int, str]] = []
        for row_number_value in range(profile.sqlite_rows):
            video_number = row_number_value % profile.video_count
            start_ms = row_number_value * 1_500
            batch.append(
                (
                    f"synthetic-video-{video_number:03d}",
                    start_ms,
                    start_ms + 1_000,
                    _synthetic_text(
                        rng, row_number_value, profile.words_per_row
                    ),
                )
            )
            if len(batch) == 1_000:
                connection.executemany(
                    "INSERT INTO segments (video_id, start_ms, end_ms, text) "
                    "VALUES (?, ?, ?, ?)",
                    batch,
                )
                batch.clear()
        if batch:
            connection.executemany(
                "INSERT INTO segments (video_id, start_ms, end_ms, text) "
                "VALUES (?, ?, ?, ?)",
                batch,
            )
        connection.execute("CREATE INDEX segments_video_id ON segments(video_id)")
        connection.commit()
    finally:
        connection.close()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize_search_text(text: str) -> str:
    """Mirror the current NFKC, casefold, and whitespace search baseline."""

    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(normalized.split())


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise BenchmarkError("cannot summarize an empty sample set")
    rank = (len(ordered) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _measure(
    name: str,
    sample_count: int,
    operation: Callable[[], int],
    throughput_bytes: int | None = None,
    **metadata: object,
) -> dict[str, object]:
    samples_ms: list[float] = []
    result_value: int | None = None
    for _ in range(sample_count):
        started = time.perf_counter_ns()
        current_value = operation()
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        if result_value is None:
            result_value = current_value
        elif current_value != result_value:
            raise BenchmarkError("a benchmark operation returned inconsistent results")
        samples_ms.append(elapsed_ms)

    result = {
        "name": name,
        "sample_count": sample_count,
        "samples_ms": [round(value, 6) for value in samples_ms],
        "p50_ms": round(_percentile(samples_ms, 0.50), 6),
        "p95_ms": round(_percentile(samples_ms, 0.95), 6),
        "result_value": result_value,
        **metadata,
    }
    if throughput_bytes is not None:
        size_mib = throughput_bytes / (1024.0 * 1024.0)
        throughput_samples = [
            size_mib / (elapsed_ms / 1_000.0) for elapsed_ms in samples_ms
        ]
        result.update(
            {
                "samples_mib_per_s": [
                    round(value, 6) for value in throughput_samples
                ],
                "p50_mib_per_s": round(_percentile(throughput_samples, 0.50), 6),
                "p95_mib_per_s": round(_percentile(throughput_samples, 0.95), 6),
            }
        )
    return result


def _sqlite_cases(database: Path, profile: Profile) -> list[dict[str, object]]:
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA cache_size=-4096")

        normalized_query = _normalize_search_text(QUERY_TOKEN)

        def selected_scan() -> int:
            rows = connection.execute(
                "SELECT text FROM segments WHERE video_id = ?",
                ("synthetic-video-000",),
            )
            return sum(
                normalized_query in _normalize_search_text(str(row[0]))
                for row in rows
            )

        def all_scan() -> int:
            rows = connection.execute("SELECT text FROM segments")
            return sum(
                normalized_query in _normalize_search_text(str(row[0]))
                for row in rows
            )

        return [
            _measure(
                "sqlite_selected_text_scan",
                profile.sample_count,
                selected_scan,
                scope="one_synthetic_video",
                measurement_boundary=(
                    "execute_iterate_python_nfkc_casefold_whitespace_existing_connection"
                ),
            ),
            _measure(
                "sqlite_all_text_scan",
                profile.sample_count,
                all_scan,
                scope="all_synthetic_videos",
                measurement_boundary=(
                    "execute_iterate_python_nfkc_casefold_whitespace_existing_connection"
                ),
            ),
        ]
    finally:
        connection.close()


def _write_exact(handle, block: bytes, total_bytes: int) -> None:
    remaining = total_bytes
    while remaining:
        portion = block if remaining >= len(block) else block[:remaining]
        handle.write(portion)
        remaining -= len(portion)


def _io_cases(workspace: Path, profile: Profile, seed: int) -> list[dict[str, object]]:
    rng = random.Random(seed)
    block = rng.randbytes(profile.io_block_bytes)
    write_target = workspace / "sequential-write.bin"
    read_source = workspace / "sequential-read.bin"

    def write_fsync() -> int:
        with write_target.open("wb", buffering=0) as handle:
            _write_exact(handle, block, profile.io_bytes)
            os.fsync(handle.fileno())
        return write_target.stat().st_size

    with read_source.open("wb", buffering=0) as handle:
        _write_exact(handle, block, profile.io_bytes)
        os.fsync(handle.fileno())

    read_buffer = bytearray(profile.io_block_bytes)

    def sequential_read() -> int:
        total = 0
        with read_source.open("rb", buffering=0) as handle:
            while True:
                count = handle.readinto(read_buffer)
                if not count:
                    break
                total += count
        return total

    return [
        _measure(
            "sequential_write_fsync",
            profile.sample_count,
            write_fsync,
            throughput_bytes=profile.io_bytes,
            bytes_per_sample=profile.io_bytes,
            measurement_boundary="open_truncate_to_file_fsync_complete",
            fsync=True,
        ),
        _measure(
            "sequential_read",
            profile.sample_count,
            sequential_read,
            throughput_bytes=profile.io_bytes,
            bytes_per_sample=profile.io_bytes,
            measurement_boundary="open_to_complete_sequential_read",
            fsync=False,
        ),
    ]


def benchmark_root(root_spec: RootSpec, profile: Profile) -> dict[str, object]:
    with owned_workspace(root_spec.path) as workspace:
        database = workspace.path / "synthetic-transcripts.sqlite"
        create_sqlite_fixture(database, profile)
        fixture_sha256 = _sha256_file(database)
        cases = _sqlite_cases(database, profile)
        cases.extend(_io_cases(workspace.path, profile, FIXED_SEED))
    return {
        "label": root_spec.label,
        "fixture_sha256": fixture_sha256,
        "cases": cases,
    }


def run_benchmark(
    root_specs: Sequence[RootSpec], profile_name: str = "smoke"
) -> dict[str, object]:
    """Run the same synthetic cases for every labeled root."""

    _validate_root_specs(root_specs)
    try:
        profile = PROFILES[profile_name]
    except KeyError as error:
        raise BenchmarkError("unknown benchmark profile") from error

    roots = [benchmark_root(root_spec, profile) for root_spec in root_specs]
    fixture_digests = {str(root["fixture_sha256"]) for root in roots}
    if len(fixture_digests) != 1:
        raise BenchmarkError("synthetic SQLite fixture digest differs between roots")
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "benchmark": "cut_video_phase0_storage",
        "profile": profile.name,
        "seed": FIXED_SEED,
        "measurement": {
            "timer": "perf_counter_ns",
            "cache_condition": "uncontrolled_os_cache_repeated_measurements",
            "cold_claimed": False,
            "case_order_fixed": True,
        },
        "fixture": {
            "synthetic_only": True,
            "network_used": False,
            "models_used": False,
            "media_used": False,
            "sqlite_rows": profile.sqlite_rows,
            "video_count": profile.video_count,
            "words_per_row": profile.words_per_row,
            "samples_per_case": profile.sample_count,
            "io_bytes_per_sample": profile.io_bytes,
            "io_block_bytes": profile.io_block_bytes,
            "sqlite_cache_kib": 4096,
            "sqlite_version": sqlite3.sqlite_version,
            "sqlite_fixture_digest": "sha256",
        },
        "runtime": {
            "python_version": (
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            ),
            "platform": sys.platform,
            "logical_cpu_count": os.cpu_count(),
            "gpu_used": False,
        },
        "roots": roots,
    }


def serialize_report(report: Mapping[str, object]) -> str:
    """Serialize only the sanitized report DTO; filesystem paths are absent."""

    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run synthetic SQLite and sequential I/O measurements below labeled roots. "
            "Cache state is uncontrolled; results are not described as cold."
        )
    )
    parser.add_argument(
        "--root",
        action="append",
        required=True,
        type=parse_root_spec,
        metavar="LABEL=DIRECTORY",
        help="repeat for comparisons, for example hdd=E:\\bench and ssd=F:\\bench",
    )
    parser.add_argument(
        "--profile",
        choices=("smoke", "baseline", "tiny"),
        default="smoke",
        help="smoke is quick; baseline uses 20 samples; tiny is for tests",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional JSON file; the parent directory must already exist",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    try:
        report = run_benchmark(options.root, options.profile)
        rendered = serialize_report(report)
        if options.output is None:
            sys.stdout.write(rendered)
        else:
            output = options.output.expanduser()
            if not output.parent.is_dir():
                raise BenchmarkError("output parent directory must already exist")
            output.write_text(rendered, encoding="utf-8", newline="\n")
    except (BenchmarkError, OSError, sqlite3.Error) as error:
        # BenchmarkError messages are deliberately path-free.  Other exception
        # strings can contain local paths, so report only their type.
        detail = str(error) if isinstance(error, BenchmarkError) else type(error).__name__
        print(f"benchmark failed: {detail}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
