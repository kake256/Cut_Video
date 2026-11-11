"""Listing finished clips and posting them later, with a fake YouTube client."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from moment_retrieval import config, made_clips, youtube_upload


class _Status:
    def progress(self):
        return 0.5


class _Request:
    def __init__(self, service):
        self.service = service
        self.steps = [(_Status(), None), (None, {"id": "vidNEW00001", "status": {"privacyStatus": "private"}})]

    def next_chunk(self, num_retries=0):
        return self.steps.pop(0)


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class _Service:
    def __init__(self):
        self.inserted, self.updated, self.listed = [], [], []
        self.items = []

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.inserted.append(body)
        return _Request(self)

    def update(self, part, body):
        self.updated.append(body)
        return _Exec({"status": {"privacyStatus": body["status"]["privacyStatus"]}})

    def list(self, part, id):
        self.listed.append(id)
        return _Exec({"items": self.items})


class MadeClipsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        patcher = patch.object(config, "CACHE_ROOT", root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        folder = root / "clips" / "20251012_002528_EPbvm8wJ9pk__vid_73bf502f7846"
        folder.mkdir(parents=True)
        self.clip = folder / "a_short.mp4"
        self.clip.write_bytes(b"mp4")
        self.clip.with_suffix(".metadata.json").write_text(json.dumps({"title": "見出し"}), encoding="utf-8")
        self.old = folder / "b_short.mp4"
        self.old.write_bytes(b"mp4")
        jobs = root / "auto_jobs"
        jobs.mkdir()
        self._job(jobs, "auto_new", 200, [str(self.clip), str(folder / "gone_short.mp4")], upload=False)
        self._job(jobs, "auto_old", 100, [str(self.old)], upload=True)
        self._job(jobs, "auto_run", 300, [str(folder / "c_short.mp4")], upload=False, state="running")
        (jobs / "auto_new.export.json").write_text(json.dumps({"outputs": []}), encoding="utf-8")
        self.service = _Service()

    def _job(self, directory, job_id, created, outputs, *, upload, state="done"):
        data = {"job_id": job_id, "state": state, "created_at": created, "outputs": outputs, "upload": upload}
        (directory / f"{job_id}.json").write_text(json.dumps(data), encoding="utf-8")

    def test_lists_finished_jobs_clips_newest_first_skipping_missing_files(self):
        clips = made_clips.list_clips()
        self.assertEqual([clip.path for clip in clips], [self.clip, self.old])
        self.assertEqual(clips[0].title, "見出し")
        self.assertTrue(clips[0].batch)
        self.assertEqual(clips[0].source_name, "20251012_002528_EPbvm8wJ9pk")
        self.assertEqual(made_clips.status_label(clips[0].receipt), "未投稿")

    def test_unlisted_upload_writes_receipt_and_second_press_is_skipped(self):
        with patch("moment_retrieval.used_ranges.record_from_clip") as record:
            list(made_clips.upload(self.clip, "unlisted", service_factory=lambda: self.service))
            record.assert_called_once()
        self.assertEqual(self.service.inserted[0]["status"]["privacyStatus"], "unlisted")
        receipt = made_clips.read_receipt(self.clip)
        self.assertEqual(receipt["video_id"], "vidNEW00001")
        messages = [m for m, _r in made_clips.upload(self.clip, "public", service_factory=lambda: self.service)]
        self.assertIn("省略", messages[0])
        self.assertEqual(len(self.service.inserted), 1)
        self.assertEqual(self.service.updated, [])

    def test_public_upload_goes_private_first_then_publishes(self):
        with patch("moment_retrieval.used_ranges.record_from_clip"):
            results = list(made_clips.upload(self.clip, "public", service_factory=lambda: self.service))
        self.assertEqual(self.service.inserted[0]["status"]["privacyStatus"], "private")
        self.assertEqual(self.service.updated[0]["status"]["privacyStatus"], "public")
        self.assertEqual(results[-1][1]["privacy_status"], "public")
        self.assertEqual(made_clips.read_receipt(self.clip)["privacy_status"], "public")

    def test_only_listed_clips_can_be_uploaded(self):
        stray = Path(self.tmp.name) / "stray.mp4"
        stray.write_bytes(b"mp4")
        with self.assertRaises(youtube_upload.UploadError):
            list(made_clips.upload(stray, "private", service_factory=lambda: self.service))
        self.assertEqual(self.service.inserted, [])

    def test_change_privacy_and_refresh_status_update_the_receipt(self):
        youtube_upload.receipt_path(self.old).write_text(json.dumps(
            {"video_id": "vidOLD00001", "privacy_status": "private", "title": "t",
             "watch_url": "https://www.youtube.com/watch?v=vidOLD00001"}), encoding="utf-8")
        with self.assertRaises(youtube_upload.UploadError):
            made_clips.change_privacy(self.clip, "public", service_factory=lambda: self.service)
        receipt = made_clips.change_privacy(self.old, "unlisted", service_factory=lambda: self.service)
        self.assertEqual(receipt["privacy_status"], "unlisted")
        self.service.items = [{"id": "vidOLD00001", "status": {"privacyStatus": "unlisted", "uploadStatus": "uploaded"}}]
        self.assertEqual(made_clips.refresh_statuses(service_factory=lambda: self.service), 1)
        self.assertEqual(made_clips.status_label(made_clips.read_receipt(self.old)), "限定公開・処理中")
        self.service.items = []
        made_clips.refresh_statuses([self.old], service_factory=lambda: self.service)
        self.assertIn("見つかりません", made_clips.status_label(made_clips.read_receipt(self.old)))

    def test_status_label_tells_scheduled_and_public_apart(self):
        receipt = {"video_id": "v", "privacy_status": "private", "publish_at": "2026-11-13T09:00:00Z"}
        label = made_clips.status_label(receipt)
        self.assertTrue(label.startswith("公開予約 "), label)
        self.assertIn("今は非公開", label)
        self.assertIsNotNone(made_clips.scheduled_publish_at(receipt))
        public = {**receipt, "privacy_status": "public"}
        self.assertEqual(made_clips.status_label(public), "公開済み")
        self.assertIsNone(made_clips.scheduled_publish_at(public))
        self.assertEqual(made_clips.status_label({**receipt, "publish_at": "broken"}), "非公開")

    def test_refresh_reads_the_scheduled_time_and_privacy_change_clears_it(self):
        youtube_upload.receipt_path(self.old).write_text(json.dumps(
            {"video_id": "vidOLD00001", "privacy_status": "private", "title": "t",
             "watch_url": "https://www.youtube.com/watch?v=vidOLD00001"}), encoding="utf-8")
        self.service.items = [{"id": "vidOLD00001", "status": {
            "privacyStatus": "private", "uploadStatus": "processed", "publishAt": "2026-11-13T09:00:00Z"}}]
        made_clips.refresh_statuses(service_factory=lambda: self.service)
        self.assertTrue(made_clips.status_label(made_clips.read_receipt(self.old)).startswith("公開予約"))
        receipt = made_clips.change_privacy(self.old, "unlisted", service_factory=lambda: self.service)
        self.assertEqual(receipt["publish_at"], "")

    def test_set_privacy_rejects_unknown_values_and_reports_a_locked_project(self):
        with self.assertRaises(youtube_upload.UploadError):
            youtube_upload.set_privacy("x", "everyone", service_factory=lambda: self.service)

        class _Locked(_Service):
            def update(self, part, body):
                return _Exec({"status": {"privacyStatus": "private"}})

        with self.assertRaisesRegex(youtube_upload.UploadError, "受け付けませんでした"):
            youtube_upload.set_privacy("x", "public", service_factory=_Locked)


class _HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = type("_Resp", (), {"status": status})()


class _FlakyRequest:
    """Gets a session, then the connection drops; a resumed request finishes."""

    def __init__(self, service):
        self.service = service
        self.resumable_uri = None
        self._in_error_state = False

    def next_chunk(self, num_retries=0):
        if self.resumable_uri is None:
            self.resumable_uri = "https://upload.example/session1"
            self.service.sessions += 1
            raise ConnectionError("dropped")
        if self.service.session_gone:
            raise _HttpError(404)
        self.service.resumed.append((self.resumable_uri, self._in_error_state))
        return None, {"id": "vidRESUMED1", "status": {"privacyStatus": "unlisted"}}


class _ChannelService(_Service):
    def __init__(self, uploads=(), status="processed"):
        super().__init__()
        self.uploads, self.status = list(uploads), status
        self.sessions, self.resumed, self.session_gone = 0, [], False

    def insert(self, part, body, media_body):
        self.inserted.append(body)
        return _FlakyRequest(self)

    def channels(self):
        return self

    def playlistItems(self):
        return self

    def list(self, part, **kwargs):
        if kwargs.get("mine"):
            return _Exec({"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
        if "playlistId" in kwargs:
            return _Exec({"items": [{"snippet": {"title": title}, "contentDetails": {"videoId": video_id}}
                                    for title, video_id in self.uploads]})
        return _Exec({"items": [{"id": kwargs["id"], "status": {"privacyStatus": "unlisted",
                                                                "uploadStatus": self.status}}]})


class DuplicateGuardTest(MadeClipsTest):
    def test_a_dropped_upload_resumes_its_session_instead_of_posting_again(self):
        service = _ChannelService()
        with self.assertRaisesRegex(youtube_upload.UploadError, "続きから再開"):
            list(youtube_upload.upload_private(self.clip, privacy="unlisted", service_factory=lambda: service))
        self.assertTrue(youtube_upload.session_path(self.clip).is_file())
        self.assertFalse(youtube_upload._lock_path(self.clip).exists())
        results = list(youtube_upload.upload_private(self.clip, privacy="unlisted", service_factory=lambda: service))
        self.assertEqual(service.sessions, 1)  # no second video was started
        self.assertEqual(service.resumed, [("https://upload.example/session1", True)])
        self.assertEqual(results[-1][1]["video_id"], "vidRESUMED1")
        self.assertFalse(youtube_upload.session_path(self.clip).exists())

    def test_an_expired_session_is_dropped_and_reported(self):
        service = _ChannelService()
        with self.assertRaises(youtube_upload.UploadError):
            list(youtube_upload.upload_private(self.clip, privacy="unlisted", service_factory=lambda: service))
        service.session_gone = True
        with self.assertRaisesRegex(youtube_upload.UploadError, "再開できませんでした"):
            list(youtube_upload.upload_private(self.clip, privacy="unlisted", service_factory=lambda: service))
        self.assertFalse(youtube_upload.session_path(self.clip).exists())

    def test_same_title_on_the_channel_blocks_or_is_adopted(self):
        stuck = _ChannelService(uploads=[("見出し", "vidSTUCK001")], status="uploaded")
        with self.assertRaisesRegex(youtube_upload.UploadError, "既にあります"):
            list(youtube_upload.upload_private(self.clip, privacy="unlisted", service_factory=lambda: stuck))
        self.assertEqual(stuck.inserted, [])
        done = _ChannelService(uploads=[("見出し", "vidDONE0001")])
        results = list(youtube_upload.upload_private(self.clip, privacy="unlisted", service_factory=lambda: done))
        self.assertEqual(done.inserted, [])
        self.assertEqual(results[-1][1]["video_id"], "vidDONE0001")
        self.assertEqual(made_clips.read_receipt(self.clip)["video_id"], "vidDONE0001")

    def test_a_lost_receipt_is_restored_from_the_folder_log_without_posting(self):
        service = _ChannelService()
        service.insert = lambda part, body, media_body: _Request(service)
        list(youtube_upload.upload_private(self.clip, privacy="unlisted", service_factory=lambda: service))
        youtube_upload.receipt_path(self.clip).unlink()
        calls = []
        results = list(youtube_upload.upload_private(self.clip, privacy="unlisted",
                                                     service_factory=lambda: calls.append(1)))
        self.assertEqual(calls, [])  # not even a YouTube call: the upload list lags behind
        self.assertEqual(results[-1][1]["video_id"], "vidNEW00001")
        self.assertEqual(made_clips.read_receipt(self.clip)["video_id"], "vidNEW00001")

    def test_a_clip_being_uploaded_elsewhere_is_not_started_again(self):
        youtube_upload._lock_path(self.clip).write_text("123", encoding="utf-8")
        service = _ChannelService()
        with self.assertRaisesRegex(youtube_upload.UploadError, "アップロード中"):
            list(youtube_upload.upload_private(self.clip, privacy="unlisted", service_factory=lambda: service))
        self.assertEqual(service.inserted, [])


if __name__ == "__main__":
    unittest.main()
