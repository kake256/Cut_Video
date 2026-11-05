"""Prototype: build a short "音MAD"-style vertical video from a streamer's own shouts.

Words come from the library's word-level transcript (asr_segments.words_json), are
tightened to the actual voiced span by energy, and are chopped onto a self-made beat
(numpy-synthesised drums and bass, no third-party music). The video cuts to the
matching source frames on every hit, with the word in large type.

    venv\\Scripts\\python.exe scripts\\otomad_prototype.py mine --video <video_id>
    venv\\Scripts\\python.exe scripts\\otomad_prototype.py render --video <video_id> --out exports\\otomad.mp4

Read-only on the library: it opens data/index.db with mode=ro and writes only to
--out and a temp working directory.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "index.db"
SR = 44100
FPS = 30
BPM = 150                      # 16th note = 0.1 s = 3 frames at 30 fps
STEP = 60.0 / BPM / 4          # seconds per 16th
FONT = "C\\:/Windows/Fonts/YuGothB.ttc"

PHRASES = ["待って", "やばい", "なんで", "うそ", "きた", "ナイス", "おい", "勝った", "無理", "うわ", "は?", "いけ"]


@dataclass
class Hit:
    phrase: str
    start: float
    end: float
    gap: float = 0.0


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)


def video_path(video_id: str) -> Path:
    row = _conn().execute("select path from videos where video_id=?", (video_id,)).fetchone()
    if not row:
        raise SystemExit(f"unknown video_id: {video_id}")
    return Path(row[0])


def find_hits(video_id: str, phrases: list[str]) -> list[Hit]:
    """Phrase occurrences at word resolution, matched across consecutive ASR words."""
    hits: list[Hit] = []
    rows = _conn().execute(
        "select words_json from asr_segments where video_id=? order by start_sec", (video_id,),
    )
    for (words_json,) in rows:
        words = [w for w in json.loads(words_json or "[]") if w.get("word", "").strip()]
        for i in range(len(words)):
            text = ""
            for j in range(i, min(i + 4, len(words))):
                text += words[j]["word"].strip()
                matched = next((p for p in phrases if text == p or text.rstrip("!！、。?？") == p), None)
                if matched:
                    start, end = float(words[i]["start"]), float(words[j]["end"])
                    # silence around the word: a shout said on its own chops cleanly
                    before = start - float(words[i - 1]["end"]) if i > 0 else 0.5
                    after = float(words[j + 1]["start"]) - end if j + 1 < len(words) else 0.5
                    hits.append(Hit(matched, start, end, gap=min(before, after)))
                    break
                if not any(p.startswith(text) for p in phrases):
                    break
    return hits


def read_audio(src: Path, start: float, dur: float) -> np.ndarray:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}", "-i", str(src),
         "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(out, dtype=np.float32).copy()


def tighten(audio: np.ndarray, pad: float = 0.0) -> tuple[np.ndarray, float, float, float]:
    """Trim the padded window to its voiced span.

    Returns the trimmed audio, its peak level, how far the peak stands out from the
    window, and the trim offset in seconds.
    """
    hop = int(SR * 0.01)
    frames = len(audio) // hop
    if frames < 5:
        return audio, 0.0, 0.0, 0.0
    rms = np.sqrt(np.mean(audio[: frames * hop].reshape(frames, hop) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    peak = db.max()
    voiced = np.where(db > peak - 18)[0]
    a, b = voiced[0], voiced[-1] + 1
    a = max(0, a - 2)
    b = min(frames, b + 4)
    seg = audio[a * hop: b * hop]
    # how much louder the word is than its surroundings: shouts stand out
    ctx = np.median(db)
    return seg, float(peak), float(peak - ctx), a * hop / SR


def mine(video_id: str, phrases: list[str] = PHRASES, per_phrase: int = 15,
         clean_gap: float = 0.15, span: tuple[float, float] | None = None) -> dict[str, list[dict]]:
    """Loud occurrences of each phrase, isolated ones (silence on both sides) first."""
    src = video_path(video_id)
    by_phrase: dict[str, list[dict]] = {}
    for hit in find_hits(video_id, phrases):
        if span and not span[0] <= hit.start <= span[1]:
            continue
        dur = hit.end - hit.start
        if not 0.15 <= dur <= 1.2:
            continue
        start, win = hit.start - 0.05, dur + 0.15
        seg, peak, contrast, _ = tighten(read_audio(src, start, win))
        seg_dur = len(seg) / SR
        if not 0.15 <= seg_dur <= 1.0:
            continue
        score = peak + 0.5 * contrast
        by_phrase.setdefault(hit.phrase, []).append(
            {"start": round(start, 3), "dur": round(win, 3), "voiced": round(seg_dur, 3), "gap": round(hit.gap, 2),
             "peak_db": round(peak, 1), "contrast": round(contrast, 1), "score": round(score, 1)})
    for phrase in by_phrase:
        by_phrase[phrase].sort(key=lambda c: (c["gap"] < clean_gap, -c["score"]))
        by_phrase[phrase] = by_phrase[phrase][:per_phrase]
    return by_phrase


# ---------------------------------------------------------------- samples

@dataclass
class Sample:
    key: str
    label: str
    src_start: float      # where the voiced word starts in the source video
    audio: np.ndarray
    video_start: float | None = None   # show the picture from here instead (e.g. the hit before the scream)


def load_sample(src: Path, key: str, label: str, start: float, dur: float) -> Sample:
    seg, _, _, offset = tighten(read_audio(src, start, dur))
    seg = seg / (np.abs(seg).max() + 1e-9) * 0.9
    fade = int(SR * 0.004)
    seg[:fade] *= np.linspace(0, 1, fade)
    seg[-fade * 3:] *= np.linspace(1, 0, fade * 3)
    return Sample(key, label, start + offset, seg.astype(np.float32))


def _heard(model, audio: np.ndarray) -> str:
    audio16 = np.interp(np.arange(0, len(audio), SR / 16000), np.arange(len(audio)), audio).astype(np.float32)
    pad = np.zeros(8000, dtype=np.float32)
    segs, _ = model.transcribe(np.concatenate([pad, audio16, pad]), language="ja", beam_size=5, vad_filter=False)
    return "".join(s.text for s in segs).strip()


def _clean(text: str) -> str:
    return "".join(ch for ch in text if ch not in " 、。!！?？ー~〜…")


# A screen-layout check: (x, y, w, h, (r, g, b), tolerance) on the 1920x1080 source. A stream's
# camera box moves between scenes (lobby vs race), so a pick is used only where the layout is the
# one DEFAULT_FACE/--face describes.
Probe = tuple[int, int, int, int, tuple[int, int, int], int]
PROBES: dict[str, Probe] = {
    "mk8_race": (90, 975, 40, 40, (220, 200, 15), 70),   # Mario Kart 8 DX coin counter, bottom left
}


def frame_matches(src: Path, t: float, probe: Probe) -> bool:
    x, y, w, h, rgb, tol = probe
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src), "-vf",
         f"crop={w}:{h}:{x}:{y},scale=1:1:flags=area", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    return len(raw) == 3 and max(abs(a - b) for a, b in zip(raw, rgb)) <= tol


def pick_samples(video_id: str, wanted: dict[str, str], candidates: dict[str, list[dict]],
                 probe: Probe | None = None) -> dict[str, Sample]:
    """For each phrase, the best candidate that, re-listened on its own, is that phrase and little else.

    The library's word times are often off by a syllable (「あー待って」「よいしょ!おい」),
    which a chopped 音MAD stutter makes very audible, so every pick is checked by ear (ASR).
    """
    sys.path.insert(0, str(ROOT))
    from moment_retrieval import relisten
    model = relisten._get_model()
    src = video_path(video_id)
    picked = {}
    for key, label in wanted.items():
        target = _clean(label)
        tried = []          # (rank, sample): 0 = exactly the word, 1 = word plus a syllable, 2 = unverified
        for cand in candidates.get(label, []):
            if probe and not frame_matches(src, cand["start"] + 0.1, probe):
                continue
            smp = load_sample(src, key, label, cand["start"], cand["dur"])
            heard = _clean(_heard(model, smp.audio))
            rank = 0 if heard == target else 1 if target in heard and len(heard) <= len(target) + 1 else 2
            print(f"{key:>2} {label:<5} @{smp.src_start:9.2f}s {len(smp.audio) / SR:.2f}s heard={heard!r} rank={rank}")
            tried.append((rank, smp))
            if rank == 0:
                break
        if not tried:
            raise SystemExit(f"no candidate for {label}")
        picked[key] = min(tried, key=lambda t: t[0])[1]
    return picked


def pitch(audio: np.ndarray, semitones: float) -> np.ndarray:
    """Resample pitch shift (speed changes too: the classic chipmunk 音MAD sound)."""
    if semitones == 0:
        return audio
    ratio = 2 ** (semitones / 12)
    idx = np.arange(0, len(audio) - 1, ratio)
    return np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)


# ---------------------------------------------------------------- beat

def _env(n: int, decay: float) -> np.ndarray:
    return np.exp(-np.arange(n) / (SR * decay))


def kick() -> np.ndarray:
    n = int(SR * 0.35)
    t = np.arange(n) / SR
    freq = 45 + 110 * np.exp(-t / 0.03)
    return (np.sin(2 * np.pi * np.cumsum(freq) / SR) * _env(n, 0.12)).astype(np.float32)


def snare(rng: np.random.Generator) -> np.ndarray:
    from scipy.signal import butter, lfilter
    n = int(SR * 0.2)
    noise = lfilter(*butter(2, [1500 / (SR / 2), 8000 / (SR / 2)], "band"), rng.standard_normal(n))
    tone = np.sin(2 * np.pi * 190 * np.arange(n) / SR)
    return (0.8 * noise * _env(n, 0.05) + 0.5 * tone * _env(n, 0.03)).astype(np.float32)


def hat(rng: np.random.Generator, length: float = 0.05) -> np.ndarray:
    from scipy.signal import butter, lfilter
    n = int(SR * length * 2)
    noise = lfilter(*butter(2, 7000 / (SR / 2), "high"), rng.standard_normal(n))
    return (0.35 * noise * _env(n, length / 3)).astype(np.float32)


def saw_note(freq: float, dur: float, cutoff: float = 900) -> np.ndarray:
    from scipy.signal import butter, lfilter
    n = int(SR * dur)
    t = np.arange(n) / SR
    wave_ = sum(np.sin(2 * np.pi * freq * k * t) / k for k in range(1, 12))
    wave_ = lfilter(*butter(2, cutoff / (SR / 2)), wave_)
    env = np.minimum(1, np.arange(n) / (SR * 0.005)) * np.minimum(1, (n - np.arange(n)) / (SR * 0.02))
    return (wave_ * env).astype(np.float32)


def midi(n: int) -> float:
    return 440.0 * 2 ** ((n - 69) / 12)


def add(buf: np.ndarray, x: np.ndarray, at: float, gain: float = 1.0) -> None:
    i = int(round(at * SR))
    if i >= len(buf):
        return
    j = min(len(buf), i + len(x))
    buf[i:j] += x[: j - i] * gain


# ---------------------------------------------------------------- arrangement

@dataclass
class Event:
    step: int            # absolute 16th-note index
    key: str
    semis: float = 0.0
    chop: float = 0.0    # seconds to keep from the start (0 = whole word)


def arrange(first: str = "W") -> tuple[list[Event], int]:
    """17 bars at 150 BPM = 27.2 s: intro (0), call/response (1-4), melody (5-8), build (9-12), drop (13-16)."""
    ev: list[Event] = []

    def at(b: int, s: int) -> int:
        return b * 16 + s

    # intro: "待って" then a stuttered "ま" roll into the beat
    ev.append(Event(at(0, 0), first))
    for s in (8, 10, 12, 13, 14, 15):
        ev.append(Event(at(0, s), first, chop=0.09))
    # call and response
    for i, (a, b) in enumerate([("Y", "N"), ("U", "H"), ("Y", "N"), ("M", "O")]):
        ev += [Event(at(1 + i, 0), a), Event(at(1 + i, 6), b), Event(at(1 + i, 10), b, chop=0.12),
               Event(at(1 + i, 12), "W" if i % 2 else "K")]
    # melody: chopped words tuned onto Am-F-C-G (人力ボーカル風)
    melody = [[0, 0, 3, 5, 7, 5, 3, 0], [-4, -4, 0, 3, 5, 3, 0, -4],
              [3, 3, 7, 10, 12, 10, 7, 3], [-2, -2, 2, 5, 7, 5, 2, -2]]
    for i, notes in enumerate(melody):
        for k, semi in enumerate(notes):
            ev.append(Event(at(5 + i, k * 2), "Y" if k % 4 != 3 else "N", semis=semi, chop=0.18))
    # build: denser and denser, rising at the end
    for i, every in enumerate((4, 2, 2, 1)):
        for s in range(0, 16, every):
            ev.append(Event(at(9 + i, s), "N" if i < 3 else "W", semis=(s / 2 if i == 3 else 0),
                            chop=0.1 if every < 4 else 0))
    # drop
    for i, (a, b, c) in enumerate([("K", "Y", "U"), ("O", "N", "H"), ("K", "Y", "M"), ("W", "N", "U")]):
        ev += [Event(at(13 + i, 0), a), Event(at(13 + i, 4), b, chop=0.12), Event(at(13 + i, 6), b, chop=0.12),
               Event(at(13 + i, 8), c), Event(at(13 + i, 14), a, semis=5, chop=0.1)]
    ev.sort(key=lambda e: e.step)
    return ev, 17 * 16


def render_audio(samples: dict[str, Sample], events: list[Event], total_steps: int) -> tuple[np.ndarray, list[tuple]]:
    """Mix drums, bass/chords and the voice chops; returns the mono mix and one video cue per voice event."""
    rng = np.random.default_rng(7)
    total = total_steps * STEP + 0.6
    drums = np.zeros(int(SR * total), np.float32)
    music = np.zeros_like(drums)
    voice = np.zeros_like(drums)
    kick_, snare_ = kick(), snare(rng)
    roots = [57, 53, 48, 55]                    # A F C G
    for b in range(total_steps // 16):
        t0 = b * 16 * STEP
        section = "intro" if b == 0 else "A" if b <= 4 else "mel" if b <= 8 else "build" if b <= 12 else "drop"
        if section == "intro":
            for s in (0, 4, 8, 12):
                add(drums, hat(rng, 0.03), t0 + s * STEP, 0.6)
            continue
        kick_steps = (0, 4, 8, 12) if section != "build" else tuple(range(0, 16, 4 if b < 11 else 2))
        for s in kick_steps:
            add(drums, kick_, t0 + s * STEP, 0.9)
        if section != "build" or b >= 11:
            for s in (4, 12):
                add(drums, snare_, t0 + s * STEP, 0.6)
        if b == 12:
            for s in range(8, 16):
                add(drums, snare_, t0 + s * STEP, 0.25 + 0.05 * (s - 8))
        for s in range(0, 16, 2 if section != "drop" else 1):
            add(drums, hat(rng), t0 + s * STEP + (0.012 if s % 4 == 2 else 0), 0.5 if s % 4 else 0.3)
        root = roots[(b - 1) % 4]
        for s in range(0, 16, 2):
            add(music, saw_note(midi(root - 12), STEP * 1.6, 500 if section != "drop" else 1200),
                t0 + s * STEP, 0.35)
        if section in ("mel", "drop"):
            for n in (0, 3 if root == 57 else 4, 7):
                add(music, saw_note(midi(root + n), 16 * STEP, 1800), t0, 0.06)
    # duck the music on each beat so the kick and the voice punch through
    pump = np.ones_like(music)
    n = int(SR * 0.18)
    for b in range(1, total_steps // 16):
        for s in (0, 4, 8, 12):
            i = int((b * 16 + s) * STEP * SR)
            j = min(len(pump), i + n)
            pump[i:j] = np.minimum(pump[i:j], (0.35 + 0.65 * np.linspace(0, 1, n))[: j - i])
    music *= pump

    cues = []
    for idx, e in enumerate(events):
        nxt = events[idx + 1].step if idx + 1 < len(events) else total_steps
        smp = samples[e.key]
        x = smp.audio[: int(SR * e.chop)] if e.chop else smp.audio
        x = pitch(x, e.semis)[: int((nxt - e.step) * STEP * SR)].copy()
        f = min(len(x), int(SR * 0.006))
        if f:
            x[-f:] *= np.linspace(1, 0, f)
        add(voice, x, e.step * STEP)
        cues.append((nxt - e.step, smp, e))
    mix = 0.75 * drums + music + 1.1 * voice
    return np.tanh(mix * 1.2) * 0.89, cues


def write_wav(path: Path, audio: np.ndarray) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.repeat(pcm[:, None], 2, axis=1).tobytes())


# ---------------------------------------------------------------- video

def _ff_path(path: Path) -> str:
    return path.as_posix().replace(":", "\\:")


def group_shots(cues: list[tuple], min_steps: int = 4) -> list[tuple[int, object, object]]:
    """Merge voice cues into camera shots so the picture does not flicker.

    A cut every 16th note (0.1 s) strobes. A new shot starts only on a different word once the
    current shot has run min_steps, or on a bar's downbeat; rapid stutters keep one continuous shot.
    """
    shots: list[list] = []
    step = 0
    for steps, smp, e in cues:
        if shots:
            cur = shots[-1]
            new_word = smp.key != cur[1].key
            if not ((new_word and cur[0] >= min_steps) or (step % 16 == 0 and cur[0] >= min_steps)):
                cur[0] += steps
                step += steps
                continue
        shots.append([steps, smp, e])
        step += steps
    return [tuple(s) for s in shots]


def render_video(src: Path, cues: list[tuple], work: Path, encoder: list[str],
                 face: tuple[int, int, int, int], tag: str = "音MAD") -> Path:
    """One segment per shot: the word's source frames in a vertical layout with the word in big type."""
    listing = []
    for n, (steps, smp, e) in enumerate(group_shots(cues)):
        txt = work / f"t{n:03d}.txt"
        txt.write_text(smp.label, encoding="utf-8")
        band = ["#ff3b6b", "#3bd1ff", "#ffe14d", "#7cff6b"][n % 4]
        # a short nudge of the camera panel at the cut, no full-screen flash
        shake = "8*sin(n*2.5)*lt(n\,3)"
        fx, fy, fw, fh = face
        vf = (
            f"[0:v]fps={FPS},split=2[a][b];"
            # top: the game, centre-cropped; bottom: the streamer's camera, zoomed
            f"[a]crop=ih*1080/1000:ih,scale=1080:1000,hue=s=1.15[top];"
            f"[b]crop={fw}:{fh}:{fx}+{shake}:{fy},scale=1080:920,eq=saturation=1.3[cam];"
            f"[top][cam]vstack,"
            f"drawbox=0:1000-6:iw:12:color={band}:t=fill,"
            f"drawtext=fontfile='{FONT}':textfile='{_ff_path(txt)}':"
            f"fontsize={210 if len(smp.label) <= 3 else 170 if len(smp.label) <= 5 else 130}:fontcolor=#ffe14d:borderw=14:bordercolor=black:"
            f"x=(w-tw)/2:y=1000-th/2,"
            f"drawtext=fontfile='{FONT}':text='{tag}':fontsize=56:fontcolor=white:borderw=6:"
            f"bordercolor=black:x=(w-tw)/2:y=120[v]"
        )
        out = work / f"s{n:03d}.ts"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{smp.video_start or smp.src_start:.3f}", "-i", str(src),
             "-filter_complex", vf, "-map", "[v]", "-frames:v", str(steps * 3), "-an", *encoder,
             "-r", str(FPS), str(out)],
            check=True,
        )
        listing.append(f"file '{out.as_posix()}'")
    lst = work / "list.txt"
    lst.write_text("\n".join(listing), encoding="utf-8")
    return lst


# ---------------------------------------------------------------- fake-out intro

def _words_between(video_id: str, start: float, end: float) -> list[tuple[str, float, float]]:
    rows = _conn().execute(
        "select words_json from asr_segments where video_id=? and end_sec>? and start_sec<? order by start_sec",
        (video_id, start, end),
    )
    words = []
    for (words_json,) in rows:
        for w in json.loads(words_json or "[]"):
            if w.get("word", "").strip() and start <= float(w["start"]) and float(w["end"]) <= end:
                words.append((w["word"].strip(), float(w["start"]), float(w["end"])))
    return words


def choose_intro(video_id: str, samples: dict[str, Sample], length: float) -> tuple[str, list[tuple]]:
    """The word whose lead-up is the chattiest: the video opens as an ordinary talk clip and
    the beat drops on that word. Returns its key and caption lines relative to the intro start."""
    best = None
    for key, smp in samples.items():
        words = _words_between(video_id, smp.src_start - length, smp.src_start - 0.05)
        if not words:
            continue
        gaps = [b[1] - a[2] for a, b in zip(words, words[1:])] + [words[0][1] - (smp.src_start - length)]
        score = sum(len(w) for w, _, _ in words) - 10 * sum(g > 1.5 for g in gaps)
        if best is None or score > best[0]:
            best = (score, key, words)
    if best is None:
        raise SystemExit("no talk before any picked word")
    _, key, words = best
    t0 = samples[key].src_start - length
    lines: list[list] = []
    for w, a, b in words:
        if lines and len(lines[-1][0]) + len(w) <= 13 and a - lines[-1][2] < 0.5:
            lines[-1][0] += w
            lines[-1][2] = b
        else:
            lines.append([w, a, b])
    cues = []
    for i, (text, a, b) in enumerate(lines):
        end = lines[i + 1][1] if i + 1 < len(lines) else length + t0
        cues.append((text, max(0.0, a - t0), min(length, max(b, end) - t0)))
    return key, cues


def render_intro(src: Path, smp: Sample, length: float, captions: list[tuple], work: Path,
                 encoder: list[str]) -> Path:
    """Looks like a normal CUT talk clip: blurred fill, fitted frame, plain white captions."""
    draws = []
    for i, (text, a, b) in enumerate(captions):
        txt = work / f"intro{i:02d}.txt"
        txt.write_text(text, encoding="utf-8")
        draws.append(
            f"drawtext=fontfile='{FONT}':textfile='{_ff_path(txt)}':fontsize=64:fontcolor=white:"
            f"borderw=6:bordercolor=black:x=(w-tw)/2:y=1380:enable='between(t,{a:.2f},{b:.2f})'"
        )
    vf = (
        f"[0:v]fps={FPS},split=2[a][b];"
        f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,scale=1080:1920[bg];"
        f"[b]scale=1080:-2[fg];[bg][fg]overlay=0:(H-h)/2,setsar=1" + "".join("," + d for d in draws) + "[v]"
    )
    out = work / "intro.ts"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{smp.src_start - length:.3f}", "-i", str(src),
         "-filter_complex", vf, "-map", "[v]", "-frames:v", str(round(length * FPS)), "-an", *encoder,
         "-r", str(FPS), str(out)],
        check=True,
    )
    return out


DEFAULT_VIDEO = "20260713_013129_v2818333996_488c1cbb40"
# streamer camera box in the 1920x1080 source (x, y, w, h); differs per channel layout
DEFAULT_FACE = (20, 700, 430, 366)
# arrangement slot -> phrase; any phrase the streamer actually shouts works here
VOICES = {"W": "待って", "Y": "やばい", "N": "なんで", "U": "ちょっと",
          "H": "は?", "M": "無理", "O": "おい", "K": "勝った"}


def render(video_id: str, out: Path, voices: dict[str, str] | None = None,
           face: tuple[int, int, int, int] = DEFAULT_FACE, intro: float = 5.0,
           span: tuple[float, float] | None = None, probe: Probe | None = None, tag: str = "音MAD",
           samples: dict[str, Sample] | None = None) -> Path:
    """intro > 0 opens with that many seconds of ordinary talk; the beat drops on the next word."""
    sys.path.insert(0, str(ROOT))
    from moment_retrieval.video_encoder import h264_args
    voices = voices or VOICES
    src = video_path(video_id)
    if samples is None:
        samples = pick_samples(video_id, voices, mine(video_id, sorted(set(voices.values())), span=span), probe)
    voices = {k: smp.label for k, smp in samples.items()}
    first, captions = choose_intro(video_id, samples, intro) if intro > 0 else ("W", [])
    events, total_steps = arrange(first)
    mix, cues = render_audio(samples, events, total_steps)
    if intro > 0:
        talk = read_audio(src, samples[first].src_start - intro, intro)[: int(intro * SR)]
        talk = talk / (np.abs(talk).max() + 1e-9) * 0.8
        fade = int(SR * 0.15)
        talk[:fade] *= np.linspace(0, 1, fade)
        talk = np.pad(talk, (0, int(intro * SR) - len(talk)))
        mix = np.concatenate([talk, mix])
        print(f"intro: drop on {voices[first]} @ {samples[first].src_start:.2f}s; captions:",
              " / ".join(c[0] for c in captions))
    encoder = h264_args(crf=18, preset="veryfast")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="otomad_") as tmp:
        work = Path(tmp)
        wav = work / "mix.wav"
        write_wav(wav, mix)
        lst = render_video(src, cues, work, encoder, face, tag)
        if intro > 0:
            head = render_intro(src, samples[first], intro, captions, work, encoder)
            lst.write_text(f"file '{head.as_posix()}'\n" + lst.read_text(encoding="utf-8"), encoding="utf-8")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(wav),
             "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
             "-t", f"{intro + total_steps * STEP + 0.3:.2f}", "-movflags", "+faststart", str(out)],
            check=True,
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mine")
    m.add_argument("--video", required=True)
    m.add_argument("--out", type=Path)
    r = sub.add_parser("render")
    r.add_argument("--video", default=DEFAULT_VIDEO)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--intro", type=float, default=5.0, help="seconds of plain talk before the beat drops (0 = none)")
    r.add_argument("--voices", help="override slots, e.g. K=サンダー,H=マジ")
    r.add_argument("--tag", default="音MAD", help="small label at the top of the frame")
    r.add_argument("--face", default=",".join(map(str, DEFAULT_FACE)), help="camera box x,y,w,h in the source")
    r.add_argument("--span", help="only words between these source seconds, e.g. 3850,10866")
    r.add_argument("--probe", choices=sorted(PROBES), help="only words where this screen layout is showing")
    args = parser.parse_args(argv)
    if args.cmd == "render":
        face = tuple(int(v) for v in args.face.split(","))
        span = tuple(float(v) for v in args.span.split(",")) if args.span else None
        voices = dict(VOICES)
        for pair in filter(None, (args.voices or "").split(",")):
            k, v = pair.split("=", 1)
            voices[k.strip()] = v.strip()
        print(render(args.video, args.out, voices=voices, face=face, intro=round(args.intro, 1), span=span,
                     probe=PROBES[args.probe] if args.probe else None, tag=args.tag))
    elif args.cmd == "mine":
        result = mine(args.video)
        text = json.dumps(result, ensure_ascii=False, indent=1)
        if args.out:
            args.out.write_text(text, encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
