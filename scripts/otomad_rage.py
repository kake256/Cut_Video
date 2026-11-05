"""キレ集 音MAD: the streamer's angriest lines from his latest streams, as a short looping Short.

Lines are found by angry words in the word-level transcript, ranked by how loud they are against
their surroundings, and re-listened so each sample is that line and little else. The camera moves
around between scenes and streams, so the streamer is located in each shot by BiRefNet matting of
the whole frame (largest person-shaped blob) and cut out in his own shape: the scene on top, him
shaking with rage below.

    venv\\Scripts\\python.exe scripts\\otomad_rage.py --latest 5 --out exports\\otomad\\rage.mp4
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import otomad_dance as dance  # noqa: E402  (sets up the GPU onnxruntime first)
import otomad_loop as loop  # noqa: E402
import otomad_prototype as om  # noqa: E402

RAGE = ["おい", "バカ", "ゴミ", "クソ", "くそ", "は?", "はあ?", "ざけんな", "ふざけんな", "ふざけるな", "キモい", "最悪",
        "やめろ", "イライラ", "ありえない", "ありえねえ", "もういい", "カス", "許さない", "なんでだよ", "知らねえ",
        "うるさい", "うるせえ", "うざ", "雑魚", "何してんの", "ちげえよ", "違うって", "黙れ"]
# eight arrangement slots plus four extra lines for the chaos bars
KEYS = list(om.VOICES) + ["A", "B", "C", "D"]
# threats read badly out of context in a public Short, so they are left out unless asked for
STRONG = ["死ね", "殺す"]


def latest_videos(n: int) -> list[str]:
    """The n newest streams by the date in their file name."""
    rows = []
    for vid, path in om._conn().execute("select video_id, path from videos where asr_complete=1"):
        m = re.search(r"(20\d{6})_(\d{6})_", Path(path).name)
        if m and Path(path).exists():
            rows.append((m.group(1) + m.group(2), vid))
    return [vid for _, vid in sorted(rows, reverse=True)[:n]]


# where a streamer's camera usually sits: bottom-left, bottom-right (the middle is where game
# characters stand, and matting picked a PUBG backpack there)
CAMERA_REGIONS = [(0, 540, 760, 540), (1160, 540, 760, 540)]


def person_box(src: Path, t: float, pad: float = 0.25) -> tuple[int, int, int, int] | None:
    """Bounding box (x, y, w, h) of the streamer at t, or None when he is not on screen.

    Matting the whole frame picks whatever is most salient (a game character, a browser window),
    so each usual camera corner is matted on its own and only a blob standing on the bottom edge
    of the frame (or just above a UI bar), the way a camera person sits, counts."""
    from scipy import ndimage
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True,
    ).stdout
    frame = np.frombuffer(raw, np.uint8).reshape(1080, 1920, 3)
    best = None
    for rx, ry, rw, rh in CAMERA_REGIONS:
        alpha = dance.matte(np.ascontiguousarray(frame[ry:ry + rh, rx:rx + rw])) > 128
        labels, count = ndimage.label(alpha)
        for i, (ys, xs) in enumerate(ndimage.find_objects(labels)):
            area = int((labels[ys, xs] == i + 1).sum())
            w, h = xs.stop - xs.start, ys.stop - ys.start
            # a camera person sits in the corner itself, against the frame's side
            near_side = rx + xs.start <= 300 or rx + xs.stop >= 1920 - 300
            if not near_side or ys.stop < rh - 40 or area < 8000 or h < 150 or w > 0.9 * rw or h < 0.5 * w:
                continue
            if best is None or area > best[0]:
                best = (area, rx + xs.start, ry + ys.start, w, h)
        if best is not None:
            break        # corners first: the middle is where game characters stand
    if best is None:
        return None
    _, x0, y0, w, h = best
    x1, y1 = min(1920, int(x0 + w * (1 + pad))), 1080
    x0, y0 = max(0, int(x0 - w * pad)), max(0, int(y0 - h * pad))
    return x0, y0, (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2


def pick(videos: list[str], phrases: list[str], n: int = 8, per_video: int = 3) -> list[om.Sample]:
    sys.path.insert(0, str(om.ROOT))
    from moment_retrieval import relisten
    model = relisten._get_model()
    cands = []
    for vid in videos:
        for phrase, items in om.mine(vid, phrases, per_phrase=6).items():
            cands += [(c["score"], vid, phrase, c) for c in items]
    cands.sort(key=lambda c: -c[0])
    picked: list[om.Sample] = []
    used_per_video: dict[str, int] = {}
    for score, vid, phrase, c in cands:
        if used_per_video.get(vid, 0) >= per_video:
            continue
        src = om.video_path(vid)
        smp = om.load_sample(src, "?", phrase, c["start"], c["dur"])
        heard = om._clean(om._heard(model, smp.audio))
        ok = (om._clean(phrase) in heard or any(om._clean(p) in heard for p in phrases)) and len(heard) <= 8 \
            and not any(h in heard for h in ("ご視聴", "ありがとう", "字幕"))
        print(f"  {vid[-6:]} @{c['start']:8.1f}s {phrase:<6} heard={heard!r} score={score} {'OK' if ok else '-'}")
        if not ok or heard in {s.label for s in picked}:
            continue
        smp.label = heard
        smp.src = src
        smp.video_id = vid
        smp.window = (c["start"], c["dur"])
        picked.append(smp)
        used_per_video[vid] = used_per_video.get(vid, 0) + 1
        if len(picked) == n:
            break
    if len(picked) < n:
        raise SystemExit(f"only {len(picked)} usable lines")
    return picked


def shot_filter(n: int, g0: float, label_file: Path, label: str, level: int) -> str:
    T = f"({g0:.3f}+t)"
    beat = loop.BEAT
    pulse = f"abs(sin(PI*{T}/{beat:.3f}))"
    shake = 6 + 8 * level
    parts = [
        f"[0:v]fps={om.FPS},split=2[a][b]",
        # backdrop: the scene itself, blurred and pushed to angry red
        f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,scale=1080:1920,"
        f"colorchannelmixer=rr=0.9:rg=0.3:rb=0.2:gr=0.1:gg=0.15:gb=0.05:br=0.1:bg=0.1:bb=0.15,"
        f"hue=h='{18 * level}*sin(PI*{T}/{2 * beat:.3f})'[bg]",
        f"[b]scale=1080:608[scene]",
        f"[bg][scene]overlay=0:180[s1]",
        f"[1:v]fps={om.FPS},format=rgba,scale=1000:-2[me]",
        # him, shaking: hard on each new line, then a steady tremble on the beat
        f"[s1][me]overlay=x='(W-w)/2+{shake}*sin(n*2.7)*(lt(n,5)+0.3)':"
        f"y='H-h+20-30*{pulse}':shortest=1[s2]",
    ]
    fs = 210 if len(label) <= 3 else 160 if len(label) <= 5 else 120
    parts.append(
        f"[s2]drawtext=fontfile='{om.FONT}':textfile='{om._ff_path(label_file)}':fontsize={fs}:"
        f"fontcolor=white:borderw=18:bordercolor=#d40000:x=(w-tw)/2+{4 + 2 * level}*sin(n*3.1):"
        f"y=900-th/2-20*{pulse}[v]"
    )
    return ";".join(parts)


def render(videos: list[str], out: Path, cache: Path | None, phrases: list[str]) -> Path:
    from moment_retrieval.video_encoder import h264_args
    keys = KEYS
    if cache and cache.exists():
        samples = {}
        for k, p in json.loads(cache.read_text(encoding="utf-8")).items():
            src = om.video_path(p["video_id"])
            smp = om.load_sample(src, k, p["label"], *p["window"])
            smp.src, smp.video_id, smp.window = src, p["video_id"], p["window"]
            samples[k] = smp
    else:
        samples = {}
        lines = pick(videos, phrases, n=len(keys))
        # the gag (slot W) is the snappiest line: 「は?」 if it was found
        lines.sort(key=lambda s: s.label not in ("は?", "はあ?"))
        for k, smp in zip(keys, lines):
            smp.key = k
            samples[k] = smp
        if cache:
            cache.write_text(json.dumps({k: {"label": s.label, "video_id": s.video_id, "window": s.window}
                                         for k, s in samples.items()}, ensure_ascii=False, indent=1), encoding="utf-8")
    for k, s in samples.items():
        print(f"{k}: {s.label} ({s.video_id[-6:]} @{s.src_start:.1f}s)")
    events = loop.arrange("W", keys=list(samples))
    # voice well above the beat, and chops left to ring: this one is about his voice
    mix, cues = loop.render_audio(samples, events, voice_gain=1.8, beat_gain=0.6, min_chop=0.3)
    encoder = h264_args(crf=18, preset="veryfast")
    boxes: dict[str, tuple] = {}
    out.parent.mkdir(parents=True, exist_ok=True)
    shots = om.group_shots(cues)
    with tempfile.TemporaryDirectory(prefix="otomad_rage_") as tmp:
        work = Path(tmp)
        wav = work / "mix.wav"
        om.write_wav(wav, mix)
        listing = []
        g = 0
        for n, (steps, smp, e) in enumerate(shots):
            if smp.key not in boxes:
                # not found (scene change, popup over him): the usual bottom-right camera spot
                boxes[smp.key] = person_box(smp.src, smp.src_start + 0.1) or (1560, 800, 360, 280)
                print(f"{smp.label}: person at {boxes[smp.key]}")
            cut_dir = work / f"cut{n:03d}"
            dance.cutout_frames(smp.src, smp.src_start, steps * om.STEP, boxes[smp.key], cut_dir)
            label_file = work / f"t{n:03d}.txt"
            label_file.write_text(smp.label, encoding="utf-8")
            seg = work / f"s{n:03d}.ts"
            cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{smp.src_start:.3f}", "-i", str(smp.src),
                   "-framerate", str(dance.MATTE_FPS), "-i", str(cut_dir / "%03d.png"),
                   "-filter_complex", shot_filter(n, g * om.STEP, label_file, smp.label, loop.chaos_level(g)),
                   "-map", "[v]", "-frames:v", str(steps * 3), "-an", *encoder, "-r", str(om.FPS), str(seg)]
            for _ in range(3):
                if subprocess.run(cmd).returncode == 0:
                    break
            else:
                raise SystemExit(f"shot {n} failed to render")
            listing.append(f"file '{seg.as_posix()}'")
            g += steps
        lst = work / "list.txt"
        lst.write_text("\n".join(listing), encoding="utf-8")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(wav),
             "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
             "-t", f"{loop.TOTAL * om.STEP:.2f}", "-movflags", "+faststart", str(out)],
            check=True,
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latest", type=int, default=5, help="use the newest N streams")
    parser.add_argument("--strong", action="store_true", help="also allow threats (死ね・殺す)")
    parser.add_argument("--cache", type=Path, default=om.ROOT / "exports" / "otomad" / "rage_picks.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    videos = latest_videos(args.latest)
    print("streams:", videos)
    print(render(videos, args.out, args.cache, RAGE + (STRONG if args.strong else [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
