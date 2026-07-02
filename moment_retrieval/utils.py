import hashlib
import json
import subprocess
from pathlib import Path


def make_video_id(video_path: Path) -> str:
    """ファイルパスから安定した短いvideo_idを生成する(ファイル名+ハッシュ)。"""
    digest = hashlib.sha1(str(video_path.resolve()).encode("utf-8")).hexdigest()[:10]
    return f"{video_path.stem}_{digest}"


def probe_duration(video_path: Path) -> float:
    """ffprobeで動画の総再生時間(秒)を取得する。"""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def format_timestamp(sec: float) -> str:
    sec = max(sec, 0.0)
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    return f"{h:02d}:{int(m):02d}:{s:05.2f}"
