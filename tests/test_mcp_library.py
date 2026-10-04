"""MCP library tools against a synthetic SQLite library (no media, no models)."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cut_mcp
from moment_retrieval import config, db
from moment_retrieval.mcp_library import LibraryToolError, LibraryTools


class LibraryToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "index.db"
        conn = self.connect()
        db.init_db(conn, create_backup=False)
        self.video_id = "vid_" + "c" * 32
        db.insert_video(conn, self.video_id, str(Path(self.tmp.name) / "synthetic.mp4"), 120)
        texts = ["こんにちは", "今日はカレーを", "作ります", "まず玉ねぎ", "を炒めます", "完成です"]
        for index, text in enumerate(texts):
            db.insert_segment(conn, self.video_id, SimpleNamespace(
                start=float(index * 10), end=float(index * 10 + 9), text=text, words=[]))
        self.revision = db.mark_asr_complete(conn, self.video_id)
        self.public_id = db.public_video_id(conn, self.video_id)
        self.segment_ids = [int(r["segment_id"]) for r in db.get_segments(conn, self.video_id)]
        conn.close()
        self.tools = LibraryTools(self.connect)

    def tearDown(self):
        self.tmp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_list_hides_local_paths(self):
        result = self.tools.list_videos()
        self.assertEqual(result["videos"][0]["video_id"], self.public_id)
        self.assertTrue(result["videos"][0]["transcript_ready"])
        self.assertNotIn(self.tmp.name, str(result))

    def test_read_pages_cover_every_segment(self):
        page = self.tools.read_transcript(self.public_id, 0, 4)
        self.assertEqual(page["next_start_index"], 4)
        self.assertFalse(page["covers_all_segments"])
        rest = self.tools.read_transcript(self.public_id, 4, 4)
        ids = [r["segment_id"] for r in page["untrusted_source_data"] + rest["untrusted_source_data"]]
        self.assertEqual(ids, self.segment_ids)
        self.assertIsNone(rest["next_start_index"])
        self.assertEqual(page["transcript_revision"], self.revision)

    def test_search_matches_across_adjacent_segments(self):
        hits = self.tools.search_transcript("玉ねぎを炒め")["untrusted_source_data"]
        self.assertEqual(len(hits), 1)
        self.assertEqual((hits[0]["start_segment_id"], hits[0]["end_segment_id"]),
                         (self.segment_ids[3], self.segment_ids[4]))
        single = self.tools.search_transcript("炒めます")["untrusted_source_data"]
        self.assertEqual([(h["start_segment_id"], h["end_segment_id"]) for h in single],
                         [(self.segment_ids[4], self.segment_ids[4])])

    def test_proposal_is_snapped_stored_and_hidden_from_summary(self):
        result = self.tools.propose_clips(self.public_id, self.revision, [
            {"start_segment_id": self.segment_ids[1], "end_segment_id": self.segment_ids[2],
             "title": "カレー作り", "reason": "料理の導入"},
            {"start_segment_id": self.segment_ids[5], "end_segment_id": self.segment_ids[1],
             "title": "逆順", "reason": "不正"},
        ], min_duration_sec=25, max_duration_sec=60)
        self.assertFalse(result["exported"])
        self.assertEqual(len(result["saved_candidates"]), 1)
        self.assertEqual(len(result["rejected"]), 1)
        saved = result["saved_candidates"][0]
        self.assertGreaterEqual(saved["end_ms"] - saved["start_ms"], 25000)
        self.assertTrue(saved["boundary_expanded"])
        conn = self.connect()
        try:
            run = db.get_latest_ready_highlight_run(conn, self.video_id, self.revision)
            self.assertEqual(run["highlight_run_id"], result["highlight_run_id"])
            self.assertEqual(run["result"]["generation_mode"], "mcp")
            chapters = db.get_analysis_chapters(conn, run["analysis_run_id"])
            self.assertEqual(chapters[0]["title"], "カレー作り")
            self.assertIsNone(db.get_latest_ready_analysis_run(conn, self.video_id, self.revision))
            self.assertEqual(db.list_analysis_runs(conn, self.video_id, self.revision), [])
        finally:
            conn.close()

    def test_already_posted_ranges_are_shown_and_overlapping_proposals_rejected(self):
        conn = self.connect()
        db.add_used_clip_range(conn, self.public_id, 10.0, 29.0, "投稿済み")
        conn.close()
        page = self.tools.read_transcript(self.public_id)
        self.assertEqual(page["already_posted_ranges"], [{"start_ms": 10000, "end_ms": 29000, "title": "投稿済み"}])
        result = self.tools.propose_clips(self.public_id, self.revision, [
            {"start_segment_id": self.segment_ids[1], "end_segment_id": self.segment_ids[2],
             "title": "重なる", "reason": "r"},
            {"start_segment_id": self.segment_ids[4], "end_segment_id": self.segment_ids[5],
             "title": "新しい", "reason": "r"},
        ], min_duration_sec=15, max_duration_sec=60)
        self.assertEqual([c["title"] for c in result["saved_candidates"]], ["新しい"])
        self.assertIn("投稿済み", result["rejected"][0]["reason"])

    def test_used_range_bookkeeping_dedupes_and_validates(self):
        conn = self.connect()
        try:
            self.assertTrue(db.add_used_clip_range(conn, self.public_id, 1, 2, "a"))
            self.assertFalse(db.add_used_clip_range(conn, self.public_id, 1, 2, "a"))
            with self.assertRaises(ValueError):
                db.add_used_clip_range(conn, self.public_id, 5, 5)
            self.assertEqual(len(db.list_used_clip_ranges(conn, self.public_id)), 1)
        finally:
            conn.close()

    def test_stale_revision_and_unknown_video_are_rejected(self):
        with self.assertRaises(LibraryToolError) as ctx:
            self.tools.propose_clips(self.public_id, "tr_old", [
                {"start_segment_id": self.segment_ids[0], "end_segment_id": self.segment_ids[0],
                 "title": "t", "reason": "r"}])
        self.assertEqual(ctx.exception.code, "TRANSCRIPT_CHANGED")
        with self.assertRaises(LibraryToolError):
            self.tools.read_transcript("vid_missing")

    def test_short_export_runs_in_background_for_the_latest_proposal_only(self):
        source = Path(self.tmp.name) / "synthetic.mp4"
        source.write_bytes(b"synthetic")
        proposal = self.tools.propose_clips(self.public_id, self.revision, [
            {"start_segment_id": self.segment_ids[1], "end_segment_id": self.segment_ids[2],
             "title": "カレー作り", "reason": "料理の導入"}], min_duration_sec=20, max_duration_sec=60)
        commands = []

        class _Process:
            def poll(self):
                return None

        tools = LibraryTools(self.connect, spawn=lambda command, **kwargs: commands.append(command) or _Process())
        with patch.object(config, "CACHE_ROOT", Path(self.tmp.name) / "cache"), \
                patch.object(config, "ARTIFACT_ROOT", Path(self.tmp.name) / "clips"):
            with self.assertRaises(LibraryToolError) as ctx:
                tools.start_short_export(self.public_id, "highlight_stale")
            self.assertEqual(ctx.exception.code, "PROPOSAL_NOT_LATEST")
            started = tools.start_short_export(self.public_id, proposal["highlight_run_id"], layout="crop")
            self.assertIn("--layout", commands[0])
            self.assertEqual(commands[0][commands[0].index("--layout") + 1], "crop")
            self.assertNotIn("--no-captions", commands[0])
            with self.assertRaises(LibraryToolError) as ctx:
                tools.start_short_export(self.public_id, proposal["highlight_run_id"])
            self.assertEqual(ctx.exception.code, "EXPORT_BUSY")
            clip = Path(self.tmp.name) / "clips" / "highlights" / "v" / "a.mp4"
            status_file = Path(self.tmp.name) / "cache" / "mcp_exports" / f"{started['job_id']}.json"
            status_file.write_text(json.dumps({"state": "done", "log": "1/1 保存完了", "outputs": [str(clip)]}),
                                   encoding="utf-8")
            status = tools.export_status(started["job_id"])
        self.assertEqual(status["state"], "done")
        self.assertEqual(status["outputs_relative_to_clips"], [str(Path("highlights") / "v" / "a.mp4")])

    def test_mcp_requires_transfer_consent_and_validates_nested_items(self):
        tools = cut_mcp.CaptionTools(library=self.tools)
        with self.assertRaises(cut_mcp.ToolError) as ctx:
            tools.call("cut_read_transcript", {"video_id": self.public_id, "allow_transcript_transfer": False})
        self.assertEqual(ctx.exception.code, "PRIVACY_CONFIRMATION_REQUIRED")
        page = tools.call("cut_read_transcript", {"video_id": self.public_id, "allow_transcript_transfer": True})
        self.assertTrue(page["covers_all_segments"])
        with self.assertRaises(cut_mcp.ToolError):
            tools.call("cut_propose_clips", {"video_id": self.public_id, "transcript_revision": self.revision,
                                             "candidates": [{"start_segment_id": 1, "title": "x", "reason": "y"}]})
        with self.assertRaises(cut_mcp.ToolError) as ctx:
            tools.call("cut_read_transcript", {"video_id": "vid_missing", "allow_transcript_transfer": True})
        self.assertEqual(ctx.exception.code, "VIDEO_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
