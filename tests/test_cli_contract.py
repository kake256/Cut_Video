import io
import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

import video_tool
from moment_retrieval import db
from moment_retrieval.contracts import ContractError, failure, success


class CLIContractTest(unittest.TestCase):
    def test_success_contract_is_versioned(self):
        payload = success("search", {"text_hits": [], "semantic_hits": []})
        self.assertEqual(payload["schema_version"], 1)
        self.assertTrue(payload["ok"])

    def test_failure_contract_is_structured(self):
        payload = failure("clip", ContractError("INVALID_PLAN", "bad plan"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "INVALID_PLAN")

    def test_cli_emits_only_json_to_stdout(self):
        expected = success("search", {"text_hits": [], "semantic_hits": []})
        output = io.StringIO()
        with patch("video_tool._search", return_value=expected), redirect_stdout(output):
            exit_code = video_tool.main(["search", "--query", "synthetic"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), expected)

    def test_search_reports_pending_semantics_without_loading_the_model(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        db.insert_video(conn, "synthetic-storage", "X:/synthetic.mp4", 30.0)
        conn.execute(
            "INSERT INTO asr_segments(video_id,start_sec,end_sec,text,words_json) "
            "VALUES(?,?,?,?,?)",
            ("synthetic-storage", 1.0, 2.0, "synthetic needle", "[]"),
        )
        db.mark_asr_complete(conn, "synthetic-storage")
        public_id = db.public_video_id(conn, "synthetic-storage")
        args = SimpleNamespace(
            query="needle",
            video_id=public_id,
            text_limit=20,
            semantic_limit=5,
            min_score=0.55,
        )

        with (
            patch("video_tool.db.get_conn", return_value=conn),
            patch("video_tool.TextEmbedder") as embedder,
        ):
            payload = video_tool._search(args)

        self.assertEqual(payload["warnings"], ["SEMANTIC_PENDING"])
        self.assertEqual(len(payload["data"]["text_hits"]), 1)
        self.assertEqual(payload["data"]["semantic_hits"], [])
        embedder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
