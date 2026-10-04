"""AI detection/setup and local selection with fakes (no installs, no network, no Ollama)."""
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from moment_retrieval import ai_setup, local_selector


class AiSetupTest(unittest.TestCase):
    def test_status_reads_cli_login_state(self):
        with patch.object(ai_setup.agent_runner, "find_executable", return_value="codex.exe"):
            ok = ai_setup.codex_status(runner=lambda *a, **k: SimpleNamespace(
                returncode=0, stdout="Logged in using ChatGPT", stderr=""))
            no = ai_setup.codex_status(runner=lambda *a, **k: SimpleNamespace(
                returncode=1, stdout="Not logged in", stderr=""))
        self.assertTrue(ok["ready"])
        self.assertFalse(no["ready"])
        with patch.object(ai_setup.agent_runner, "find_executable", return_value="claude"):
            claude = ai_setup.claude_status(runner=lambda *a, **k: SimpleNamespace(
                returncode=0, stdout=json.dumps({"loggedIn": True}), stderr=""))
        self.assertTrue(claude["ready"])
        with patch.object(ai_setup.agent_runner, "find_executable", return_value=None):
            self.assertFalse(ai_setup.claude_status()["installed"])

    def test_ollama_status_needs_the_configured_model(self):
        def opener(models):
            return lambda url, timeout: io.BytesIO(json.dumps({"models": [{"name": m} for m in models]}).encode())

        self.assertTrue(ai_setup.ollama_status(opener=opener([ai_setup.config.LLM_ANALYSIS_MODEL]))["ready"])
        self.assertFalse(ai_setup.ollama_status(opener=opener(["other:1b"]))["ready"])
        down = ai_setup.ollama_status(opener=lambda *a, **k: (_ for _ in ()).throw(OSError("refused")))
        self.assertFalse(down["ready"])

    def test_install_and_login_run_fixed_commands_in_a_visible_window(self):
        started = []
        popen = lambda command, **kwargs: started.append(command)
        with patch.object(ai_setup.shutil, "which", return_value=None):
            message = ai_setup.install("codex", popen=popen)
        self.assertIn("Node.js", message)
        self.assertEqual(started[-1][:2], ["cmd", "/k"])
        self.assertIn("winget install --id OpenJS.NodeJS.LTS", started[-1][2])
        with patch.object(ai_setup.shutil, "which", return_value="npm"):
            ai_setup.install("claude", popen=popen)
        self.assertIn("@anthropic-ai/claude-code", started[-1][2])
        ai_setup.install("local", popen=popen)
        self.assertIn("setup_ollama.bat", started[-1][2])
        with patch.object(ai_setup.agent_runner, "find_executable", return_value=r"C:\x\codex.exe"):
            ai_setup.login("codex", popen=popen)
        self.assertEqual(started[-1][2], '"C:\\x\\codex.exe" login')
        with self.assertRaises(ValueError):
            ai_setup.install("unknown", popen=popen)


class PrepareTest(unittest.TestCase):
    def test_one_button_installs_logs_in_or_reports_ready(self):
        started = []
        popen = lambda command, **kwargs: started.append(command[2])
        ready = {"installed": True, "ready": True, "detail": ""}
        self.assertIn("準備済み", ai_setup.prepare("codex", status=ready, popen=popen))
        self.assertEqual(started, [])
        with patch.object(ai_setup.shutil, "which", return_value="npm"):
            ai_setup.prepare("codex", status={"installed": False, "ready": False}, popen=popen)
        self.assertIn("@openai/codex", started[-1])
        with patch.object(ai_setup.agent_runner, "find_executable", return_value="claude.cmd"):
            ai_setup.prepare("claude", status={"installed": True, "ready": False}, popen=popen)
        self.assertEqual(started[-1], '"claude.cmd" auth login')
        ai_setup.prepare("local", status={"installed": True, "ready": False}, popen=popen)
        self.assertIn("setup_ollama.bat", started[-1])


class _Library:
    def __init__(self, rows):
        self.rows = rows
        self.proposed = None

    def read_transcript(self, video_id, start, count):
        return {"untrusted_source_data": self.rows[start:start + count], "transcript_revision": "tr_1",
                "next_start_index": None if start + count >= len(self.rows) else start + count}

    def propose_clips(self, video_id, revision, candidates, **kwargs):
        self.proposed = (revision, candidates, kwargs)
        return {"saved_candidates": candidates}


class _Provider:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def generate(self, *, model, prompt, output_schema):
        self.prompts.append(prompt)
        return json.dumps({"candidates": self.answers.pop(0)})


class UsedRangesTest(unittest.TestCase):
    def test_focus_changes_the_prompt_only_when_given(self):
        from moment_retrieval import agent_runner, auto_pipeline

        plain = agent_runner.ClipRequest("vid_x", 3).prompt()
        focused = agent_runner.ClipRequest("vid_x", 3, focus="謎解きができなくてキレている箇所").prompt()
        self.assertNotIn("利用者が探している場面", plain)
        self.assertIn("「謎解きができなくてキレている箇所」", focused)
        self.assertIn("件数未満でよく", focused)
        local = local_selector._prompt([{"segment_id": 1, "start_ms": 0, "end_ms": 1000, "text": "x"}], 20, 60,
                                       focus="キレている")
        self.assertIn("「キレている」", local)
        self.assertNotIn("探している場面", local_selector._prompt(
            [{"segment_id": 1, "start_ms": 0, "end_ms": 1000, "text": "x"}], 20, 60))
        self.assertEqual(auto_pipeline.clean_focus(" 「謎解き」で\n キレる'場面' "), "謎解きで キレる場面")
        self.assertEqual(len(auto_pipeline.clean_focus("あ" * 500)), auto_pipeline.MAX_FOCUS_CHARS)

    def test_overlap_rule_and_prompts_mention_posted_scenes(self):
        from moment_retrieval import agent_runner, used_ranges

        used = [{"start_sec": 60.0, "end_sec": 90.0, "title": "既出"}]
        self.assertIsNotNone(used_ranges.overlaps(70, 100, used))
        self.assertIsNone(used_ranges.overlaps(88, 150, used))  # 2s of a 30s clip is not a repeat
        self.assertIsNone(used_ranges.overlaps(200, 230, used))
        prompt = agent_runner.ClipRequest("vid_x", 2, used_ranges=tuple(used)).prompt()
        self.assertIn("0:01:00〜0:01:30「既出」", prompt)
        self.assertNotIn("投稿済み", agent_runner.ClipRequest("vid_x", 2).prompt())

    def test_upload_records_the_range_from_the_clip_metadata(self):
        import tempfile
        from pathlib import Path
        from moment_retrieval import config, db, used_ranges

        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "DB_PATH", Path(tmp) / "index.db"):
            clip = Path(tmp) / "clip_short.mp4"
            clip.write_bytes(b"x")
            self.assertFalse(used_ranges.record_from_clip(clip))  # no metadata JSON yet
            clip.with_suffix(".metadata.json").write_text(json.dumps({
                "title": "見どころ", "source_range": {"video_id": "vid_" + "e" * 32, "start_sec": 12.5, "end_sec": 40}
            }), encoding="utf-8")
            self.assertTrue(used_ranges.record_from_clip(clip))
            conn = db.get_conn()
            try:
                rows = db.list_used_clip_ranges(conn, "vid_" + "e" * 32)
            finally:
                conn.close()
        self.assertEqual(rows, [{"start_sec": 12.5, "end_sec": 40.0, "title": "見どころ", "origin": "upload"}])


class LocalSelectorTest(unittest.TestCase):
    def test_best_non_overlapping_candidates_are_proposed(self):
        rows = [{"segment_id": i, "start_ms": i * 5000, "end_ms": i * 5000 + 4500, "text": f"文{i}"}
                for i in range(1, 21)]
        provider = _Provider([[
            {"start_segment_id": 2, "end_segment_id": 6, "title": "A", "reason": "r", "score": 9},
            {"start_segment_id": 4, "end_segment_id": 8, "title": "重複", "reason": "r", "score": 8},
            {"start_segment_id": 10, "end_segment_id": 14, "title": "B", "reason": "r", "score": 7},
            {"start_segment_id": 99, "end_segment_id": 100, "title": "範囲外", "reason": "r", "score": 10},
            {"start_segment_id": 16, "end_segment_id": 18, "title": "C", "reason": "r", "score": 3},
        ]])
        library = _Library(rows)
        local_selector.select_clips("vid_x", clip_count=2, min_duration_sec=20, max_duration_sec=60,
                                    provider=provider, library=library)
        revision, candidates, kwargs = library.proposed
        self.assertEqual(revision, "tr_1")
        self.assertEqual([c["title"] for c in candidates], ["A", "B"])
        self.assertEqual(kwargs["min_duration_sec"], 20)
        self.assertIn("[1]", provider.prompts[0])

    def test_overlong_ranges_are_trimmed_to_the_maximum(self):
        rows = [{"segment_id": i, "start_ms": i * 10000, "end_ms": i * 10000 + 9000, "text": f"文{i}"}
                for i in range(1, 21)]
        library = _Library(rows)
        local_selector.select_clips(
            "vid_x", clip_count=1, min_duration_sec=20, max_duration_sec=60, library=library,
            provider=_Provider([[{"start_segment_id": 1, "end_segment_id": 20, "title": "長い", "reason": "r",
                                  "score": 9}]]),
        )
        candidate = library.proposed[1][0]
        self.assertEqual((candidate["start_segment_id"], candidate["end_segment_id"]), (1, 6))

    def test_posted_scenes_are_skipped_before_choosing(self):
        rows = [{"segment_id": i, "start_ms": i * 10000, "end_ms": i * 10000 + 9000, "text": f"文{i}"}
                for i in range(1, 21)]
        library = _Library(rows)
        provider = _Provider([[
            {"start_segment_id": 2, "end_segment_id": 4, "title": "既出と重なる", "reason": "r", "score": 10},
            {"start_segment_id": 10, "end_segment_id": 12, "title": "新しい", "reason": "r", "score": 5},
        ]])
        local_selector.select_clips("vid_x", clip_count=1, min_duration_sec=20, max_duration_sec=60,
                                    provider=provider, library=library,
                                    used=[{"start_sec": 20.0, "end_sec": 50.0, "title": "既出"}])
        self.assertEqual([c["title"] for c in library.proposed[1]], ["新しい"])
        self.assertIn("投稿済み", provider.prompts[0])

    def test_no_candidates_is_an_error(self):
        rows = [{"segment_id": 1, "start_ms": 0, "end_ms": 4000, "text": "短い"}]
        with self.assertRaises(local_selector.LocalSelectionError):
            local_selector.select_clips("vid_x", clip_count=1, min_duration_sec=20, max_duration_sec=60,
                                        provider=_Provider([[]]), library=_Library(rows))


if __name__ == "__main__":
    unittest.main()
