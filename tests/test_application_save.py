import json
import tempfile
import threading
import unittest
from pathlib import Path

from moment_retrieval.application import DocumentRepository
from moment_retrieval.edit_domain import EditPlan, TimeRange
from moment_retrieval.export_jobs import ExportJobRegistry, ExportStage
from moment_retrieval.output_profile import AudioProfile, CaptionProfile, OutputProfile
from moment_retrieval.publication import private_source_fingerprint
from moment_retrieval.save_service import (
    ExportVariantRequest,
    ProbedAudioArtifact,
    ProbedArtifact,
    SaveError,
    recover_artifact_transactions,
    save_document,
    save_document_variants,
)


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
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )

            def cutter(_source, _ranges, target, **_kwargs):
                Path(target).write_bytes(b"video")

            result = save_document(
                doc.document_id, source, output, True,
                subtitle_text="1\n00:00:00,000 --> 00:00:01,000\ntest\n",
                documents=self.documents, cutter=cutter,
                probe=lambda _path: ProbedArtifact(7_000, 34),
            )
            self.assertTrue(result.video_path.exists())
            self.assertTrue(result.subtitle_path.exists())
            self.assertTrue(result.manifest_path.exists())
            self.assertFalse(self.documents.get(doc.document_id).history.dirty)

    def test_manifest_records_output_profile_without_private_source_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "private-source.mp4"
            source.write_bytes(b"source")
            output = root / "clip.mp4"
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )

            result = save_document(
                doc.document_id, source, output, True,
                documents=self.documents,
                cutter=lambda _source, _ranges, target, **_kwargs: Path(target).write_bytes(b"video"),
                probe=lambda _path: ProbedArtifact(7_000, 34),
                output_profile=OutputProfile.portrait(
                    720, 1280, layout="blur",
                    caption=CaptionProfile(preset="large", position="top"),
                ),
            )
            raw = result.manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(raw)
            self.assertEqual(manifest["output_profile"]["canvas_mode"], "portrait_blur")
            self.assertEqual(manifest["output_profile"]["caption"]["preset"], "large")
            self.assertNotIn(str(source), raw)

    def test_audio_finishing_is_verified_and_clipping_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            output = root / "normalized.mp4"
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )
            profile = OutputProfile.source(
                caption=CaptionProfile(enabled=False),
                audio=AudioProfile(
                    normalize_source=True, normalization_applied=True,
                ),
            )
            result = save_document(
                doc.document_id, source, output, True,
                documents=self.documents,
                cutter=lambda _source, _ranges, target, **_kwargs: Path(target).write_bytes(b"video"),
                probe=lambda _path: ProbedArtifact(7_000, 34),
                audio_probe=lambda _path: ProbedAudioArtifact(True, -0.05),
                output_profile=profile,
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(manifest["audio_verification"]["present"])
            self.assertTrue(manifest["audio_verification"]["clipping_detected"])
            self.assertIn("AUDIO_CLIPPING_DETECTED", manifest["warnings"])

    def test_missing_processed_audio_rolls_back_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            output = root / "normalized.mp4"
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )
            with self.assertRaisesRegex(SaveError, "AUDIO_STREAM_MISSING"):
                save_document(
                    doc.document_id, source, output, True,
                    documents=self.documents,
                    cutter=lambda _source, _ranges, target, **_kwargs: Path(target).write_bytes(b"video"),
                    probe=lambda _path: ProbedArtifact(7_000, 34),
                    audio_probe=lambda _path: ProbedAudioArtifact(False, None),
                    output_profile=OutputProfile.source(
                        caption=CaptionProfile(enabled=False),
                        audio=AudioProfile(
                            normalize_source=True, normalization_applied=True,
                        ),
                    ),
                )
            self.assertFalse(output.exists())

    def test_export_job_tracks_save_stages_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            output = root / "clip.mp4"
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )
            class RecordingJobs(ExportJobRegistry):
                def __init__(self):
                    super().__init__()
                    self.stages = []

                def transition(self, job_id, stage, **kwargs):
                    self.stages.append(ExportStage(stage))
                    return super().transition(job_id, stage, **kwargs)

            jobs = RecordingJobs()
            job = jobs.create(job_id="export_save_success")

            result = save_document(
                doc.document_id, source, output, True,
                documents=self.documents,
                cutter=lambda _source, _ranges, target, **_kwargs: Path(target).write_bytes(b"video"),
                probe=lambda _path: ProbedArtifact(7_000, 34),
                export_job_id=job.job_id,
                export_jobs=jobs,
            )

            self.assertTrue(result.video_path.exists())
            state = jobs.get(job.job_id)
            self.assertIsNotNone(state)
            self.assertEqual(state.stage, ExportStage.COMPLETED)
            self.assertEqual(state.progress, 100)
            self.assertEqual(jobs.stages, [
                ExportStage.VALIDATING,
                ExportStage.JOINING,
                ExportStage.PROBING,
                ExportStage.PUBLISHING,
            ])

    def test_cancelled_export_job_is_acknowledged_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            output = root / "clip.mp4"
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )
            jobs = ExportJobRegistry()
            job = jobs.create(job_id="export_save_cancel")
            jobs.request_cancel(job.job_id)

            with self.assertRaisesRegex(SaveError, "cancelled"):
                save_document(
                    doc.document_id, source, output, True,
                    documents=self.documents,
                    cutter=lambda *_args, **_kwargs: self.fail("cutter must not run"),
                    probe=lambda _path: ProbedArtifact(7_000, 34),
                    export_job_id=job.job_id,
                    export_jobs=jobs,
                )

            state = jobs.get(job.job_id)
            self.assertEqual(state.stage, ExportStage.CANCELLED)
            self.assertFalse(output.exists())

    def test_variant_export_joins_snapshot_once_and_publishes_each_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )
            join_calls = []

            def join_once(_source, ranges, target, **_kwargs):
                join_calls.append(tuple(tuple(item) for item in ranges))
                Path(target).write_bytes(b"joined")

            variants = [
                ExportVariantRequest(
                    root / "source-profile.mp4",
                    OutputProfile.source(caption=CaptionProfile(enabled=False)),
                ),
                ExportVariantRequest(
                    root / "portrait-profile.mp4",
                    OutputProfile.portrait(
                        720, 1280,
                        caption=CaptionProfile(enabled=False),
                    ),
                    postprocessor=lambda joined, output, _duration: Path(output).write_bytes(
                        Path(joined).read_bytes() + b"-portrait"
                    ),
                ),
            ]

            result = save_document_variants(
                doc.document_id,
                source,
                variants,
                documents=self.documents,
                cutter=join_once,
                probe=lambda _path: ProbedArtifact(7_000, 34),
            )

            self.assertEqual(len(join_calls), 1)
            self.assertEqual(len(result.results), 2)
            self.assertEqual(result.failures, ())
            self.assertTrue((root / "source-profile.mp4").is_file())
            self.assertTrue((root / "portrait-profile.mp4").is_file())
            self.assertFalse(list(root.glob(".variant-export-job-*")))

    def test_variant_export_keeps_success_when_another_output_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            conflict = root / "existing.mp4"
            conflict.write_bytes(b"existing")
            successful = root / "successful.mp4"
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )
            result = save_document_variants(
                doc.document_id,
                source,
                [
                    ExportVariantRequest(conflict, OutputProfile.source()),
                    ExportVariantRequest(successful, OutputProfile.source()),
                ],
                documents=self.documents,
                cutter=lambda _source, _ranges, target, **_kwargs: Path(target).write_bytes(b"joined"),
                probe=lambda _path: ProbedArtifact(7_000, 34),
            )
            self.assertEqual(len(result.results), 1)
            self.assertEqual(len(result.failures), 1)
            self.assertEqual(result.failures[0].output_name, "existing.mp4")
            self.assertEqual(conflict.read_bytes(), b"existing")
            self.assertTrue(successful.exists())

    def test_cancel_before_cut_leaves_no_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            output = root / "clip.mp4"
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(SaveError):
                save_document(
                    doc.document_id, source, output, True,
                    cancel_event=cancel, documents=self.documents,
                    cutter=lambda *_args, **_kwargs: None,
                )
            self.assertFalse(output.exists())

    def test_changed_source_is_rejected_before_cutter_or_save_ticket(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source-v1")
            expected = private_source_fingerprint(source)
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=expected,
            )
            source.write_bytes(b"source-v2")
            cutter_called = False

            def cutter(*_args, **_kwargs):
                nonlocal cutter_called
                cutter_called = True

            with self.assertRaisesRegex(SaveError, "SOURCE_CHANGED"):
                save_document(
                    doc.document_id, source, root / "clip.mp4", True,
                    documents=self.documents, cutter=cutter,
                )
            self.assertFalse(cutter_called)
            self.assertEqual(self.documents.get(doc.document_id).latest_save_sequence, 0)

    def test_source_is_revalidated_immediately_before_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source-v1")
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )
            output = root / "clip.mp4"

            def cutter(_source, _ranges, target, **_kwargs):
                Path(target).write_bytes(b"video")

            def probe(_path):
                source.write_bytes(b"source-v2")
                return ProbedArtifact(7_000, 34)

            with self.assertRaisesRegex(SaveError, "SOURCE_CHANGED"):
                save_document(
                    doc.document_id, source, output, True,
                    documents=self.documents, cutter=cutter, probe=probe,
                )
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".mp4.manifest.json").exists())

    def test_srt_forces_precise_and_is_validated_against_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )
            received_precise = None

            def cutter(_source, _ranges, target, **kwargs):
                nonlocal received_precise
                received_precise = kwargs["precise"]
                Path(target).write_bytes(b"video")

            result = save_document(
                doc.document_id, source, root / "clip.mp4", False,
                subtitle_text="1\n00:00:06,900 --> 00:00:07,020\ntest\n",
                documents=self.documents, cutter=cutter,
                probe=lambda _path: ProbedArtifact(7_000, 34),
            )
            self.assertTrue(received_precise)
            self.assertTrue(result.subtitle_path.exists())

    def test_postprocessor_receives_precisely_joined_result_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )
            received = {}

            def cutter(_source, ranges, target, **kwargs):
                received["ranges"] = ranges
                received["precise"] = kwargs["precise"]
                Path(target).write_bytes(b"joined")

            def postprocessor(joined, output, duration):
                received["joined"] = Path(joined).read_bytes()
                received["duration"] = duration
                Path(output).write_bytes(b"rendered")

            result = save_document(
                doc.document_id, source, root / "clip.mp4", False,
                documents=self.documents, cutter=cutter,
                probe=lambda _path: ProbedArtifact(7_000, 34),
                postprocessor=postprocessor,
            )

            self.assertTrue(received["precise"])
            self.assertEqual(received["ranges"], [[1.0, 4.0], [5.0, 9.0]])
            self.assertEqual(received["joined"], b"joined")
            self.assertEqual(received["duration"], 7.0)
            self.assertEqual(result.video_path.read_bytes(), b"rendered")

    def test_duration_mismatch_stops_before_subtitle_or_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )
            output = root / "clip.mp4"

            def cutter(_source, _ranges, target, **_kwargs):
                Path(target).write_bytes(b"video")

            with self.assertRaisesRegex(SaveError, "ARTIFACT_DURATION_MISMATCH"):
                save_document(
                    doc.document_id, source, output, True,
                    subtitle_text="1\n00:00:00,000 --> 00:00:01,000\ntest\n",
                    documents=self.documents, cutter=cutter,
                    probe=lambda _path: ProbedArtifact(6_000, 34),
                )
            self.assertFalse(output.exists())
            self.assertFalse(output.with_suffix(".srt").exists())
            self.assertFalse(output.with_suffix(".mp4.manifest.json").exists())

    def test_srt_beyond_probed_duration_is_not_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=private_source_fingerprint(source),
            )

            def cutter(_source, _ranges, target, **_kwargs):
                Path(target).write_bytes(b"video")

            with self.assertRaisesRegex(SaveError, "SUBTITLE_INVALID"):
                save_document(
                    doc.document_id, source, root / "clip.mp4", False,
                    subtitle_text="1\n00:00:06,900 --> 00:00:07,100\ntest\n",
                    documents=self.documents, cutter=cutter,
                    probe=lambda _path: ProbedArtifact(7_000, 34),
                )

    def test_manifest_has_measured_duration_but_no_private_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            fingerprint = private_source_fingerprint(source)
            doc = self.documents.open(
                "vid_test", "src_test", self.plan,
                expected_source_fingerprint=fingerprint,
            )

            def cutter(_source, _ranges, target, **_kwargs):
                Path(target).write_bytes(b"video")

            result = save_document(
                doc.document_id, source, root / "clip.mp4", True,
                documents=self.documents, cutter=cutter,
                probe=lambda _path: ProbedArtifact(7_000, 34),
            )
            manifest_text = result.manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            self.assertTrue(manifest["precise"])
            self.assertEqual(manifest["planned_duration_ms"], 7_000)
            self.assertEqual(manifest["probed_duration_ms"], 7_000)
            self.assertNotIn(fingerprint, manifest_text)
            self.assertNotIn(str(source), manifest_text)

    def test_unknown_expected_fingerprint_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            doc = self.documents.open("vid_unknown", "src_unknown", self.plan)
            with self.assertRaisesRegex(SaveError, "SOURCE_IDENTITY_UNKNOWN"):
                save_document(
                    doc.document_id, source, root / "clip.mp4", True,
                    documents=self.documents,
                    source_fingerprint_resolver=lambda _public, _generation: None,
                )

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
