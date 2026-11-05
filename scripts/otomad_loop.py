"""A short, looping, chaotic 音MAD: 8 bars (12.8 s) that run straight back into their own start.

Built for the "one gag, watched on repeat" format: one word carries the whole thing, the beat
never stops (so the seam is invisible), and the streamer's camera is cut out as a circle sticker
that bounces on every beat and multiplies as it gets more chaotic.

    venv\\Scripts\\python.exe scripts\\otomad_loop.py --out exports\\otomad\\loop.mp4
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import otomad_hits as hits  # noqa: E402
import otomad_prototype as om  # noqa: E402
from otomad_prototype import Event, STEP, SR, add  # noqa: E402

BARS = 8
TOTAL = BARS * 16
BEAT = STEP * 4


def arrange(gag: str = "W", seed: int = 3, keys: list[str] | None = None) -> list[Event]:
    """Gag word up front, call/response, a tuned ladder, two bars of chaos, a roll, and a
    last-beat gag hit that lands the loop back on bar 0."""
    rng = np.random.default_rng(seed)
    others = [k for k in (keys or om.VOICES) if k != gag]   # the chaos bars draw from all of these
    ev = [Event(0, gag)]
    ev += [Event(s, gag, chop=0.08, semis=(s - 8) * 0.75) for s in (8, 10, 12, 13, 14, 15)]
    ev += [Event(16, "Y"), Event(22, gag, chop=0.12), Event(24, "N"), Event(28, gag, chop=0.1, semis=7)]
    ev += [Event(32, "K"), Event(38, "H"), Event(40, gag), Event(44, "O", chop=0.12), Event(46, "O", chop=0.12)]
    ladder = [0, 3, 5, 7, 10, 12, 15, 19]
    ev += [Event(48 + 2 * i, gag, chop=0.14, semis=n) for i, n in enumerate(ladder)]
    for s in range(64, 96, 2):                                     # chaos: anyone, any pitch
        k = gag if s % 8 == 0 else others[rng.integers(len(others))]
        ev.append(Event(s, k, chop=0.0 if s % 8 == 0 else 0.16, semis=float(rng.choice([-7, -5, 0, 0, 5, 7, 12]))))
    ev += [Event(96 + s, gag, chop=0.07, semis=s * 0.8) for s in range(16)]  # roll, rising
    ev += [Event(112, "K"), Event(116, "U"), Event(120, "M", chop=0.15), Event(122, "M", chop=0.15),
           Event(124, gag, semis=5)]
    ev.sort(key=lambda e: e.step)
    return ev


def reverse_some(events: list[Event]) -> set[int]:
    """Indexes of chaos hits played backwards."""
    return {i for i, e in enumerate(events) if 64 <= e.step < 96 and e.step % 6 == 4}


def beat_track() -> np.ndarray:
    rng = np.random.default_rng(11)
    n = int(TOTAL * STEP * SR)
    drums, music = np.zeros(n, np.float32), np.zeros(n, np.float32)
    k, sn = om.kick(), om.snare(rng)
    roots = [57, 53, 48, 55]
    for b in range(BARS):
        t0 = b * 16 * STEP
        chaos = 4 <= b <= 5
        roll = b == 6
        for s in (range(0, 16, 2) if chaos else (0, 4, 8, 12)):
            add(drums, k, t0 + s * STEP, 0.9)
        for s in (4, 12):
            add(drums, sn, t0 + s * STEP, 0.6)
        if roll:
            for s in range(0, 16):
                add(drums, sn, t0 + s * STEP, 0.15 + 0.03 * s)
        for s in range(16):
            add(drums, om.hat(rng), t0 + s * STEP, 0.45 if s % 2 else 0.25)
        root = roots[b % 4] + (1 if chaos and b == 5 else 0)       # a semitone lurch in the chaos
        for s in range(0, 16, 2):
            add(music, om.saw_note(om.midi(root - 12), STEP * 1.6, 1400 if chaos else 700), t0 + s * STEP, 0.33)
        for nn in (0, 3 if root == 57 else 4, 7):
            add(music, om.saw_note(om.midi(root + nn), 16 * STEP, 2200), t0, 0.05)
    pump = np.ones(n, np.float32)
    m = int(SR * 0.16)
    for q in range(TOTAL // 4):
        i = int(q * BEAT * SR)
        j = min(n, i + m)
        pump[i:j] = np.minimum(pump[i:j], (0.35 + 0.65 * np.linspace(0, 1, m))[: j - i])
    return 0.75 * drums + music * pump


def render_audio(samples, events: list[Event], voice_gain: float = 1.1, beat_gain: float = 1.0,
                 min_chop: float = 0.0) -> tuple[np.ndarray, list[tuple]]:
    """min_chop lets short chops ring on (up to the next hit), so more of the voice is heard."""
    n = int(TOTAL * STEP * SR)
    voice = np.zeros(n + SR, np.float32)
    rev = reverse_some(events)
    cues = []
    for idx, e in enumerate(events):
        nxt = events[idx + 1].step if idx + 1 < len(events) else TOTAL
        smp = samples[e.key]
        x = smp.audio[: int(SR * max(e.chop, min_chop))] if e.chop else smp.audio
        if idx in rev:
            x = x[::-1]
        x = om.pitch(x, e.semis)[: int((nxt - e.step) * STEP * SR)].copy()
        f = min(len(x), int(SR * 0.006))
        if f:
            x[-f:] *= np.linspace(1, 0, f)
        add(voice, x, e.step * STEP)
        cues.append((nxt - e.step, smp, e))
    # anything ringing past the end wraps to the start, so the loop point is seamless
    voice[: SR] += voice[n: n + SR]
    mix = beat_gain * beat_track() + voice_gain * voice[:n]
    return np.tanh(mix * 1.2) * 0.89, cues


def chaos_level(step: int) -> int:
    bar = step // 16
    return 2 if 4 <= bar <= 5 else 3 if bar >= 6 else 1 if bar >= 2 else 0


def shot_filter(n: int, g0: float, label_file: Path, label: str, face: tuple[int, int, int, int],
                level: int) -> str:
    """Background: the game, recoloured; stickers: circular camera cut-outs bouncing on the beat."""
    fx, fy, fw, fh = face
    T = f"({g0:.3f}+t)"
    bounce = f"abs(sin(PI*{T}/{BEAT:.3f}))"
    copies = [1, 1, 3, 5][level]
    parts = [f"[0:v]fps={om.FPS},split=2[g][c]"]
    bg = "[g]crop=ih*9/16:ih,scale=1080:1920,eq=saturation=1.6"
    if level >= 2:   # mirror kaleidoscope
        bg += ",split[l][r];[l]crop=iw/2:ih:0:0[l1];[r]crop=iw/2:ih:0:0,hflip[r1];[l1][r1]hstack"
    bg += f",hue=h='{T}*{90 * level}'[bg]"
    parts.append(bg)
    size = 760 if copies == 1 else 520
    parts.append(
        # square around the middle of the camera box, so the circle is not squashed
        f"[c]crop={fh}:{fh}:{fx + (fw - fh) // 2}:{fy},scale={size}:{size},format=rgba,"
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lt(hypot(X-W/2,Y-H/2),W/2-6),255,0)',"
        f"split={copies}" + "".join(f"[s{i}]" for i in range(copies))
    )
    layer = "bg"
    rng = np.random.default_rng(n)
    for i in range(copies):
        cx = 540 if i == 0 else int(rng.integers(150, 930))
        cy = 1050 if i == 0 else int(rng.integers(350, 1650))
        spin = 0.0 if level < 2 else float(rng.choice([-1, 1])) * (0.6 + 0.4 * level)
        amp = 90 if i == 0 else 60
        rot = f"[s{i}]rotate=a='{spin:.2f}*{T}+0.15*sin(PI*{T}/{BEAT:.3f})':c=none:ow=hypot(iw\\,ih):oh=ow[r{i}]"
        parts.append(rot)
        parts.append(
            f"[{layer}][r{i}]overlay=x='{cx}-w/2+{40 * (i % 2)}*sin(2*PI*{T}/{4 * BEAT:.3f})':"
            f"y='{cy}-h/2-{amp}*{bounce}':shortest=1[o{i}]"
        )
        layer = f"o{i}"
    colors = ["#ffe14d", "#ff3b6b", "#3bd1ff", "#7cff6b"]
    fs = 230 if len(label) <= 3 else 170 if len(label) <= 5 else 130
    angle = f"{0.12 * (1 if n % 2 else -1):.2f}"
    txt = (
        f"drawtext=fontfile='{om.FONT}':textfile='{om._ff_path(label_file)}':fontsize={fs}:"
        f"fontcolor={colors[n % 4]}:borderw=16:bordercolor=black:x=(w-tw)/2+{angle}*200:"
        f"y=330-th/2-30*{bounce}"
    )
    echo = ""
    if level >= 2:
        echo = (
            f",drawtext=fontfile='{om.FONT}':textfile='{om._ff_path(label_file)}':fontsize={fs // 2}:"
            f"fontcolor={colors[(n + 2) % 4]}:borderw=8:bordercolor=black:x=(w-tw)/2:y=1700"
        )
    parts.append(f"[{layer}]{txt}{echo}[v]")
    return ";".join(parts)


def render(video_id: str, span, out: Path, face, cache: Path | None, gag: str) -> Path:
    from moment_retrieval.video_encoder import h264_args
    samples = hits.hit_samples(video_id, span, cache)
    for k, smp in samples.items():
        print(f"{k}: {smp.label}")
    events = arrange(gag)
    mix, cues = render_audio(samples, events)
    src = om.video_path(video_id)
    encoder = h264_args(crf=18, preset="veryfast")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="otomad_loop_") as tmp:
        work = Path(tmp)
        wav = work / "mix.wav"
        om.write_wav(wav, mix)
        listing = []
        g = 0
        for n, (steps, smp, e) in enumerate(om.group_shots(cues)):
            label_file = work / f"t{n:03d}.txt"
            label_file.write_text(smp.label, encoding="utf-8")
            vf = shot_filter(n, g * STEP, label_file, smp.label, face, chaos_level(g))
            seg = work / f"s{n:03d}.ts"
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-ss", f"{smp.video_start or smp.src_start:.3f}", "-i", str(src),
                 "-filter_complex", vf, "-map", "[v]", "-frames:v", str(steps * 3), "-an", *encoder,
                 "-r", str(om.FPS), str(seg)],
                check=True,
            )
            listing.append(f"file '{seg.as_posix()}'")
            g += steps
        lst = work / "list.txt"
        lst.write_text("\n".join(listing), encoding="utf-8")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(wav),
             "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
             "-t", f"{TOTAL * STEP:.2f}", "-movflags", "+faststart", str(out)],
            check=True,
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", default="vid_09b94439359b444fbe239eaa0313b7e5")
    parser.add_argument("--span", default="3850,10866")
    parser.add_argument("--face", default=",".join(map(str, hits.MK8_FACE)))
    parser.add_argument("--cache", type=Path, default=om.ROOT / "exports" / "otomad" / "mk8_hits_picks.json")
    parser.add_argument("--gag", default="W", help="slot whose word carries the loop")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    span = tuple(float(v) for v in args.span.split(","))
    face = tuple(int(v) for v in args.face.split(","))
    print(render(args.video, span, args.out, face, args.cache, args.gag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
