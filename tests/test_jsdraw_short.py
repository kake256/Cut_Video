"""JS描画ショート：APIを呼ばずに確かめられる部分（差分の適用、無音への寄せ、時刻の読み取り）。"""
import json

from moment_retrieval import jsdraw_short as js


def test_apply_edits_replaces_only_unique_matches():
    html = "a = 1; b = 2; b = 2;"
    reply = ("<<<<<<< FIND\na = 1\n=======\na = 9\n>>>>>>> END\n"
             "<<<<<<< FIND\nb = 2\n=======\nb = 3\n>>>>>>> END\n")
    out, ok, ng = js._apply_edits(html, reply)
    assert out == "a = 9; b = 2; b = 2;"
    assert (ok, ng) == (1, 1)


def test_quiet_moves_cut_to_nearest_silence(tmp_path):
    env = [60.0] * 300
    for i in range(140, 150):
        env[i] = 0.0  # 1.40〜1.50秒が無音
    (tmp_path / "env.json").write_text(json.dumps(env))
    scene = js._Scene.__new__(js._Scene)
    scene.meta = {"off": 100.0}
    assert abs(scene._quiet(env, 101.2) - 101.41) < 0.05  # 0.6秒以内の無音へ
    assert abs(scene._quiet(env, 102.5) - 102.5) < 0.05   # 遠ければ動かさない（どこも同じ音量）


def test_models_offer_fable_default_and_sonnet():
    assert js.DEFAULT_MODEL == "fable"
    assert list(js.MODELS) == ["fable", "opus", "sonnet"]
    assert js.MODELS["opus"] == ("claude-opus-5-5", 4.0, 20.0)


def test_default_length_keeps_the_one_minute_rules():
    length = js.Length()
    assert (length.video_min, length.video_max, length.segs_min, length.segs_max) == (50.0, 58.0, 48.5, 56.5)
    assert (length.scenes, length.lines, length.max_segs) == ((5, 7), 600, 5)
    spec = js._spec("題", "話", 100, 200)
    assert "50〜58秒" in spec and "5〜7つ" in spec and "600行以内" in spec
    assert "合計50〜56秒（最後に1.5秒の余韻が付き、動画は58秒以内）、区間は3〜5個まで" in js._rules(100, 200)
    assert "55秒前後" in js.length_note(40.0)


def test_longest_of_the_job_is_capped_at_ninety_seconds():
    assert js.video_max_for(180) == 90.0
    assert js.video_max_for(75) == 75.0
    assert js.video_max_for(30) == 58.0  # never shorter than the old one-minute Short
    assert js.video_max_for(None) == 58.0


def test_longer_short_scales_the_prompt_rules():
    length = js.Length(90)
    assert (length.video_max, length.segs_max, length.scenes, length.lines, length.max_segs) == \
        (90.0, 88.5, (5, 11), 900, 8)
    spec = js._spec("題", "話", 100, 250, length)
    assert "50〜90秒" in spec and "5〜11つ" in spec and "900行以内" in spec
    assert "動画は90秒以内" in spec and "区間は3〜8個まで" in spec and "無理に延ばさない" in spec
    assert js.length_note(70.0, length) == ""
    assert "87秒以内" in js.length_note(95.0, length)
