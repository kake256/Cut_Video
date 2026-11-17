"""JS描画ショートの顔カメラだけを作り直して、描画コード（scene.html）はそのまま書き出し直す。

顔カメラの切り出し位置が「1920x1080 の右下」で決め打ちだった頃に作ったショートのうち、
画面の作りが違う配信のものは顔の小窓が真っ暗になっている。AI には頼まず（費用なし）、
素材を作り直して同じ scene.html で描き直し、クリップを差し替える。

使い方（F:\\myapp\\cut で）:
  venv\\Scripts\\python.exe scripts\\jsdraw_rerender_face.py --check  <clip.mp4> ...   # 位置を探して確認画像だけ出す
  venv\\Scripts\\python.exe scripts\\jsdraw_rerender_face.py --backup D:\\myapp\\jsdraw_black_backup <clip.mp4> ...
  （--stand-in を付けると、配信者を探さずに data\\jsdraw_stand_in の写真を小窓に使う）

差し替える前の mp4 は --backup のフォルダへコピーする。素材フォルダ（data/jsdraw/<stem>）の顔カメラは上書きする。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from moment_retrieval import config  # noqa: E402
from moment_retrieval import jsdraw_short as js  # noqa: E402


def _duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def _workdir(record: dict) -> Path:
    """この切り抜きの素材フォルダ（元ファイル名_開始_終了）。元動画がまだあるものを選ぶ。"""
    start, end = record["range"]
    found = []
    for meta in sorted((Path(config.DATA_DIR) / "jsdraw").glob(f"*_{int(start)}_{int(end)}/meta.json")):
        video = json.loads(meta.read_text(encoding="utf-8")).get("video", "")
        if video and Path(video).is_file():
            found.append(meta.parent)
    if not found:
        raise js.JsDrawError(f"素材フォルダか元動画が見つかりません（{start:.0f}〜{end:.0f}秒）")
    return found[0]


def _scene(workdir: Path) -> Path:
    scenes = sorted(workdir.glob("scene_run_*.html"), key=lambda p: p.stat().st_mtime)
    if not scenes:
        raise js.JsDrawError(f"描画コードがありません: {workdir}")
    return scenes[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("clips", nargs="+", type=Path)
    ap.add_argument("--check", action="store_true", help="位置を探して確認画像を出すだけ（差し替えない）")
    ap.add_argument("--stand-in", action="store_true", help="配信者を探さず、別の配信の写真（data/jsdraw_stand_in）を使う")
    ap.add_argument("--backup", type=Path, help="差し替える前の mp4 のコピー先")
    ap.add_argument("--out", type=Path, default=Path("D:/myapp/jsdraw_black_check"), help="確認画像の置き場所")
    args = ap.parse_args()
    if not args.check and not args.backup:
        ap.error("差し替えるときは --backup を指定してください")
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for clip in args.clips:
        row = {"clip": str(clip)}
        try:
            record_path = clip.with_suffix(".jsdraw.json")
            record = json.loads(record_path.read_text(encoding="utf-8"))
            meta_clip = json.loads(clip.with_suffix(".metadata.json").read_text(encoding="utf-8"))
            video_id = str(meta_clip["source_range"]["video_id"])
            workdir = _workdir(record)
            old = json.loads((workdir / "meta.json").read_text(encoding="utf-8"))
            start, end, video = float(old["start"]), float(old["end"]), old["video"]
            row.update(workdir=str(workdir), video=video)
            if args.check:
                off = max(0.0, start - 8.0)
                crop = js.find_face_crop(video, off, end - off + 8.0, print)
                w, h, x, y = crop
                shot = args.out / f"{clip.stem}_newcrop.jpg"
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{(start + end) / 2:.1f}", "-i", video,
                                "-frames:v", "1", "-vf", f"crop={w}:{h}:{x}:{y},scale=440:320", str(shot)], check=True)
                row.update(crop=list(crop), image=str(shot), ok=True)
                print(clip.name, crop)
                results.append(row)
                continue
            scene = _scene(workdir)
            old_seconds = _duration(clip)
            length = js.Length(min(js.LONGEST_VIDEO_SEC, max(js.VIDEO_MAX_SEC, old_seconds + 0.05)))
            meta = js.prepare(video, start, end, workdir, print, video_id=video_id,
                              face="stand_in" if args.stand_in else "auto")
            if meta["face_mode"] == "none":
                raise js.JsDrawError("配信者が映っておらず写真も無いため、いまの描画コードでは小窓が空になります")
            tmp = clip.with_name(f".{clip.stem}.rerender.mp4")
            seconds = js.render(workdir, scene, tmp, print, length)
            args.backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(clip, args.backup / clip.name)
            tmp.replace(clip)
            record["face_rerender"] = {"face_mode": meta["face_mode"], "crop": meta.get("crop"),
                                       "face_coverage": meta.get("face_coverage"),
                                       "scene": scene.name, "seconds_before": round(old_seconds, 2)}
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            row.update(face_mode=meta["face_mode"], crop=meta.get("crop"), face_coverage=meta.get("face_coverage"),
                       seconds=round(seconds, 2),
                       seconds_before=round(old_seconds, 2), ok=True)
        except Exception as exc:  # 1本失敗しても残りは続ける
            row.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        print(json.dumps(row, ensure_ascii=False))
        results.append(row)
    (args.out / ("check.json" if args.check else "rerender.json")).write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
