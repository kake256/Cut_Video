import json
import tempfile
import threading
import unittest
from pathlib import Path

from moment_retrieval.application import DocumentRepository
from moment_retrieval.edit_domain import EditPlan, TimeRange
from moment_retrieval.save_service import SaveError, recover_artifact_transactions, save_document


class ApplicationSaveTest(unittest.TestCase):
    def setUp(self):
        self.documents = DocumentRepository()
        self.plan = EditPlan.create(10_000, 1_000, 9_000, (TimeRange(4_000, 5_000),))

    def test_revision_idempotency_and_history(self):
        doc = self.documents.open("vid_test", "src_test", self.plan)
        edited = self.documents.apply(doc.document_id, "cmd-1", 0, "add_exclusion", {
            "start_ms": 6_000, "end_ms": 7_000,
        })
        same = self.documents.apply(doc.document_id, "cmd-1", 0, "add_exclusion", {
            "start_ms": 2_000, "end_ms": 3_000,
        })
        self.assertEqual(same.current, edited.current)
        undone = self.documents.apply(doc.document_id, "cmd-2", 1, "undo")
        self.assertEqual(undone.current, self.plan)

    def test_reverse_save_completion_does_not_replace_newer_clean_reference(self):
        doc = self.documents.open("vid_test", "src_test", self.plan)
        first = self.documents.begin_save(doc.document_id)
        edited = self.plan.add_exclusion(6_000, 7_000)
        self.documents.apply(doc.document_id, "edit", 0, "add_exclusion", {"start_ms": 6000, "end_ms": 7000})
        second = self.documents.begin_save(doc.document_id)
        self.documents.complete_save(second, "artifact-new")
        self.documents.complete_save(first, "artifact-old")
        current = self.documents.get(doc.document_id)
        self.assertEqual(current.history.clean_reference, edited)

    def test_artifact_transaction_commits_manifest_last(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            output = root / "clip.mp4"
            doc = self.documents.open("vid_test", "src_test", self.plan)

            def cutter(_source, _ranges, target, **_kwargs):
                Path(target).write_bytes(b"video")

            result = save_document(
                doc.document_id, source, output, True,
                subtitle_text="1\n00:00:00,000 --> 00:00:01,000\ntest\n",
                documents=self.documents, cutter=cutter,
            )
            self.assertTrue(result.video_path.exists())
            self.assertTrue(result.subtitle_path.exists())
            self.assertTrue(result.manifest_path.exists())
            self.assertFalse(self.documents.get(doc.document_id).history.dirty)

    def test_cancel_before_cut_leaves_no_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            output = root / "clip.mp4"
            doc = self.documents.open("vid_test", "src_test", self.plan)
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(SaveError):
                save_document(
                    doc.document_id, source, output, True,
                    cancel_event=cancel, documents=self.documents,
                    cutter=lambda *_args, **_kwargs: None,
                )
            self.assertFalse(output.exists())

    def test_crash_recovery_removes_uncommitted_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "partial.mp4"
            output.write_bytes(b"partial")
            commit_id = f"artifact_{'a' * 32}"
            staging = root / f".cut-video-1-{commit_id}-crashed"
            staging.mkdir()
            claim = root / f".{output.name}.cut-video-claim"
            claim.write_text(json.dumps({
                "schema_version": 1,
                "commit_id": commit_id,
                "output_name": output.name,
            }), encoding="utf-8")
            (staging / "publish-journal.json").write_text(json.dumps({
                "schema_version": 2,
                "output_path": str(output),
                "subtitle_path": str(root / "partial.srt"),
                "manifest_path": str(root / "partial.mp4.manifest.json"),
                "claim_path": str(claim),
                "commit_id": commit_id,
            }), encoding="utf-8")
            self.assertEqual(recover_artifact_transactions(root), [staging])
            self.assertFalse(output.exists())
            self.assertFalse(claim.exists())

    def test_crash_recovery_never_deletes_path_outside_output_root(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            root = workspace / "clips"
            root.mkdir()
            outside = workspace / "keep.mp4"
            outside.write_bytes(b"keep")
            commit_id = f"artifact_{'b' * 32}"
            staging = root / f".cut-video-1-{commit_id}-crashed"
            staging.mkdir()
            claim = workspace / f".{outside.name}.cut-video-claim"
            claim.write_text(json.dumps({
                "schema_version": 1,
                "commit_id": commit_id,
                "output_name": outside.name,
            }), encoding="utf-8")
            (staging / "publish-journal.json").write_text(json.dumps({
                "schema_version": 2,
                "output_path": str(outside),
                "subtitle_path": str(outside.with_suffix(".srt")),
                "manifest_path": str(outside.with_suffix(".mp4.manifest.json")),
                "claim_path": str(claim),
                "commit_id": commit_id,
            }), encoding="utf-8")

            self.assertEqual(recover_artifact_transactions(root), [staging])
            self.assertEqual(outside.read_bytes(), b"keep")

    def test_crash_recovery_requires_matching_transaction_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "keep.mp4"
            output.write_bytes(b"keep")
            commit_id = f"artifact_{'c' * 32}"
            staging = root / f".cut-video-1-{commit_id}-crashed"
            staging.mkdir()
            claim = root / f".{output.name}.cut-video-claim"
            claim.write_text(json.dumps({
                "schema_version": 1,
                "commit_id": f"artifact_{'d' * 32}",
                "output_name": output.name,
            }), encoding="utf-8")
            (staging / "publish-journal.json").write_text(json.dumps({
                "schema_version": 2,
                "output_path": str(output),
                "subtitle_path": str(output.with_suffix(".srt")),
                "manifest_path": str(output.with_suffix(".mp4.manifest.json")),
                "claim_path": str(claim),
                "commit_id": commit_id,
            }), encoding="utf-8")

            self.assertEqual(recover_artifact_transactions(root), [staging])
            self.assertEqual(output.read_bytes(), b"keep")
            self.assertTrue(claim.exists())


if __name__ == "__main__":
    unittest.main()
