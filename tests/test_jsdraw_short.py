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
