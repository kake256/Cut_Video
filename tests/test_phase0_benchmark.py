import getpass
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.benchmark_phase0 import (
    CASE_NAMES,
    PROFILES,
    REPORT_SCHEMA_VERSION,
    WORKSPACE_MARKER,
    WORKSPACE_PREFIX,
    BenchmarkSafetyError,
    RootSpec,
    Workspace,
    BenchmarkError,
    parse_root_spec,
    remove_workspace,
    run_benchmark,
    serialize_report,
)


class PhaseZeroBenchmarkTest(unittest.TestCase):
    def assert_report_schema(self, report, profile_name):
        self.assertEqual(report["schema_version"], REPORT_SCHEMA_VERSION)
        self.assertEqual(report["benchmark"], "cut_video_phase0_storage")
        self.assertEqual(report["profile"], profile_name)
        self.assertFalse(report["measurement"]["cold_claimed"])
        self.assertIn("uncontrolled", report["measurement"]["cache_condition"])
        self.assertTrue(report["fixture"]["synthetic_only"])
        self.assertFalse(report["fixture"]["network_used"])
        self.assertFalse(report["fixture"]["models_used"])
        self.assertFalse(report["fixture"]["media_used"])
        self.assertFalse(report["runtime"]["gpu_used"])
        self.assertIsInstance(report["runtime"]["python_version"], str)
        self.assertIsInstance(report["runtime"]["platform"], str)

        self.assertEqual(len(report["roots"]), 1)
        root_result = report["roots"][0]
        self.assertEqual(root_result["label"], "disk-a")
        self.assertRegex(root_result["fixture_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(tuple(case["name"] for case in root_result["cases"]), CASE_NAMES)
        for case in root_result["cases"]:
            self.assertEqual(case["sample_count"], PROFILES[profile_name].sample_count)
            self.assertEqual(len(case["samples_ms"]), case["sample_count"])
            self.assertGreaterEqual(case["p50_ms"], 0.0)
            self.assertGreaterEqual(case["p95_ms"], case["p50_ms"])
            self.assertIsInstance(case["result_value"], int)
            if case["name"].startswith("sequential_"):
                self.assertEqual(
                    len(case["samples_mib_per_s"]), case["sample_count"]
                )
                self.assertGreater(case["p50_mib_per_s"], 0.0)
                self.assertGreaterEqual(
                    case["p95_mib_per_s"], case["p50_mib_per_s"]
                )

    def test_tiny_profile_schema_cleanup_and_sanitized_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sentinel = root / "keep.txt"
            sentinel.write_text("synthetic sentinel", encoding="utf-8")

            report = run_benchmark([RootSpec("disk-a", root)], "tiny")
            self.assert_report_schema(report, "tiny")
            cases = {
                case["name"]: case for case in report["roots"][0]["cases"]
            }
            # The fixture stores the target in full-width uppercase, so these
            # hits prove that Python-side NFKC/casefold normalization ran.
            self.assertEqual(cases["sqlite_selected_text_scan"]["result_value"], 1)
            self.assertEqual(cases["sqlite_all_text_scan"]["result_value"], 1)
            self.assertTrue(sentinel.exists())
            self.assertEqual(
                [item for item in root.iterdir() if item.name.startswith(WORKSPACE_PREFIX)],
                [],
            )

            rendered = serialize_report(report)
            self.assertEqual(json.loads(rendered), report)
            self.assertNotIn(str(root), rendered)
            self.assertNotIn(root.as_posix(), rendered)
            self.assertNotIn(socket.gethostname(), rendered)
            username = getpass.getuser()
            if len(username) >= 5:
                self.assertNotIn(username, rendered)

    def test_smoke_profile_uses_same_public_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            report = run_benchmark([RootSpec("disk-a", root)], "smoke")
        self.assert_report_schema(report, "smoke")

    def test_multiple_roots_use_identical_deterministic_sqlite_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            first = parent / "first"
            second = parent / "second"
            first.mkdir()
            second.mkdir()
            report = run_benchmark(
                [RootSpec("disk-a", first), RootSpec("disk-b", second)], "tiny"
            )
        self.assertEqual(len(report["roots"]), 2)
        self.assertEqual(
            report["roots"][0]["fixture_sha256"],
            report["roots"][1]["fixture_sha256"],
        )

    def test_fixture_digest_mismatch_fails_the_comparison(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            first = parent / "first"
            second = parent / "second"
            first.mkdir()
            second.mkdir()
            fake_results = [
                {"label": "disk-a", "fixture_sha256": "a" * 64, "cases": []},
                {"label": "disk-b", "fixture_sha256": "b" * 64, "cases": []},
            ]
            with patch(
                "scripts.benchmark_phase0.benchmark_root", side_effect=fake_results
            ):
                with self.assertRaises(BenchmarkError):
                    run_benchmark(
                        [RootSpec("disk-a", first), RootSpec("disk-b", second)],
                        "tiny",
                    )

    def test_cleanup_refuses_a_directory_without_a_matching_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            candidate = root / f"{WORKSPACE_PREFIX}not-owned"
            candidate.mkdir()
            workspace = Workspace(root=root, path=candidate, token="expected-token")

            with self.assertRaises(BenchmarkSafetyError):
                remove_workspace(workspace)
            self.assertTrue(candidate.exists())

            (candidate / WORKSPACE_MARKER).write_text(
                json.dumps({"kind": "wrong-kind", "token": "expected-token"}),
                encoding="utf-8",
            )
            with self.assertRaises(BenchmarkSafetyError):
                remove_workspace(workspace)
            self.assertTrue(candidate.exists())

    def test_root_parser_requires_label_and_existing_directory(self):
        with self.assertRaises(Exception):
            parse_root_spec("missing-label-separator")
        with tempfile.TemporaryDirectory() as temporary:
            spec = parse_root_spec(f"ssd={temporary}")
            self.assertEqual(spec.label, "ssd")
            self.assertTrue(spec.path.is_absolute())

            identity = getpass.getuser()
            if len(identity) >= 3:
                with self.assertRaises(Exception):
                    parse_root_spec(f"disk-{identity}={temporary}")


if __name__ == "__main__":
    unittest.main()
