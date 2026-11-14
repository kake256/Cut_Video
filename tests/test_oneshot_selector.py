import json
from types import SimpleNamespace

import pytest

from moment_retrieval import oneshot_selector as oneshot


def _segments(count=60, step=5.0):
    """Back-to-back 5 s segments: id i covers [i*5, i*5+5)."""
    return [{"segment_id": i, "start_sec": i * step, "end_sec": i * step + step, "text": f"発話{i}"}
            for i in range(count)]


def test_clock_round_trip():
    assert oneshot.clock(3725.9) == "1:02:05"
    assert oneshot.parse_clock("1:02:05") == 3725
    assert oneshot.parse_clock("02:05") == 125
    assert oneshot.parse_clock(12.5) == 12.5
    assert oneshot.parse_clock("abc") is None


def test_transcript_blocks_merge_short_segments_to_about_ten_seconds():
    text = oneshot.transcript_text(_segments(6))
    lines = text.splitlines()
    assert lines[0] == "[0:00:00] 発話0 発話1"
    assert lines[1] == "[0:00:10] 発話2 発話3"
    assert lines[-1] == "[0:00:31] （配信の終わり）"


def test_transcript_blocks_keep_long_segments_alone_and_skip_empty_text():
    segments = [
        {"segment_id": 1, "start_sec": 0.0, "end_sec": 30.0, "text": "長い  発話"},
        {"segment_id": 2, "start_sec": 30.0, "end_sec": 31.0, "text": ""},
        {"segment_id": 3, "start_sec": 31.0, "end_sec": 33.0, "text": "次"},
    ]
    blocks = oneshot.transcript_blocks(segments)
    assert [b["text"] for b in blocks] == ["長い 発話", "次"]
    assert blocks[1]["start_sec"] == 30.0


def test_check_accepts_line_times_and_snaps_to_segments():
    kept, rejected = oneshot.check_proposals(
        _segments(), [{"start": "0:00:10", "end": "0:00:50", "title": "場面", "reason": "面白い"}])
    assert rejected == []
    assert (kept[0]["start_segment_id"], kept[0]["end_segment_id"]) == (2, 9)
    assert (kept[0]["start_sec"], kept[0]["end_sec"]) == (10.0, 50.0)


def test_check_expands_short_clip_to_minimum():
    kept, _ = oneshot.check_proposals(
        _segments(), [{"start": "0:01:00", "end": "0:01:10", "title": "短い", "reason": "r"}], min_sec=20)
    assert kept[0]["end_sec"] - kept[0]["start_sec"] >= 20
    assert kept[0]["boundary_expanded"]


def test_check_rejects_too_long_reversed_and_unreadable():
    items = [
        {"start": "0:00:00", "end": "0:04:00", "title": "長い", "reason": "r"},
        {"start": "0:01:00", "end": "0:00:30", "title": "逆", "reason": "r"},
        {"start": "そのあたり", "end": "0:00:30", "title": "不明", "reason": "r"},
        {"start": "0:00:00", "end": "0:00:30", "title": "", "reason": "r"},
    ]
    kept, rejected = oneshot.check_proposals(_segments(), items, max_sec=180)
    assert kept == []
    reasons = [r["reason"] for r in rejected]
    assert reasons[0].startswith("長すぎます")
    assert reasons[1] == "終わりが始まりより前です"
    assert reasons[2].startswith("時刻を読めません")
    assert reasons[3] == "タイトルか理由が空です"


def test_check_rejects_used_ranges_and_mutual_overlap_and_extra_count():
    used = [{"start_sec": 100.0, "end_sec": 150.0, "title": "投稿済み", "origin": "upload"}]
    items = [
        {"start": "0:00:00", "end": "0:00:40", "title": "A", "reason": "r"},
        {"start": "0:00:10", "end": "0:00:45", "title": "Aと重なる", "reason": "r"},
        {"start": "0:01:40", "end": "0:02:20", "title": "投稿済みと重なる", "reason": "r"},
        {"start": "0:03:00", "end": "0:03:40", "title": "B", "reason": "r"},
        {"start": "0:04:00", "end": "0:04:40", "title": "C", "reason": "r"},
    ]
    kept, rejected = oneshot.check_proposals(_segments(), items, used=used, clip_count=2)
    assert [c["title"] for c in kept] == ["A", "B"]
    by_title = {r["title"]: r["reason"] for r in rejected}
    assert by_title["Aと重なる"] == "ほかの候補と重なっています"
    assert "投稿済みの範囲" in by_title["投稿済みと重なる"]
    assert by_title["C"].startswith("件数が多すぎます")


def test_prompt_lists_used_ranges_focus_and_hints():
    text = oneshot.user_prompt("[0:00:00] x", clip_count=3, min_sec=20, max_sec=180,
                               used=[{"start_sec": 60, "end_sec": 120, "title": "済"}],
                               focus="キレる場面", hints="盛り上がり: 0:10:00\n")
    assert "0:01:00〜0:02:00「済」" in text
    assert "キレる場面" in text and "最大3件" in text
    assert "盛り上がり: 0:10:00" in text


def test_cost_uses_sonnet_prices():
    assert oneshot.cost_usd({"input_tokens": 1_000_000, "output_tokens": 100_000}) == 3.0


class _FakeClient:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = json.dumps({"clips": self.answers.pop(0)}, ensure_ascii=False)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0),
        )


def test_select_retries_once_with_reasons(monkeypatch):
    monkeypatch.setattr("moment_retrieval.used_ranges.for_video", lambda _vid: [])
    good = {"start": "0:00:00", "end": "0:00:40", "title": "A", "reason": "r"}
    client = _FakeClient([
        [good, {"start": "0:00:00", "end": "0:04:00", "title": "長すぎ", "reason": "r"}],
        [good, {"start": "0:02:00", "end": "0:02:40", "title": "B", "reason": "r"}],
    ])
    result = oneshot.select_clips("vid", clip_count=2, hints="", client=client, segments=_segments())
    assert result["retried"] is True
    assert [c["title"] for c in result["clips"]] == ["A", "B"]
    assert len(client.calls) == 2
    feedback = client.calls[1]["messages"][-1]["content"]
    assert "長すぎ" in feedback and "長すぎます" in feedback
    assert client.calls[0]["output_config"]["effort"] == "medium"
    assert client.calls[0]["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in client.calls[0]
    assert result["usage"]["input_tokens"] == 2000
    assert result["cost_usd"] == oneshot.cost_usd({"input_tokens": 2000, "output_tokens": 400})


def test_select_does_not_retry_when_all_pass(monkeypatch):
    monkeypatch.setattr("moment_retrieval.used_ranges.for_video", lambda _vid: [])
    client = _FakeClient([[{"start": "0:00:00", "end": "0:00:40", "title": "A", "reason": "r"}]])
    result = oneshot.select_clips("vid", clip_count=1, hints="", client=client, segments=_segments())
    assert result["retried"] is False and len(client.calls) == 1


def test_select_keeps_first_answer_when_retry_has_nothing(monkeypatch):
    monkeypatch.setattr("moment_retrieval.used_ranges.for_video", lambda _vid: [])
    client = _FakeClient([
        [{"start": "0:00:00", "end": "0:00:40", "title": "A", "reason": "r"}],
        [{"start": "0:00:00", "end": "0:09:00", "title": "だめ", "reason": "r"}],
    ])
    result = oneshot.select_clips("vid", clip_count=2, hints="", client=client, segments=_segments())
    assert [c["title"] for c in result["clips"]] == ["A"]


def test_refusal_is_an_error():
    with pytest.raises(oneshot.OneshotSelectionError):
        oneshot._response_json(SimpleNamespace(stop_reason="refusal", content=[]))
