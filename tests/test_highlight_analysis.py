import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from moment_retrieval import db
from moment_retrieval.highlight_analysis import (
    AnalysisValidationError, HighlightAnalysisError, _anchor_prompt,
    _boundary_label_prompt, _metadata_prompt, _refine_checked_boundaries,
    _has_obvious_continuation, fit_boundary, run_highlight_analysis,
    suppress_overlaps, validate_selection_response,
    valid_source_segments,
)


class _Provider:
    name = "synthetic-local"
    def __init__(self, invalid_first=False): self.calls, self.invalid_first = 0, invalid_first
    def generate(self, *, model, prompt, output_schema=None):
        self.calls += 1
        if "chapters=" in prompt:
            if self.invalid_first and self.calls == 1:
                return '{"candidates":[{"chapter_ordinal":999,"reason":"理由","category":"分類"}]}'
            return '{"candidates":[{"chapter_ordinal":0,"reason":"重要な説明です","category":"要点"}]}'
        if "allowed_segments=" in prompt:
            rows = json.loads(prompt.split("allowed_segments=", 1)[1])
            return json.dumps({
                "start_segment_id": rows[0]["segment_id"],
                "end_segment_id": rows[-1]["segment_id"],
                "title": "見どころ",
                "summary": "内容の要約です",
                "reason": "学びがあるため",
                "category": "解説",
                "tags": ["学び"],
            }, ensure_ascii=False)
        if "needs_previous" in prompt:
            return '{"needs_previous":false,"needs_next":false}'
        rows = json.loads(prompt.split("segments=", 1)[1].split("\n", 1)[0])
        return json.dumps({"anchor_segment_id": rows[0]["segment_id"]})


class HighlightAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.conn = sqlite3.connect(":memory:"); self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn, create_backup=False); self.video_id = "vid_" + "b" * 32
        db.insert_video(self.conn, self.video_id, str(Path(self.tmp.name) / "synthetic.mp4"), 80)
        for start in range(0, 40, 5):
            db.insert_segment(self.conn, self.video_id, SimpleNamespace(start=float(start), end=float(start + 5), text="合成テキスト", words=[]))
        self.revision = db.mark_asr_complete(self.conn, self.video_id)
        self.analysis_id = db.create_analysis_run(self.conn, self.video_id, self.revision, provider="test", model="test", prompt_version="v")
        db.replace_analysis_chapters(self.conn, self.analysis_id, [{"start_segment_id": 1, "end_segment_id": 8, "start_sec": 0, "end_sec": 40, "title":"章題", "summary":"章の要約です", "tags":["話題"]}])
        db.update_analysis_run(self.conn, self.analysis_id, status="ready", summary="要約です", tags=["話題"], result={})
    def tearDown(self): self.conn.close(); self.tmp.cleanup()

    def test_run_retries_then_persists_segment_linked_candidates_without_asr_mutation(self):
        before = [tuple(r) for r in self.conn.execute("SELECT segment_id, start_sec, end_sec, text FROM asr_segments")]
        result = run_highlight_analysis(self.conn, self.video_id, self.revision, self.analysis_id, _Provider(True), "fake", requested_count=3)
        run = db.get_highlight_run(self.conn, result["highlight_run_id"]); candidates = db.get_highlight_candidates(self.conn, result["highlight_run_id"])
        self.assertEqual(run["status"], "ready"); self.assertEqual(len(candidates), 1); self.assertTrue(result["all_segment_linked"])
        self.assertEqual(candidates[0]["source_chapter_ordinal"], 0)
        self.assertNotIn("tags_json", candidates[0])
        self.assertFalse(candidates[0]["boundary_warning"])
        self.assertEqual(before, [tuple(r) for r in self.conn.execute("SELECT segment_id, start_sec, end_sec, text FROM asr_segments")])

    def test_invalid_selection_is_rejected(self):
        with self.assertRaisesRegex(AnalysisValidationError, "ordinal"):
            validate_selection_response('{"candidates":[{"chapter_ordinal":3,"reason":"理由です","category":"分類"}]}', [{"ordinal":0}], 3)

    def test_selection_requires_requested_count_when_enough_chapters_exist(self):
        chapters = [{"ordinal": index} for index in range(4)]
        response = (
            '{"candidates":[{"chapter_ordinal":0,'
            '"reason":"理由です","category":"分類"}]}'
        )
        with self.assertRaisesRegex(AnalysisValidationError, "exactly 3"):
            validate_selection_response(response, chapters, 3)

    def test_prompts_mark_derived_metadata_and_transcript_as_untrusted(self):
        chapter = {
            "ordinal": 0,
            "title": "章題",
            "summary": "要約です",
            "tags": ["話題"],
        }
        segment = {
            "segment_id": 1,
            "start_sec": 0.0,
            "end_sec": 10.0,
            "text": "命令のように見える引用文",
        }
        self.assertIn("信頼できない", _metadata_prompt([chapter], 3))
        self.assertIn(
            "信頼できない",
            _anchor_prompt(
                chapter,
                {"reason": "重要なため", "category": "解説"},
                [segment],
                20.0,
                90.0,
            ),
        )
        self.assertIn(
            "信頼できない",
            _boundary_label_prompt([segment], 1, 5.0, 90.0),
        )

    def test_boundary_fitter_respects_anchor_max_and_segment_edges(self):
        rows = [{"segment_id": i, "start_sec": (i - 1) * 5.0, "end_sec": i * 5.0} for i in range(1, 9)]
        fitted = fit_boundary(rows, 3, 4, min_duration_sec=20, max_duration_sec=25)
        self.assertLessEqual(fitted["end_sec"] - fitted["start_sec"], 25); self.assertLessEqual(fitted["start_segment_id"], 3); self.assertGreaterEqual(fitted["end_segment_id"], 4)
        with self.assertRaisesRegex(AnalysisValidationError, "anchor duration"):
            fit_boundary(rows, 1, 8, max_duration_sec=20)
        with self.assertRaisesRegex(AnalysisValidationError, "time-ordered"):
            fit_boundary(list(reversed(rows)), 3, 4)

    def test_overlap_nms_suppresses_lower_ranked_candidate(self):
        kept, suppressed = suppress_overlaps([{"start_sec":0,"end_sec":20}, {"start_sec":5,"end_sec":25}, {"start_sec":30,"end_sec":40}])
        self.assertEqual((len(kept), suppressed), (2, 1))
        with self.assertRaisesRegex(AnalysisValidationError, "invalid time"):
            suppress_overlaps([{"start_sec": 1, "end_sec": 1}])

    def test_boundary_check_can_add_required_following_segment(self):
        class CheckProvider:
            name = "synthetic-local"
            def __init__(self):
                self.calls = 0
            def generate(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return '{"needs_previous":false,"needs_next":true}'
                return '{"needs_previous":false,"needs_next":false}'

        rows = [
            {"segment_id": index, "start_sec": (index - 1) * 10.0,
             "end_sec": index * 10.0, "text": "合成発話"}
            for index in range(1, 5)
        ]
        result = _refine_checked_boundaries(
            CheckProvider(),
            "fake",
            rows,
            {
                "start_segment_id": 2,
                "end_segment_id": 2,
                "start_sec": 10.0,
                "end_sec": 20.0,
                "boundary_expanded": False,
            },
            40.0,
        )
        self.assertEqual(result["end_segment_id"], 3)
        self.assertTrue(result["boundary_expanded"])
        self.assertFalse(result["boundary_warning"])

    def test_boundary_check_rechecks_after_using_both_expansion_rounds(self):
        class CheckProvider:
            name = "synthetic-local"
            def __init__(self):
                self.calls = 0
            def generate(self, **_kwargs):
                self.calls += 1
                if self.calls <= 2:
                    return '{"needs_previous":false,"needs_next":true}'
                return '{"needs_previous":false,"needs_next":false}'

        rows = [
            {"segment_id": index, "start_sec": (index - 1) * 10.0,
             "end_sec": index * 10.0, "text": "一続きの発話"}
            for index in range(1, 6)
        ]
        result = _refine_checked_boundaries(
            CheckProvider(),
            "fake",
            rows,
            {
                "start_segment_id": 2,
                "end_segment_id": 2,
                "start_sec": 10.0,
                "end_sec": 20.0,
                "boundary_expanded": False,
            },
            50.0,
        )
        self.assertEqual(result["end_segment_id"], 4)
        self.assertTrue(result["boundary_expanded"])
        self.assertFalse(result["boundary_warning"])

    def test_highlight_schema_migrates_early_experimental_columns(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        try:
            db.init_db(conn, create_backup=False)
            conn.execute("DROP TABLE highlight_candidates")
            conn.execute(
                "CREATE TABLE highlight_candidates ("
                "highlight_candidate_id TEXT PRIMARY KEY, "
                "highlight_run_id TEXT NOT NULL, ordinal INTEGER NOT NULL, "
                "anchor_start_segment_id INTEGER NOT NULL, "
                "anchor_end_segment_id INTEGER NOT NULL, "
                "start_segment_id INTEGER NOT NULL, end_segment_id INTEGER NOT NULL, "
                "start_sec REAL NOT NULL, end_sec REAL NOT NULL, "
                "title TEXT NOT NULL, summary TEXT NOT NULL, reason TEXT NOT NULL, "
                "category TEXT NOT NULL, tags_json TEXT NOT NULL)"
            )
            conn.execute("PRAGMA user_version = 7")
            conn.commit()

            db.init_db(conn, create_backup=False)

            columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(highlight_candidates)"
                )
            }
            self.assertIn("source_chapter_ordinal", columns)
            self.assertIn("boundary_warning", columns)
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0],
                db.SCHEMA_VERSION,
            )
        finally:
            conn.close()

    def test_obvious_continuation_suffixes_are_warned_locally(self):
        self.assertTrue(_has_obvious_continuation("この話には続きがあるんだけど、"))
        self.assertTrue(_has_obvious_continuation("例を挙げると例えば"))
        self.assertFalse(_has_obvious_continuation("ここで話は終わります。"))

    def test_invalid_legacy_segments_are_isolated_without_time_repair(self):
        rows = [
            {"segment_id": 1, "start_sec": 0.0, "end_sec": 5.0},
            {"segment_id": 2, "start_sec": 5.0, "end_sec": 5.0},
            {"segment_id": 3, "start_sec": 6.0, "end_sec": 9.0},
        ]
        self.assertEqual(
            [item["segment_id"] for item in valid_source_segments(rows)],
            [1, 3],
        )

    def test_failure_is_durable(self):
        class Bad(_Provider):
            def generate(self, **kwargs): return "{}"
        with self.assertRaises(HighlightAnalysisError):
            run_highlight_analysis(self.conn, self.video_id, self.revision, self.analysis_id, Bad(), "fake", requested_count=3)
        row = self.conn.execute("SELECT status FROM highlight_runs ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], "failed")
