#!/usr/bin/env python
"""指定した時間区間を動画から切り出すCLI。search_video.pyの--cutからも呼ばれる。"""
import argparse
import math
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple


ClipRange = Tuple[float, float]


def _remaining_timeout(deadline: Optional[float]) -> Optional[float]:
    """Return the remaining shared ffmpeg budget or raise consistently."""
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(["ffmpeg"], 0)
    return remaining


def validate_clip_ranges(
    ranges: Iterable[Sequence[float]],
    duration: Optional[float] = None,
) -> list[ClipRange]:
    """保持区間を検証し、floatの(start, end)リストとして返す。"""
    if duration is not None:
        duration = float(duration)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("動画の長さは正の有限値で指定してください")

    validated: list[ClipRange] = []
    previous_end: Optional[float] = None
    for index, item in enumerate(ranges, start=1):
        if len(item) != 2:
            raise ValueError(f"区間{index}は開始秒と終了秒の2値で指定してください")
        start, end = float(item[0]), float(item[1])
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError(f"区間{index}の開始秒と終了秒は有限値で指定してください")
        if start < 0:
            raise ValueError(f"区間{index}の開始秒は0以上にしてください")
        if end <= start:
            raise ValueError(f"区間{index}の終了秒は開始秒より後にしてください")
        if duration is not None and end > duration:
            raise ValueError(f"区間{index}の終了秒が動画の長さを超えています")
        if previous_end is not None and start < previous_end:
            raise ValueError("保持区間は時系列順かつ重ならないように指定してください")
        validated.append((start, end))
        previous_end = end

    if not validated:
        raise ValueError("保持区間を1つ以上指定してください")
    return validated


def _apply_outer_padding(
    ranges: list[ClipRange],
    pad: float,
    duration: Optional[float],
) -> list[ClipRange]:
    pad = float(pad)
    if not math.isfinite(pad) or pad < 0:
        raise ValueError("パディング秒数は0以上の有限値で指定してください")
    if pad == 0:
        return ranges

    padded = list(ranges)
    if len(padded) == 1:
        start, end = padded[0]
        return [(
            max(0.0, start - pad),
            min(end + pad, duration) if duration is not None else end + pad,
        )]

    first_start, first_end = padded[0]
    last_start, last_end = padded[-1]
    padded[0] = (max(0.0, first_start - pad), first_end)
    padded[-1] = (
        last_start,
        min(last_end + pad, duration) if duration is not None else last_end + pad,
    )
    return padded


def cut_clip(
    video_path: Path,
    start: float,
    end: float,
    output_path: Path,
    pad: float = 1.5,
    precise: bool = False,
    duration: Optional[float] = None,
    timeout_sec: Optional[float] = None,
) -> Path:
    s = max(0.0, start - pad)
    e = end + pad
    if duration is not None:
        e = min(e, duration)
    length = max(e - s, 0.1)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if precise:
        # -ssを-iの前に置く入力シーク + 再エンコード。
        # 再エンコード時は入力シークでもフレーム精度が保たれ、かつ
        # 目的位置まで瞬間シークするため長時間動画でも数秒で切り出せる
        # (-ssを-iの後に置くと先頭から全デコードになり12時間動画では数十分かかる)
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{s:.3f}",
            "-i", str(video_path),
            "-t", f"{length:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
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

    subprocess.run(cmd, check=True, capture_output=True, timeout=timeout_sec)
    return output_path


def cut_clips(
    video_path: Path,
    ranges: Iterable[Sequence[float]],
    output_path: Path,
    precise: bool = False,
    duration: Optional[float] = None,
    pad: float = 0.0,
    timeout_sec: Optional[float] = None,
) -> Path:
    """複数の保持区間を切り出し、時系列順に1本の動画へ連結する。

    単一区間は従来の :func:`cut_clip` と同じ処理を使う。複数区間の
    ``pad`` は削除した中間部分へ食い込まないよう、全体の先頭と末尾に
    だけ適用する。
    """
    deadline = (
        None if timeout_sec is None
        else time.monotonic() + float(timeout_sec)
    )
    normalized_duration = float(duration) if duration is not None else None
    checked_ranges = validate_clip_ranges(ranges, duration=normalized_duration)
    checked_ranges = _apply_outer_padding(checked_ranges, pad, normalized_duration)
    video_path = Path(video_path)
    output_path = Path(output_path)

    if len(checked_ranges) == 1:
        start, end = checked_ranges[0]
        return cut_clip(
            video_path,
            start,
            end,
            output_path,
            pad=0.0,
            precise=precise,
            duration=normalized_duration,
            timeout_sec=_remaining_timeout(deadline),
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="cut_video_", dir=str(output_path.parent)
    ) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        segment_paths: list[Path] = []
        for index, (start, end) in enumerate(checked_ranges):
            segment_path = temp_dir / f"segment_{index:04d}.mp4"
            cut_clip(
                video_path,
                start,
                end,
                segment_path,
                pad=0.0,
                precise=precise,
                duration=normalized_duration,
                timeout_sec=_remaining_timeout(deadline),
            )
            segment_paths.append(segment_path)

        concat_list = temp_dir / "concat.txt"
        concat_list.write_text(
            "".join(f"file '{path.name}'\n" for path in segment_paths),
            encoding="utf-8",
        )
        staged_output = temp_dir / f"joined{output_path.suffix or '.mp4'}"
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(staged_output),
        ]
        subprocess.run(
            cmd, check=True, capture_output=True,
            timeout=_remaining_timeout(deadline),
        )
        staged_output.replace(output_path)

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
