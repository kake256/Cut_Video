"""Detect where speech happens, and how loud the audio is, over time (Silero VAD bundled with faster-whisper).

Run as a separate process so the ONNX/ctranslate2 runtime never loads inside the
Gradio app process, matching the project's rule of keeping Whisper-related work
out of GUI worker threads.

* ``python -m moment_retrieval.speech_regions <source> <start> <end>`` prints a JSON
  list of ``[start, end]`` seconds relative to the clip start.
* ``python -m moment_retrieval.speech_regions --profile <source> <output.json>``
  writes the whole file's audio profile (speech regions and loudness every
  ``LOUDNESS_STEP`` seconds), streaming the audio in blocks so long streams fit in memory.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RATE = 16000
LOUDNESS_STEP = 0.1  # seconds per loudness sample
BLOCK_SEC = 600  # audio decoded and analysed 10 minutes at a time
SILENCE_DB = -90


def _vad_options():
    from faster_whisper.vad import VadOptions

    return VadOptions(threshold=0.5, min_speech_duration_ms=120, min_silence_duration_ms=250,
                      speech_pad_ms=60)


def _detect(source: str, start: float, end: float) -> list[list[float]]:
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import get_speech_timestamps

    with tempfile.TemporaryDirectory(prefix="cut_vad_") as tmp:
        wav = Path(tmp, "clip.wav")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", source,
                        "-t", f"{end - start:.3f}", "-vn", "-ac", "1", "-ar", str(RATE), str(wav)],
                       check=True, capture_output=True)
        audio = decode_audio(str(wav), sampling_rate=RATE)
    return [[round(item["start"] / RATE, 3), round(item["end"] / RATE, 3)]
            for item in get_speech_timestamps(audio, _vad_options())]


def loudness_db(samples, step: int) -> list[int]:
    """RMS level in dBFS (rounded) for each ``step`` samples; quiet parts floor at SILENCE_DB."""
    import numpy as np

    usable = len(samples) - len(samples) % step
    if usable <= 0:
        return []
    frames = samples[:usable].reshape(-1, step).astype(np.float64)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    levels = 20 * np.log10(np.maximum(rms, 10 ** (SILENCE_DB / 20)))
    return [int(round(value)) for value in levels]


def merge_regions(regions: list[list[float]], gap: float = 0.3) -> list[list[float]]:
    merged: list[list[float]] = []
    for start, end in sorted(regions):
        if merged and start - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _profile(source: str) -> dict:
    import numpy as np
    from faster_whisper.vad import get_speech_timestamps

    step = int(RATE * LOUDNESS_STEP)
    block_bytes = RATE * BLOCK_SEC * 2
    process = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", source, "-vn", "-ac", "1",
                                "-ar", str(RATE), "-f", "s16le", "-"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    regions: list[list[float]] = []
    loudness: list[int] = []
    offset = 0.0
    carry = b""
    try:
        while True:
            data = carry + process.stdout.read(block_bytes)
            if not data:
                break
            data, carry = data[:len(data) - len(data) % 2], data[len(data) - len(data) % 2:]
            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            # Loudness frames never straddle blocks: block_bytes is a multiple of the step.
            loudness.extend(loudness_db(samples, step))
            regions.extend([round(offset + item["start"] / RATE, 3), round(offset + item["end"] / RATE, 3)]
                           for item in get_speech_timestamps(samples, _vad_options()))
            offset += len(samples) / RATE
            if len(data) < block_bytes:
                break
    finally:
        process.stdout.close()
        process.wait()
    if process.returncode not in (0, None) and not loudness:
        raise RuntimeError("ffmpeg could not decode the audio")
    return {"version": 1, "duration": round(offset, 3), "loudness_step": LOUDNESS_STEP,
            "loudness_db": loudness, "speech": merge_regions(regions)}


def detect(source: Path, start: float, end: float, *, timeout: float = 600) -> list[tuple[float, float]]:
    """Speech regions of ``source[start:end]``, computed in a child process."""
    result = subprocess.run(
        [sys.executable, "-m", "moment_retrieval.speech_regions", str(source), f"{start}", f"{end}"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=True,
    )
    return [(float(a), float(b)) for a, b in json.loads(result.stdout.strip().splitlines()[-1])]


def build_profile(source: Path, output: Path, *, timeout: float = 7200) -> None:
    """Write the whole file's audio profile to ``output``, computed in a child process."""
    subprocess.run(
        [sys.executable, "-m", "moment_retrieval.speech_regions", "--profile", str(source), str(output)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=True,
    )


if __name__ == "__main__":
    if sys.argv[1] == "--profile":
        target = Path(sys.argv[3])
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(_profile(sys.argv[2]), separators=(",", ":")), encoding="utf-8")
        temporary.replace(target)
    else:
        print(json.dumps(_detect(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]))))
