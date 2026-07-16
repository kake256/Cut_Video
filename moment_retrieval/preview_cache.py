"""Pure filesystem cache used by video preview renderers.

This module deliberately knows nothing about Gradio, the database, ffmpeg, or
``cut_clip``.  Callers provide a renderer which writes to the temporary output
path passed to it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import Callable


DEFAULT_MAX_BYTES = 8 * 1024 ** 3
DEFAULT_MAX_FILES = 200
DEFAULT_TEMP_MAX_AGE_SEC = 24 * 60 * 60
DEFAULT_LOCK_STRIPES = 32
FINGERPRINT_VERSION = 1
FINGERPRINT_SAMPLE_BYTES = 64 * 1024


class PreviewCache:
    """Bounded, concurrency-safe cache for rendered preview files."""

    def __init__(
        self,
        directory: Path | str,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_files: int = DEFAULT_MAX_FILES,
        temp_max_age_sec: float = DEFAULT_TEMP_MAX_AGE_SEC,
        lock_stripes: int = DEFAULT_LOCK_STRIPES,
    ) -> None:
        if lock_stripes <= 0:
            raise ValueError("lock_stripes must be positive")
        self.directory = Path(directory)
        self.max_bytes = int(max_bytes)
        self.max_files = int(max_files)
        self.temp_max_age_sec = float(temp_max_age_sec)
        self.locks = tuple(threading.Lock() for _ in range(lock_stripes))
        self.prune_lock = threading.Lock()
        self.protected_outputs: set[Path] = set()

    def configure(
        self,
        *,
        directory: Path | str,
        max_bytes: int,
        max_files: int,
        temp_max_age_sec: float,
    ) -> None:
        """Update policy values while retaining lock/protection identity.

        The application uses this only to preserve compatibility with tests
        and deployments which override its public cache constants.
        """
        self.directory = Path(directory)
        self.max_bytes = int(max_bytes)
        self.max_files = int(max_files)
        self.temp_max_age_sec = float(temp_max_age_sec)

    @staticmethod
    def _source_signature(source_stat) -> tuple[int, int, int | None]:
        """Values used only to detect a source changing while sampled."""
        return (
            int(source_stat.st_size),
            int(source_stat.st_mtime_ns),
            getattr(source_stat, "st_ctime_ns", None),
        )

    @staticmethod
    def source_fingerprint(source: Path | str, source_size: int) -> str:
        """Hash bounded source samples, including their exact locations."""
        source = Path(source)
        if source_size <= FINGERPRINT_SAMPLE_BYTES * 3:
            samples = [(0, source_size)]
        else:
            samples = [
                (0, FINGERPRINT_SAMPLE_BYTES),
                (
                    max(0, source_size // 2 - FINGERPRINT_SAMPLE_BYTES // 2),
                    FINGERPRINT_SAMPLE_BYTES,
                ),
                (
                    max(0, source_size - FINGERPRINT_SAMPLE_BYTES),
                    FINGERPRINT_SAMPLE_BYTES,
                ),
            ]

        samples = list(dict.fromkeys(samples))
        digest = hashlib.sha256()
        with source.open("rb") as source_file:
            for offset, requested_length in samples:
                source_file.seek(offset)
                content = source_file.read(requested_length)
                digest.update(int(offset).to_bytes(8, "big", signed=False))
                digest.update(len(content).to_bytes(8, "big", signed=False))
                digest.update(content)
        return digest.hexdigest()

    @classmethod
    def source_version(cls, source: Path | str) -> tuple[int, int, str | None]:
        """Return stable stat data and a bounded content fingerprint.

        A stat/sample/stat sequence is retried once if the source changes.
        Read errors fall back to stat-only identity so preview rendering stays
        available for unusual or transiently locked files.
        """
        source = Path(source)
        last_stat = None
        for _attempt in range(2):
            try:
                before = source.stat()
            except OSError:
                return 0, 0, None
            last_stat = before
            try:
                fingerprint = cls.source_fingerprint(source, int(before.st_size))
                after = source.stat()
            except OSError:
                return int(before.st_mtime_ns), int(before.st_size), None
            last_stat = after
            if cls._source_signature(before) == cls._source_signature(after):
                return int(after.st_mtime_ns), int(after.st_size), fingerprint
        return int(last_stat.st_mtime_ns), int(last_stat.st_size), None

    def cache_path(
        self,
        prefix: str,
        video_id: str | None,
        video_path: str,
        ranges: list[tuple[float, float]],
    ) -> Path:
        """Return a source/version/ranges-specific, filename-safe path."""
        source = Path(video_path)
        stamp, source_size, source_fingerprint = self.source_version(source)
        key_data = {
            "video_id": str(video_id or ""),
            "path": str(source.resolve()),
            "mtime_ns": stamp,
            "size": source_size,
            "fingerprint_version": FINGERPRINT_VERSION,
            "fingerprint": source_fingerprint,
            "ranges": [
                [float(start).hex(), float(end).hex()] for start, end in ranges
            ],
        }
        cache_key = hashlib.sha256(
            json.dumps(
                key_data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        return self.directory / f"{prefix}_{cache_key}.mp4"

    def lock_for(self, output: Path | str) -> threading.Lock:
        """Return one fixed stripe; the lock collection cannot grow."""
        output = Path(output)
        lock_key = hashlib.sha256(str(output.resolve()).encode("utf-8")).digest()
        return self.locks[
            int.from_bytes(lock_key[:4], "big") % len(self.locks)
        ]

    @staticmethod
    def is_completed_file(path: Path | str) -> bool:
        name = Path(path).name.lower()
        return (
            name.endswith(".mp4")
            and not name.endswith(".tmp.mp4")
            and name.startswith(("preview_", "preview_multi_", "intuitive_"))
        )

    @staticmethod
    def touch(path: Path | str) -> None:
        try:
            Path(path).touch(exist_ok=True)
        except OSError:
            pass

    def prune(
        self,
        protected: Path | str | None = None,
        *,
        cleanup_stale_temps: bool = False,
        now: float | None = None,
    ) -> None:
        """Best-effort stale-temp cleanup and mtime-LRU pruning."""
        protected_path = Path(protected) if protected is not None else None
        with self.prune_lock:
            protected_outputs = set(self.protected_outputs)
            if protected_path is not None:
                protected_outputs.add(protected_path)
            try:
                entries = list(self.directory.iterdir())
            except OSError:
                return

            current_time = time.time() if now is None else float(now)
            if cleanup_stale_temps:
                for path in entries:
                    if not path.name.lower().endswith(".tmp.mp4"):
                        continue
                    try:
                        file_stat = path.stat()
                        if not stat.S_ISREG(file_stat.st_mode):
                            continue
                        if current_time - file_stat.st_mtime <= self.temp_max_age_sec:
                            continue
                        path.unlink()
                    except OSError:
                        continue

            completed = []
            for path in entries:
                if not self.is_completed_file(path):
                    continue
                try:
                    file_stat = path.stat()
                    if not stat.S_ISREG(file_stat.st_mode):
                        continue
                except OSError:
                    continue
                completed.append((file_stat.st_mtime_ns, file_stat.st_size, path))

            completed.sort(key=lambda item: (item[0], item[2].name))
            total_bytes = sum(size for _mtime, size, _path in completed)
            remaining_count = len(completed)
            for _mtime, size, path in completed:
                if (
                    remaining_count <= self.max_files
                    and total_bytes <= self.max_bytes
                ):
                    break
                if path in protected_outputs:
                    continue
                try:
                    path.unlink()
                except OSError:
                    continue
                remaining_count -= 1
                total_bytes -= size

    def initialize(self) -> None:
        """Perform startup-only stale temporary file cleanup."""
        self.prune(cleanup_stale_temps=True)

    def create(self, output: Path | str, renderer: Callable[[Path], None]) -> str:
        """Render once to a unique temp path and atomically publish it."""
        output = Path(output)
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.lock_for(output):
            with self.prune_lock:
                self.protected_outputs.add(output)
            try:
                if output.exists():
                    self.touch(output)
                    self.prune(protected=output)
                    return str(output)
                temporary = output.with_name(
                    f"{output.stem}.{secrets.token_hex(6)}.tmp.mp4"
                )
                try:
                    renderer(temporary)
                    temporary.replace(output)
                finally:
                    temporary.unlink(missing_ok=True)
                self.prune(protected=output)
                return str(output)
            finally:
                with self.prune_lock:
                    self.protected_outputs.discard(output)
