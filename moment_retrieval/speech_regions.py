"""Detect where speech actually happens inside one clip (Silero VAD bundled with faster-whisper).

Run as a separate process (``python -m moment_retrieval.speech_regions <source> <start> <end>``)
so the ONNX/ctranslate2 runtime never loads inside the Gradio app process, matching the
project's rule of keeping Whisper-related work out of GUI worker threads.
Prints a JSON list of ``[start, end]`` seconds relative to the clip start.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _detect(source: str, start: float, end: float) -> list[list[float]]:
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    with tempfile.TemporaryDirectory(prefix="cut_vad_") as tmp:
        wav = Path(tmp, "clip.wav")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", source,
                        "-t", f"{end - start:.3f}", "-vn", "-ac", "1", "-ar", "16000", str(wav)],
                       check=True, capture_output=True)
        audio = decode_audio(str(wav), sampling_rate=16000)
    options = VadOptions(threshold=0.5, min_speech_duration_ms=120, min_silence_duration_ms=250,
                         speech_pad_ms=60)
    return [[round(item["start"] / 16000, 3), round(item["end"] / 16000, 3)]
            for item in get_speech_timestamps(audio, options)]


def detect(source: Path, start: float, end: float, *, timeout: float = 600) -> list[tuple[float, float]]:
    """Speech regions of ``source[start:end]``, computed in a child process."""
    result = subprocess.run(
        [sys.executable, "-m", "moment_retrieval.speech_regions", str(source), f"{start}", f"{end}"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=True,
    )
    return [(float(a), float(b)) for a, b in json.loads(result.stdout.strip().splitlines()[-1])]


if __name__ == "__main__":
    print(json.dumps(_detect(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]))))
