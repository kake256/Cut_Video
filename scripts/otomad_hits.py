"""音MAD from the moments the streamer gets hit (Mario Kart 8 DX).

A hit is read off the screen: the coin counter in the race HUD drops (a shell, a thunder or a
fall all cost coins). For each hit the streamer's loudest reaction in the next ~2.5 s becomes a
sample, and its shot starts just before the hit so the crash itself is on screen.

    venv\\Scripts\\python.exe scripts\\otomad_hits.py --video <video_id> --span 3850,10866 --out exports\\otomad\\hits.mp4
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import otomad_prototype as om  # noqa: E402

sys.path.insert(0, str(om.ROOT))

COIN_BOX = (135, 978, 86, 56)          # two-digit coin counter in the 1920x1080 source
DIGITS = om.ROOT / "assets" / "otomad" / "mk8_coin_digits.npz"
MK8_FACE = (50, 650, 300, 256)         # camera box during races on this channel
HALLUCINATIONS = ("ご視聴", "ありがとう", "字幕", "次回")


def _templates() -> dict[str, tuple[np.ndarray, np.ndarray]]:
    d = np.load(DIGITS)
    return {"tens": (d["tens"], d["tens_label"]), "ones": (d["ones"], d["ones_label"])}


def _classify(img: np.ndarray, table: tuple[np.ndarray, np.ndarray], th: float = 0.07) -> int | None:
    reps, labels = table
    dist = (reps != img[None]).mean(axis=(1, 2))
    i = int(dist.argmin())
    return int(labels[i]) if dist[i] < th else None


def coin_counts(frames: np.ndarray) -> list[int | None]:
    """Coin count per 56x86 grey crop, None when the HUD is not showing (menus, results)."""
    t = _templates()
    out = []
    for img in frames > 150:
        tens, ones = _classify(img[:, :43], t["tens"]), _classify(img[:, 43:], t["ones"])
        out.append(tens * 10 + ones if tens is not None and ones is not None else None)
    return out


def _crops(src: Path, start: float, end: float, extra: list[str]) -> tuple[np.ndarray, list[float]]:
    x, y, w, h = COIN_BOX
    proc = subprocess.run(
        ["ffmpeg", "-v", "info", *extra, "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
         "-vf", f"crop={w}:{h}:{x}:{y},format=gray,showinfo", "-vsync", "0", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    )
    times = [start + float(t) for t in re.findall(rb"pts_time:([0-9.]+)", proc.stderr)]
    frames = np.frombuffer(proc.stdout, np.uint8).reshape(-1, h, w)
    return frames, times[: len(frames)]


def find_hits(src: Path, span: tuple[float, float]) -> list[tuple[float, int, int]]:
    """(time, coins before, coins after) for every drop. Keyframes (~4 s apart) find the drops
    fast; each one is then pinned to the frame by decoding that window at 10 fps."""
    frames, times = _crops(src, span[0], span[1], ["-skip_frame", "nokey"])
    counts = coin_counts(frames)
    hits = []
    for i in range(1, len(counts)):
        a, b = counts[i - 1], counts[i]
        if a is None or b is None or b >= a:
            continue
        fine, ftimes = _crops(src, times[i - 1], times[i] + 0.2, [])
        fine_counts = coin_counts(fine[::6])          # 60 fps source -> 10 fps
        ftimes = ftimes[::6]
        t_hit = times[i]
        for j in range(1, len(fine_counts)):
            if fine_counts[j] is not None and fine_counts[j - 1] is not None and fine_counts[j] < fine_counts[j - 1]:
                t_hit = ftimes[j]
                break
        hits.append((round(t_hit, 2), a, b))
    return hits


def _words(video_id: str, start: float, end: float) -> list[tuple[str, float, float]]:
    return om._words_between(video_id, start, end)


def reaction(model, video_id: str, src: Path, t_hit: float, key: str) -> tuple[float, om.Sample] | None:
    """The loudest short phrase said within 2.5 s after the hit, checked by re-listening."""
    words = _words(video_id, t_hit - 0.2, t_hit + 2.5)
    phrases: list[list] = []
    for w, a, b in words:
        if phrases and a - phrases[-1][2] < 0.15 and len(phrases[-1][0]) + len(w) <= 6:
            phrases[-1][0] += w
            phrases[-1][2] = b
        else:
            phrases.append([w, a, b])
    best = None
    for text, a, b in phrases:
        if not 0.15 <= b - a <= 1.2:
            continue
        smp = om.load_sample(src, key, text, a - 0.05, b - a + 0.15)
        heard = om._clean(om._heard(model, smp.audio))
        if not heard or len(heard) > 7 or any(h in heard for h in HALLUCINATIONS):
            continue
        _, peak, contrast, _ = om.tighten(om.read_audio(src, a - 0.05, b - a + 0.15))
        score = peak + 0.5 * contrast
        smp.label = heard
        smp.window = (a - 0.05, b - a + 0.15)
        smp.video_start = max(t_hit - 0.5, smp.src_start - 1.6)
        if best is None or score > best[0]:
            best = (score, smp)
    return best


def hit_samples(video_id: str, span: tuple[float, float], cache: Path | None = None) -> dict[str, om.Sample]:
    """Eight best hit reactions keyed W Y N U H M O K (W opens, K lands on the drop downbeats).

    The scan and re-listening take ~2 min, so the picks are cached as source windows."""
    import json
    src = om.video_path(video_id)
    keys = list(om.VOICES)
    if cache and cache.exists():
        picks = json.loads(cache.read_text(encoding="utf-8"))
        samples = {}
        for k, p in picks.items():
            smp = om.load_sample(src, k, p["label"], *p["window"])
            smp.video_start = p["video_start"]
            samples[k] = smp
        return samples
    hits = find_hits(src, span)
    print(f"{len(hits)} hits (coin drops)")
    from moment_retrieval import relisten
    model = relisten._get_model()
    scored = []
    for t_hit, before, after in hits:
        r = reaction(model, video_id, src, t_hit, "?")
        if r:
            print(f"  hit @{t_hit:8.2f}s coins {before}->{after}: {r[1].label!r} score={r[0]:.1f}")
            scored.append(r)
    scored.sort(key=lambda r: -r[0])
    samples: dict[str, om.Sample] = {}
    for score, smp in scored:
        if smp.label in {s.label for s in samples.values()}:
            continue
        smp.key = keys[len(samples)]
        samples[smp.key] = smp
        if len(samples) == len(keys):
            break
    if len(samples) < len(keys):
        raise SystemExit(f"only {len(samples)} usable reactions")
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({k: {"label": s.label, "window": s.window, "video_start": s.video_start}
                                     for k, s in samples.items()}, ensure_ascii=False, indent=1), encoding="utf-8")
    return samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True)
    parser.add_argument("--span", required=True, help="source seconds of the Mario Kart part, e.g. 3850,10866")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--face", default=",".join(map(str, MK8_FACE)))
    parser.add_argument("--cache", type=Path, help="reuse/save the picked reactions here")
    args = parser.parse_args(argv)
    span = tuple(float(v) for v in args.span.split(","))
    samples = hit_samples(args.video, span, args.cache)
    for k, smp in samples.items():
        print(f"{k}: {smp.label} (voice @{smp.src_start:.2f}s, picture from {smp.video_start:.2f}s)")
    face = tuple(int(v) for v in args.face.split(","))
    print(om.render(args.video, args.out, face=face, intro=0, samples=samples, tag="被弾集 音MAD"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
