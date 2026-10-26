"""Audio-first import: audio-only download, background video download and the source swap
(synthetic files and a fake yt-dlp; no network, ffmpeg or ASR)."""
import json
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from moment_retrieval import audio_first, config, db, signals, source_origin
from moment_retrieval.downloader import (
    DownloadCancelled, DownloadError, audio_proxy_for, download_video, video_path_for_proxy,
)
from tests.test_downloader import _FakeYoutubeDL


@contextmanager
def configured_store(root: Path):
    data_dir = root / "data"
    with patch.object(config, "DATA_DIR", data_dir), \
            patch.object(config, "DB_PATH", data_dir / "index.db"), \
            patch.object(config, "TEXT_INDEX_PATH", data_dir / "text.index"), \
            patch.object(config, "CACHE_ROOT", data_dir):
        yield


class AudioOnlyDownloadTest(unittest.TestCase):
    def setUp(self):
        _FakeYoutubeDL.metadata_error = None
        _FakeYoutubeDL.download_error = None
        _FakeYoutubeDL.options_seen = []
        self.module = types.ModuleType("yt_dlp")
        self.module.YoutubeDL = _FakeYoutubeDL

    def test_audio_is_saved_next_to_where_the_video_will_go(self):
        info = {**_FakeYoutubeDL.info, "formats": [{"format_id": "Audio_Only", "vcodec": "none"}]}
        with tempfile.TemporaryDirectory() as root, patch.dict(sys.modules, {"yt_dlp": self.module}), \
                patch.object(_FakeYoutubeDL, "info", info):
            events = list(download_video("https://example.invalid/v", Path(root), audio_only=True))
            path = events[-1][1]
            self.assertEqual(path.name, "20240102_synthetic-id.audio.mp4")
            self.assertEqual(_FakeYoutubeDL.options_seen[-1]["format"], "bestaudio/Audio_Only")
            self.assertNotIn("merge_output_format", _FakeYoutubeDL.options_seen[-1])
            self.assertEqual(video_path_for_proxy(path), Path(root) / "20240102_synthetic-id.mp4")
            self.assertEqual(audio_proxy_for(Path(root) / "20240102_synthetic-id.mp4"), [path])
            # a second call reuses the finished audio
            again = list(download_video("https://example.invalid/v", Path(root), audio_only=True))
            self.assertIn("取得済み", again[-1][0])

    def test_no_audio_only_format_is_an_error_so_callers_fall_back(self):
        info = {**_FakeYoutubeDL.info, "formats": [{"format_id": "720p", "vcodec": "avc1"}]}
        with tempfile.TemporaryDirectory() as root, patch.dict(sys.modules, {"yt_dlp": self.module}), \
                patch.object(_FakeYoutubeDL, "info", info):
            with self.assertRaisesRegex(DownloadError, "音声だけの形式"):
                list(download_video("https://example.invalid/v", Path(root), audio_only=True))
            self.assertIsNone(audio_first.fetch_audio("https://example.invalid/v", Path(root)))

    def test_cancel_event_stops_the_download(self):
        cancel = threading.Event()
        cancel.set()
        with tempfile.TemporaryDirectory() as root, patch.dict(sys.modules, {"yt_dlp": self.module}):
            with self.assertRaises(DownloadCancelled):
                list(download_video("https://example.invalid/v", Path(root), cancel_event=cancel))

    def test_partial_files_are_not_mistaken_for_finished_audio(self):
        with tempfile.TemporaryDirectory() as root:
            video = Path(root) / "a.mp4"
            Path(root, "a.audio.mp4.part").write_bytes(b"x")
            Path(root, "a.audio.mp4.part-Frag3.part").write_bytes(b"x")
            self.assertEqual(audio_proxy_for(video), [])
            self.assertIsNone(video_path_for_proxy(video))


class VideoDownloadTest(unittest.TestCase):
    def test_background_download_reports_progress_and_result(self):
        release = threading.Event()

        def downloader(url, save_dir, cancel_event=None):
            yield "  ダウンロード中... 50%", None
            release.wait(2)
            yield "完了", Path(save_dir) / "a.mp4"

        download = audio_first.VideoDownload("u", Path("x"), downloader=downloader).start()
        seen = []
        release.set()
        self.assertEqual(download.wait(poll=seen.append, interval=0.01), Path("x") / "a.mp4")
        self.assertTrue(download.done)

    def test_failure_and_cancel_surface_from_wait(self):
        def failing(url, save_dir, cancel_event=None):
            raise DownloadError("network down")
            yield  # pragma: no cover

        with self.assertRaisesRegex(DownloadError, "network down"):
            audio_first.VideoDownload("u", Path("x"), downloader=failing).start().wait(interval=0.01)

        def cancellable(url, save_dir, cancel_event=None):
            while not cancel_event.is_set():
                yield "  ダウンロード中...", None
                cancel_event.wait(0.01)
            raise DownloadCancelled("中断")

        download = audio_first.VideoDownload("u", Path("x"), downloader=cancellable).start()
        download.cancel()
        with self.assertRaises(DownloadCancelled):
            download.wait(interval=0.01)


class SwapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = configured_store(self.root)
        self.store.__enter__()
        self.audio = self.root / "20260831_v1.audio.mp4"
        self.audio.write_bytes(b"audio stand-in")
        self.video_file = self.root / "20260831_v1.mp4"
        self.video_file.write_bytes(b"full video with the same audio")
        conn = db.get_conn()
        try:
            db.init_db(conn, create_backup=False)
            db.insert_video(conn, "vid_" + "a" * 32, str(self.audio.resolve()), 100.0)
            db.insert_segment(conn, "vid_" + "a" * 32, SimpleNamespace(start=0.0, end=5.0, text="発話", words=[]))
            source_origin.record_download(conn, self.audio, "https://www.twitch.tv/videos/123456")
            conn.commit()
        finally:
            conn.close()
        self.video_id = "vid_" + "a" * 32
        signals.signals_dir().mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.store.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_swap_points_the_entry_at_the_video_and_keeps_transcript_and_loudness(self):
        conn = db.get_conn()
        try:
            storage_id = db.get_video(conn, self.video_id)["video_id"]
        finally:
            conn.close()
        profile_path = signals._path(storage_id, "audio")
        profile_path.write_text(json.dumps({"version": 1, "source": signals._stamp(self.audio),
                                            "loudness_db": [-20]}), encoding="utf-8")
        with patch("moment_retrieval.utils.probe_duration", return_value=100.4):
            audio_first.swap_to_video(self.video_id, self.video_file)
        conn = db.get_conn()
        try:
            video = db.get_video(conn, self.video_id)
            self.assertEqual(Path(video["path"]), self.video_file.resolve())
            self.assertEqual(video["display_name"], self.video_file.name)
            self.assertEqual(len(db.get_segments(conn, self.video_id)), 1)
            self.assertEqual(source_origin.origin_url_for_video(conn, video), "https://www.twitch.tv/videos/123456")
            self.assertEqual(db.find_video_by_path(conn, str(self.video_file.resolve()))["video_id"], storage_id)
        finally:
            conn.close()
        self.assertFalse(self.audio.exists())
        self.assertIsNotNone(signals.load_audio(storage_id, self.video_file))

    def test_swap_refuses_a_different_length_or_an_unrelated_file(self):
        with patch("moment_retrieval.utils.probe_duration", return_value=130.0):
            with self.assertRaisesRegex(audio_first.SwapError, "長さ"):
                audio_first.swap_to_video(self.video_id, self.video_file)
        other = self.root / "other.mp4"
        other.write_bytes(b"x")
        with patch("moment_retrieval.utils.probe_duration", return_value=100.0):
            with self.assertRaisesRegex(audio_first.SwapError, "音声だけで登録"):
                audio_first.swap_to_video(self.video_id, other)
        self.assertTrue(self.audio.exists())

    def test_pending_video_path(self):
        self.assertEqual(audio_first.pending_video_path({"path": str(self.audio)}), self.video_file)
        self.assertIsNone(audio_first.pending_video_path({"path": str(self.video_file)}))


class PipelineAudioFirstTest(unittest.TestCase):
    def test_export_waits_for_the_video_and_cancel_stops_it(self):
        from moment_retrieval import auto_pipeline

        calls = []
        with tempfile.TemporaryDirectory() as root, configured_store(Path(root)):
            pipeline = auto_pipeline.AutoPipeline(steps={
                "metadata": lambda job: (None, None, None),
                "download": lambda job: (calls.append("download"), "a.audio.mp4")[1],
                "index": lambda job, path: (calls.append("index"), "vid_a")[1],
                "select": lambda job: (calls.append("select"), "run")[1],
                "await_video": lambda job: calls.append("await_video"),
                "export": lambda job: (calls.append("export"), [])[1],
            })
            job = auto_pipeline.AutoJob(job_id="auto_af", source="https://www.twitch.tv/videos/1",
                                        agent="codex", upload=False)
            pipeline.jobs[job.job_id] = job
            pipeline.run(job)
            self.assertEqual(calls, ["download", "index", "select", "await_video", "export"])

            class _Download:
                cancelled = False
                done = False
                message = "  ダウンロード中... 10%"
                error = None

                def cancel(self):
                    self.cancelled = True

            download = _Download()
            pipeline.video_downloads["auto_x"] = download
            pipeline.jobs["auto_x"] = auto_pipeline.AutoJob(job_id="auto_x", source="s", agent="codex",
                                                            state="running")
            self.assertIn("10%", pipeline.video_download_status("auto_x"))
            pipeline.cancel("auto_x")
            self.assertTrue(download.cancelled)


if __name__ == "__main__":
    unittest.main()
