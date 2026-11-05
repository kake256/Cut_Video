"""暴言集 音MAD: whole angry sentences (1-2.5 s, e.g. 「ふざけんなよお前」) instead of single words.

Single-word chops are hard to follow, so here every line plays in full with its text on screen,
the beat ducks under the voice, and the remix happens around the line: a stutter of its first
syllable leading in, and the insult inside it repeated on the second bar.

    venv\\Scripts\\python.exe scripts\\otomad_rant.py --out exports\\otomad\\rant.mp4
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import otomad_dance as dance  # noqa: E402  (sets up the GPU onnxruntime first)
import otomad_prototype as om  # noqa: E402
import otomad_rage as rage  # noqa: E402
from otomad_prototype import SR, STEP, add  # noqa: E402

INSULTS = ["バカ", "ゴミ", "クソ", "くそ", "ざけんな", "ふざけんな", "ふざけるな", "キモい", "最悪", "やめろ", "イライラ",
           "ありえない", "ありえねえ", "カス", "許さない", "なんでだよ", "うるせえ", "うざ", "雑魚", "黙れ", "ボケ", "アホ",
           "舐めんな", "なめんな", "てめえ"]
LINES = 8
BARS_PER_LINE = 2
TOTAL = LINES * BARS_PER_LINE * 16


@dataclass
class Line:
    video_id: str
    start: float          # window in the source
    dur: float
    text: str
    insult: str
    insult_at: float      # insult's offset from the window start
    insult_len: float
    score: float = 0.0


def find_lines(video_id: str, min_chars: int = 5, max_dur: float = 2.4) -> list[Line]:
    """Sentences around an insult: the insult's words, grown to the neighbouring words of the
    same breath (gaps < 0.3 s) up to max_dur."""
    out = []
    rows = om._conn().execute("select words_json from asr_segments where video_id=? order by start_sec", (video_id,))
    for (words_json,) in rows:
        words = [w for w in json.loads(words_json or "[]") if w.get("word", "").strip()]
        for i in range(len(words)):
            # the insult must start on a word boundary and end inside the last word joined
            # (otherwise 「ソニックソニック」 contains 「クソ」)
            text = ""
            insult = None
            for j in range(i, min(i + 4, len(words))):
                text += words[j]["word"].strip()
                insult = next((p for p in INSULTS if text.startswith(p)
                               and len(text) - len(words[j]["word"].strip()) < len(p)), None)
                if insult:
                    break
            if not insult:
                continue
            a, b = i, j
            while a > 0 and words[a]["start"] - words[a - 1]["end"] < 0.3 \
                    and words[b]["end"] - words[a - 1]["start"] <= max_dur:
                a -= 1
            while b + 1 < len(words) and words[b + 1]["start"] - words[b]["end"] < 0.3 \
                    and words[b + 1]["end"] - words[a]["start"] <= max_dur:
                b += 1
            line = "".join(w["word"].strip() for w in words[a:b + 1])
            dur = words[b]["end"] - words[a]["start"]
            # a single "word" lasting seconds is a timestamp glitch, not a long line
            if len(line) < min_chars or not 0.8 <= dur <= max_dur                     or any(w["end"] - w["start"] > 1.2 for w in words[a:b + 1]):
                continue
            start = words[a]["start"] - 0.05
            out.append(Line(video_id, start, dur + 0.2, line, insult, words[i]["start"] - start,
                            words[j]["end"] - words[i]["start"]))
    # one line per breath
    out.sort(key=lambda l: l.start)
    dedup: list[Line] = []
    for line in out:
        if dedup and line.start < dedup[-1].start + dedup[-1].dur:
            continue
        dedup.append(line)
    return dedup


def pick(videos: list[str], n: int = LINES, per_video: int = 2) -> list[Line]:
    sys.path.insert(0, str(om.ROOT))
    from moment_retrieval import relisten
    model = relisten._get_model()
    from concurrent.futures import ThreadPoolExecutor

    def score(line: Line) -> Line:
        _, peak, contrast, _ = om.tighten(om.read_audio(om.video_path(line.video_id), line.start, line.dur))
        line.score = peak + 0.5 * contrast
        return line

    with ThreadPoolExecutor(8) as pool:      # one ffmpeg seek per line; they run fine side by side
        cands = list(pool.map(score, [line for vid in videos for line in find_lines(vid)]))
    print(f"{len(cands)} candidate lines", flush=True)
    cands.sort(key=lambda l: -l.score)
    picked: list[Line] = []
    for line in cands:
        if sum(p.video_id == line.video_id for p in picked) >= per_video:
            continue
        audio = om.read_audio(om.video_path(line.video_id), line.start, line.dur)
        heard = om._clean(om._heard(model, audio / (np.abs(audio).max() + 1e-9) * 0.9))
        ok = line.insult in heard and 5 <= len(heard) <= len(om._clean(line.text)) + 4 \
            and not any(h in heard for h in ("ご視聴", "ありがとう", "字幕"))
        print(f"  {line.video_id[-6:]} @{line.start:8.1f}s {line.dur:.1f}s {line.text!r} heard={heard!r} "
              f"score={line.score:.1f} {'OK' if ok else '-'}", flush=True)
        if ok:
            line.text = heard
            picked.append(line)
            if len(picked) == n:
                break
    if len(picked) < n:
        raise SystemExit(f"only {len(picked)} usable lines")
    return picked


def beat(voice: np.ndarray) -> np.ndarray:
    """Steady groove that ducks under the voice so every word stays clear."""
    rng = np.random.default_rng(5)
    n = len(voice)
    drums, music = np.zeros(n, np.float32), np.zeros(n, np.float32)
    k, sn = om.kick(), om.snare(rng)
    roots = [57, 53, 48, 55]
    for b in range(TOTAL // 16):
        t0 = b * 16 * STEP
        for s in (0, 4, 8, 12):
            add(drums, k, t0 + s * STEP, 0.9)
        for s in (4, 12):
            add(drums, sn, t0 + s * STEP, 0.6)
        for s in range(0, 16, 2):
            add(drums, om.hat(rng), t0 + s * STEP, 0.4)
        root = roots[(b // 2) % 4]
        for s in range(0, 16, 2):
            add(music, om.saw_note(om.midi(root - 12), STEP * 1.6, 800), t0 + s * STEP, 0.3)
    env = np.convolve(np.abs(voice), np.ones(int(SR * 0.05)) / (SR * 0.05), "same")
    duck = 1 - 0.6 * np.clip(env / (env.max() + 1e-9) * 4, 0, 1)
    return (0.7 * drums + music) * duck


def render_audio(samples: list[om.Sample], lines: list[Line]) -> tuple[np.ndarray, list[tuple]]:
    n = int(TOTAL * STEP * SR)
    voice = np.zeros(n + SR * 3, np.float32)
    cues = []
    for i, (smp, line) in enumerate(zip(samples, lines)):
        bar0 = i * BARS_PER_LINE * 16
        nxt = samples[(i + 1) % len(samples)]
        # the whole line (at most 2.6 s, so it ends before step 26), then its insult three times, rising
        add(voice, smp.audio[: int(SR * 26 * STEP)], bar0 * STEP)
        a = int(SR * max(0.0, line.insult_at - 0.03))
        ins = smp.audio[a: a + int(SR * min(line.insult_len + 0.05, 0.35))]
        for k, s in enumerate((26, 28, 30)):
            add(voice, om.pitch(ins, 2 * k), (bar0 + s) * STEP, 0.9)
        # lead-in: the next line's first syllable on the last 16ths
        add(voice, nxt.audio[: int(SR * 0.09)], (bar0 + 31) * STEP, 0.8)
        cues.append((BARS_PER_LINE * 16, smp, line))
    voice[: SR * 3] += voice[n: n + SR * 3]          # wrap so the loop is seamless
    voice = voice[:n]
    mix = beat(voice) + 1.6 * voice
    return np.tanh(mix * 1.1) * 0.89, cues


def wrap(text: str, width: int = 8) -> str:
    return "\n".join(text[i:i + width] for i in range(0, len(text), width))


def shot_filter(g0: float, label_file: Path, label: str) -> str:
    T = f"({g0:.3f}+t)"
    pulse = f"abs(sin(PI*{T}/{4 * STEP:.3f}))"
    lines = label.count("\n") + 1
    fs = 120 if lines == 1 else 105
    return ";".join([
        f"[0:v]fps={om.FPS},split=2[a][b]",
        f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,scale=1080:1920,"
        f"colorchannelmixer=rr=0.9:rg=0.3:rb=0.2:gr=0.1:gg=0.15:gb=0.05:br=0.1:bg=0.1:bb=0.15[bg]",
        "[b]scale=1080:608[scene]",
        "[bg][scene]overlay=0:120[s1]",
        f"[1:v]fps={om.FPS},format=rgba,scale=980:-2[me]",
        f"[s1][me]overlay=x='(W-w)/2+10*sin(n*2.7)*(lt(n,5)+0.3)':y='H-h+20-24*{pulse}':shortest=1[s2]",
        f"[s2]drawtext=fontfile='{om.FONT}':textfile='{om._ff_path(label_file)}':fontsize={fs}:line_spacing=10:"
        f"fontcolor=white:borderw=14:bordercolor=#d40000:x=(w-tw)/2:y=880-th/2-12*{pulse}[v]",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latest", type=int, default=5)
    parser.add_argument("--cache", type=Path, default=om.ROOT / "exports" / "otomad" / "rant_picks.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    from moment_retrieval.video_encoder import h264_args

    if args.cache.exists():
        lines = [Line(**d) for d in json.loads(args.cache.read_text(encoding="utf-8"))]
    else:
        lines = pick(rage.latest_videos(args.latest))
        args.cache.write_text(json.dumps([line.__dict__ for line in lines], ensure_ascii=False, indent=1),
                              encoding="utf-8")
    samples = []
    for k, line in enumerate(lines):
        src = om.video_path(line.video_id)
        smp = om.load_sample(src, str(k), line.text, line.start, line.dur)
        line.insult_at -= smp.src_start - line.start     # tighten() may have trimmed the start
        smp.src = src
        samples.append(smp)
        print(f"{k}: {line.text} ({line.video_id[-6:]} @{smp.src_start:.1f}s, {len(smp.audio) / SR:.1f}s)")
    mix, cues = render_audio(samples, lines)
    encoder = h264_args(crf=18, preset="veryfast")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # shots are kept between runs (keyed by the picks), so a render cut short resumes where it stopped
    work = args.out.resolve().with_suffix(".parts")
    work.mkdir(parents=True, exist_ok=True)
    if True:
        wav = work / "mix.wav"
        om.write_wav(wav, mix)
        listing = []
        for n, (steps, smp, line) in enumerate(cues):
            seg = work / f"s{n:03d}_{smp.src_start:.2f}.ts"
            if seg.exists():
                listing.append(f"file '{seg.as_posix()}'")
                continue
            box = rage.person_box(smp.src, smp.src_start + 0.1) or (1560, 800, 360, 280)
            print(f"{line.text}: person at {box}", flush=True)
            cut_dir = work / f"cut{n:03d}"
            dance.cutout_frames(smp.src, smp.src_start, steps * STEP, box, cut_dir)
            label_file = work / f"t{n:03d}.txt"
            label = wrap(line.text)
            label_file.write_text(label, encoding="utf-8", newline=chr(10))   # a CR shows up as a blank line
            cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{smp.src_start:.3f}", "-i", str(smp.src),
                   "-framerate", str(dance.MATTE_FPS), "-i", str(cut_dir / "%03d.png"),
                   "-filter_complex", shot_filter(n * steps * STEP, label_file, label),
                   "-map", "[v]", "-frames:v", str(steps * 3), "-an", *encoder, "-r", str(om.FPS), str(seg)]
            tmp_seg = seg.with_suffix(".tmp.ts")
            cmd[-1] = str(tmp_seg)
            for _ in range(3):
                if subprocess.run(cmd).returncode == 0:
                    break
            else:
                raise SystemExit(f"shot {n} failed to render")
            tmp_seg.replace(seg)
            shutil.rmtree(cut_dir, ignore_errors=True)
            listing.append(f"file '{seg.as_posix()}'")
        lst = work / "list.txt"
        lst.write_text("\n".join(listing), encoding="utf-8")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(wav),
             "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
             "-t", f"{TOTAL * STEP:.2f}", "-movflags", "+faststart", str(args.out)],
            check=True,
        )
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
