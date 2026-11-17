"""JS描画ショート：APIを呼ばずに確かめられる部分（差分の適用、無音への寄せ、時刻の読み取り）。"""
import json
import random
import shutil
import subprocess

import pytest

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
    assert "動画は90秒以内" in spec and "区間は3〜8個まで" in spec and "71〜87秒を目安" in spec
    assert js.length_note(70.0, length) == ""
    assert "87秒以内" in js.length_note(95.0, length)


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed")
def test_muxed_audio_passes_upload_timing_check(tmp_path):
    """The joined, loudness-normalised audio must pass the same check the upload runs (av_check)."""
    from moment_retrieval import av_check

    wav = tmp_path / "src.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=70:c=pink:a=0.3:r=48000",
                    "-ac", "1", str(wav)], check=True, timeout=120)
    # About 49 s of speech in uneven pieces, like a real Short.  Without the retiming filter after
    # loudnorm one AAC packet came out 4720 samples long here (the fault that blocked 9 of 10 uploads).
    rng, pieces, t = random.Random(1), [], 0.5
    while t < 58:  # unrounded lengths matter: rounded ones happened not to trip the fault
        length = rng.uniform(1.5, 6)
        pieces.append((t, t + length))
        t += length + rng.uniform(0.3, 1.2)
    pieces = [(a, b) for a, b in pieces if b < 60]
    segs = [(100.0 + a, 100.0 + b) for a, b in pieces]
    # A length that is not a whole number of frames: with -shortest the audio overran the video by 0.13 s.
    tend = 56.43
    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=black:s=64x64:r={js.FPS}",
                    "-frames:v", str(round(tend * js.FPS)), "-c:v", "libx264", "-preset", "ultrafast", str(video)],
                   check=True, timeout=120)
    out = tmp_path / "short.mp4"
    js.mux_audio(video, wav, segs, 100.0, tend, out)
    assert av_check.audio_timing_problems(out) == []
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "csv=p=0",
                            str(out)], capture_output=True, text=True, check=True).stdout.split()
    lengths = dict(line.split(",") for line in probe)
    assert abs(float(lengths["video"]) - float(lengths["audio"])) < 0.03


def test_crop_around_person_puts_person_in_the_face_box():
    # 1080x1920 の縦長の配信の上の方に、幅300・高さ240の人物
    w, h, x, y = js.crop_around_person((400, 300, 300, 240), (1080, 1920))
    assert (w, h) == js.FACE_SIZE  # 拡大率1：FACE と同じ大きさ
    px, py, pw, ph = js.FACE_PERSON_BOX
    assert (x, y) == (400 + 150 - (px + pw // 2), 300 - py)


def test_crop_around_person_stays_inside_the_frame():
    # 全画面カメラ：人物が画面の大半を占める
    w, h, x, y = js.crop_around_person((300, 100, 1500, 980), (1920, 1080))
    assert 0 <= x and x + w <= 1920 and 0 <= y and y + h <= 1080
    assert abs(w / h - 440 / 320) < 0.01
    # 画面の右下の隅にいる人物でも、枠は外へはみ出さない
    w, h, x, y = js.crop_around_person((1800, 1000, 120, 80), (1920, 1080))
    assert x + w <= 1920 and y + h <= 1080


def test_largest_component_ignores_small_specks():
    import numpy as np

    mask = np.zeros((20, 30), bool)
    mask[2, 2] = True
    mask[5:15, 10:25] = True
    assert js._largest_component(mask) == (10, 5, 15, 10, 150)
    assert js._largest_component(np.zeros((4, 4), bool)) is None



def test_fit_stand_in_places_person_like_the_face_camera():
    import numpy as np
    from PIL import Image

    im = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    person = Image.new("RGBA", (400, 600), (200, 150, 120, 255))
    im.paste(person, (500, 120))
    face = js.fit_stand_in(im)
    assert face.size == js.FACE_SIZE
    alpha = np.asarray(face.split()[3]) > 127
    ys, xs = np.nonzero(alpha)
    px, py, pw, ph = js.FACE_PERSON_BOX
    assert abs(ys.min() - py) <= 2  # 頭の上の余白は顔カメラと同じ
    assert px - 2 <= xs.min() and xs.max() <= px + pw + 2  # 人物は枠の左右に収まる


def _fake_stream(tmp_path):
    src = tmp_path / "stream.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=320x180:d=80",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=80", "-shortest", "-c:v", "libx264",
                    "-c:a", "aac", str(src)], check=True)
    return src


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg がない")
def test_prepare_falls_back_to_stand_in_photo_then_to_no_window(tmp_path, monkeypatch):
    from PIL import Image

    src = _fake_stream(tmp_path)
    monkeypatch.setattr(js.config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(js, "_transcript", lambda *a, **k: [[10.0, 12.0, "テスト"]])

    def no_streamer(*_args, **_kwargs):
        raise js.JsDrawError("配信画面に配信者（顔カメラ）が見つかりませんでした。")

    monkeypatch.setattr(js, "_prepare_live_face", no_streamer)
    # 写真が無い：小窓なし
    meta = js.prepare(str(src), 10.0, 70.0, tmp_path / "w1", lambda _m: None)
    assert (meta["face_mode"], meta["nface"]) == ("none", 0)
    assert "配信者" in meta["face_note"]
    # 写真がある：写真1枚を小窓に
    js.stand_in_dir().mkdir(parents=True)
    person = Image.new("RGBA", (300, 400), (0, 0, 0, 0))
    person.paste(Image.new("RGBA", (120, 300), (180, 140, 120, 255)), (90, 60))
    person.save(js.stand_in_dir() / "a.png")
    meta = js.prepare(str(src), 10.0, 70.0, tmp_path / "w2", lambda _m: None)
    assert (meta["face_mode"], meta["nface"]) == ("stand_in", 1)
    assert Image.open(tmp_path / "w2" / "face" / "00001.webp").size == js.FACE_SIZE
    assert json.loads((tmp_path / "w2" / "meta.json").read_text(encoding="utf-8"))["face_mode"] == "stand_in"


def test_rules_tell_the_ai_when_there_is_no_live_face():
    assert "常に null" in js._rules(0, 60, face_mode="none")
    assert "写真" in js._rules(0, 60, face_mode="stand_in")
    assert "口の動きと声が合う" in js._rules(0, 60)
