import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from moment_retrieval import config, db, shared_ledger, source_origin, used_ranges

URL = "https://www.youtube.com/watch?v=abcdefghijk"


@contextmanager
def person(root: Path, name: str, folder: Path):
    """Run as one CUT user: own data folder, shared ledger folder and name."""
    data = root / name
    env = {key: value for key, value in os.environ.items() if not key.startswith("CUT_SHARED_LEDGER_")}
    with patch.dict(os.environ, env, clear=True), \
            patch.object(config, "CACHE_ROOT", data), \
            patch.object(config, "DATA_DIR", data), \
            patch.object(config, "DB_PATH", data / "index.db"):
        data.mkdir(parents=True, exist_ok=True)
        (data / "shared_ledger.json").write_text(
            json.dumps({"folder": str(folder), "name": name}), encoding="utf-8")
        yield


class SharedLedgerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.folder = self.root / "shared"
        self.folder.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_off_without_settings(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("CUT_SHARED_LEDGER_")}
        with patch.dict(os.environ, env, clear=True), patch.object(config, "CACHE_ROOT", self.root / "none"):
            self.assertFalse(shared_ledger.enabled())
            self.assertFalse(shared_ledger.record("claim", URL))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_other_people_see_claims_and_ranges_by_public_video(self):
        with person(self.root, "taro", self.folder):
            # A different spelling of the same video is the same key.
            self.assertTrue(shared_ledger.record("claim", "https://youtu.be/abcdefghijk?t=30"))
            self.assertTrue(shared_ledger.record("made", URL, 10.0, 40.0, "場面1"))
            self.assertTrue(shared_ledger.record("uploaded", URL, 10.0, 40.0, "場面1"))
            self.assertEqual(shared_ledger.claimed_by(URL), [])  # own entries are not "others"
        with person(self.root, "hanako", self.folder):
            self.assertEqual(shared_ledger.claimed_by(URL), ["taro"])
            self.assertEqual(shared_ledger.ranges(URL), [
                {"start_sec": 10.0, "end_sec": 40.0, "title": "場面1", "origin": "ledger", "by": "taro"},
            ])
            self.assertEqual(shared_ledger.claimed_by("https://www.youtube.com/watch?v=zzzzzzzzzzz"), [])

    def test_writer_name_comes_from_the_file_and_broken_lines_are_skipped(self):
        (self.folder / "jiro.jsonl").write_text(
            json.dumps({"v": 1, "kind": "made", "source": URL, "by": "taro", "start_sec": 1, "end_sec": 5})
            + "\n{\"v\": 1, \"kind\": \"ma", encoding="utf-8")
        with person(self.root, "hanako", self.folder):
            self.assertEqual([row["by"] for row in shared_ledger.ranges(URL)], ["jiro"])

    def test_unreachable_folder_keeps_entries_until_next_write(self):
        missing = self.root / "drive-not-mounted"
        with person(self.root, "taro", missing):
            self.assertFalse(shared_ledger.record("claim", URL))
            self.assertFalse(missing.exists())  # never created in place of the synced drive
            missing.mkdir()
            self.assertTrue(shared_ledger.record("made", URL, 1.0, 2.0))
            self.assertFalse(shared_ledger.pending_path().exists())
        kinds = [json.loads(line)["kind"] for line in (missing / "taro.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(kinds, ["claim", "made"])

    def test_selection_avoids_ranges_other_people_clipped(self):
        def indexed_video(name: str) -> str:
            conn = db.get_conn()
            try:
                db.init_db(conn)
                path = self.root / name / "20261101_abcdefghijk.mp4"
                db.insert_video(conn, f"vid_{name}", str(path), 3600.0)
                db.init_db(conn)
                source_origin.record_download(conn, path, URL)
                return f"vid_{name}"
            finally:
                conn.close()

        with person(self.root, "taro", self.folder):
            video_id = indexed_video("taro")
            clip = self.root / "taro" / "clip.mp4"
            clip.with_suffix(".metadata.json").write_text(json.dumps({
                "title": "taroの切り抜き",
                "source_range": {"video_id": video_id, "start_sec": 100.0, "end_sec": 160.0},
            }), encoding="utf-8")
            self.assertTrue(used_ranges.record_made(clip))
            self.assertEqual(used_ranges.for_video(video_id), [])  # this PC may still use its own exports

        with person(self.root, "hanako", self.folder):
            video_id = indexed_video("hanako")
            used = used_ranges.for_video(video_id)
            self.assertEqual([(row["start_sec"], row["end_sec"], row["by"]) for row in used], [(100.0, 160.0, "taro")])
            clash = used_ranges.overlaps(140.0, 200.0, used)  # 20 of 60 s = 33% >= 30%
            self.assertEqual(used_ranges.clash_reason(clash), "taroさんが切り抜いた範囲（taroの切り抜き）と重なっています")
            self.assertIsNone(used_ranges.overlaps(150.0, 220.0, used))  # 10 of 60 s = 17%
            self.assertIn("（taroさんが作成）", used_ranges.prompt_lines(used))


class LedgerSettingsFormTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        env = {key: value for key, value in os.environ.items() if not key.startswith("CUT_SHARED_LEDGER_")}
        self._patches = [patch.dict(os.environ, env, clear=True),
                         patch.object(config, "CACHE_ROOT", self.root / "data")]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()
        self._tmp.cleanup()

    def test_save_creates_the_folder_only_under_an_existing_parent(self):
        drive = self.root / "drive"
        drive.mkdir()
        saved = shared_ledger.save_settings(str(drive / "CUT台帳"), "taro san")
        self.assertTrue((drive / "CUT台帳").is_dir())
        self.assertEqual(saved.name, "taro_san")
        self.assertEqual(shared_ledger.settings(), saved)
        with self.assertRaises(shared_ledger.LedgerSettingsError):
            shared_ledger.save_settings(str(self.root / "missing" / "CUT台帳"), "taro")
        with self.assertRaises(shared_ledger.LedgerSettingsError):
            shared_ledger.save_settings(str(drive), "  ")

    def test_status_lists_other_people_and_waiting_entries(self):
        folder = self.root / "shared"
        folder.mkdir()
        (folder / "hanako.jsonl").write_text("", encoding="utf-8")
        self.assertFalse(shared_ledger.status()["enabled"])
        shared_ledger.save_settings(str(folder), "taro")
        state = shared_ledger.status()
        self.assertTrue(state["enabled"] and state["reachable"])
        self.assertEqual(state["others"], ["hanako"])
        self.assertTrue(shared_ledger.clear_settings())
        self.assertFalse(shared_ledger.enabled())

    def test_suggested_folder_prefers_a_shared_ledger_folder_in_my_drive(self):
        drive = self.root / "G" / "マイドライブ"
        drive.mkdir(parents=True)
        with patch.object(shared_ledger, "google_drive_roots", return_value=[drive]):
            self.assertEqual(shared_ledger.suggested_folder(), str(drive / "CUT台帳"))
            (drive / "配信CUT台帳").mkdir()  # a folder someone shared, added as a shortcut
            self.assertEqual(shared_ledger.suggested_folder(), str(drive / "配信CUT台帳"))
        with patch.object(shared_ledger, "google_drive_roots", return_value=[]):
            self.assertEqual(shared_ledger.suggested_folder(), "")


if __name__ == "__main__":
    unittest.main()
