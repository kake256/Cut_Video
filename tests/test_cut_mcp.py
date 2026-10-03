"""Read-only MCP contract tests; synthetic captions only, no real network."""
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock

import cut_mcp
from moment_retrieval.youtube_captions import CaptionCue, CaptionError, CaptionPreview


def preview(count=5, text="合成字幕"):
    return CaptionPreview(
        "https://www.youtube.com/watch?v=syn00000001", "合成テスト動画", "ja", True,
        tuple(CaptionCue(i * 1000, (i + 1) * 1000, f"{text} {i}") for i in range(count)),
    )


class CaptionToolTests(unittest.TestCase):
    def setUp(self):
        self.fetch = Mock(return_value=preview())
        self.now = 0
        self.tools = cut_mcp.CaptionTools(self.fetch, lambda: self.now)

    def inspect(self):
        return self.tools.call("cut_inspect_youtube", {"url": "https://youtu.be/syn00000001"})

    def read(self, key, **kwargs):
        return self.tools.call("cut_read_youtube_captions", {
            "preview_id": key, "allow_caption_transfer": True, **kwargs,
        })

    def test_inspection_returns_metadata_not_caption_body(self):
        result = self.inspect()
        self.assertNotIn("合成字幕", json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["total_cues"], 5)
        self.fetch.assert_called_once_with("https://youtu.be/syn00000001", "ja")

    def test_pages_are_complete_and_never_claim_partial_is_whole(self):
        key = self.inspect()["preview_id"]
        result = self.read(key, max_cues=2)
        self.assertFalse(result["covers_all_captions"])
        self.assertEqual(result["next_start_cue"], 2)
        cues = result["untrusted_source_data"]
        while result["next_start_cue"] is not None:
            result = self.read(key, start_cue=result["next_start_cue"], max_cues=2)
            cues.extend(result["untrusted_source_data"])
        self.assertEqual([cue["cue_index"] for cue in cues], list(range(5)))
        self.assertFalse(result["covers_all_captions"])
        self.assertTrue(self.read(key)["covers_all_captions"])
        self.assertFalse(result["visual_content_available"])
        self.assertIn("ASR未照合", result["timestamp_basis"])

    def test_consent_is_required_even_for_cached_public_text(self):
        key = self.inspect()["preview_id"]
        for value in (False, "true", 1):
            with self.subTest(value=value), self.assertRaises(cut_mcp.ToolError):
                self.read(key, allow_caption_transfer=value)
        with self.assertRaises(cut_mcp.ToolError):
            self.tools.call("cut_read_youtube_captions", {"preview_id": key})

    def test_bad_arguments_and_local_file_tools_rejected_before_fetch(self):
        for args in (None, [], {}, {"url": "x", "path": "private"}, {"url": 1},
                     {"url": "x", "preferred_language": "other"}):
            with self.subTest(args=args), self.assertRaises(cut_mcp.ToolError):
                self.tools.call("cut_inspect_youtube", args)
        with self.assertRaises(cut_mcp.ToolError):
            self.tools.call("read_local_transcript", {"path": "private"})
        self.fetch.assert_not_called()

    def test_page_bounds_reject_bool_negative_and_oversized(self):
        key = self.inspect()["preview_id"]
        for args in ({"start_cue": -1}, {"start_cue": True}, {"start_cue": 5},
                     {"max_cues": 0}, {"max_cues": 101}, {"max_cues": 2.0}):
            with self.subTest(args=args), self.assertRaises(cut_mcp.ToolError):
                self.read(key, **args)

    def test_bounded_page_uses_cue_boundaries_without_truncation(self):
        self.fetch.return_value = preview(10, "x" * 3000)
        key = self.inspect()["preview_id"]
        result = self.read(key)
        self.assertEqual(result["next_start_cue"], 2)
        self.assertEqual(len(result["untrusted_source_data"][0]["text"]), 3002)
        self.fetch.return_value = preview(1, "x" * 9000)
        key = self.inspect()["preview_id"]
        with self.assertRaisesRegex(cut_mcp.ToolError, "字幕1件"):
            self.read(key)

    def test_ttl_eviction_and_forget_do_not_touch_files(self):
        first = self.inspect()["preview_id"]
        for _ in range(cut_mcp.MAX_PREVIEWS):
            self.inspect()
        self.assertEqual(len(self.tools.previews), cut_mcp.MAX_PREVIEWS)
        with self.assertRaisesRegex(cut_mcp.ToolError, "一時字幕"):
            self.read(first)
        current = self.inspect()["preview_id"]
        self.now = cut_mcp.CACHE_TTL_SECONDS
        with self.assertRaises(cut_mcp.ToolError):
            self.read(current)
        self.assertEqual(len(self.tools.previews), 0)
        current = self.inspect()["preview_id"]
        for _ in range(2):
            self.assertTrue(self.tools.call("cut_forget_youtube_preview", {"preview_id": current})["forgotten"])


class StdioTests(unittest.TestCase):
    def setUp(self):
        self.fetch = Mock(return_value=preview())
        self.server = cut_mcp.StdioServer(cut_mcp.CaptionTools(self.fetch))

    def request(self, method, params=None, request_id=1):
        return self.server.dispatch({"jsonrpc": "2.0", "id": request_id,
                                     "method": method, "params": params or {}})

    def initialize(self):
        return self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                            "clientInfo": {"name": "synthetic-test", "version": "1"}})

    def test_initialize_negotiate_tools_and_notifications(self):
        self.assertIn("error", self.request("tools/list"))
        initialized = self.initialize()["result"]
        self.assertEqual(initialized["protocolVersion"], "2024-11-05")
        self.assertIn("untrusted", initialized["instructions"])
        self.assertIsNone(self.server.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [tool["name"] for tool in self.request("tools/list")["result"]["tools"]]
        self.assertEqual(names, [
            "cut_inspect_youtube", "cut_read_youtube_captions", "cut_forget_youtube_preview",
            "cut_list_videos", "cut_read_transcript", "cut_search_transcript", "cut_propose_clips",
            "cut_export_shorts", "cut_export_status",
        ])
        self.assertEqual(self.request("ping")["result"], {})
        self.assertEqual(self.request("not-supported")["error"]["code"], -32601)
        self.assertEqual(self.request("initialize", {"protocolVersion": "future"})["result"]["protocolVersion"], "2025-06-18")

    def test_tool_results_safe_errors_and_stdout_isolation(self):
        self.initialize()
        def noisy_fetch(*args):
            print("synthetic-sensitive-chatter")
            return preview()
        self.fetch.side_effect = noisy_fetch
        payload = self.request("tools/call", {"name": "cut_inspect_youtube", "arguments": {"url": "synthetic"}})
        self.assertFalse(payload["result"]["isError"])
        self.fetch.side_effect = RuntimeError("synthetic-sensitive-chatter")
        payload = self.request("tools/call", {"name": "cut_inspect_youtube", "arguments": {"url": "synthetic"}})
        self.assertTrue(payload["result"]["isError"])
        self.assertNotIn("synthetic-sensitive-chatter", json.dumps(payload))
        self.fetch.side_effect = CaptionError("CAPTIONS_UNAVAILABLE", "字幕なし")
        payload = self.request("tools/call", {"name": "cut_inspect_youtube", "arguments": {"url": "synthetic"}})
        self.assertIn("CAPTIONS_UNAVAILABLE", json.dumps(payload))

    def test_stdio_utf8_framing_parse_recovery_and_limits(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "cut_inspect_youtube", "arguments": {"url": "synthetic"}}},
        ]
        incoming = io.BytesIO(b"not-json\n" + b"\n".join(json.dumps(req).encode() for req in requests) + b"\n")
        outgoing = io.BytesIO()
        cut_mcp.serve(incoming, outgoing, self.server)
        responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertIn("合成テスト動画", responses[-1]["result"]["content"][0]["text"])
        outgoing = io.BytesIO()
        cut_mcp.serve(io.BytesIO(b"x" * (cut_mcp.MAX_MESSAGE_BYTES + 1)), outgoing)
        self.assertEqual(json.loads(outgoing.getvalue())["error"]["message"], "Request too large")

    def test_real_process_handshake_does_not_import_application_or_need_network(self):
        root = Path(__file__).resolve().parents[1]
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "cut_inspect_youtube", "arguments": {"url": "file:///not-allowed"}}},
        ]
        result = subprocess.run([sys.executable, str(root / "cut_mcp.py")],
                                input="\n".join(json.dumps(m) for m in messages).encode() + b"\n",
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([m["id"] for m in responses], [1, 2, 3])
        self.assertEqual(len(responses[1]["result"]["tools"]), 9)
        self.assertTrue(responses[2]["result"]["isError"])
        self.assertIn("INVALID_URL", responses[2]["result"]["content"][0]["text"])
        self.assertEqual(result.stderr, b"")
        code = "import cut_mcp, sys; assert not any(n in sys.modules for n in ('app', 'gradio', 'faster_whisper', 'moment_retrieval.db', 'moment_retrieval.config'))"
        subprocess.run([sys.executable, "-c", code], cwd=root, check=True, timeout=15)

    def test_config_template_requires_approval_without_writing_config(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(root / "cut_mcp.py"), "--print-codex-config"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=15)
        config_text = result.stdout.decode("utf-8")
        self.assertIn('default_tools_approval_mode = "prompt"', config_text)
        self.assertIn("tool_timeout_sec = 120", config_text)
        self.assertIn("[mcp_servers.cut_youtube]", config_text)


if __name__ == "__main__":
    unittest.main()
