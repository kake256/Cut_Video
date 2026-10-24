"""Start work on a URL from its audio while the full video downloads.

The audio-only rendition (Twitch ``Audio_Only``, YouTube ``bestaudio``) is a
small fraction of the video, so transcription, loudness, LLM analysis and
highlight selection can begin after about a minute.  The full-quality video is
fetched in the background at the same time.  When it arrives, the library entry
is switched from the audio file to the video file: the transcript, search index
and analyses stay as they are, because the audio track is the same stream with
the same timing (checked: identical samples, offset 0).

Anything that needs pictures (preview, clip export) waits for :meth:`VideoDownload.wait`
and :func:`swap_to_video` first.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import db, signals, source_origin
from .downloader import DownloadCancelled, DownloadError, download_video, video_path_for_proxy

DURATION_TOLERANCE_SEC = 2.0


class SwapError(RuntimeError):
    pass


class VideoDownload:
    """The full-quality download running in a background thread."""

    def __init__(self, url: str, save_dir: Path, *, downloader: Callable = download_video):
        self.url = url
        self.save_dir = Path(save_dir)
        self.cancel_event = threading.Event()
        self.message = "動画のダウンロードを準備しています。"
        self.path: Optional[Path] = None
        self.error: Optional[BaseException] = None
        self._downloader = downloader
        self._thread = threading.Thread(target=self._run, name="video-download", daemon=True)

    def start(self) -> "VideoDownload":
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            for message, path in self._downloader(self.url, self.save_dir, cancel_event=self.cancel_event):
                self.message = message
                if path is not None:
                    self.path = Path(path)
            if self.path is None:
                raise DownloadError("ダウンロードしたファイルが見つかりません")
        except BaseException as exc:  # reported by wait(); the thread must not die silently
            self.error = exc

    @property
    def done(self) -> bool:
        return not self._thread.is_alive()

    def cancel(self) -> None:
        self.cancel_event.set()

    def wait(self, *, poll: Callable[[str], None] | None = None, interval: float = 2.0) -> Path:
        """Block until the video is there; ``poll`` gets the latest progress (and may raise to stop)."""
        last = None
        while self._thread.is_alive():
            if poll and self.message != last:
                poll(self.message)
                last = self.message
            self._thread.join(timeout=interval)
        if self.error is not None:
            if isinstance(self.error, DownloadCancelled):
                raise self.error
            raise DownloadError(str(self.error)) from self.error
        assert self.path is not None
        return self.path


def fetch_audio(url: str, save_dir: Path, *, log: Callable[[str], None] | None = None,
                cancel_event: threading.Event | None = None) -> Optional[Path]:
    """Download the audio-only rendition; None when the source has none (use the normal download)."""
    path = None
    try:
        for message, result in download_video(url, save_dir, audio_only=True, cancel_event=cancel_event):
            if log:
                log(message)
            if result is not None:
                path = Path(result)
    except DownloadCancelled:
        raise
    except DownloadError as exc:
        if log:
            log(f"音声だけの先行取得はできませんでした（{exc}）。動画をダウンロードしてから処理します。")
        return None
    return path


def is_audio_proxy(path: str | Path) -> bool:
    return video_path_for_proxy(Path(path)) is not None


def pending_video_path(video: dict) -> Optional[Path]:
    """For a library entry still pointing at its audio stand-in, where the video belongs."""
    return video_path_for_proxy(Path(str(video.get("path") or "")))


def swap_to_video(video_id: str, video_path: Path, *, remove_audio: bool = True) -> dict:
    """Point a library entry registered from its audio stand-in at the downloaded video.

    Only an entry whose file is ``<basename>.audio.*`` can be switched, and only to
    ``<basename>.mp4`` of (nearly) the same length.  Transcript revisions, chunks,
    the search index, analyses and highlight runs are keyed by the video ID and are
    left untouched.  The stored loudness profile is re-stamped for the new file (it
    is the same audio).
    """
    from . import utils
    from .publication import LeaseManager, PublicationError, private_source_fingerprint

    video_path = Path(video_path)
    if not video_path.is_file():
        raise SwapError(f"動画ファイルが見つかりません: {video_path}")
    conn = db.get_conn()
    try:
        with LeaseManager(conn).writer() as lease:
            db.init_db(conn)
            video = db.get_video(conn, video_id)
            if video is None:
                raise SwapError("差し替える動画がライブラリにありません。")
            audio_path = Path(str(video["path"]))
            expected = video_path_for_proxy(audio_path)
            if expected is None or expected.resolve() != video_path.resolve():
                raise SwapError("音声だけで登録した動画ではないため、差し替えません。")
            duration = float(utils.probe_duration(video_path))
            if abs(duration - float(video.get("duration") or 0.0)) > DURATION_TOLERANCE_SEC:
                raise SwapError("音声と動画の長さが一致しないため、差し替えません。")
            fingerprint = private_source_fingerprint(video_path)
            resolved = str(video_path.resolve())
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT owner_token, state, expires_at FROM job_records WHERE job_id = ?",
                               (lease.job_id,)).fetchone()
            if not row or str(row[0]) != lease.owner_token or str(row[1]) != "running" or float(row[2]) < time.time():
                raise PublicationError("writer lease was lost")
            conn.execute(
                "UPDATE sources SET locator = ?, private_fingerprint = ?, status = 'available' "
                "WHERE source_generation = ?",
                (resolved, fingerprint, video["source_generation"]),
            )
            conn.execute("UPDATE videos SET path = ?, source_state = 'available' WHERE video_id = ?",
                         (resolved, video["video_id"]))
            origin = conn.execute("SELECT origin_url FROM downloaded_sources WHERE path = ?",
                                  (source_origin._path_key(audio_path),)).fetchone()
            conn.commit()
            lease.publication_committed = True
            storage_id = str(video["video_id"])
        if origin:
            source_origin.record_download(conn, video_path, str(origin[0]))
        updated = db.get_video(conn, video_id) or {}
    except PublicationError as exc:
        conn.rollback()
        raise SwapError("別のライブラリ更新処理が実行中のため、差し替えられませんでした。") from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    _restamp_audio_profile(storage_id, audio_path, video_path)
    if remove_audio:
        try:
            audio_path.unlink()
        except OSError:
            pass
    return updated


def _restamp_audio_profile(storage_id: str, audio_path: Path, video_path: Path) -> None:
    target = signals._path(storage_id, "audio")
    data = signals._read(target)
    if not data:
        return
    try:
        if data.get("source") != signals._stamp(audio_path):
            return  # built from something else; leave it to be rebuilt
        data["source"] = signals._stamp(video_path)
    except OSError:
        return
    target.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


def should_use(url: str) -> bool:
    """Audio-first only for a fresh URL: a shared index waiting for this URL is relinked to the video."""
    from . import config

    if not config.AUDIO_FIRST_DOWNLOAD or not str(url or "").lower().startswith(("http://", "https://")):
        return False
    canonical = source_origin.canonical_source_url(url)
    if canonical is None:
        return True
    conn = db.get_conn()
    try:
        db.init_db(conn)
        return not source_origin.unlinked_videos_for_origin(conn, canonical)
    finally:
        conn.close()
