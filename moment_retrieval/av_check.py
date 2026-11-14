"""Check a rendered MP4's audio timestamps before it is uploaded.

Some ffmpeg builds give the frames coming out of a one-pass ``loudnorm`` (and the
first frame of a linear one) timestamps that do not follow their sample count:
the first ~3 s of audio got times squeezed into ~0.6 s.  Local players play the
samples in order and sound fine, but YouTube re-encodes by timestamp and garbles
the opening.  Renders therefore end their audio chain with ``AUDIO_RETIME`` and
are checked here: every AAC packet except the encoder's priming packet and the
last one must last one frame (1024 samples; ±2 samples of rounding, as plain
re-encodes of 44.1 kHz sources show, is harmless), and audio and video must be
equally long (within ``MAX_LENGTH_GAP`` s).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

# Renumber audio times from the sample count (contiguous, starting at 0).
AUDIO_RETIME = "asetpts=N/SR/TB"
AAC_FRAME = 1024
ROUNDING = 2  # samples (plain re-encodes show 1022/1026 pairs)
MAX_LENGTH_GAP = 0.1


class AudioTimingError(RuntimeError):
    pass


def _ffprobe(path: Path, *args: str) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", *args, "-of", "json", str(Path(path).resolve())],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
                            timeout=120)
    return json.loads(result.stdout or "{}")


def packet_problems(durations: list[int], frame: int = AAC_FRAME,
                    rounding: int = ROUNDING) -> list[tuple[int, int]]:
    """(index, duration) of packets other than the first and last that are not one frame long."""
    return [(index, value) for index, value in enumerate(durations)
            if 0 < index < len(durations) - 1 and abs(value - frame) > rounding]


def length_gap(streams: list[dict]) -> float | None:
    """|video − audio| duration in seconds, None when either is missing."""
    lengths = {}
    for stream in streams:
        kind = stream.get("codec_type")
        try:
            lengths.setdefault(kind, float(stream["duration"]))
        except (KeyError, TypeError, ValueError):
            continue
    if "video" not in lengths or "audio" not in lengths:
        return None
    return abs(lengths["video"] - lengths["audio"])


def audio_timing_problems(path: Path) -> list[str]:
    """Human-readable problems with ``path``'s audio timing ([] when fine or when it has no audio)."""
    streams = _ffprobe(path, "-show_entries", "stream=codec_type,codec_name,duration").get("streams", [])
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not audio:
        return []
    problems = []
    if audio[0].get("codec_name") == "aac":
        packets = _ffprobe(path, "-select_streams", "a:0", "-show_entries", "packet=duration").get("packets", [])
        bad = packet_problems([int(packet.get("duration") or 0) for packet in packets])
        if bad:
            sample = ", ".join(f"#{index}={value}" for index, value in bad[:5])
            problems.append(f"音声の時刻が {len(bad)} か所で乱れています（{sample}）")
    gap = length_gap(streams)
    if gap is not None and gap > MAX_LENGTH_GAP:
        problems.append(f"映像と音声の長さが {gap:.2f} 秒ずれています")
    return problems


def verify_audio_timing(path: Path) -> None:
    problems = audio_timing_problems(path)
    if problems:
        raise AudioTimingError(f"{Path(path).name}: " + "／".join(problems))
