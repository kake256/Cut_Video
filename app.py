#!/usr/bin/env python
"""動画シーン検索GUI (Gradio)。

    python app.py

「検索・切り抜き」タブ: 検索 → 結果選択 → プレビュー → 手動調整 → 保存。
「動画の追加」タブ: 新規動画の文字起こし〜インデックス化をWebUIから実行。
"""
import faulthandler
import hashlib
import math
import subprocess
import threading
from pathlib import Path

import gradio as gr

# ネイティブ層のクラッシュ(access violation等)の原因追跡用
_crash_log = open("data/crash_trace.log", "a", encoding="utf-8", errors="replace")
faulthandler.enable(file=_crash_log)

from cut_clip import cut_clip, cut_clips
from moment_retrieval import config, db, utils
from moment_retrieval.downloader import DownloadError, download_video
from moment_retrieval.embedder import TextEmbedder
from moment_retrieval.refine import expand_to_speech_boundary
from moment_retrieval.search import search_chunks
from moment_retrieval.share import ShareError, export_index, import_index
from moment_retrieval.vector_index import VectorIndex

PREVIEW_DIR = Path("data/previews")
THUMBNAIL_DIR = Path("data/thumbnails")
ALL_VIDEOS_IMAGE = Path("assets/all_videos.svg")
VIDEO_UNAVAILABLE_IMAGE = Path("assets/video_unavailable.svg")
DEFAULT_CLIPS_DIR = "clips"
APP_PORT = 7860
INDEX_JOB_PIDFILE = Path("data/index_job.pid")
ALL_VIDEOS_VALUE = "__all_videos__"

ADJUST_STEPS = [0.1, 1.0, 10.0, 30.0, 60.0, 600.0]

_embedder = None
_index_lock = threading.Lock()
# 実行中のインデックス処理サブプロセス (停止ボタン用)
_index_state = {"proc": None, "stopped": False}


def get_embedder() -> TextEmbedder:
    global _embedder
    if _embedder is None:
        _embedder = TextEmbedder()
    return _embedder


# ---------- ネイティブのファイル/フォルダ選択ダイアログ (ローカルアプリ前提) ----------

def _tk_dialog(kind: str) -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if kind == "folder":
            path = filedialog.askdirectory(title="保存先フォルダを選択")
        else:
            path = filedialog.askopenfilename(
                title="動画ファイルを選択",
                filetypes=[("動画", "*.mp4 *.mkv *.webm *.mov *.avi *.ts *.m4a *.mp3 *.wav"), ("すべて", "*.*")],
            )
    finally:
        root.destroy()
    return path or ""


def browse_folder(current: str) -> str:
    path = _tk_dialog("folder")
    return path if path else current


def browse_video(current: str) -> str:
    path = _tk_dialog("file")
    return path if path else current


# ---------- 検索・切り抜き ----------

def list_video_choices() -> list:
    conn = db.get_conn()
    db.init_db(conn)
    videos = db.list_videos(conn)
    conn.close()
    # Dropdownには「表示名, 内部値」の組を渡す。内部IDを先頭に表示すると
    # 動画を見分けにくいため、利用者にはファイル名と長さだけを見せる。
    choices = [("すべての動画", ALL_VIDEOS_VALUE)]
    for v in videos:
        name = Path(v["path"]).name
        label = f"{name}  —  {utils.format_timestamp(v['duration'])}"
        choices.append((label, v["video_id"]))
    return choices


def list_video_choices_only() -> list:
    """「すべての動画」を除いた個別動画の選択肢一覧(共有タブ用)。"""
    return list_video_choices()[1:]


def parse_video_choice(choice: str) -> str:
    if not choice or choice == ALL_VIDEOS_VALUE or choice.startswith("("):
        return None
    # 旧UIの「video_id  (filename, duration)」形式も受け付ける。
    return choice.split()[0] if "  (" in choice else choice


def _thumbnail_path(video: dict) -> Path:
    """動画の内容が変わった場合だけ作り直すサムネイルの保存先を返す。"""
    source = Path(video["path"])
    try:
        stamp = source.stat().st_mtime_ns
    except OSError:
        stamp = 0
    key = hashlib.sha256(
        f"{video['video_id']}|{source}|{stamp}".encode("utf-8")
    ).hexdigest()[:20]
    return THUMBNAIL_DIR / f"{key}.jpg"


def _make_video_thumbnail(video: dict) -> str | None:
    """冒頭の黒画面を避けた位置からローカル用サムネイルを生成する。"""
    source = Path(video["path"])
    if not source.exists():
        return None
    output = _thumbnail_path(video)
    if output.exists():
        return str(output.resolve())

    THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
    duration = float(video.get("duration") or 0)
    seek = min(30.0, max(1.0, duration * 0.1)) if duration > 0 else 1.0
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-ss", f"{seek:.3f}", "-i", str(source),
            "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "3", str(output),
        ],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not output.exists():
        output.unlink(missing_ok=True)
        return None
    return str(output.resolve())


def selected_video_info(video_choice: str):
    """検索前に、現在選択している対象動画を視覚的に確認できるようにする。"""
    video_id = parse_video_choice(video_choice)
    if not video_id:
        return gr.update(value=None, visible=False), "**検索対象:** すべての動画"

    conn = db.get_conn()
    try:
        video = db.get_video(conn, video_id)
    finally:
        conn.close()
    if not video:
        return gr.update(value=None, visible=False), "対象動画の情報を取得できませんでした。"

    name = Path(video["path"]).name
    duration = utils.format_timestamp(video["duration"])
    thumbnail = _make_video_thumbnail(video)
    detail = f"**検索対象:** {name}  \n**長さ:** {duration}"
    return gr.update(value=thumbnail, visible=bool(thumbnail)), detail


def build_video_gallery(filter_text: str = "", selected_video_id: str = ALL_VIDEOS_VALUE):
    """サムネイル付きの動画選択メニューと、各カードに対応する内部IDを返す。"""
    conn = db.get_conn()
    try:
        db.init_db(conn)
        videos = db.list_videos(conn)
    finally:
        conn.close()

    needle = (filter_text or "").strip().casefold()
    items = [(str(ALL_VIDEOS_IMAGE.resolve()), f"すべての動画（{len(videos)}本）")]
    video_ids = [ALL_VIDEOS_VALUE]
    for video in videos:
        name = Path(video["path"]).name
        if needle and needle not in name.casefold():
            continue
        thumbnail = _make_video_thumbnail(video)
        image = thumbnail or str(VIDEO_UNAVAILABLE_IMAGE.resolve())
        caption = f"{name}\n{utils.format_timestamp(video['duration'])}"
        items.append((image, caption))
        video_ids.append(video["video_id"])

    try:
        selected_index = video_ids.index(selected_video_id)
    except ValueError:
        selected_index = None
    return gr.update(value=items, selected_index=selected_index), video_ids


def select_video_from_gallery(video_ids: list[str], evt: gr.SelectData):
    """カードを検索対象にして選択中表示を更新する。"""
    index = evt.index[0] if isinstance(evt.index, (tuple, list)) else evt.index
    if not isinstance(index, int) or not 0 <= index < len(video_ids):
        raise gr.Error("動画を選択できませんでした。一覧を更新してください。")
    video_id = video_ids[index]
    thumbnail_update, detail = selected_video_info(video_id)
    return video_id, thumbnail_update, detail, gr.update(selected_index=index)


def region_transcript(conn, video_id: str, start: float, end: float) -> str:
    rows = conn.execute(
        "SELECT text FROM asr_segments WHERE video_id = ? AND end_sec > ? AND start_sec < ? "
        "ORDER BY start_sec",
        (video_id, start, end),
    ).fetchall()
    return " ".join(r["text"] for r in rows)


def sync_range_to_video(video_choice: str, current_end: float):
    """動画選択時、範囲用シークバーの上限をその動画の長さに合わせる。

    終了(秒)が未設定(0)の場合は、シークバー・数値欄とも動画の長さで初期化する。
    """
    video_id = parse_video_choice(video_choice)
    if not video_id:
        return gr.update(), gr.update(), gr.update()
    conn = db.get_conn()
    video = db.get_video(conn, video_id)
    conn.close()
    if not video:
        return gr.update(), gr.update(), gr.update()

    duration = round(video["duration"], 1)
    start_slider_update = gr.update(maximum=duration)
    if current_end:
        end_slider_update = gr.update(maximum=duration)
        end_num_update = gr.update()
    else:
        end_slider_update = gr.update(maximum=duration, value=duration)
        end_num_update = gr.update(value=duration)
    return start_slider_update, end_slider_update, end_num_update


def _build_table(results: list, selected_idx=None) -> list:
    """検索結果を表形式にする。選択中の行はラジオボタン風に●で示す。"""
    return [
        [
            f"{'●' if i == selected_idx else '○'} {i + 1}",
            r.get("video_name") or r["video_id"],
            utils.format_timestamp(r["start"]),
            utils.format_timestamp(r["end"]),
            r["match_type"],
            f"{r['score']:.3f}",
            r["text"][:60] + ("..." if len(r["text"]) > 60 else ""),
        ]
        for i, r in enumerate(results)
    ]


def _preview_update(
    preview_path: str | None,
    video_path: str | None,
    video_id: str | None = None,
    edited: bool = False,
):
    """プレビュー値と、現在の動画を識別できるラベルをまとめて返す。"""
    if not preview_path or not video_path:
        return gr.update(value=None, label="共通プレビュー")
    filename = Path(video_path).name
    identifier = f" [{video_id}]" if video_id and video_id != filename else ""
    suffix = "（編集結果）" if edited else ""
    return gr.update(
        value=preview_path,
        label=f"プレビュー: {filename}{identifier}{suffix}",
    )


def _safe_time_range(start, end) -> tuple[float, float] | None:
    """イベント競合でNone等が届いた場合に安全に判定する。"""
    try:
        start_value, end_value = float(start), float(end)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(start_value) or not math.isfinite(end_value):
        return None
    return start_value, end_value


def do_search(
    query: str,
    video_choice: str,
    top_k: int,
    min_score: float,
    range_enabled: bool = False,
    range_start: float = 0.0,
    range_end: float = 0.0,
):
    if not query.strip():
        return ([], [], *_EMPTY_SELECTION)
    if not config.TEXT_INDEX_PATH.exists():
        raise gr.Error("インデックスがありません。「動画の追加」タブで動画を登録してください。")

    video_filter = parse_video_choice(video_choice)

    if range_enabled and not video_filter:
        raise gr.Error("範囲を指定した検索は、動画を1本選んでから行ってください。")

    start_sec = end_sec = None
    if range_enabled and video_filter:
        start_sec, end_sec = float(range_start), float(range_end)
        if end_sec <= start_sec:
            raise gr.Error("範囲の終了は開始より後にしてください。")

    conn = db.get_conn()
    db.init_db(conn)

    try:
        query_vec = get_embedder().encode([query])
    except Exception as e:
        raise gr.Error(
            "検索用モデルのロードに失敗しました。インデックス処理の実行中は"
            "VRAMが不足することがあります。処理の完了を待つか停止してから"
            f"再試行してください。({type(e).__name__}: {e})"
        )
    vindex = VectorIndex.load(config.TEXT_INDEX_PATH, query_vec.shape[1])
    results = search_chunks(
        conn,
        vindex,
        query,
        query_vec,
        top_k=int(top_k),
        min_score=float(min_score),
        video_id=video_filter,
        start_sec=start_sec,
        end_sec=end_sec,
    )

    videos_by_id = {}
    for video in db.list_videos(conn):
        filename = Path(video["path"]).name
        videos_by_id[video["video_id"]] = (
            f"{filename} [{video['video_id']}]"
            if filename != video["video_id"] else filename
        )
    for result in results:
        result["video_name"] = videos_by_id.get(result["video_id"], result["video_id"])
    conn.close()
    if not results:
        gr.Info("該当するシーンが見つかりませんでした。")
        return ([], [], *_EMPTY_SELECTION)

    # 検索直後は先頭候補(行クリックと同じ処理)を自動で選択する
    table = _build_table(results, selected_idx=0)
    return (table, results, *_select_result(0, results))


def make_preview(video_path: str, start: float, end: float, duration: float) -> str:
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    out = PREVIEW_DIR / f"preview_{int(start * 10)}_{int(end * 10)}.mp4"
    if not out.exists():
        cut_clip(Path(video_path), start, end, out, pad=0.0, precise=False, duration=duration)
    return str(out)


def get_region_sentences(conn, video_id: str, lo: float, hi: float) -> list:
    rows = conn.execute(
        "SELECT start_sec, end_sec, text FROM asr_segments "
        "WHERE video_id = ? AND end_sec > ? AND start_sec < ? ORDER BY start_sec",
        (video_id, lo, hi),
    ).fetchall()
    return [dict(r) for r in rows]


def _sentence_choices(sents: list) -> list:
    return [
        f"{i + 1}. [{utils.format_timestamp(s['start_sec'])}] {s['text'][:28]}"
        for i, s in enumerate(sents)
    ]


# _select_result/do_searchの「未選択・該当なし」時に返す空の状態
# (preview, start, end, start_slider, end_slider, ctx, info, transcript,
#  sents, start_sent_dd, end_sent_dd, preview_origin)
_EMPTY_SELECTION = (
    gr.update(value=None, label="共通プレビュー"),
    gr.update(), gr.update(), gr.update(), gr.update(),
    None, "", "", [], gr.update(), gr.update(),
    gr.update(value=0.0),
)


def _select_result(idx, results: list):
    """検索結果の指定順位(0始まり)を読み込み、プレビュー等の状態を返す。

    検索直後の自動選択とテーブル行クリックの両方から呼ばれる共通処理。
    """
    if idx is None or not results or not (0 <= idx < len(results)):
        return _EMPTY_SELECTION
    r = results[idx]

    conn = db.get_conn()
    video = db.get_video(conn, r["video_id"])
    start, end = expand_to_speech_boundary(conn, r["video_id"], r["start"], r["end"])
    if not video:
        conn.close()
        raise gr.Error(f"動画が見つかりません: {r['video_id']}")
    transcript = region_transcript(conn, r["video_id"], start, end)
    # 文字起こしベースの調整用: 区間の前後90秒の文一覧
    sents = get_region_sentences(
        conn, r["video_id"], max(0.0, start - 90.0), end + 90.0
    )
    conn.close()

    duration = video["duration"]
    preview = make_preview(video["path"], start, end, duration)
    info = (
        "プレビュー中: 元の全体範囲\n"
        f"動画: {video['path']}\n"
        f"ヒット区間: {utils.format_timestamp(r['start'])} - {utils.format_timestamp(r['end'])}\n"
        f"拡張後: {utils.format_timestamp(start)} - {utils.format_timestamp(end)} "
        f"(長さ {end - start:.1f} 秒)"
    )
    ctx = {"video_path": video["path"], "duration": duration, "video_id": r["video_id"]}
    choices = _sentence_choices(sents)
    return (
        _preview_update(preview, video["path"], r["video_id"]),
        round(start, 1),
        round(end, 1),
        gr.update(minimum=0, maximum=round(duration, 1), value=round(start, 1)),
        gr.update(minimum=0, maximum=round(duration, 1), value=round(end, 1)),
        ctx,
        info,
        transcript,
        sents,
        gr.update(choices=choices, value=None),
        gr.update(choices=choices, value=None),
        round(start, 1),
    )


def on_table_select(results: list, evt: gr.SelectData):
    """検索結果テーブルの行クリックで、その行の候補を読み込む。

    選択行の●印を更新するため、テーブル自体も再描画して返す。
    """
    idx = evt.index[0] if evt.index is not None else None
    table = _build_table(results, selected_idx=idx)
    return (table, *_select_result(idx, results))


def manual_load(video_choice: str):
    """検索せずに動画を直接読み込み、時間指定だけで切り抜ける状態にする。"""
    video_id = parse_video_choice(video_choice)
    if not video_id:
        raise gr.Error("動画を選択してください(「すべての動画」は指定できません)。")

    conn = db.get_conn()
    video = db.get_video(conn, video_id)
    if not video:
        conn.close()
        raise gr.Error(f"動画が見つかりません: {video_id}")

    duration = video["duration"]
    start, end = 0.0, min(30.0, duration)
    transcript = region_transcript(conn, video_id, start, end)
    sents = get_region_sentences(conn, video_id, 0.0, end + 90.0)
    conn.close()

    preview = make_preview(video["path"], start, end, duration)
    info = (
        "プレビュー中: 元の全体範囲\n"
        f"動画: {video['path']}\n"
        f"区間: {utils.format_timestamp(start)} - {utils.format_timestamp(end)} "
        f"(長さ {end - start:.1f} 秒) ※検索なしの手動指定モード"
    )
    ctx = {"video_path": video["path"], "duration": duration, "video_id": video_id}
    choices = _sentence_choices(sents)
    return (
        _preview_update(preview, video["path"], video_id),
        round(start, 1),
        round(end, 1),
        gr.update(minimum=0, maximum=round(duration, 1), value=round(start, 1)),
        gr.update(minimum=0, maximum=round(duration, 1), value=round(end, 1)),
        ctx,
        info,
        transcript,
        sents,
        gr.update(choices=choices, value=None),
        gr.update(choices=choices, value=None),
        round(start, 1),
    )


def refresh_sentences(start: float, end: float, ctx: dict):
    """現在の区間の前後90秒で文リストを取り直す。"""
    if not ctx:
        raise gr.Error("先に動画を読み込むか検索結果を選択してください。")
    time_range = _safe_time_range(start, end)
    if not time_range or time_range[1] <= time_range[0]:
        raise gr.Error("開始・終了を正しく指定してください。")
    start, end = time_range
    conn = db.get_conn()
    sents = get_region_sentences(
        conn, ctx["video_id"], max(0.0, start - 90.0), end + 90.0
    )
    conn.close()
    choices = _sentence_choices(sents)
    return sents, gr.update(choices=choices, value=None), gr.update(choices=choices, value=None)


def refresh_preview(start: float, end: float, ctx: dict):
    if not ctx:
        raise gr.Error("先に検索結果を選択してください。")
    time_range = _safe_time_range(start, end)
    if not time_range:
        raise gr.Error("開始・終了を正しく指定してください。")
    start, end = time_range
    if end <= start:
        raise gr.Error("終了は開始より後にしてください。")
    preview = make_preview(ctx["video_path"], start, end, ctx["duration"])
    conn = db.get_conn()
    transcript = region_transcript(conn, ctx["video_id"], start, end)
    conn.close()
    info = (
        "プレビュー中: 元の全体範囲\n"
        f"動画: {ctx['video_path']}\n"
        f"区間: {utils.format_timestamp(start)} - {utils.format_timestamp(end)} "
        f"(長さ {end - start:.1f} 秒)"
    )
    return (
        _preview_update(preview, ctx["video_path"], ctx.get("video_id")),
        info,
        transcript,
        start,
    )


def adjust_time(value: float, ctx: dict, delta: float):
    hi = ctx["duration"] if ctx else None
    v = (value or 0) + delta
    v = max(0.0, v)
    if hi is not None:
        v = min(v, hi)
    return round(v, 1)


def adjust_time_with_step(value: float, ctx: dict, step: float, direction: float):
    """UIで選択した調整幅だけ開始・終了時刻を前後させる。"""
    return adjust_time(value, ctx, float(step or 1.0) * float(direction))


def always_refresh(start: float, end: float, ctx: dict):
    """選択済みならプレビュー・区間情報・文字起こしを更新する。"""
    time_range = _safe_time_range(start, end)
    if not ctx or not time_range or time_range[1] <= time_range[0]:
        return gr.update(), gr.update(), gr.update(), gr.update()
    return refresh_preview(time_range[0], time_range[1], ctx)


def pick_sentence(sel: str, sents: list, which: str):
    """文ドロップダウンの選択から開始/終了秒を返す。"""
    if not sel or not sents:
        return gr.update(), gr.update()
    idx = int(sel.split(".")[0]) - 1
    if idx < 0 or idx >= len(sents):
        return gr.update(), gr.update()
    s = sents[idx]
    v = round(s["start_sec"] if which == "start" else s["end_sec"], 1)
    return v, gr.update(value=v)


def _clip_plan_ranges(start: float, end: float, plan: dict | None) -> list[list[float]]:
    """現在の外側区間に対応する保持区間を返す。区間変更後の古い計画は使わない。"""
    start, end = float(start or 0.0), float(end or 0.0)
    if end <= start:
        return []
    if (
        isinstance(plan, dict)
        and abs(float(plan.get("base_start", -1.0)) - start) < 0.001
        and abs(float(plan.get("base_end", -1.0)) - end) < 0.001
        and plan.get("ranges")
    ):
        return [[float(s), float(e)] for s, e in plan["ranges"]]
    return [[start, end]]


def _clip_plan_exclusions(
    start: float, end: float, plan: dict | None
) -> list[list[float]]:
    """内部の保持区間から、UIに表示する除外区間を復元する。"""
    start, end = float(start or 0.0), float(end or 0.0)
    ranges = _clip_plan_ranges(start, end, plan)
    if not ranges:
        return []
    exclusions: list[list[float]] = []
    cursor = start
    for range_start, range_end in ranges:
        if range_start > cursor + 0.001:
            exclusions.append([cursor, range_start])
        cursor = max(cursor, range_end)
    if cursor < end - 0.001:
        exclusions.append([cursor, end])
    return exclusions


def _clip_plan_view(
    start: float,
    end: float,
    ranges: list[list[float]],
    notice: str = "",
    selected_index: int | None = None,
) -> tuple[list, str]:
    """ユーザーが操作した除外区間と、完成予定時間を表示する。"""
    plan = {"base_start": float(start), "base_end": float(end), "ranges": ranges}
    exclusions = _clip_plan_exclusions(start, end, plan)
    table = [
        [
            f"{'●' if selected_index == i - 1 else '○'} {i}",
            utils.format_timestamp(s),
            utils.format_timestamp(e),
            round(e - s, 1),
        ]
        for i, (s, e) in enumerate(exclusions, 1)
    ]
    total = max(0.0, float(end) - float(start))
    removed = sum(e - s for s, e in exclusions)
    result = sum(e - s for s, e in ranges)
    summary = (
        f"**全体範囲:** {utils.format_timestamp(start)} ～ {utils.format_timestamp(end)} "
        f"({total:.1f}秒)　｜　**途中カット:** {len(exclusions)}箇所 / {removed:.1f}秒　"
        f"｜　**完成予定:** {result:.1f}秒"
    )
    if notice:
        summary += f"\n\n{notice}"
    return table, summary


def render_clip_plan_timeline(start: float, end: float, plan: dict | None) -> str:
    """全体範囲に対する除外位置をハッチ表示した簡易タイムライン。"""
    start, end = float(start or 0.0), float(end or 0.0)
    total = end - start
    if total <= 0:
        return "<div class='clip-timeline-empty'>動画を選択するとタイムラインを表示します。</div>"
    overlays = []
    for cut_start, cut_end in _clip_plan_exclusions(start, end, plan):
        left = max(0.0, min(100.0, (cut_start - start) / total * 100.0))
        width = max(0.0, min(100.0 - left, (cut_end - cut_start) / total * 100.0))
        overlays.append(
            f"<span class='clip-timeline-cut' style='left:{left:.4f}%;width:{width:.4f}%' "
            f"title='除外 {utils.format_timestamp(cut_start)} ～ "
            f"{utils.format_timestamp(cut_end)}'></span>"
        )
    return (
        "<div class='clip-timeline-label'>保存予定タイムライン "
        "<span>青: 保存</span> <span class='clip-timeline-hatch-key'>斜線: 除外</span></div>"
        "<div class='clip-timeline-track' role='img' aria-label='途中カットの位置'>"
        + "".join(overlays)
        + "</div>"
    )


def reset_clip_plan(start: float, end: float):
    """外側の開始・終了を1つの保持区間として編集計画を初期化する。"""
    time_range = _safe_time_range(start, end)
    if not time_range or time_range[1] <= time_range[0]:
        return None, [], "開始・終了を正しく指定してください。"
    start, end = time_range
    ranges = _clip_plan_ranges(start, end, None)
    plan = {"base_start": float(start), "base_end": float(end), "ranges": ranges}
    table, summary = _clip_plan_view(start, end, ranges)
    return plan, table, summary


def reset_clip_plan_after_range_change(start: float, end: float, plan: dict | None):
    """全体範囲変更時に、古い途中カットを明示的に破棄する。"""
    old_exclusions = _clip_plan_exclusions(
        float(plan.get("base_start", 0.0)), float(plan.get("base_end", 0.0)), plan
    ) if isinstance(plan, dict) else []
    new_plan, table, summary = reset_clip_plan(start, end)
    if old_exclusions:
        summary += "\n\n⚠️ 全体範囲を変更したため、途中カットをリセットしました。"
    return new_plan, table, summary, None


def exclude_clip_range(
    start: float,
    end: float,
    exclude_start: float,
    exclude_end: float,
    plan: dict | None,
):
    """保持区間から指定区間を差し引く。複数回の除外で複数窓を作れる。"""
    start, end = float(start or 0.0), float(end or 0.0)
    cut_start, cut_end = float(exclude_start or 0.0), float(exclude_end or 0.0)
    if end <= start:
        raise gr.Error("先に切り抜く開始・終了を正しく指定してください。")
    if cut_end <= cut_start:
        raise gr.Error("除外終了は除外開始より後にしてください。")
    if cut_start < start or cut_end > end:
        raise gr.Error("除外区間は、選択中の開始・終了の内側に指定してください。")

    ranges = _clip_plan_ranges(start, end, plan)
    new_ranges = []
    changed = False
    for range_start, range_end in ranges:
        if cut_end <= range_start or cut_start >= range_end:
            new_ranges.append([range_start, range_end])
            continue
        changed = True
        if range_start < cut_start:
            new_ranges.append([range_start, min(cut_start, range_end)])
        if cut_end < range_end:
            new_ranges.append([max(cut_end, range_start), range_end])

    if not changed:
        gr.Info("その区間は既に除外されています。編集内容は変更していません。")
        current_plan = {
            "base_start": start,
            "base_end": end,
            "ranges": ranges,
        }
        table, summary = _clip_plan_view(start, end, ranges)
        return current_plan, table, summary
    if not new_ranges:
        raise gr.Error("選択区間のすべてを除外することはできません。")

    new_plan = {"base_start": start, "base_end": end, "ranges": new_ranges}
    table, summary = _clip_plan_view(start, end, new_ranges)
    return new_plan, table, summary


def select_clip_exclusion(
    start: float, end: float, plan: dict | None, evt: gr.SelectData
):
    """除外一覧で選ばれた行番号を保持する。"""
    if evt is None or evt.index is None:
        ranges = _clip_plan_ranges(start, end, plan)
        table, summary = _clip_plan_view(start, end, ranges)
        return None, table, summary
    index = evt.index[0] if isinstance(evt.index, (tuple, list)) else evt.index
    selected_index = int(index)
    ranges = _clip_plan_ranges(start, end, plan)
    table, summary = _clip_plan_view(
        start, end, ranges, selected_index=selected_index
    )
    return selected_index, table, summary


def remove_clip_exclusion(
    start: float, end: float, selected_index: int | None, plan: dict | None
):
    """一覧で選択した除外区間だけを取り消す。"""
    exclusions = _clip_plan_exclusions(start, end, plan)
    if selected_index is None or not 0 <= int(selected_index) < len(exclusions):
        raise gr.Error("一覧から取り消す除外区間を選択してください。")
    exclusions.pop(int(selected_index))
    ranges: list[list[float]] = [[float(start), float(end)]]
    for cut_start, cut_end in exclusions:
        next_ranges = []
        for range_start, range_end in ranges:
            if cut_end <= range_start or cut_start >= range_end:
                next_ranges.append([range_start, range_end])
            else:
                if range_start < cut_start:
                    next_ranges.append([range_start, cut_start])
                if cut_end < range_end:
                    next_ranges.append([cut_end, range_end])
        ranges = next_ranges
    new_plan = {"base_start": float(start), "base_end": float(end), "ranges": ranges}
    table, summary = _clip_plan_view(start, end, ranges)
    return new_plan, table, summary, None


def adjust_exclusion_time(
    value: float, start: float, end: float, delta: float
) -> float:
    """除外境界を全体範囲内で微調整する。"""
    adjusted = float(value or start or 0.0) + float(delta)
    return round(min(max(adjusted, float(start or 0.0)), float(end or 0.0)), 1)


def adjust_exclusion_time_with_step(
    value: float,
    start: float,
    end: float,
    step: float,
    direction: float,
) -> float:
    """選択した調整幅だけ除外境界を動かす。"""
    return adjust_exclusion_time(
        value, start, end, float(step or 1.0) * float(direction)
    )


def sync_exclusion_controls(start: float, end: float):
    """全体範囲に合わせて途中カットの入力範囲と初期値を揃える。"""
    time_range = _safe_time_range(start, end)
    if not time_range or time_range[1] <= time_range[0]:
        return gr.update(), gr.update(), gr.update(), gr.update()
    start, end = time_range
    maximum = max(start, end)
    return (
        gr.update(minimum=start, maximum=maximum, value=start),
        gr.update(minimum=start, maximum=maximum, value=maximum),
        gr.update(value=start, minimum=start, maximum=maximum),
        gr.update(value=maximum, minimum=start, maximum=maximum),
    )


def preview_clip_plan(start: float, end: float, ctx: dict, plan: dict | None):
    """除外後の保持区間を連結したプレビューを作る。"""
    if not ctx:
        raise gr.Error("先に検索結果を選択してください。")
    ranges = _clip_plan_ranges(start, end, plan)
    if not ranges:
        raise gr.Error("終了は開始より後にしてください。")
    if len(ranges) == 1:
        preview = make_preview(ctx["video_path"], ranges[0][0], ranges[0][1], ctx["duration"])
    else:
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        range_key = repr([(round(s, 3), round(e, 3)) for s, e in ranges])
        key = hashlib.sha1(range_key.encode("utf-8")).hexdigest()[:16]
        out = PREVIEW_DIR / f"preview_multi_{ctx['video_id']}_{key}.mp4"
        if not out.exists():
            cut_clips(
                Path(ctx["video_path"]), ranges, out,
                precise=False, duration=ctx["duration"],
            )
        preview = str(out)
    conn = db.get_conn()
    try:
        transcripts = [
            region_transcript(conn, ctx["video_id"], range_start, range_end)
            for range_start, range_end in ranges
        ]
    finally:
        conn.close()
    transcript = "\n\n--- カットのつなぎ目 ---\n\n".join(
        text for text in transcripts if text
    )
    exclusions = _clip_plan_exclusions(start, end, plan)
    removed = sum(cut_end - cut_start for cut_start, cut_end in exclusions)
    result = sum(range_end - range_start for range_start, range_end in ranges)
    info = (
        "プレビュー中: 編集結果（途中カット適用後）\n"
        f"途中カット: {len(exclusions)}箇所 / {removed:.1f}秒、"
        f"完成予定: {result:.1f}秒"
    )
    return (
        _preview_update(
            preview, ctx["video_path"], ctx.get("video_id"), edited=True
        ),
        info,
        transcript,
    )


def on_save(
    start: float,
    end: float,
    ctx: dict,
    plan: dict | None,
    precise: bool,
    out_dir: str,
    filename: str,
):
    if not ctx:
        raise gr.Error("先に検索結果を選択してください。")
    if end <= start:
        raise gr.Error("終了は開始より後にしてください。")

    out_dir = Path(out_dir.strip() or DEFAULT_CLIPS_DIR)
    name = filename.strip()
    if not name:
        name = f"{ctx['video_id']}_{int(start)}_{int(end)}"
    if not name.lower().endswith(".mp4"):
        name += ".mp4"

    out = out_dir / name
    if out.exists():
        raise gr.Error(f"既に存在します: {out} (ファイル名を変えてください)")

    ranges = _clip_plan_ranges(start, end, plan)
    cut_clips(
        Path(ctx["video_path"]), ranges, out,
        precise=precise, duration=ctx["duration"],
    )
    gr.Info(f"保存しました: {out}")
    return str(out.resolve())


# ---------- 動画の追加 (インデックス作成) ----------

def do_index(video_path: str, asr_model: str, force: bool, batch_infer: bool = True):
    """新規動画をインデックス化する。進捗ログをストリーミング表示する。

    video_pathがhttp(s)://で始まる場合は、先にダウンロードしてからインデックス化する。
    """
    video_path = video_path.strip()
    if not video_path:
        raise gr.Error("動画ファイルのパスまたはURLを指定してください。")
    if not _index_lock.acquire(blocking=False):
        raise gr.Error("別のインデックス処理が実行中です。完了までお待ちください。")

    log_lines = ["インデックス処理を開始します。ASRモデルの初回ロードには数分かかることがあります。"]
    try:
        yield "\n".join(log_lines), gr.update()

        if video_path.lower().startswith(("http://", "https://")):
            log_lines.append(f"URLを検出しました。動画をダウンロードします: {video_path}")
            yield "\n".join(log_lines), gr.update()
            try:
                local_path = None
                for msg, path in download_video(video_path):
                    # 進捗行(ダウンロード中...)は追記せず最終行を置き換える
                    if (
                        msg.startswith("  ダウンロード中")
                        and log_lines
                        and log_lines[-1].startswith("  ダウンロード中")
                    ):
                        log_lines[-1] = msg
                    else:
                        log_lines.append(msg)
                    yield "\n".join(log_lines), gr.update()
                    if path is not None:
                        local_path = path
                if local_path is None:
                    log_lines.append("エラー: ダウンロードに失敗しました(ファイルパスを取得できませんでした)。")
                    yield "\n".join(log_lines), gr.update()
                    return
                video_path = str(local_path)
            except DownloadError as e:
                log_lines.append(f"エラー: {e}")
                yield "\n".join(log_lines), gr.update()
                return

        # インデックス処理はサブプロセスで実行する。
        # (Gradioワーカースレッド内でのWhisperモデルロードはaccess violationで
        #  プロセスごと落ちるため、隔離して実行する)
        import os
        import subprocess
        import sys

        cmd = [
            sys.executable, "index_video.py",
            "--video", str(video_path),
            "--asr-model", asr_model,
        ]
        if force:
            cmd.append("--force")
        if not batch_infer:
            cmd += ["--batch-size", "1"]
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # 行バッファ
            env=env,
            cwd=str(Path(__file__).parent),
        )
        # 残留ジョブ掃除用にPIDを記録 (アプリごと落ちた場合、次回起動時に停止される)
        INDEX_JOB_PIDFILE.write_text(str(proc.pid), encoding="utf-8")
        _index_state["proc"] = proc
        _index_state["stopped"] = False
        # `for line in proc.stdout` は先読みバッファで表示が遅延するためreadlineで読む
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip()
            if not line:
                continue
            # ライブラリのプログレスバーや警告はログに出さない
            if "it/s]" in line or "Warning" in line or line.startswith(("Fetching", "Loading weights", "pre tokenize", "Inference Embeddings")):
                continue
            # 進捗行(文字起こし中... XX%)は追記せず最終行を置き換える
            if (
                line.startswith("  文字起こし中")
                and log_lines
                and log_lines[-1].startswith("  文字起こし中")
            ):
                log_lines[-1] = line
            else:
                log_lines.append(line)
            yield "\n".join(log_lines), gr.update()
        code = proc.wait()
        INDEX_JOB_PIDFILE.unlink(missing_ok=True)
        if code == 0:
            gr.Info("インデックス作成が完了しました。")
            yield "\n".join(log_lines), gr.update(choices=list_video_choices())
        elif _index_state["stopped"]:
            log_lines.append(
                "処理を停止しました。文字起こしは途中保存されているため、"
                "同じ動画で再実行すれば続きから再開されます。"
            )
            yield "\n".join(log_lines), gr.update()
        else:
            log_lines.append(f"エラー: インデックス処理が異常終了しました (exit code {code})")
            yield "\n".join(log_lines), gr.update()
    finally:
        _index_state["proc"] = None
        _index_lock.release()


# 終了ボタン押下時にブラウザ側で実行するJS。まずタブを閉じようとし、
# ブラウザのセキュリティ制約で閉じられない場合は画面を終了表示に差し替える
# (サーバー停止による接続エラー画面になるより分かりやすい)。
_QUIT_JS = """() => {
    try { window.open('', '_self'); window.close(); } catch (e) {}
    setTimeout(() => {
        document.body.innerHTML =
            '<div style="display:flex;align-items:center;justify-content:center;'
            + 'height:100vh;font-family:sans-serif;font-size:1.4rem;color:#888;">'
            + 'アプリを終了しました。このタブは閉じてください。</div>';
    }, 200);
}"""


def shutdown_app():
    """実行中のジョブを停止してアプリ本体(サーバープロセス)を終了する。"""
    import os
    import subprocess

    proc = _index_state.get("proc")
    if proc is not None and proc.poll() is None:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True
        )
    INDEX_JOB_PIDFILE.unlink(missing_ok=True)
    # このレスポンスを返してからプロセスを終了する
    threading.Timer(3.0, lambda: os._exit(0)).start()
    return gr.update(
        visible=True,
        value="**アプリを終了しました。このタブは閉じてください。**",
    )


def stop_indexing():
    """実行中のインデックス処理サブプロセスを停止する(子プロセスごと)。"""
    import subprocess

    proc = _index_state.get("proc")
    if proc is None or proc.poll() is not None:
        raise gr.Error("実行中のインデックス処理はありません。")
    _index_state["stopped"] = True
    subprocess.run(
        ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True
    )
    gr.Info("インデックス処理を停止しました。")


# ---------- インデックスの共有 (エクスポート/インポート) ----------

def do_export(video_choice: str):
    video_id = parse_video_choice(video_choice)
    if not video_id:
        raise gr.Error("エクスポートする動画を選択してください。")
    try:
        out_path = export_index(video_id)
    except ShareError as e:
        raise gr.Error(str(e))
    gr.Info(f"エクスポートしました: {out_path}")
    return str(out_path), f"保存先: {out_path.resolve()}"


def do_import(zip_file):
    if zip_file is None:
        yield "インポートするzipファイルを選択してください。", gr.update()
        return
    log_lines = []
    try:
        for msg in import_index(Path(zip_file.name if hasattr(zip_file, "name") else zip_file)):
            log_lines.append(msg)
            yield "\n".join(log_lines), gr.update()
        gr.Info("インポートが完了しました。")
        yield "\n".join(log_lines), gr.update(choices=list_video_choices())
    except ShareError as e:
        log_lines.append(f"エラー: {e}")
        yield "\n".join(log_lines), gr.update()


def build_adjust_row(label: str, target_number, ctx_state, preview_io, slider, step_input):
    with gr.Row():
        gr.Markdown(f"**{label}**")
        for label_text, direction in (("前へ", -1.0), ("後ろへ", 1.0)):
            btn = gr.Button(label_text, size="sm")
            btn.click(
                adjust_time_with_step,
                # 画面上のSliderを正として計算し、結果を内部Stateへ同期する。
                inputs=[slider, ctx_state, step_input, gr.State(direction)],
                outputs=[target_number],
            ).then(
                lambda v: gr.update(value=v), inputs=[target_number], outputs=[slider]
            ).then(
                always_refresh,
                inputs=preview_io["inputs"],
                outputs=preview_io["outputs"],
            )


_APP_CSS = """
.reverse-fill-slider input[type=range]::-webkit-slider-runnable-track {
  background: linear-gradient(
    to right,
    var(--neutral-200) var(--range_progress),
    var(--slider-color) var(--range_progress)
  ) !important;
}
.reverse-fill-slider input[type=range]::-moz-range-track {
  background: var(--slider-color) !important;
}
.reverse-fill-slider input[type=range]::-moz-range-progress {
  background: var(--neutral-200) !important;
}
.time-slider input[type=number] {
  min-width: 8.5rem !important;
  width: 8.5rem !important;
  font-size: 1.05rem !important;
  padding: .45rem .6rem !important;
}
.clip-timeline-label {
  display: flex; gap: .8rem; align-items: center; margin: .2rem 0 .35rem;
  font-size: .9rem;
}
.clip-timeline-track {
  position: relative; height: 1.1rem; overflow: hidden; border-radius: .4rem;
  background: var(--primary-500); border: 1px solid var(--border-color-primary);
}
.clip-timeline-cut, .clip-timeline-hatch-key {
  background: repeating-linear-gradient(
    135deg,
    rgba(45, 45, 45, .92) 0,
    rgba(45, 45, 45, .92) 4px,
    rgba(245, 245, 245, .96) 4px,
    rgba(245, 245, 245, .96) 8px
  );
}
.clip-timeline-cut { position: absolute; inset-block: 0; }
.clip-timeline-hatch-key { padding: .05rem .35rem; border-radius: .2rem; color: #fff; }
.clip-timeline-empty { color: var(--body-text-color-subdued); font-size: .9rem; }
"""


with gr.Blocks(title="動画シーン検索") as demo:
    with gr.Row():
        gr.Markdown("# 動画シーン検索・切り抜き")
        quit_btn = gr.Button("アプリを終了", variant="stop", scale=0, min_width=120)
        quit_confirm_btn = gr.Button(
            "本当に終了する (実行中の処理も停止)", variant="stop", visible=False, scale=0
        )
        quit_cancel_btn = gr.Button("キャンセル", visible=False, scale=0, min_width=100)
    quit_msg = gr.Markdown(visible=False)

    quit_btn.click(
        lambda: (gr.update(visible=True), gr.update(visible=True), gr.update(visible=False)),
        outputs=[quit_confirm_btn, quit_cancel_btn, quit_btn],
    )
    quit_cancel_btn.click(
        lambda: (gr.update(visible=False), gr.update(visible=False), gr.update(visible=True)),
        outputs=[quit_confirm_btn, quit_cancel_btn, quit_btn],
    )
    quit_confirm_btn.click(shutdown_app, outputs=[quit_msg], js=_QUIT_JS)

    with gr.Tab("検索・切り抜き"):
        results_state = gr.State([])
        ctx_state = gr.State(None)
        video_select = gr.State(ALL_VIDEOS_VALUE)
        video_gallery_ids = gr.State([])

        with gr.Row():
            target_thumbnail = gr.Image(
                label="現在の検索対象",
                interactive=False,
                visible=False,
                height=150,
                scale=1,
                elem_id="current-target-thumbnail",
            )
            target_video_detail = gr.Markdown(
                "**検索対象:** すべての動画",
                container=True,
                scale=2,
                elem_id="current-target-detail",
            )
            manual_btn = gr.Button("検索せずにこの動画を切り抜く", scale=1)

        with gr.Accordion("検索対象の動画を選ぶ", open=False) as video_picker:
            gr.Markdown("サムネイルをクリックすると検索対象が切り替わります。")
            with gr.Row():
                video_filter_box = gr.Textbox(
                    label="ファイル名で絞り込み",
                    placeholder="ファイル名の一部を入力",
                    scale=3,
                )
                video_filter_btn = gr.Button("絞り込む", scale=1)
                reload_btn = gr.Button("一覧更新", scale=1)
            video_gallery = gr.Gallery(
                label="動画一覧",
                columns=4,
                height=460,
                allow_preview=False,
                object_fit="cover",
                selected_index=None,
                elem_id="video-picker-gallery",
            )

        with gr.Row():
            query_box = gr.Textbox(
                label="検索クエリ", placeholder="例: 奨学金について話しているところ", scale=4
            )
            search_btn = gr.Button("検索", variant="primary", scale=1)
        with gr.Accordion("検索の詳細設定", open=False):
            gr.Markdown("文字一致は類似度に関係なく表示され、閾値は意味検索だけに適用されます。")
            with gr.Row():
                top_k = gr.Slider(1, 20, value=5, step=1, label="表示する候補数")
                min_score = gr.Slider(
                    0.0, 1.0, value=config.MIN_SCORE, step=0.01,
                    label="意味検索の類似度閾値",
                )
        with gr.Accordion("検索範囲を指定する", open=False):
            gr.Markdown(
                "動画を1本選んだ上で範囲を絞ると、その範囲内だけを検索対象にします"
                "(シークバーまたは秒数で指定)。"
            )
            range_chk = gr.Checkbox(value=False, label="検索範囲を指定する")
            with gr.Row():
                range_start_slider = gr.Slider(
                    0, 1, value=0, step=0.1, label="範囲の開始", scale=4
                )
                range_start_num = gr.Number(
                    value=0, label="開始 (秒)", precision=1, scale=1, min_width=90
                )
            with gr.Row():
                range_end_slider = gr.Slider(
                    0, 1, value=1, step=0.1, label="範囲の終了", scale=4
                )
                range_end_num = gr.Number(
                    value=0, label="終了 (秒)", precision=1, scale=1, min_width=90
                )

        gr.Markdown("行をクリックすると、その候補を切り抜き対象として読み込みます。")
        result_table = gr.Dataframe(
            headers=["#", "動画", "開始", "終了", "一致方法", "類似度", "テキスト"],
            interactive=False,
            label="検索結果",
        )

        with gr.Row():
            with gr.Column(scale=3, min_width=420):
                preview_video = gr.Video(
                    label="共通プレビュー",
                    autoplay=False,
                    elem_id="clip-preview-video",
                )
            with gr.Column(scale=2, min_width=280):
                with gr.Accordion(
                    "プレビュー区間の文字起こし", open=True
                ) as transcript_accordion:
                    transcript_box = gr.Textbox(
                        label="文字起こし",
                        interactive=False,
                        lines=12,
                        show_label=False,
                    )
        info_box = gr.Textbox(
            label="プレビュー中の内容", interactive=False, lines=4, render=False
        )
        # ブラウザで現在位置を元動画時刻へ換算するための、実際のプレビュー開始時刻。
        # visible=Falseならコンポーネントは設定に存在しつつ画面の場所を取らない。
        preview_origin = gr.Number(value=0.0, visible=False)

        clip_plan_state = gr.State(None)
        selected_exclusion_state = gr.State(None)
        clip_plan_summary = gr.Markdown(
            "動画を選択すると、全体範囲と完成予定時間が表示されます。"
        )
        clip_plan_timeline = gr.HTML(render_clip_plan_timeline(0, 0, None))

        sentences_state = gr.State([])
        with gr.Tabs():
            with gr.Tab("① 全体範囲"):
                gr.Markdown(
                    "まず、保存したい範囲全体の開始と終了を決めます。"
                    "途中カットを使わない場合は、このタブだけで従来どおり保存できます。"
                )
                with gr.Accordion("現在位置で全体範囲を設定", open=True):
                    gr.Markdown(
                        "プレビューの現在位置を、全体範囲の開始または終了に設定します。"
                    )
                    with gr.Row():
                        mark_overall_start_btn = gr.Button(
                            "現在位置を全体開始に設定"
                        )
                        mark_overall_end_btn = gr.Button(
                            "現在位置を全体終了に設定"
                        )
                with gr.Accordion("文単位で全体範囲を調整", open=False):
                    gr.Markdown(
                        "文字起こしの文を選ぶと、全体範囲の開始・終了時刻が"
                        "文の境界に合います。"
                    )
                    with gr.Row():
                        start_sent_dd = gr.Dropdown(
                            choices=[], label="この文から (全体の開始)", scale=2
                        )
                        end_sent_dd = gr.Dropdown(
                            choices=[], label="この文まで (全体の終了)", scale=2
                        )
                        refresh_sents_btn = gr.Button("周辺の文を再取得", scale=1)

                with gr.Accordion("秒単位で区間を微調整", open=True):
                    start_slider = gr.Slider(
                        0, 1, value=0, step=0.1, label="開始位置",
                        elem_classes=["time-slider"],
                    )
                    end_slider = gr.Slider(
                        0, 1, value=1, step=0.1, label="終了位置",
                        elem_classes=["time-slider"],
                    )

                    # Slider右側の組み込み数値欄を使うため、重複する独立Numberは
                    # 描画しない。イベント間で共有する値はStateとして保持する。
                    start_num = gr.State(0.0)
                    end_num = gr.State(0.0)

                    adjust_step = gr.Radio(
                        choices=ADJUST_STEPS, value=1.0, label="調整幅 (秒)"
                    )

                    preview_io = {
                        "inputs": [start_num, end_num, ctx_state],
                        "outputs": [
                            preview_video, info_box, transcript_box, preview_origin,
                        ],
                    }
                    build_adjust_row(
                        "開始を調整", start_num, ctx_state,
                        preview_io, start_slider, adjust_step,
                    )
                    build_adjust_row(
                        "終了を調整", end_num, ctx_state,
                        preview_io, end_slider, adjust_step,
                    )
                    update_btn = gr.Button("全体範囲をプレビュー更新")

            with gr.Tab("② 途中カット（任意）"):
                gr.Markdown(
                    "全体範囲の中から不要な箇所を指定します。プレビューを再生し、"
                    "不要部分の先頭・末尾で現在位置を設定してください。"
                )
                with gr.Row():
                    mark_exclude_start_btn = gr.Button("現在位置を除外開始に設定")
                    mark_exclude_end_btn = gr.Button("現在位置を除外終了に設定")
                with gr.Accordion("文単位で除外範囲を調整", open=False):
                    gr.Markdown(
                        "文字起こしの文を選ぶと、除外範囲の開始・終了時刻が"
                        "文の境界に合います。"
                    )
                    with gr.Row():
                        exclude_start_sent_dd = gr.Dropdown(
                            choices=[], label="この文から (除外の開始)", scale=2
                        )
                        exclude_end_sent_dd = gr.Dropdown(
                            choices=[], label="この文まで (除外の終了)", scale=2
                        )
                        refresh_exclude_sents_btn = gr.Button(
                            "周辺の文を再取得", scale=1
                        )
                exclude_start_slider = gr.Slider(
                    0, 1, value=0, step=0.1, label="除外開始位置",
                    elem_classes=["time-slider"],
                )
                exclude_end_slider = gr.Slider(
                    0, 1, value=1, step=0.1, label="除外終了位置",
                    elem_classes=["time-slider", "reverse-fill-slider"],
                )
                gr.Markdown(
                    "除外終了sliderの **右側の色付き部分は、終了後に保存する範囲** です。"
                )
                exclude_start_num = gr.State(0.0)
                exclude_end_num = gr.State(1.0)
                exclude_adjust_step = gr.Radio(
                    choices=ADJUST_STEPS, value=1.0, label="調整幅 (秒)"
                )
                exclusion_adjust_buttons = []
                for target_name, target, slider in (
                    ("除外開始", exclude_start_num, exclude_start_slider),
                    ("除外終了", exclude_end_num, exclude_end_slider),
                ):
                    with gr.Row():
                        gr.Markdown(f"**{target_name}を調整**", min_width=100)
                        before_btn = gr.Button("前へ", size="sm")
                        after_btn = gr.Button("後ろへ", size="sm")
                        exclusion_adjust_buttons.extend((
                            (before_btn, target, slider, -1.0),
                            (after_btn, target, slider, 1.0),
                        ))
                with gr.Row():
                    exclude_btn = gr.Button("除外を追加", variant="primary")
                    multi_preview_btn = gr.Button("編集結果をプレビュー")
                clip_plan_table = gr.Dataframe(
                    headers=["#", "除外開始", "除外終了", "長さ (秒)"],
                    interactive=False,
                    label="除外箇所（行を選択して個別に取り消せます）",
                )
                with gr.Row():
                    remove_exclusion_btn = gr.Button("選択した除外を取り消す")
                    reset_plan_btn = gr.Button("除外をすべて取り消す")
                gr.Markdown(
                    "※「編集結果をプレビュー」後に現在位置から追加する場合は、"
                    "先に①の「全体範囲をプレビュー更新」を押してください。"
                )

        info_box.render()

        gr.Markdown("### 保存")
        with gr.Row():
            out_dir_box = gr.Textbox(value=DEFAULT_CLIPS_DIR, label="保存先フォルダ", scale=3)
            folder_btn = gr.Button("フォルダ参照", scale=1)
        with gr.Row():
            filename_box = gr.Textbox(
                value="", label="ファイル名 (空なら自動)", placeholder="my_clip.mp4", scale=2
            )
            precise_chk = gr.Checkbox(value=True, label="フレーム精度で保存 (再エンコード)")
            save_btn = gr.Button("クリップ保存", variant="primary")
        saved_path = gr.Textbox(label="保存先", interactive=False)

        # --- イベント配線 ---
        # 検索結果選択(自動選択・行クリック・手動読み込み)で共通して更新する出力
        selection_outputs = [
            preview_video, start_num, end_num, start_slider, end_slider,
            ctx_state, info_box, transcript_box,
            sentences_state, start_sent_dd, end_sent_dd,
            preview_origin,
        ]
        search_inputs = [
            query_box, video_select, top_k, min_score,
            range_chk, range_start_num, range_end_num,
        ]
        search_outputs = [result_table, results_state, *selection_outputs]
        search_btn.click(do_search, inputs=search_inputs, outputs=search_outputs)
        query_box.submit(do_search, inputs=search_inputs, outputs=search_outputs)
        gallery_outputs = [video_gallery, video_gallery_ids]
        gallery_inputs = [video_filter_box, video_select]
        video_picker.expand(
            build_video_gallery,
            inputs=gallery_inputs,
            outputs=gallery_outputs,
        )
        video_filter_btn.click(
            build_video_gallery,
            inputs=gallery_inputs,
            outputs=gallery_outputs,
        )
        video_filter_box.submit(
            build_video_gallery,
            inputs=gallery_inputs,
            outputs=gallery_outputs,
        )
        reload_btn.click(
            build_video_gallery,
            inputs=gallery_inputs,
            outputs=gallery_outputs,
        )
        collapse_video_picker_js = """() => {
            const gallery = document.getElementById('video-picker-gallery');
            const accordion = gallery ? gallery.closest('.gr-accordion') : null;
            const content = accordion
                ? accordion.querySelector(':scope > [data-testid="accordion-content"]')
                : null;
            const button = accordion
                ? accordion.querySelector(':scope > button.label-wrap')
                : null;
            if (content && button && getComputedStyle(content).display !== 'none') {
                button.click();
            }
        }"""
        video_gallery.select(
            select_video_from_gallery,
            inputs=[video_gallery_ids],
            outputs=[video_select, target_thumbnail, target_video_detail, video_gallery],
        ).then(
            sync_range_to_video,
            inputs=[video_select, range_end_num],
            outputs=[range_start_slider, range_end_slider, range_end_num],
        ).then(
            fn=None,
            js=collapse_video_picker_js,
        )
        # 範囲用シークバーと数値欄の相互同期(切り抜き区間のシークバーと同じ方式)
        range_start_slider.release(
            lambda v: round(v, 1), inputs=[range_start_slider], outputs=[range_start_num]
        )
        range_end_slider.release(
            lambda v: round(v, 1), inputs=[range_end_slider], outputs=[range_end_num]
        )
        range_start_num.input(
            lambda v: gr.update(value=v), inputs=[range_start_num], outputs=[range_start_slider]
        )
        range_end_num.input(
            lambda v: gr.update(value=v), inputs=[range_end_num], outputs=[range_end_slider]
        )
        manual_btn.click(manual_load, inputs=[video_select], outputs=selection_outputs)
        refresh_sents_btn.click(
            refresh_sentences,
            inputs=[start_num, end_num, ctx_state],
            outputs=[sentences_state, start_sent_dd, end_sent_dd],
        )
        result_table.select(
            on_table_select,
            inputs=[results_state],
            outputs=[result_table, *selection_outputs]
        )
        # 文字起こしの文ベースの区間調整
        start_sent_dd.change(
            pick_sentence,
            inputs=[start_sent_dd, sentences_state, gr.State("start")],
            outputs=[start_num, start_slider],
        ).then(
            always_refresh,
            inputs=preview_io["inputs"],
            outputs=preview_io["outputs"],
        )
        end_sent_dd.change(
            pick_sentence,
            inputs=[end_sent_dd, sentences_state, gr.State("end")],
            outputs=[end_num, end_slider],
        ).then(
            always_refresh,
            inputs=preview_io["inputs"],
            outputs=preview_io["outputs"],
        )
        # シークバー値を内部Stateへ反映し、離した時に自動プレビュー
        start_slider.input(
            lambda v: round(v, 1), inputs=[start_slider], outputs=[start_num]
        )
        end_slider.input(
            lambda v: round(v, 1), inputs=[end_slider], outputs=[end_num]
        )
        start_slider.release(
            lambda v: round(v, 1), inputs=[start_slider], outputs=[start_num]
        ).then(
            always_refresh,
            inputs=preview_io["inputs"],
            outputs=preview_io["outputs"],
        )
        end_slider.release(
            lambda v: round(v, 1), inputs=[end_slider], outputs=[end_num]
        ).then(
            always_refresh,
            inputs=preview_io["inputs"],
            outputs=preview_io["outputs"],
        )
        # 外側の区間が変わった場合、以前の除外を明示して初期化する
        start_num.change(
            reset_clip_plan_after_range_change,
            inputs=[start_num, end_num, clip_plan_state],
            outputs=[
                clip_plan_state, clip_plan_table,
                clip_plan_summary, selected_exclusion_state,
            ],
        ).then(
            sync_exclusion_controls,
            inputs=[start_num, end_num],
            outputs=[
                exclude_start_slider, exclude_end_slider,
                exclude_start_num, exclude_end_num,
            ],
        ).then(
            render_clip_plan_timeline,
            inputs=[start_num, end_num, clip_plan_state],
            outputs=[clip_plan_timeline],
        )
        end_num.change(
            reset_clip_plan_after_range_change,
            inputs=[start_num, end_num, clip_plan_state],
            outputs=[
                clip_plan_state, clip_plan_table,
                clip_plan_summary, selected_exclusion_state,
            ],
        ).then(
            sync_exclusion_controls,
            inputs=[start_num, end_num],
            outputs=[
                exclude_start_slider, exclude_end_slider,
                exclude_start_num, exclude_end_num,
            ],
        ).then(
            render_clip_plan_timeline,
            inputs=[start_num, end_num, clip_plan_state],
            outputs=[clip_plan_timeline],
        )
        update_btn.click(
            refresh_preview,
            inputs=[start_num, end_num, ctx_state],
            outputs=[preview_video, info_box, transcript_box, preview_origin],
        )
        exclude_btn.click(
            exclude_clip_range,
            inputs=[
                start_num, end_num, exclude_start_num, exclude_end_num, clip_plan_state,
            ],
            outputs=[clip_plan_state, clip_plan_table, clip_plan_summary],
        ).then(
            lambda: None, outputs=[selected_exclusion_state]
        ).then(
            render_clip_plan_timeline,
            inputs=[start_num, end_num, clip_plan_state],
            outputs=[clip_plan_timeline],
        )
        reset_plan_btn.click(
            reset_clip_plan,
            inputs=[start_num, end_num],
            outputs=[clip_plan_state, clip_plan_table, clip_plan_summary],
        ).then(
            lambda: None, outputs=[selected_exclusion_state]
        ).then(
            render_clip_plan_timeline,
            inputs=[start_num, end_num, clip_plan_state],
            outputs=[clip_plan_timeline],
        )
        clip_plan_table.select(
            select_clip_exclusion,
            inputs=[start_num, end_num, clip_plan_state],
            outputs=[selected_exclusion_state, clip_plan_table, clip_plan_summary],
        )
        remove_exclusion_btn.click(
            remove_clip_exclusion,
            inputs=[start_num, end_num, selected_exclusion_state, clip_plan_state],
            outputs=[
                clip_plan_state, clip_plan_table,
                clip_plan_summary, selected_exclusion_state,
            ],
        ).then(
            render_clip_plan_timeline,
            inputs=[start_num, end_num, clip_plan_state],
            outputs=[clip_plan_timeline],
        )
        _CURRENT_PREVIEW_TIME_JS = """
        (base, current, previewInfo) => {
            const root = document.getElementById('clip-preview-video');
            const video = root ? root.querySelector('video') : null;
            if (!video || !Number.isFinite(video.currentTime)) return current;
            if (String(previewInfo || '').includes('編集結果')) {
                window.alert(
                    '編集結果プレビューでは元動画の時刻を取得できません。' +
                    '① 全体範囲の「全体範囲をプレビュー更新」を押してから再試行してください。'
                );
                return current;
            }
            const value = Math.round((Number(base || 0) + video.currentTime) * 10) / 10;
            return value;
        }
        """
        mark_overall_start_btn.click(
            fn=None,
            inputs=[preview_origin, start_slider, info_box],
            outputs=[start_slider],
            js=_CURRENT_PREVIEW_TIME_JS,
        ).then(
            lambda v: round(float(v), 1),
            inputs=[start_slider],
            outputs=[start_num],
        )
        mark_overall_end_btn.click(
            fn=None,
            inputs=[preview_origin, end_slider, info_box],
            outputs=[end_slider],
            js=_CURRENT_PREVIEW_TIME_JS,
        ).then(
            lambda v: round(float(v), 1),
            inputs=[end_slider],
            outputs=[end_num],
        )
        mark_exclude_start_btn.click(
            fn=None,
            inputs=[preview_origin, exclude_start_slider, info_box],
            outputs=[exclude_start_slider],
            js=_CURRENT_PREVIEW_TIME_JS,
        ).then(
            lambda v: round(float(v), 1),
            inputs=[exclude_start_slider],
            outputs=[exclude_start_num],
        )
        mark_exclude_end_btn.click(
            fn=None,
            inputs=[preview_origin, exclude_end_slider, info_box],
            outputs=[exclude_end_slider],
            js=_CURRENT_PREVIEW_TIME_JS,
        ).then(
            lambda v: round(float(v), 1),
            inputs=[exclude_end_slider],
            outputs=[exclude_end_num],
        )
        exclude_start_slider.release(
            lambda v: round(v, 1),
            inputs=[exclude_start_slider],
            outputs=[exclude_start_num],
        )
        exclude_end_slider.release(
            lambda v: round(v, 1),
            inputs=[exclude_end_slider],
            outputs=[exclude_end_num],
        )
        exclude_start_slider.input(
            lambda v: round(v, 1),
            inputs=[exclude_start_slider],
            outputs=[exclude_start_num],
        )
        exclude_end_slider.input(
            lambda v: round(v, 1),
            inputs=[exclude_end_slider],
            outputs=[exclude_end_num],
        )
        refresh_exclude_sents_btn.click(
            refresh_sentences,
            inputs=[start_num, end_num, ctx_state],
            outputs=[
                sentences_state, exclude_start_sent_dd, exclude_end_sent_dd,
            ],
        )
        exclude_start_sent_dd.change(
            pick_sentence,
            inputs=[exclude_start_sent_dd, sentences_state, gr.State("start")],
            outputs=[exclude_start_num, exclude_start_slider],
        )
        exclude_end_sent_dd.change(
            pick_sentence,
            inputs=[exclude_end_sent_dd, sentences_state, gr.State("end")],
            outputs=[exclude_end_num, exclude_end_slider],
        )
        for button, target, slider, direction in exclusion_adjust_buttons:
            button.click(
                adjust_exclusion_time_with_step,
                inputs=[
                    slider, start_slider, end_slider,
                    exclude_adjust_step, gr.State(direction),
                ],
                outputs=[target],
            ).then(
                lambda v: gr.update(value=v),
                inputs=[target],
                outputs=[slider],
            )
        multi_preview_btn.click(
            preview_clip_plan,
            inputs=[start_num, end_num, ctx_state, clip_plan_state],
            outputs=[preview_video, info_box, transcript_box],
        )
        folder_btn.click(browse_folder, inputs=[out_dir_box], outputs=[out_dir_box])
        save_btn.click(
            on_save,
            inputs=[
                start_num, end_num, ctx_state, clip_plan_state,
                precise_chk, out_dir_box, filename_box,
            ],
            outputs=[saved_path],
        )

    with gr.Tab("動画の追加"):
        gr.Markdown(
            "新規動画を文字起こしして検索対象に追加します。"
            "動画の長さに応じて数分かかります(処理中はこのページを開いたままにしてください)。"
        )
        with gr.Row():
            new_video_box = gr.Textbox(label="動画ファイルのパス または URL", scale=3)
            video_browse_btn = gr.Button("ファイル参照", scale=1)
        with gr.Row():
            asr_model_dd = gr.Dropdown(
                choices=["large-v3", "large-v3-turbo", "medium"],
                value=config.ASR_MODEL_SIZE,
                label="ASRモデル (turbo/mediumは高速・低精度)",
            )
            force_chk = gr.Checkbox(value=False, label="再インデックス (既存を削除して作り直す)")
        with gr.Row():
            batch_infer_chk = gr.Checkbox(
                value=True,
                label="バッチ並列推論 (高速。切り出し境界がやや粗くなる)",
            )
            index_btn = gr.Button("インデックス作成", variant="primary")
            stop_btn = gr.Button("処理を停止", variant="stop")
        index_log = gr.Textbox(label="進捗ログ", interactive=False, lines=10)

        video_browse_btn.click(browse_video, inputs=[new_video_box], outputs=[new_video_box])
        index_btn.click(
            do_index,
            inputs=[new_video_box, asr_model_dd, force_chk, batch_infer_chk],
            outputs=[index_log, video_select],
        )
        stop_btn.click(stop_indexing)

    with gr.Tab("インデックスの共有"):
        gr.Markdown(
            "文字起こし済みのインデックスをzipファイルとして書き出し/読み込みし、"
            "他のPCと再文字起こしなしで共有できます。"
        )
        with gr.Row():
            gr.Markdown("### エクスポート")
        with gr.Row():
            export_video_select = gr.Dropdown(
                choices=list_video_choices_only(), label="エクスポートする動画", scale=3
            )
            export_reload_btn = gr.Button("動画リスト更新", scale=1)
        export_btn = gr.Button("エクスポート", variant="primary")
        export_file = gr.File(label="ダウンロード", interactive=False)
        export_path_box = gr.Textbox(label="保存先パス", interactive=False)

        with gr.Row():
            gr.Markdown("### インポート")
        import_file = gr.File(label="インポートするzipファイル", file_types=[".zip"])
        import_btn = gr.Button("インポート", variant="primary")
        import_log = gr.Textbox(label="進捗ログ", interactive=False, lines=8)

        export_reload_btn.click(
            lambda: gr.update(choices=list_video_choices_only()), outputs=[export_video_select]
        )
        export_btn.click(
            do_export, inputs=[export_video_select], outputs=[export_file, export_path_box]
        )
        import_btn.click(
            do_import, inputs=[import_file], outputs=[import_log, video_select]
        )


def _already_running(port: int = APP_PORT) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _cleanup_stale_index_job() -> None:
    """前回の異常終了で残ったインデックス処理のサブプロセスを停止する。

    ASRは途中保存されるため、停止しても次回は続きから再開できる。
    """
    import os
    import subprocess

    if not INDEX_JOB_PIDFILE.exists():
        return
    try:
        pid = int(INDEX_JOB_PIDFILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        INDEX_JOB_PIDFILE.unlink(missing_ok=True)
        return

    # 掃除は1回だけ試行する
    INDEX_JOB_PIDFILE.unlink(missing_ok=True)

    # 自分自身は絶対に停止しない。Windowsは終了プロセスのPIDを再利用するため、
    # 残留PIDが起動中のこのプロセスを指していると自殺してしまう。
    if pid == os.getpid():
        return

    # コマンドラインを確認し、index_video.py を実行しているプロセスだけを停止する。
    # (PID再利用で app.py 自身や無関係なpythonプロセスを誤って停止しないため)
    try:
        check = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.SubprocessError, OSError):
        return
    if "index_video" in (check.stdout or ""):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        print(f"前回の残留インデックス処理 (PID {pid}) を停止しました。"
              "文字起こしは途中保存から再開できます。")


if __name__ == "__main__":
    if _already_running():
        import webbrowser

        print(f"アプリは既に起動しています: http://127.0.0.1:{APP_PORT}")
        print("(二重起動を防ぐため、このプロセスは終了します)")
        webbrowser.open(f"http://127.0.0.1:{APP_PORT}")
        raise SystemExit(0)

    _cleanup_stale_index_job()
    demo.launch(
        server_name="127.0.0.1",
        server_port=APP_PORT,
        inbrowser=True,
        css=_APP_CSS,
    )
