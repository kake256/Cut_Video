"""Choose the H.264 encoder for rendered clips: NVIDIA NVENC when it works, else libx264.

NVENC needs a driver new enough for the installed ffmpeg (the 2025 builds need
driver 570+), so it is probed once with a tiny test encode instead of trusting
``ffmpeg -encoders``.  ``CUT_VIDEO_ENCODER`` forces ``cpu`` or ``nvenc``.
"""
from __future__ import annotations

import os
import subprocess
import threading

_lock = threading.Lock()
_nvenc_ok: bool | None = None


def _probe_nvenc() -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "color=c=black:s=256x256:r=30:d=0.2", "-pix_fmt", "yuv420p",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def nvenc_available() -> bool:
    mode = os.environ.get("CUT_VIDEO_ENCODER", "auto").strip().lower()
    if mode == "cpu":
        return False
    if mode == "nvenc":
        return True
    global _nvenc_ok
    with _lock:
        if _nvenc_ok is None:
            _nvenc_ok = _probe_nvenc()
        return _nvenc_ok


def h264_args(*, crf: int, preset: str, pixel_format: str = "yuv420p") -> list[str]:
    """ffmpeg video-encoder arguments at roughly the quality ``crf`` gives libx264."""
    if nvenc_available():
        # Measured on a 2-min 1080x1920 60 fps clip: cq = crf + 8 gives the same file size
        # as libx264 at that crf, with SSIM 0.99 between the two.
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(int(crf) + 8),
                "-b:v", "0", "-pix_fmt", pixel_format]
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(int(crf)), "-pix_fmt", pixel_format]
