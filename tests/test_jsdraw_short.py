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
