import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from moment_retrieval import db
from moment_retrieval.llm_analysis import (
    AnalysisValidationError,
    OllamaProvider,
    ProviderError,
    run_transcript_analysis,
    split_segments_into_windows,
    validate_aggregation_response,
    validate_analysis_response,
)


def _raw_response(group_count, *, title="雑談", summary="配信中の雑談について話している。"):
    return json.dumps({
        "summary": "配信で話した主な内容をまとめている。",
        "tags": ["雑談", "配信"],
        "chapters": [{
            "chapter_ordinal": ordinal,
            "title": title,
            "summary": summary,
            "tags": ["雑談"],
        } for ordinal in range(group_count)],
    })


def _final_response(raw_count):
    return json.dumps({
        "summary": "配信全体では近況と今後の予定について話している。",
        "tags": ["雑談", "近況", "予定", "配信", "日常"],
    })


class _CoverageProvider:
    name = "synthetic-local"

    def __init__(self):
        self.prompts = []

    def generate(self, *, model, prompt, output_schema=None):
        self.prompts.append((model, prompt, output_schema))
        if "raw_chapters=" in prompt:
            raw = json.loads(prompt.split("raw_chapters=", 1)[1])
            return _final_response(len(raw))
        groups = json.loads(prompt.split("chapter_groups=", 1)[1])
        return _raw_response(len(groups))


class LlmAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn, create_backup=False)
        self.video_id = "vid_" + "a" * 32
        db.insert_video(self.conn, self.video_id, str(Path(self.temp.name) / "synthetic.mp4"), 60)
        for start, end, text in ((0, 4, "あいうえお"), (4, 8, "かきくけこ"), (8, 12, "さしすせそ")):
            db.insert_segment(self.conn, self.video_id, SimpleNamespace(
                start=float(start), end=float(end), text=text, words=[]
            ))
        self.revision = db.mark_asr_complete(self.conn, self.video_id)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_ready_run_preserves_asr_and_maps_final_chapter_ids_to_times(self):
        segments_before = [dict(row) for row in self.conn.execute(
            "SELECT segment_id, start_sec, end_sec, text, words_json, transcript_revision "
            "FROM asr_segments ORDER BY segment_id"
        )]
        provider = _CoverageProvider()

        result = run_transcript_analysis(
            self.conn, self.video_id, self.revision, provider, "fake-model"
        )

        run = db.get_analysis_run(self.conn, result["analysis_run_id"])
        chapters = db.get_analysis_chapters(self.conn, result["analysis_run_id"])
        self.assertEqual(run["status"], "ready")
        self.assertEqual(run["provider"], "synthetic-local")
        self.assertEqual(run["tags"], ["雑談", "近況", "予定", "配信", "日常"])
        self.assertEqual(chapters[0]["start_sec"], 0.0)
        self.assertEqual(chapters[0]["end_sec"], 12.0)
        self.assertEqual(result["segment_coverage_ratio"], 1.0)
        self.assertEqual(result["raw_chapter_count"], 1)
        self.assertEqual(result["chapter_count"], 1)
        self.assertEqual(segments_before, [dict(row) for row in self.conn.execute(
            "SELECT segment_id, start_sec, end_sec, text, words_json, transcript_revision "
            "FROM asr_segments ORDER BY segment_id"
        )])

    def test_invalid_group_ordinal_is_rejected_and_failure_is_durable(self):
        class InvalidProvider(_CoverageProvider):
            def generate(self, *, model, prompt, output_schema=None):
                if "raw_chapters=" in prompt:
                    return super().generate(model=model, prompt=prompt, output_schema=output_schema)
                response = json.loads(_raw_response(1))
                response["chapters"][0]["chapter_ordinal"] = 999999
                return json.dumps(response)

        with self.assertRaises(AnalysisValidationError):
            run_transcript_analysis(self.conn, self.video_id, self.revision, InvalidProvider(), "fake")
        run = db.list_analysis_runs(self.conn, self.video_id, self.revision)[0]
        self.assertEqual(run["status"], "failed")
        self.assertIn("group ordinal", run["error_message"])
        self.assertEqual(db.get_analysis_chapters(self.conn, run["analysis_run_id"]), [])

    def test_multiple_windows_are_aggregated_to_one_final_result_with_coverage_metrics(self):
        provider = _CoverageProvider()
        result = run_transcript_analysis(
            self.conn, self.video_id, self.revision, provider, "fake",
            max_window_chars=128,
        )
        self.assertGreater(result["window_count"], 1)
        self.assertEqual(result["raw_chapter_count"], result["window_count"])
        self.assertEqual(result["chapter_count"], result["raw_chapter_count"])
        self.assertEqual(result["segment_coverage_ratio"], 1.0)
        self.assertEqual(
            len(db.get_analysis_chapters(self.conn, result["analysis_run_id"])),
            result["raw_chapter_count"],
        )
        self.assertEqual(len(provider.prompts), result["window_count"] + 1)

    def test_raw_schema_requires_each_group_ordinal_and_aggregate_schema_limits_tags(self):
        provider = _CoverageProvider()
        run_transcript_analysis(
            self.conn, self.video_id, self.revision, provider, "fake-model",
            max_window_chars=128,
        )
        raw_schema = provider.prompts[0][2]
        self.assertEqual(raw_schema["properties"]["chapters"]["minItems"], 1)
        self.assertEqual(raw_schema["properties"]["chapters"]["maxItems"], 1)
        properties = raw_schema["properties"]["chapters"]["items"]["properties"]
        self.assertEqual(properties["chapter_ordinal"]["enum"], [0])
        aggregate_schema = provider.prompts[-1][2]
        self.assertEqual(aggregate_schema["required"], ["summary", "tags"])
        self.assertEqual(aggregate_schema["properties"]["tags"]["maxItems"], 10)

    def test_english_summary_is_retried_then_rejected(self):
        segments = db.get_segments(self.conn, self.video_id, transcript_revision=self.revision)
        response = json.dumps({
            "summary": "English summary",
            "tags": ["tag"],
            "chapters": [{
                "chapter_ordinal": 0,
                "title": "English chapter",
                "summary": "English chapter summary",
                "tags": ["tag"],
            }],
        })
        with self.assertRaisesRegex(AnalysisValidationError, "Japanese"):
            validate_analysis_response(response, segments)

    def test_product_name_tag_is_preserved_but_english_phrase_is_rejected(self):
        segments = db.get_segments(self.conn, self.video_id, transcript_revision=self.revision)
        valid = json.loads(_raw_response(1))
        valid["tags"] = ["雑談", "ChatGPT"]
        valid["chapters"][0]["tags"] = ["YouTube"]
        result = validate_analysis_response(json.dumps(valid), segments)
        self.assertIn("ChatGPT", result["tags"])
        self.assertEqual(result["chapters"][0]["tags"], ["YouTube"])

        valid["tags"] = ["financial struggles"]
        with self.assertRaisesRegex(AnalysisValidationError, "Japanese"):
            validate_analysis_response(json.dumps(valid), segments)

    def test_invalid_window_response_is_retried_once_with_validation_feedback(self):
        class RetryProvider(_CoverageProvider):
            def __init__(self):
                super().__init__()
                self.raw_calls = 0

            def generate(self, *, model, prompt, output_schema=None):
                self.prompts.append((model, prompt, output_schema))
                if "raw_chapters=" in prompt:
                    raw = json.loads(prompt.split("raw_chapters=", 1)[1])
                    return _final_response(len(raw))
                self.raw_calls += 1
                groups = json.loads(prompt.split("chapter_groups=", 1)[1].split("\n直前の回答", 1)[0])
                if self.raw_calls == 1:
                    return json.dumps({
                        "summary": "English", "tags": ["雑談"], "chapters": [{
                            "chapter_ordinal": 0,
                            "title": "English", "summary": "English", "tags": ["雑談"],
                        }],
                    })
                return _raw_response(len(groups))

        provider = RetryProvider()
        result = run_transcript_analysis(
            self.conn, self.video_id, self.revision, provider, "fake-model"
        )
        self.assertEqual(result["segment_coverage_ratio"], 1.0)
        self.assertEqual(provider.raw_calls, 2)
        self.assertIn("Japanese", provider.prompts[1][1])

    def test_raw_chapters_with_an_out_of_order_group_are_rejected(self):
        response = json.dumps({
            "summary": "要約です。",
            "tags": ["雑談"],
            "chapters": [{
                "chapter_ordinal": 1,
                "title": "冒頭",
                "summary": "冒頭の話題です。",
                "tags": ["雑談"],
            }],
        })
        segments = db.get_segments(self.conn, self.video_id, transcript_revision=self.revision)
        with self.assertRaisesRegex(AnalysisValidationError, "group ordinal"):
            validate_analysis_response(response, segments)

    def test_aggregate_response_rejects_attempted_chapter_rewrite(self):
        raw = [{
            "start_segment_id": 10 + index, "end_segment_id": 10 + index,
            "start_sec": float(index), "end_sec": float(index + 1),
            "title": "章", "summary": "内容です。", "tags": ["雑談"],
        } for index in range(3)]
        response = json.dumps({
            "summary": "全体の内容です。",
            "tags": ["雑談"],
            "chapters": [],
        })
        with self.assertRaisesRegex(AnalysisValidationError, "summary and tags"):
            validate_aggregation_response(response, raw)

    def test_aggregate_response_rejects_more_than_ten_tags(self):
        raw = [{
            "start_segment_id": 10 + index, "end_segment_id": 10 + index,
            "start_sec": float(index), "end_sec": float(index + 1),
            "title": "章", "summary": "内容です。", "tags": ["雑談"],
        } for index in range(4)]
        response = json.dumps({
            "summary": "全体の内容です。",
            "tags": [f"話題{index}" for index in range(11)],
        })
        with self.assertRaisesRegex(AnalysisValidationError, "at most"):
            validate_aggregation_response(response, raw)

    def test_ollama_provider_uses_stdlib_http_and_no_streaming(self):
        class Response:
            def read(self):
                return b'{"response":"{}"}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        with patch("urllib.request.urlopen", return_value=Response()) as opener:
            self.assertEqual(OllamaProvider().generate(model="local", prompt="test"), "{}")
        request = opener.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["stream"], False)
        self.assertEqual(payload["think"], False)
        self.assertEqual(payload["options"]["temperature"], 0)
        self.assertEqual(payload["options"]["num_ctx"], 32768)

    def test_window_split_does_not_split_a_segment(self):
        windows = split_segments_into_windows([
            {"segment_id": 1, "start_sec": 0, "end_sec": 1, "text": "あ" * 200},
            {"segment_id": 2, "start_sec": 1, "end_sec": 2, "text": "い" * 200},
        ], max_chars=128)
        self.assertEqual([[item["segment_id"] for item in window] for window in windows], [[1], [2]])

    def test_ollama_network_error_is_provider_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            with self.assertRaises(ProviderError):
                OllamaProvider().generate(model="local", prompt="test")

    def test_ollama_provider_rejects_external_endpoint(self):
        with self.assertRaisesRegex(ValueError, "loopback"):
            OllamaProvider(endpoint="https://example.invalid/api/generate")
