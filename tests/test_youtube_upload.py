"""Private-only YouTube upload with a fake API client (no network, no OAuth)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from moment_retrieval import config, youtube_upload


class _Status:
    def __init__(self, value):
        self.value = value

    def progress(self):
        return self.value


class _Request:
    def __init__(self, body):
        self.body = body
        self.steps = [(_Status(0.5), None), (None, {"id": "abcDEF12345", "status": {"privacyStatus": "private"}})]

    def next_chunk(self):
        return self.steps.pop(0)


class _Service:
    def __init__(self):
        self.inserted = []

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.inserted.append(body)
        return _Request(body)

    def update(self, part, body):
        self.updated = body
        privacy = getattr(self, "result_privacy", "public")
        return type("_Exec", (), {"execute": lambda _self: {"status": {"privacyStatus": privacy}}})()


class PublishTest(unittest.TestCase):
    def test_publish_scope_is_requested_and_old_tokens_are_detected(self):
        self.assertIn("https://www.googleapis.com/auth/youtube", youtube_upload.SCOPES)
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "LIBRARY_ROOT", Path(tmp)):
            self.assertEqual(youtube_upload.missing_scopes(), [])
            youtube_upload.token_path().write_text(json.dumps({"scopes": youtube_upload.SCOPES[:2]}), encoding="utf-8")
            self.assertEqual(youtube_upload.missing_scopes(), ["https://www.googleapis.com/auth/youtube"])
            with self.assertRaises(youtube_upload.UploadError):
                youtube_upload.load_credentials(interactive=False)

    def test_missing_permission_error_is_explained(self):
        class _Denied:
            def videos(self):
                return self

            def update(self, part, body):
                return SimpleNamespace(execute=lambda: (_ for _ in ()).throw(
                    RuntimeError("HttpError 403 insufficientPermissions")))

        with self.assertRaises(youtube_upload.UploadError) as ctx:
            youtube_upload.publish("abcDEF12345", service_factory=_Denied)
        self.assertIn("連携", str(ctx.exception))

    def test_publish_sets_public_and_reports_locked_private(self):
        service = _Service()
        result = youtube_upload.publish("abcDEF12345", service_factory=lambda: service)
        self.assertEqual(service.updated["status"]["privacyStatus"], "public")
        self.assertIn("watch?v=abcDEF12345", result["watch_url"])
        service.result_privacy = "private"
        with self.assertRaises(youtube_upload.UploadError):
            youtube_upload.publish("abcDEF12345", service_factory=lambda: service)


class YouTubeUploadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "clip_見どころ.mp4"
        self.video.write_bytes(b"synthetic")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sidecar_metadata_is_used_and_privacy_is_always_private(self):
        self.video.with_suffix(".metadata.json").write_text(json.dumps({
            "title": "<b>決算</b>の瞬間" + "あ" * 200, "description": "説明", "tags": ["株", "決算", ""],
        }, ensure_ascii=False), encoding="utf-8")
        body = youtube_upload.metadata_for(self.video).request_body()
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertNotIn("<", body["snippet"]["title"])
        self.assertLessEqual(len(body["snippet"]["title"]), youtube_upload.MAX_TITLE)
        self.assertEqual(body["snippet"]["tags"], ["株", "決算"])

    def test_unlisted_can_be_chosen_but_public_cannot(self):
        metadata = youtube_upload.metadata_for(self.video)
        self.assertEqual(metadata.request_body("unlisted")["status"]["privacyStatus"], "unlisted")
        with self.assertRaises(youtube_upload.UploadError):
            metadata.request_body("public")
        service = _Service()
        messages = list(youtube_upload.upload_private(self.video, privacy="unlisted", service_factory=lambda: service))
        self.assertIn("限定公開", messages[0][0])
        self.assertEqual(messages[-1][1]["watch_url"], "https://www.youtube.com/watch?v=abcDEF12345")

    def test_filename_is_the_fallback_title(self):
        self.assertEqual(youtube_upload.metadata_for(self.video).title, "clip_見どころ")

    def test_upload_writes_receipt_and_never_uploads_twice(self):
        service = _Service()
        messages = list(youtube_upload.upload_private(self.video, service_factory=lambda: service))
        receipt = messages[-1][1]
        self.assertEqual(receipt["video_id"], "abcDEF12345")
        self.assertIn("studio.youtube.com/video/abcDEF12345", receipt["studio_url"])
        self.assertTrue(any("50%" in message for message, _ in messages))
        again = list(youtube_upload.upload_private(self.video, service_factory=lambda: service))
        self.assertEqual(len(service.inserted), 1)
        self.assertIn("省略", again[0][0])

    def test_non_mp4_is_rejected(self):
        other = self.root / "notes.txt"
        other.write_text("x", encoding="utf-8")
        with self.assertRaises(youtube_upload.UploadError):
            list(youtube_upload.upload_private(other, service_factory=_Service))

    def test_setup_reports_missing_client(self):
        with patch.object(config, "LIBRARY_ROOT", self.root), \
                patch.dict("os.environ", {youtube_upload.BUNDLED_CLIENT_ENV: str(self.root / "none.json")}):
            problems = youtube_upload.setup_problems()
        self.assertTrue(any("youtube_oauth_client.json" in item for item in problems))

    def test_bundled_client_is_used_until_the_user_loads_their_own(self):
        bundled = self.root / "bundle" / "youtube_oauth_client.json"
        own = self.root / "own.json"
        own.write_text(json.dumps({"installed": {"client_id": "a", "client_secret": "b"}}), encoding="utf-8")
        with patch.object(config, "LIBRARY_ROOT", self.root / "lib"), \
                patch.dict("os.environ", {youtube_upload.BUNDLED_CLIENT_ENV: str(bundled)}):
            self.assertIsNone(youtube_upload.client_source())
            with self.assertRaises(youtube_upload.UploadError):
                youtube_upload.bundle_own_client()
            youtube_upload.install_client_secret(own)
            self.assertEqual(youtube_upload.bundle_own_client(), bundled)
            youtube_upload.client_secret_path().unlink()
            self.assertEqual(youtube_upload.client_source(), "bundled")
            self.assertEqual(youtube_upload.active_client_path(), bundled)
            self.assertEqual(youtube_upload.setup_problems(), [])




class SavedPathMappingTest(unittest.TestCase):
    def test_cached_gradio_copy_maps_back_to_the_saved_clip(self):
        import app

        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as cache:
            original = Path(out) / "video__vid_x" / "clip.mp4"
            original.parent.mkdir()
            original.write_bytes(b"synthetic clip")
            cached = Path(cache) / "clip.mp4"
            cached.write_bytes(b"synthetic clip")
            self.assertEqual(app._saved_highlight_paths([str(cached)], out), [original.resolve()])
            self.assertEqual(app._saved_highlight_paths([str(original)], out), [original.resolve()])



class ClientSecretTest(unittest.TestCase):
    def test_only_desktop_client_json_is_installed(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "LIBRARY_ROOT", Path(tmp) / "lib"):
            web = Path(tmp) / "web.json"
            web.write_text(json.dumps({"web": {"client_id": "a", "client_secret": "b"}}), encoding="utf-8")
            with self.assertRaises(youtube_upload.UploadError):
                youtube_upload.install_client_secret(web)
            self.assertFalse(youtube_upload.client_secret_path().exists())
            desktop = Path(tmp) / "desktop.json"
            desktop.write_text(json.dumps({"installed": {"client_id": "a", "client_secret": "b"}}), encoding="utf-8")
            youtube_upload.install_client_secret(desktop)
            self.assertTrue(youtube_upload.client_secret_path().is_file())


class AutoPublishAppTest(unittest.TestCase):
    def test_standalone_app_builds_and_editor_no_longer_has_the_tab(self):
        import auto_publish_app
        import app

        self.assertEqual(auto_publish_app.APP_PORT, 7870)
        self.assertIsNotNone(auto_publish_app.demo)
        self.assertFalse(hasattr(app, "auto_submit"))

    def test_download_only_button_starts_link_only_jobs(self):
        import auto_publish_app

        pipeline = SimpleNamespace(submit=Mock(return_value=SimpleNamespace(job_id="auto_dl")))
        with patch.object(auto_publish_app, "_auto_pipeline", return_value=pipeline), \
                patch.object(auto_publish_app, "auto_jobs_view", return_value=("jobs", None, None)), \
                patch.object(auto_publish_app.gr, "Info"):
            result = auto_publish_app.auto_download_only("https://youtu.be/abcDEF12345\n\n")
        pipeline.submit.assert_called_once_with("https://youtu.be/abcDEF12345", "codex", link_only=True)
        self.assertEqual(result[0], "")

    def test_only_the_visible_input_is_used(self):
        import auto_publish_app

        pipeline = SimpleNamespace(submit=Mock(return_value=SimpleNamespace(job_id="auto_x")))
        common = ("codex", "gpt-6-luna", 1, "high", 20, 60, "blur", False)
        with patch.object(auto_publish_app, "_auto_pipeline", return_value=pipeline), \
                patch.object(auto_publish_app, "auto_jobs_view", return_value=("jobs", None, None)), \
                patch.object(auto_publish_app.gr, "Info"):
            auto_publish_app.auto_submit("library", "https://youtu.be/abcDEF12345", ["vid_a"], *common)
            auto_publish_app.auto_submit("url", "https://youtu.be/abcDEF12345", ["vid_a"], *common)
        sources = [call.args[0] for call in pipeline.submit.call_args_list]
        self.assertEqual(sources, ["library:vid_a", "https://youtu.be/abcDEF12345"])
        url_visible, library_visible, download_visible = auto_publish_app.on_mode_change("library")
        self.assertEqual((url_visible["visible"], library_visible["visible"], download_visible["visible"]),
                         (False, True, False))

    def test_length_layout_and_effort_are_saved_as_defaults(self):
        import auto_publish_app

        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "LIBRARY_ROOT", Path(tmp)):
            self.assertEqual(auto_publish_app.apply_ui_settings(), ("high", 20, 180, "blur", True, "private"))
            auto_publish_app.save_ui_settings("max", 15, 45, "crop", False, "unlisted")
            self.assertEqual(auto_publish_app.apply_ui_settings(), ("max", 15.0, 45.0, "crop", False, "unlisted"))
            self.assertEqual(auto_publish_app._upload_args("unlisted"), {"upload": True, "privacy": "unlisted"})
            self.assertEqual(auto_publish_app._upload_args("none"), {"upload": False, "privacy": "private"})
            with self.assertRaises(auto_publish_app.gr.Error):
                auto_publish_app.save_ui_settings("high", 50, 20, "blur")
            (Path(tmp) / "auto_publish_ui.json").write_text('{"max_sec": 999, "layout": "x", "upload": "public"}',
                                                            encoding="utf-8")
            self.assertEqual(auto_publish_app.apply_ui_settings(), ("high", 20, 180, "blur", True, "private"))

    def test_publish_buttons_publish_right_away_and_summarise_failures(self):
        import auto_publish_app

        with self.assertRaises(auto_publish_app.gr.Error):
            auto_publish_app.publish_now("auto_a", [])

        def publish(job_id, video_id):
            if video_id != "yt1":
                raise auto_publish_app.PipelineError("権限がありません")
            return {"watch_url": f"https://www.youtube.com/watch?v={video_id}"}

        pipeline = SimpleNamespace(publish=Mock(side_effect=publish))
        with patch.object(auto_publish_app, "_auto_pipeline", return_value=pipeline), \
                patch.object(auto_publish_app, "auto_jobs_view", return_value=("jobs", "[]", None, "history")), \
                patch.object(auto_publish_app.gr, "Info"):
            result = auto_publish_app.publish_now("auto_a", ["yt1", "yt2", "yt3"])
        self.assertEqual(pipeline.publish.call_count, 3)
        self.assertIn("watch?v=yt1", result[0])
        self.assertIn("2本を公開できませんでした: 権限がありません", result[0])
        self.assertEqual(len(result), 5)

    def test_upload_signature_ignores_progress_but_tracks_publication(self):
        import auto_publish_app

        job = SimpleNamespace(job_id="auto_a", uploads=[{"video_id": "yt1", "privacy_status": "private"}], log=["a"])
        before = auto_publish_app.uploads_signature([job])
        job.log.append("progress")
        self.assertEqual(auto_publish_app.uploads_signature([job]), before)
        job.uploads[0]["privacy_status"] = "public"
        self.assertNotEqual(auto_publish_app.uploads_signature([job]), before)

    def test_quit_stops_running_jobs_then_exits_later(self):
        import auto_publish_app

        jobs = [SimpleNamespace(job_id="auto_run", state="running"), SimpleNamespace(job_id="auto_done", state="done")]
        pipeline = SimpleNamespace(list_jobs=lambda: jobs, cancel=Mock())
        with patch.object(auto_publish_app, "_auto_pipeline", return_value=pipeline), \
                patch.object(auto_publish_app.threading, "Timer") as timer:
            update = auto_publish_app.shutdown_app()
        pipeline.cancel.assert_called_once_with("auto_run")
        self.assertEqual(timer.call_args[0][0], 3.0)
        timer.return_value.start.assert_called_once()
        self.assertIn("終了しました", update["value"])


if __name__ == "__main__":
    unittest.main()
