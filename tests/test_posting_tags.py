import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from moment_retrieval import posting_tags, youtube_upload


def _response(payload: dict):
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
    )


class FakeClient:
    def __init__(self, payload):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self.payload = payload

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return _response(self.payload)


ANSWER = {
    "tags": [{"tag": "ゲラサブロウ", "kind": "配信者", "reason": "r"},
             {"tag": "#ポケモン", "kind": "ゲーム", "reason": "r"},
             {"tag": "ポケモン", "kind": "ゲーム", "reason": "重複"},
             {"tag": "リザードン", "kind": "内容", "reason": "r"}],
    "hashtags": ["#ポケモン", "リザードン", "#ゲラサブロウ", "#切り抜き"],
    "game": "ポケモン",
    "game_related": True,
}


class PostingTagsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clip = self.root / "clip_short.mp4"
        self.clip.write_bytes(b"")
        self.clip.with_suffix(".metadata.json").write_text(json.dumps({
            "title": "隠し技でリザードン覚醒", "description": "要約です。", "tags": ["既存"],
        }, ensure_ascii=False), encoding="utf-8")
        patcher = mock.patch.object(posting_tags.config, "DATA_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _enable(self, **extra):
        (self.root / "posting_tags.json").write_text(json.dumps({"enabled": True, **extra}), encoding="utf-8")

    def test_off_by_default_leaves_sidecar_alone(self):
        client = FakeClient(ANSWER)
        self.assertIsNone(posting_tags.apply_to_sidecar(self.clip, client=client))
        self.assertEqual(client.calls, [])
        self.assertEqual(youtube_upload.metadata_for(self.clip).tags, ("既存",))

    def test_writes_tags_and_hashtags_once(self):
        self._enable(streamer_name="ゲラサブロウ")
        client = FakeClient(ANSWER)
        result = posting_tags.apply_to_sidecar(self.clip, client=client)
        self.assertEqual(result["tags"], ["ゲラサブロウ", "ポケモン", "リザードン"])
        self.assertEqual(result["hashtags"][:3], ["#ポケモン", "#リザードン", "#shorts"])
        metadata = youtube_upload.metadata_for(self.clip)
        self.assertEqual(metadata.tags, ("ゲラサブロウ", "ポケモン", "リザードン", "既存"))
        self.assertTrue(metadata.description.startswith("要約です。\n\n#ポケモン #リザードン #shorts"))
        self.assertIn("配信者名: ゲラサブロウ", client.calls[0]["messages"][0]["content"])
        # A retried upload does not ask again.
        self.assertIsNone(posting_tags.apply_to_sidecar(self.clip, client=client))
        self.assertEqual(len(client.calls), 1)

    def test_shorts_kept_within_first_three(self):
        cleaned = posting_tags.clean({"tags": [], "hashtags": ["#a", "#b", "#c", "#d", "#shorts"], "game": ""})
        self.assertEqual(cleaned["hashtags"][:3], ["#a", "#b", "#shorts"])
        self.assertLessEqual(len(cleaned["hashtags"]), 5)

    def test_failure_leaves_sidecar_unchanged(self):
        self._enable()
        before = self.clip.with_suffix(".metadata.json").read_text(encoding="utf-8")
        broken = FakeClient(ANSWER)
        broken.messages = SimpleNamespace(create=mock.Mock(side_effect=RuntimeError("boom")))
        with self.assertRaises(RuntimeError):
            posting_tags.apply_to_sidecar(self.clip, client=broken)
        self.assertEqual(self.clip.with_suffix(".metadata.json").read_text(encoding="utf-8"), before)


class SpellingAndFilterTest(unittest.TestCase):
    def setUp(self):
        self.aliases = posting_tags.Aliases(json.loads(
            posting_tags.BUNDLED_ALIASES.read_text(encoding="utf-8")))

    def _item(self, tag, kind="内容", public_figure=False, central=False):
        return {"tag": tag, "kind": kind, "public_figure": public_figure, "central": central, "reason": "r"}

    def test_spelling_variants_become_one(self):
        answer = {"tags": [self._item("ゲラサブロウ 切り抜き"), self._item("ゲラサブロウ切り抜き"),
                           self._item("ｽﾏﾌﾞﾗSP", "ゲーム"), self._item("大乱闘スマッシュブラザーズ SPECIAL", "ゲーム"),
                           self._item("マリカ8DX", "ゲーム")],
                  "hashtags": ["#スマブラSP", "#マリオカート8DX", "#マリカ8DX"], "game": "スマブラSP", "game_related": True}
        result = posting_tags.clean(answer, self.aliases)
        self.assertEqual(result["tags"], ["ゲラサブロウ切り抜き", "スマブラSP", "スマブラ", "マリオカート8DX"])
        self.assertEqual(result["hashtags"], ["#スマブラ", "#マリカ", "#shorts"])
        self.assertEqual(result["game"], "スマブラ")

    def test_talk_clip_keeps_one_game_tag_last_and_no_game_hashtag(self):
        answer = {"tags": [self._item("スマブラSP", "ゲーム"), self._item("スマブラ", "ゲーム"),
                           self._item("有馬記念"), self._item("競馬")],
                  "hashtags": ["#有馬記念", "#スマブラ", "#ゲラサブロウ"], "game": "スマブラSP", "game_related": False}
        result = posting_tags.clean(answer, self.aliases)
        self.assertEqual(result["tags"], ["有馬記念", "競馬", "スマブラSP"])
        self.assertEqual(result["hashtags"], ["#有馬記念", "#ゲラサブロウ", "#shorts"])

    def test_brands_and_private_people_are_dropped(self):
        answer = {"tags": [self._item("アコム", "内容"), self._item("ユニクロ", "企業・ブランド"),
                           self._item("Twitch", "企業・ブランド"), self._item("宮田くん", "人名"),
                           self._item("堀大輔", "人名", public_figure=True, central=True), self._item("睡眠")],
                  "hashtags": ["#堀大輔", "#アコム", "#睡眠", "#宮田くん"], "game": "", "game_related": False}
        result = posting_tags.clean(answer, self.aliases)
        self.assertEqual(result["tags"], ["Twitch", "堀大輔", "睡眠"])
        self.assertEqual(result["hashtags"], ["#睡眠", "#shorts"])
        self.assertEqual(set(result["dropped"]) >= {"アコム", "ユニクロ", "宮田くん"}, True)

    def test_edited_alias_file_wins(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(posting_tags.config, "DATA_DIR", Path(tmp)):
            (Path(tmp) / "tag_aliases.json").write_text(json.dumps({"exclude": ["競馬"]}), encoding="utf-8")
            aliases = posting_tags.load_aliases()
        result = posting_tags.clean({"tags": [self._item("競馬場"), self._item("有馬記念")],
                                     "hashtags": ["#競馬"], "game": "", "game_related": False}, aliases)
        self.assertEqual(result["tags"], ["有馬記念"])
        self.assertEqual(result["hashtags"], ["#shorts"])


if __name__ == "__main__":
    unittest.main()
