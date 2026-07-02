#!/usr/bin/env python
"""指定した時間区間を動画から切り出すCLI。search_video.pyの--cutからも呼ばれる。"""
import argparse
import subprocess
from pathlib import Path
from typing import Optional


def cut_clip(
    video_path: Path,
    start: float,
    end: float,
    output_path: Path,
    pad: float = 1.5,
    precise: bool = False,
    duration: Optional[float] = None,
) -> Path:
    s = max(0.0, start - pad)
    e = end + pad
    if duration is not None:
        e = min(e, duration)
    length = max(e - s, 0.1)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if precise:
        # -ssを-iの後に置き、再エンコードしてフレーム精度で切り出す(遅いが正確)
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-ss", f"{s:.3f}",
            "-t", f"{length:.3f}",
            "-c:v", "libx264", "-c:a", "aac",
            str(output_path),
        ]
    else:
        # -ssを-iの前に置き、ストリームコピーで高速に切り出す(キーフレーム単位でずれる場合あり)
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{s:.3f}",
            "-i", str(video_path),
            "-t", f"{length:.3f}",
            "-c", "copy",
            str(output_path),
        ]

    subprocess.run(cmd, check=True, capture_output=True)
    return output_path


def main():
    parser = argparse.ArgumentParser(description="動画から指定区間を切り出す")
    parser.add_argument("--video", required=True, type=Path)
    parser.add_argument("--start", required=True, type=float, help="開始秒")
    parser.add_argument("--end", required=True, type=float, help="終了秒")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pad", type=float, default=1.5, help="前後に足すパディング秒数")
    parser.add_argument(
        "--precise", action="store_true", help="再エンコードしてフレーム精度で切り出す(低速)"
    )
    args = parser.parse_args()

    cut_clip(
        args.video, args.start, args.end, args.output, pad=args.pad, precise=args.precise
    )
    print(f"書き出し完了: {args.output}")


if __name__ == "__main__":
    main()
