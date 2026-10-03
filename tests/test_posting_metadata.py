import json
import unittest

from moment_retrieval.posting_metadata import posting_metadata_from_candidate


class PostingMetadataTest(unittest.TestCase):
    def test_candidate_metadata_is_reviewable_and_contains_no_private_fields(self):
        metadata = posting_metadata_from_candidate({
            "title": "見どころ",
            "summary": "要点の説明",
            "tags": ["配信", "要点"],
            "transcript": "private transcript",
            "path": r"F:\private\video.mp4",
        })
        payload = metadata.to_dict()
        raw = json.dumps(payload, ensure_ascii=False)
        self.assertTrue(payload["generated_with_llm"])
        self.assertTrue(payload["requires_review"])
        self.assertNotIn("private transcript", raw)
        self.assertNotIn(r"F:\private", raw)

    def test_user_overrides_are_sanitized_and_deduplicated(self):
        metadata = posting_metadata_from_candidate(
            {"title": "old", "summary": "old", "tags": []},
            title_override="  編集した タイトル  ",
            description_override="説明\n\n\n続き",
            tags_override="Tag, tag、別タグ",
        )
        self.assertEqual(metadata.title, "編集した タイトル")
        self.assertEqual(metadata.description, "説明\n\n続き")
        self.assertEqual(metadata.tags, ("Tag", "別タグ"))


if __name__ == "__main__":
    unittest.main()
