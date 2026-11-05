"""The loop 音MAD with the streamer cut out in his own shape (BiRefNet matting) and "dancing":
lined up five abreast like an idol group on pastel stripes, bouncing in a wave on the beat, then
scattering and spinning over a mirrored race as it gets more chaotic.

Matting uses the GPU when the separate onnxruntime-gpu build is present (~0.25 s per frame,
cut-out at 30 fps); otherwise CPU at ~4 s per frame, cut-out at 10 fps. Install the GPU build
next to the venv, without touching the app's own onnxruntime:

    venv\Scripts\python.exe -m pip install --no-deps --target venv\ortgpu onnxruntime-gpu==1.23.2

    venv\\Scripts\\python.exe scripts\\otomad_dance.py --out exports\\otomad\\dance.mp4
"""
from __future__ import annotations

import argparse
import math
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ORT_GPU = Path(__file__).resolve().parents[1] / "venv" / "ortgpu"
if ORT_GPU.exists():
    # must win over the venv's CPU onnxruntime, so it goes first and before anything imports it;
    # torch is imported first so its CUDA/cuDNN DLLs are the ones onnxruntime-gpu picks up
    import torch  # noqa: F401
    sys.path.insert(0, str(ORT_GPU))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import otomad_hits as hits  # noqa: E402
import otomad_loop as loop  # noqa: E402
import otomad_prototype as om  # noqa: E402

MODEL_REPO = "onnx-community/BiRefNet_lite-ONNX"     # MIT
MK8_BODY = (40, 670, 340, 300)     # camera area in the race layout, below the chat, above the coin HUD
MATTE_FPS = 30 if ORT_GPU.exists() else 10
STRIPES = ["#ffd6e8", "#d6f0ff", "#e4ffd6", "#fff3c4", "#ead6ff"]

_session = None


def _matte_session():
    global _session
    if _session is None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        _session = ort.InferenceSession(hf_hub_download(MODEL_REPO, "onnx/model.onnx"),
                                        providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        print("matting on", _session.get_providers()[0], flush=True)
    return _session


def matte(rgb: np.ndarray) -> np.ndarray:
    """Alpha (uint8, same size) for one RGB frame."""
    from PIL import Image
    sess = _matte_session()
    h, w = rgb.shape[:2]
    x = np.asarray(Image.fromarray(rgb).resize((1024, 1024), Image.BILINEAR), np.float32) / 255
    x = (x - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]
    y = sess.run(None, {sess.get_inputs()[0].name: x.transpose(2, 0, 1)[None].astype(np.float32)})[0][0, 0]
    if y.min() < 0:
        y = 1 / (1 + np.exp(-y))
    y = np.clip((y - 0.3) / 0.4, 0, 1)
    # keep only the biggest blob: the person, not stray bits of chat overlay or HUD
    from scipy import ndimage
    labels, count = ndimage.label(y > 0.5)
    if count > 1:
        sizes = ndimage.sum(np.ones_like(y), labels, range(1, count + 1))
        keep = ndimage.binary_dilation(labels == (1 + int(np.argmax(sizes))), iterations=6)
        y = y * keep
    return np.asarray(Image.fromarray((y * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR))


def cutout_frames(src: Path, start: float, seconds: float, box: tuple[int, int, int, int], out_dir: Path) -> None:
    """RGBA PNG sequence of the streamer alone, MATTE_FPS frames per second."""
    from PIL import Image
    x, y, w, h = box
    n = max(1, math.ceil(seconds * MATTE_FPS) + 1)
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src), "-vf",
         f"fps={MATTE_FPS},crop={w}:{h}:{x}:{y}", "-frames:v", str(n), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, f in enumerate(frames):
        png = out_dir / f"{i:03d}.png"
        if png.exists():           # left by an earlier, interrupted run
            continue
        rgba = np.dstack([f, matte(f)])
        Image.fromarray(rgba, "RGBA").save(png.with_suffix(".tmp.png"))
        png.with_suffix(".tmp.png").replace(png)


def shot_filter(n: int, g0: float, label_file: Path, label: str, level: int) -> str:
    T = f"({g0:.3f}+t)"
    beat = loop.BEAT
    parts = [f"[0:v]fps={om.FPS}[g]", f"[1:v]fps={om.FPS},format=rgba[c]"]
    if level <= 1:
        # idol-stage stripes, colours rotating
        boxes = ",".join(f"drawbox=x={i * 216}:y=0:w=216:h=1920:color={c}:t=fill" for i, c in enumerate(STRIPES))
        parts.append(f"[g]scale=1080:1920,{boxes},hue=h='{T}*40'[bg]")
    else:
        parts.append(
            f"[g]crop=ih*9/16:ih,scale=1080:1920,eq=saturation=1.7,split[l][r];[l]crop=iw/2:ih:0:0[l1];"
            f"[r]crop=iw/2:ih:0:0,hflip[r1];[l1][r1]hstack,hue=h='{T}*{80 * level}'[bg]"
        )
    copies = [1, 5, 5, 7][level]
    parts.append(f"[c]split={copies}" + "".join(f"[s{i}]" for i in range(copies)))
    rng = np.random.default_rng(n)
    layer = "bg"
    for i in range(copies):
        if level == 0:
            size, cx, cy, phase = 3.0, 540, 1180, 0.0
        elif level == 1:
            size, cx, cy, phase = (1.6 if i == 2 else 1.15), 108 + 216 * i, 1250 + (120 if i == 2 else 0), i * 0.5
        else:
            size = float(rng.uniform(1.0, 1.8))
            cx, cy, phase = int(rng.integers(150, 930)), int(rng.integers(650, 1650)), float(rng.uniform(0, 2))
        w = int(340 * size) // 2 * 2
        bounce = f"abs(sin(PI*({T}/{beat:.3f}+{phase:.2f}/2)))"
        flip = ",hflip" if (level >= 1 and i % 2) else ""
        spin = 0.0 if level < 2 else float(rng.choice([-1, 1])) * (1.0 + 0.7 * (level - 2))
        parts.append(
            # (a per-frame squash via scale eval=frame crashed this ffmpeg build intermittently)
            f"[s{i}]scale={w}:{int(300 * size) // 2 * 2}{flip},"
            f"rotate=a='{spin:.2f}*{T}+0.18*sin(PI*({T}/{beat:.3f}+{phase:.2f}))':c=none:"
            f"ow=hypot({w}\\,{int(300 * size)})*1.2:oh=ow[r{i}]"
        )
        parts.append(
            f"[{layer}][r{i}]overlay=x='{cx}-w/2':y='{cy}-h/2-50*{bounce}':shortest=1:eval=frame[o{i}]"
        )
        layer = f"o{i}"
    colors = ["#ff3b6b", "#2b7bff", "#ff8a00", "#9b30ff"]
    fs = 230 if len(label) <= 3 else 170 if len(label) <= 5 else 130
    bounce0 = f"abs(sin(PI*{T}/{beat:.3f}))"
    parts.append(
        f"[{layer}]drawtext=fontfile='{om.FONT}':textfile='{om._ff_path(label_file)}':fontsize={fs}:"
        f"fontcolor={colors[n % 4]}:borderw=16:bordercolor=white:x=(w-tw)/2:y=330-th/2-30*{bounce0}[v]"
    )
    return ";".join(parts)


def render(video_id: str, span, out: Path, cache: Path | None, gag: str, body) -> Path:
    from moment_retrieval.video_encoder import h264_args
    samples = hits.hit_samples(video_id, span, cache)
    events = loop.arrange(gag)
    mix, cues = loop.render_audio(samples, events)
    src = om.video_path(video_id)
    encoder = h264_args(crf=18, preset="veryfast")
    out.parent.mkdir(parents=True, exist_ok=True)
    shots = om.group_shots(cues)
    with tempfile.TemporaryDirectory(prefix="otomad_dance_") as tmp:
        work = Path(tmp)
        wav = work / "mix.wav"
        om.write_wav(wav, mix)
        listing = []
        g = 0
        for n, (steps, smp, e) in enumerate(shots):
            start = smp.video_start or smp.src_start
            cut_dir = work / f"cut{n:03d}"
            cutout_frames(src, start, steps * om.STEP, body, cut_dir)
            print(f"shot {n + 1}/{len(shots)}: {smp.label} ({steps * om.STEP:.1f}s)", flush=True)
            label_file = work / f"t{n:03d}.txt"
            label_file.write_text(smp.label, encoding="utf-8")
            seg = work / f"s{n:03d}.ts"
            cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src),
                   "-framerate", str(MATTE_FPS), "-i", str(cut_dir / "%03d.png"),
                   "-filter_complex", shot_filter(n, g * om.STEP, label_file, smp.label, loop.chaos_level(g)),
                   "-map", "[v]", "-frames:v", str(steps * 3), "-an", *encoder, "-r", str(om.FPS), str(seg)]
            for attempt in range(3):     # this ffmpeg build occasionally crashes on the rotate chain
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
    parser.add_argument("--video", default="vid_09b94439359b444fbe239eaa0313b7e5")
    parser.add_argument("--span", default="3850,10866")
    parser.add_argument("--body", default=",".join(map(str, MK8_BODY)), help="camera area x,y,w,h in the source")
    parser.add_argument("--cache", type=Path, default=om.ROOT / "exports" / "otomad" / "mk8_hits_picks.json")
    parser.add_argument("--gag", default="W")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    span = tuple(float(v) for v in args.span.split(","))
    body = tuple(int(v) for v in args.body.split(","))
    print(render(args.video, span, args.out, args.cache, args.gag, body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
