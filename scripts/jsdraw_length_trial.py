"""JS描画ショートの長さ比較（58秒版と90秒版）。Claude Code のログイン枠だけで描き、アップロードはしない。

使い方（data/ のあるいつものフォルダで。--code はこのブランチを置いたフォルダ）:
  venv\\Scripts\\python.exe <code>\\scripts\\jsdraw_length_trial.py --code <code> --count 3 --out D:\\myapp\\jsdraw_trial
data/ は読むだけ（素材の作業フォルダ data/jsdraw/ だけはふだんどおり作られる）。結果は --out に置く。
"""
import argparse
import json
import sys
import time
import traceback
from pathlib import Path

_code = sys.argv[sys.argv.index("--code") + 1] if "--code" in sys.argv else str(Path.cwd())
sys.path.insert(0, _code)
from moment_retrieval import api_keys, jsdraw_pipeline, jsdraw_short, made_clips  # noqa: E402


def candidates(count):
    picked, seen = [], set()
    for clip in made_clips.list_clips():
        try:
            video_id, start, end, _meta = jsdraw_pipeline.source_range(clip.path)
            video, _segments = jsdraw_pipeline._video_and_segments(video_id)
        except Exception:
            continue
        media = str(video.get("path") or "")
        if not media or not Path(media).is_file() or (video_id, round(start)) in seen:
            continue
        seen.add((video_id, round(start)))
        picked.append((clip, video_id, media))
        if len(picked) >= count:
            break
    return picked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--model", default="fable")
    parser.add_argument("--out", required=True)
    parser.add_argument("--code", default="")
    args = parser.parse_args()
    if not hasattr(jsdraw_short, "Length"):
        sys.exit("--code が長さ変更のブランチを指していません。")
    if api_keys.claude_cli_uses_key():
        sys.exit("Claude Code がAPIキーで動く設定です。従量課金を避けるため中止します。")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.json"
    results = json.loads(results_path.read_text(encoding="utf-8")) if results_path.is_file() else []
    done = {(r["clip"], r["max_video_sec"]) for r in results if r.get("ok")}
    for clip, video_id, media in candidates(args.count):
        title, story = jsdraw_pipeline.title_and_story(clip.path)
        for video_max in (58.0, 90.0):
            if (clip.path.name, video_max) in done:
                continue
            start, end = jsdraw_pipeline.range_for_clip(clip.path, video_max)
            end = min(end, start + jsdraw_pipeline.MAX_SEC)
            marks = {"start": time.time()}

            def progress(message, marks=marks):
                print(f"[{time.strftime('%H:%M:%S')}] {clip.path.stem[:30]} {video_max:g}s: {message}", flush=True)
                if message.startswith("生成中"):
                    marks.setdefault("generate", time.time())
                elif message.startswith("書き出し中"):
                    marks.setdefault("render", time.time())

            row = {"clip": clip.path.name, "title": title, "max_video_sec": video_max,
                   "range": [start, end], "ok": False}
            try:
                result = jsdraw_short.make_short(
                    media, start, end, model=args.model, title=title, story=story,
                    out_dir=out / f"{clip.path.stem}_{int(video_max)}s", progress=progress, video_id=video_id,
                    backend="claude", max_video_sec=video_max)
                now = time.time()
                row.update(ok=True, mp4=result["mp4"], video_seconds=result["seconds"],
                           segs_total_generated=result["segs_total_generated"],
                           usd_equivalent=result["usd_equivalent"],
                           step_minutes={u["step"]: u.get("minutes") for u in result["usage"]},
                           output_tokens={u["step"]: u.get("output_tokens") for u in result["usage"]},
                           prepare_min=round((marks.get("generate", now) - marks["start"]) / 60, 1),
                           render_min=round((now - marks.get("render", now)) / 60, 1),
                           total_min=round((now - marks["start"]) / 60, 1))
            except Exception as exc:
                row.update(error=f"{type(exc).__name__}: {exc}"[:500], trace=traceback.format_exc()[-1500:],
                           total_min=round((time.time() - marks["start"]) / 60, 1))
            results.append(row)
            results_path.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
            print(json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
