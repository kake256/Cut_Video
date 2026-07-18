import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from moment_retrieval.application import DocumentRepository
from moment_retrieval.edit_domain import EditPlan, TimeRange
from moment_retrieval.publication import private_source_fingerprint
from moment_retrieval.save_service import save_document


class SaveServiceIntegrationTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_precise_synthetic_video_srt_and_manifest_share_probed_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "synthetic-source.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=160x90:r=25:d=3",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=3",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(source),
                ],
                check=True,
                capture_output=True,
            )
            documents = DocumentRepository()
            plan = EditPlan.create(3_000, 500, 2_500)
            document = documents.open(
                "vid_synthetic", "src_synthetic", plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )
            result = save_document(
                document.document_id, source, root / "clip.mp4", False,
                subtitle_text="1\n00:00:00,000 --> 00:00:01,500\nsynthetic\n",
                documents=documents,
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["planned_duration_ms"], 2_000)
            self.assertLessEqual(
                abs(manifest["probed_duration_ms"] - 2_000),
                manifest["duration_tolerance_ms"],
            )
            self.assertTrue(result.video_path.exists())
            self.assertTrue(result.subtitle_path.exists())

            fast_document = documents.open(
                "vid_synthetic", "src_synthetic", plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )
            fast_result = save_document(
                fast_document.document_id, source, root / "fast-clip.mp4", False,
                documents=documents,
            )
            fast_manifest = json.loads(
                fast_result.manifest_path.read_text(encoding="utf-8")
            )
            self.assertFalse(fast_manifest["precise"])
            self.assertEqual(
                fast_manifest["duration_delta_ms"],
                fast_manifest["probed_duration_ms"] - 2_000,
            )
            if not fast_manifest["duration_matches_plan"]:
                self.assertTrue(any(
                    warning.startswith("FAST_MODE_DURATION_DRIFT:")
                    for warning in fast_manifest["warnings"]
                ))

            multi_plan = EditPlan.create(
                3_000, 0, 2_800, (TimeRange(700, 2_350),),
            )
            multi_document = documents.open(
                "vid_synthetic", "src_synthetic", multi_plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )
            multi_result = save_document(
                multi_document.document_id, source, root / "multi-clip.mp4", False,
                subtitle_text="1\n00:00:00,000 --> 00:00:00,500\nsynthetic\n",
                documents=documents,
            )
            multi_manifest = json.loads(
                multi_result.manifest_path.read_text(encoding="utf-8")
            )
            self.assertTrue(multi_manifest["precise"])
            self.assertTrue(multi_manifest["duration_matches_plan"])
            self.assertGreater(
                abs(multi_manifest["duration_delta_ms"]),
                multi_manifest["subtitle_tolerance_ms"],
            )
            self.assertLessEqual(
                abs(multi_manifest["duration_delta_ms"]),
                multi_manifest["duration_tolerance_ms"],
            )


if __name__ == "__main__":
    unittest.main()
