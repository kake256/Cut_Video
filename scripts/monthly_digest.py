#!/usr/bin/env python
"""A month's digest: short moments from that month's streams, joined into one vertical video.

``pool`` lists the highlight candidates of the month's streams, with views and the
share kept past the opening for the ones already posted (data/channel_performance.json),
as the basis for choosing scenes.  ``render`` cuts the chosen scenes from the original
streams, snaps each to word boundaries, captions them like a finished Short, and joins them.

    venv\\Scripts\\python.exe scripts\\monthly_digest.py pool 2026-10
    venv\\Scripts\\python.exe scripts\\monthly_digest.py render scenes.json --out exports\\digest.mp4

scenes.json: [{"video_id": "vid_…", "start": 3168.0, "end": 3177.5, "label": "…", "captions": ["…"]}, …]
The first scene should be the one whose opening holds viewers best.
"""
from __future__ import annotations

import argparse
import functools
import glob
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from moment_retrieval import channel_performance, config, db, finishing  # noqa: E402
from moment_retrieval.video_encoder import h264_args  # noqa: E402

# Caption lines here are written by hand from the transcript, so a misheard word
# ("超反" for "丁半") must not throw the whole set away as the AI-line check would.
finishing.captions_from_ai_lines = functools.partial(finishing.captions_from_ai_lines, minimum_similarity=0.3)

LEAD = 0.15  # before the first word
TAIL = 0.35  # after the last word


def _posted_ranges() -> list[tuple[str, float, float, dict | None]]:
    """(library video id, start, end, performance) of every posted clip with a known source range."""
    perf = {video["id"]: video for video in json.loads(channel_performance.cache_path().read_text(encoding="utf-8"))["videos"]}
    result = []
    for path in (config.CACHE_ROOT / "auto_jobs").glob("*.json"):
        job = json.loads(path.read_text(encoding="utf-8"))
        for upload in job.get("uploads") or []:
            if not (isinstance(upload, dict) and upload.get("video_id") and upload.get("file")):
                continue
            pattern = str(config.ARTIFACT_ROOT / "highlights" / "*" / glob.escape(upload["file"].replace(".mp4", ".metadata.json")))
            for meta in glob.glob(pattern):
                source = json.loads(Path(meta).read_text(encoding="utf-8")).get("source_range")
                if source:
                    result.append((source["video_id"], source["start_sec"], source["end_sec"], perf.get(upload["video_id"])))
    return result


def pool(month: str) -> None:
    posted = _posted_ranges()
    conn = sqlite3.connect(config.DB_PATH)
    rows = conn.execute(
        "select v.video_id, v.path, hc.start_sec, hc.end_sec, hc.title from highlight_candidates hc "
        "join highlight_runs h using(highlight_run_id) join videos v on v.video_id = h.video_id "
        "where v.path like ? order by v.path, hc.start_sec", (f"%{month.replace('-', '')}%",)).fetchall()
    seen: list[tuple[str, float]] = []
    for video_id, path, start, end, title in rows:
        if any(seen_id == video_id and abs(start - seen_start) < 20 for seen_id, seen_start in seen):
            continue
        seen.append((video_id, start))
        hits = [perf for vid, a, b, perf in posted if vid == video_id and min(end, b) - max(start, a) > 10]
        note = ""
        if hits and hits[0]:
            note = f"  [投稿済 {hits[0]['views']}回 冒頭残存{hits[0].get('kept', 0):.0%}]"
        elif hits:
            note = "  [投稿済]"
        print(f"{Path(path).name[4:8]} {video_id} {start:7.0f}-{end:7.0f}  {title}{note}")


WIDE_W, WIDE_H, BAND_H = 1920, 1080, 108  # wide layout: a title band on top, the stream below it


def _scene_words(conn, scene: dict) -> tuple[Path, float, float, list]:
    """The source, the range snapped to word boundaries, and the words relative to its start."""
    video = db.get_video(conn, scene["video_id"])
    source = Path(video["path"])
    start, end = float(scene["start"]), float(scene["end"])
    segments = [dict(row) for row in conn.execute(
        "select start_sec, end_sec, text, words_json from asr_segments where video_id = ? and end_sec > ? and start_sec < ?",
        (scene["video_id"], start - 5, end + 5))]
    words, _ = finishing.words_for_clip(source, segments, start - 1.0, end + 1.0)
    words = [finishing.Word(word.start + start - 1.0, word.end + start - 1.0, word.text) for word in words]
    inside = [word for word in words if word.end > start + 0.1 and word.start < end - 0.1]
    if inside:  # never open or close in the middle of a word
        start = max(start - 0.6, inside[0].start - LEAD)
        end = min(end + 0.8, inside[-1].end + TAIL)
    clip_words = [finishing.Word(word.start - start, word.end - start, word.text)
                  for word in words if word.end > start and word.start < end]
    return source, start, end, clip_words


def _scene_clip(conn, scene: dict, out: Path) -> float:
    source, start, end, clip_words = _scene_words(conn, scene)
    # Hand-checked caption lines fix misheard words; they are timed against the heard words.
    plan = finishing.FinishPlan(hook_text=scene.get("label", ""), caption_lines=list(scene.get("captions") or []))
    finishing.render(source, start, end, clip_words, plan, out)
    return end - start


def _caption_lines(scene: dict, words: list, length: float, agent: str) -> list[str]:
    """Caption lines from the scene file, else corrected by the AI the way finished Shorts are."""
    if scene.get("captions"):
        return list(scene["captions"])
    try:
        raw = finishing.request_plan(agent, "", finishing.plan_prompt(
            scene.get("label", ""), finishing.captions_from_words(words), length))
        return list(finishing.validate_plan(raw, length).caption_lines)
    except Exception as exc:  # the heard words still make usable captions
        print(f"  字幕の手直しを取得できなかったため、聞き取りのまま出します（{type(exc).__name__}）")
        return []


def _band_styles(font: str) -> str:
    return (f"Style: Band,{font},50,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,2,0,1,0,0,4,0,0,0,1\n"
            f"Style: Tab,{font},34,&H00141414,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,2,0,1,0,0,4,0,0,0,1\n"
            f"Style: Shape,{font},20,{ACCENT},&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n")


def _band_events(scene: dict, label: str, length: float) -> str:
    """The title band: a slanted yellow tab naming the digest, a thin yellow rule below, and to the
    right of the tab the scene in large type under the source stream's title in small grey."""
    t = finishing._ass_time
    span = f"{t(0)},{t(int(length * 1000))}"
    tab = 36 + len(label) * 35 + 50 if label else 0
    lines = [rf"Dialogue: 1,{span},Shape,,0,0,0,,{{\an7\pos(0,0)\p1}}"
             rf"m 0 {BAND_H - 4} l {WIDE_W} {BAND_H - 4} l {WIDE_W} {BAND_H} l 0 {BAND_H}{{\p0}}"]
    if label:
        lines += [rf"Dialogue: 1,{span},Shape,,0,0,0,,{{\an7\pos(0,0)\p1}}m 0 0 l {tab} 0 l {tab - 30} {BAND_H} l 0 {BAND_H}{{\p0}}",
                  rf"Dialogue: 2,{span},Tab,,0,0,0,,{{\pos(36,{BAND_H // 2})}}{finishing._ass_text(label)}"]
    x = max(40, tab + 20)
    stream = finishing._ass_text(scene.get("stream_title", ""))
    if label and scene.get("label"):
        lines += [rf"Dialogue: 2,{span},Band,,0,0,0,,{{\pos({x},32)\fs26\b0\1c&H00B0B0B0&}}配信「{stream}」より",
                  rf"Dialogue: 2,{span},Band,,0,0,0,,{{\pos({x},72)\fs44}}{finishing._ass_text(scene['label'])}"]
    else:
        lines.append(rf"Dialogue: 2,{span},Band,,0,0,0,,{{\pos({x},{BAND_H // 2 + 2})}}{stream}")
    return "\n".join(lines) + "\n"


def _wide_clip(conn, scene: dict, out: Path, agent: str, label: str = "") -> float:
    """One scene on a 1920x1080 canvas: the stream title in a band on top, the stream fitted below.

    ``label`` (the digest's name, such as "10月まとめ") goes in a yellow tab at the band's left end.
    """
    from moment_retrieval import speech_regions

    source = Path(db.get_video(conn, scene["video_id"])["path"])
    timing = scene.get("timing") or {}
    if timing.get("range") == [scene["start"], scene["end"]]:
        # Same range as last time: reuse the heard words and speech regions instead of listening again.
        start, end = timing["start"], timing["end"]
        words = [finishing.Word(*item) for item in timing["words"]]
        speech = [tuple(item) for item in timing["speech"]]
    else:
        source, start, end, words = _scene_words(conn, scene)
        try:
            speech = [(a - start, b - start) for a, b in speech_regions.detect(source, start, end) if b > start and a < end]
        except Exception:
            speech = []  # Whisper's word times are close enough
        scene["timing"] = {"range": [scene["start"], scene["end"]], "start": start, "end": end,
                           "words": [[w.start, w.end, w.text] for w in words], "speech": [list(r) for r in speech]}
    length = end - start
    lines = _caption_lines(scene, words, length, agent)
    scene["captions"] = lines  # saved back, so the next render reuses (and can be fixed by hand)
    captions = (finishing.captions_from_ai_lines(words, lines) if lines else None) or finishing.captions_from_words(words)
    if speech:
        captions = finishing.snap_captions_to_speech(captions, speech)
    ass = finishing.build_ass(captions, finishing.FinishPlan(), WIDE_W, WIDE_H, length)
    font = ass.split("Style: Default,", 1)[1].split(",", 1)[0]
    ass = ass.replace("[Events]", _band_styles(font) + "\n[Events]", 1) + _band_events(scene, label, length)
    video_w, video_h = (WIDE_H - BAND_H) * 16 // 9, WIDE_H - BAND_H
    # Dropping to 30 fps first halves the work of every filter after it (the sources are 60 fps).
    graph = (f"[0:v]fps=30,split=2[bg_src][fg_src];[bg_src]{finishing.blurred_background(WIDE_W, WIDE_H)}[bg];"
             f"[fg_src]scale={video_w}:{video_h}:force_original_aspect_ratio=decrease,setsar=1[fg];"
             f"[bg][fg]overlay=(W-w)/2:{BAND_H}+({video_h}-h)/2,"
             f"drawbox=x=0:y=0:w={WIDE_W}:h={BAND_H}:color=0x181818:t=fill,setsar=1,"
             f"subtitles=filename=wide.ass[vout];[0:a]aformat=sample_rates=48000:channel_layouts=stereo[aout]")
    out = out.resolve()
    with tempfile.TemporaryDirectory(prefix="cut_digest_wide_", dir=str(out.parent)) as tmp:
        Path(tmp, "wide.ass").write_text(ass, encoding="utf-8")
        decode = ["-hwaccel", "cuda"] if "nvenc" in " ".join(h264_args(crf=21, preset="medium")) else []
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *decode, "-ss", f"{start:.3f}", "-i", str(source.resolve()),
                        "-t", f"{length:.3f}", "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
                        "-r", "30", *h264_args(crf=21, preset="medium"),
                        "-c:a", "aac", "-b:a", "192k", str(out)], cwd=tmp, check=True, capture_output=True)
    return length


TITLE_CARD_SEC = 3.4
ACCENT = "&H0035D7FF&"  # warm yellow (ASS is BGR)


def _opening_ass(head: dict, card: float, total: float) -> str:
    """Animated title card first (name, month, a growing accent bar), then a corner badge over the cuts."""
    t = finishing._ass_time
    end = t(int(card * 1000))
    month = head.get("month_label", "")
    name, english, tagline = head["title"], head.get("english", ""), head.get("tagline", "")
    font = "Yu Gothic UI"
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {WIDE_W}", f"PlayResY: {WIDE_H}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Name,{font},176,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,6,0,1,0,6,5,0,0,0,1",
        f"Style: Small,{font},38,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,22,0,1,0,0,5,0,0,0,1",
        f"Style: Month,{font},92,&H00141414,&H000000FF,{ACCENT},&H00000000,-1,0,0,0,100,100,4,0,3,18,0,5,0,0,0,1",
        f"Style: Badge,{font},40,&H00FFFFFF,&H000000FF,&H00141414,&H00000000,-1,0,0,0,100,100,3,0,3,14,0,7,0,0,0,1",
        f"Style: Bar,{font},20,{ACCENT},&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        # thin bars that grow outwards from the centre, above and below the name
        rf"Dialogue: 0,{t(0)},{end},Bar,,0,0,0,,{{\an5\pos(960,330)\fscx0\t(0,600,\fscx100)\fad(0,250)\p1}}"
        r"m -300 0 l 300 0 l 300 6 l -300 6{\p0}",
        rf"Dialogue: 0,{t(0)},{end},Bar,,0,0,0,,{{\an5\pos(960,775)\fscx0\t(150,750,\fscx100)\fad(0,250)\p1}}"
        r"m -300 0 l 300 0 l 300 6 l -300 6{\p0}",
        rf"Dialogue: 1,{t(150)},{end},Small,,0,0,0,,{{\pos(960,385)\alpha&HFF&\t(0,400,\alpha&H40&)\fad(0,250)}}{english}",
        rf"Dialogue: 1,{t(250)},{end},Name,,0,0,0,,{{\move(960,560,960,520,0,450)\fad(300,250)"
        rf"\fscx108\fscy108\t(0,450,\fscx100\fscy100)}}{name}",
        rf"Dialogue: 2,{t(700)},{end},Month,,0,0,0,,{{\pos(960,680)\fad(0,250)\fscx60\fscy60"
        rf"\t(0,160,\fscx106\fscy106)\t(160,260,\fscx100\fscy100)}}{month}",
        rf"Dialogue: 1,{t(1000)},{end},Small,,0,0,0,,{{\pos(960,820)\fad(400,250)\fs30}}{tagline}",
        # over the quick cuts: which digest this is, in the top-left corner
        rf"Dialogue: 0,{end},{t(int(total * 1000))},Badge,,0,0,0,,{{\pos(56,48)\fad(200,0)}}{name}  {{\c{ACCENT}}}{month}",
    ]
    return "\n".join(lines) + "\n"


def opening(conn, head: dict, out: Path) -> float:
    """A wide opening: an animated title card saying whose digest and which month, then one
    short shout from each scene in quick cuts.

    ``head["teasers"]`` are {"video_id", "start", "end"} in source seconds, about a second each.
    """
    teasers = head["teasers"]
    card = TITLE_CARD_SEC
    first = teasers[0]
    sources = [Path(db.get_video(conn, teaser["video_id"])["path"]).resolve() for teaser in teasers]
    # The card's background is the first scene itself, blurred and dimmed, so it is not a dead still.
    inputs = ["-ss", f"{float(first['start']) - card:.3f}", "-t", f"{card:.3f}", "-i", str(sources[0])]
    video = [f"[0:v]fps=30,scale={WIDE_W}:{WIDE_H}:force_original_aspect_ratio=increase,crop={WIDE_W}:{WIDE_H},"
             f"boxblur=24:3,eq=brightness=-0.32:saturation=0.55,setsar=1[tv0]"]
    audio = ["anullsrc=r=48000:cl=stereo,atrim=duration=" + f"{card:.3f}[ta0]"]
    effects = [f"{finishing._SFX_SOURCES['rise']},aformat=sample_rates=48000:channel_layouts=stereo,volume=0.6[fx0]",
               f"{finishing._SFX_SOURCES['thud']},aformat=sample_rates=48000:channel_layouts=stereo,"
               f"adelay=700|700,volume=0.8[fx1]"]
    offset = card
    for index, (teaser, source) in enumerate(zip(teasers, sources), start=1):
        length = float(teaser["end"]) - float(teaser["start"])
        inputs += ["-ss", f"{float(teaser['start']):.3f}", "-t", f"{length:.3f}", "-i", str(source)]
        video.append(f"[{index}:v]fps=30,scale={WIDE_W}:{WIDE_H}:force_original_aspect_ratio=decrease,"
                     f"pad={WIDE_W}:{WIDE_H}:(ow-iw)/2:(oh-ih)/2,setsar=1[tv{index}]")
        audio.append(f"[{index}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                     f"afade=in:d=0.04,afade=out:st={length - 0.06:.3f}:d=0.06[ta{index}]")
        delay = int(offset * 1000)
        effects.append(f"{finishing._SFX_SOURCES['whoosh']},aformat=sample_rates=48000:channel_layouts=stereo,"
                       f"adelay={delay}|{delay},volume=0.45[fx{index + 1}]")
        offset += length
    total = offset
    count = len(teasers) + 1
    pairs = "".join(f"[tv{i}][ta{i}]" for i in range(count))
    mixes = "".join(f"[fx{i}]" for i in range(len(effects)))
    out = out.resolve()
    with tempfile.TemporaryDirectory(prefix="cut_digest_opening_", dir=str(out.parent)) as tmp:
        Path(tmp, "opening.ass").write_text(_opening_ass(head, card, total), encoding="utf-8")
        graph = (";".join(video + audio + effects) + ";" + pairs + f"concat=n={count}:v=1:a=1[cv][ca];"
                 f"[cv]subtitles=filename=opening.ass,fade=in:st=0:d=0.3,"
                 f"fade=out:st={total - 0.12:.3f}:d=0.12:color=white[vout];"
                 f"[ca]{mixes}amix=inputs={len(effects) + 1}:duration=first:normalize=0,alimiter=limit=0.95[aout]")
        subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[vout]",
                        "-map", "[aout]", *h264_args(crf=21, preset="medium"), "-c:a", "aac", "-b:a", "192k",
                        str(out)], cwd=tmp, check=True)
    return total


def render(scenes_path: Path, out: Path, *, layout: str = "short", agent: str = "codex") -> None:
    data = json.loads(scenes_path.read_text(encoding="utf-8"))
    # A plain list is just the scenes; {"opening": {...}, "scenes": [...]} adds a wide opening.
    scenes = data["scenes"] if isinstance(data, dict) else data
    head = data.get("opening") if isinstance(data, dict) and layout == "wide" else None
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cut_digest_", dir=str(out.parent)) as tmp:
        parts, total = [], 0.0
        if head:
            part = Path(tmp) / "opening.mp4"
            length = opening(conn, head, part)
            total += length
            parts.append((part, length))
            print(f"{length:5.1f}s  オープニング")
        for index, scene in enumerate(scenes):
            part = Path(tmp) / f"{index:02d}.mp4"
            if layout == "wide":
                length = _wide_clip(conn, scene, part, agent, (head or {}).get("month_label", ""))
                scenes_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            else:
                length = _scene_clip(conn, scene, part)
            total += length
            parts.append((part, length))
            print(f"{length:5.1f}s  {scene.get('label', '')}")
        inputs, filters = [], []
        for index, (part, length) in enumerate(parts):
            inputs += ["-i", str(part)]
            filters.append(f"[{index}:v]fade=in:st=0:d=0.12,fade=out:st={length - 0.12:.3f}:d=0.12[v{index}];"
                           f"[{index}:a]afade=in:st=0:d=0.06,afade=out:st={length - 0.1:.3f}:d=0.1[a{index}]")
        graph = ";".join(filters) + ";" + "".join(f"[v{i}][a{i}]" for i in range(len(parts))) \
            + f"concat=n={len(parts)}:v=1:a=1[v][a]"
        subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                        *h264_args(crf=21, preset="medium"),
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)], check=True)
    print(f"合計 {total:.1f}秒 -> {out}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pool").add_argument("month", help="YYYY-MM")
    render_parser = commands.add_parser("render")
    render_parser.add_argument("scenes", type=Path)
    render_parser.add_argument("--out", type=Path, required=True)
    render_parser.add_argument("--layout", choices=["short", "wide"], default="short",
                               help="short: vertical 1080x1920; wide: 1920x1080 with the stream title on top")
    render_parser.add_argument("--agent", default="codex", help="AI that corrects captions for the wide layout")
    args = parser.parse_args()
    if args.command == "pool":
        pool(args.month)
    else:
        render(args.scenes, args.out, layout=args.layout, agent=args.agent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
