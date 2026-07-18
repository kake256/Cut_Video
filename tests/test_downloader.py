import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from moment_retrieval.downloader import (
    DownloadError,
    _build_basename,
    _progress_message,
    download_video,
)


class _FakeYoutubeDL:
    info = {
        "id": "synthetic-id",
        "title": "Synthetic title",
        "upload_date": "20240102",
        "duration": 65,
    }
    metadata_error = None
    download_error = None
    metadata_calls = 0
    download_calls = 0

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=False):
        if not download:
            type(self).metadata_calls += 1
            if type(self).metadata_error is not None:
                raise type(self).metadata_error
            return dict(type(self).info)

        type(self).download_calls += 1
        if type(self).download_error is not None:
            raise type(self).download_error

        for hook in self.options.get("progress_hooks", []):
            hook({
                "status": "downloading",
                "_percent_str": "50%",
                "_speed_str": "1 MiB/s",
                "_eta_str": "1s",
            })
            hook({"status": "finished"})

        output = Path(
            self.options["outtmpl"].replace("%(ext)s", "mp4")
        )
        output.write_bytes(b"synthetic download")
        return {**type(self).info, "ext": "mp4"}


class DownloaderCharacterizationTests(unittest.TestCase):
    def setUp(self):
        _FakeYoutubeDL.metadata_error = None
        _FakeYoutubeDL.download_error = None
        _FakeYoutubeDL.metadata_calls = 0
        _FakeYoutubeDL.download_calls = 0
        self._yt_dlp = types.ModuleType("yt_dlp")
        self._yt_dlp.YoutubeDL = _FakeYoutubeDL

    def test_progress_message_formats_downloading_and_finished_states(self):
        self.assertEqual(
            _progress_message({
                "status": "downloading",
                "_percent_str": " 12.5% ",
                "_speed_str": " 2 MiB/s ",
                "_eta_str": " 7s ",
            }),
            "  ダウンロード中... 12.5% / 2 MiB/s / 残り 7s",
        )
        self.assertIn(
            "結合処理中",
            _progress_message({"status": "finished"}),
        )
        self.assertIsNone(_progress_message({"status": "other"}))

    def test_basename_uses_date_and_sanitizes_unusable_characters(self):
        self.assertEqual(
            _build_basename({"id": "unsafe/id?#", "upload_date": "20240102"}),
            "20240102_unsafe_id__",
        )
        self.assertEqual(_build_basename({"id": "plain-id"}), "plain-id")

    def test_download_yields_messages_and_a_completed_synthetic_path(self):
        with tempfile.TemporaryDirectory(prefix="cut_downloader_test_") as root:
            with patch.dict(sys.modules, {"yt_dlp": self._yt_dlp}):
                events = list(
                    download_video(
                        "https://example.invalid/synthetic",
                        Path(root),
                    )
                )

            messages = [message for message, _ in events]
            completed = [path for _, path in events if path is not None]
            self.assertIn("URLから動画情報を取得中", messages[0])
            self.assertTrue(any("ダウンロードを開始" in msg for msg in messages))
            self.assertTrue(any("ダウンロード完了" in msg for msg in messages))
            self.assertEqual(len(completed), 1)
            self.assertEqual(
                completed[0].name,
                "20240102_synthetic-id.mp4",
            )
            self.assertEqual(completed[0].read_bytes(), b"synthetic download")
            self.assertEqual(_FakeYoutubeDL.metadata_calls, 1)
            self.assertEqual(_FakeYoutubeDL.download_calls, 1)

    def test_existing_output_is_reused_without_starting_download_worker(self):
        with tempfile.TemporaryDirectory(prefix="cut_downloader_test_") as root:
            existing = Path(root) / "20240102_synthetic-id.mp4"
            existing.write_bytes(b"already complete")
            with patch.dict(sys.modules, {"yt_dlp": self._yt_dlp}):
                events = list(
                    download_video(
                        "https://example.invalid/synthetic",
                        Path(root),
                    )
                )

            self.assertEqual(events[-1][1], existing)
            self.assertIn("既にダウンロード済み", events[-1][0])
            self.assertEqual(_FakeYoutubeDL.download_calls, 0)

    def test_metadata_and_worker_errors_are_wrapped_for_the_ui(self):
        with tempfile.TemporaryDirectory(prefix="cut_downloader_test_") as root:
            _FakeYoutubeDL.metadata_error = RuntimeError("synthetic metadata error")
            with patch.dict(sys.modules, {"yt_dlp": self._yt_dlp}):
                with self.assertRaisesRegex(DownloadError, "動画情報の取得に失敗"):
                    list(download_video("https://example.invalid/meta", Path(root)))

            _FakeYoutubeDL.metadata_error = None
            _FakeYoutubeDL.download_error = RuntimeError("synthetic worker error")
            with patch.dict(sys.modules, {"yt_dlp": self._yt_dlp}):
                with self.assertRaisesRegex(DownloadError, "ダウンロードに失敗"):
                    list(download_video("https://example.invalid/body", Path(root)))


if __name__ == "__main__":
    unittest.main()
