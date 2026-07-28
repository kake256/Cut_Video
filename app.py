#!/usr/bin/env python
"""動画シーン検索GUI (Gradio)。

    python app.py

「検索・編集・切り抜き」タブ: 検索 → プレビュー → 範囲編集 → 保存。
「動画の追加」タブ: 新規動画の文字起こし〜インデックス化をWebUIから実行。

従来の「検索・切り抜き」画面は CUT_VIDEO_ENABLE_LEGACY_UI=1 のときだけ
退避UIとして表示する。
"""
import copy
import faulthandler
import hashlib
import html
import json
import math
import re
import secrets
import statistics
import subprocess
import threading
import time
from pathlib import Path

import gradio as gr

from moment_retrieval import config, db, utils
from cut_clip import cut_clip, cut_clips
from moment_retrieval.downloader import DownloadError, download_video
from moment_retrieval.embedder import TextEmbedder
from moment_retrieval.highlight_analysis import (
    QUERY_PROMPT_VERSION,
    build_query_highlight_candidates,
    valid_source_segments,
)
from moment_retrieval.preview_cache import (
    DEFAULT_LOCK_STRIPES,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_FILES,
    DEFAULT_TEMP_MAX_AGE_SEC,
    PreviewCache,
)
from moment_retrieval.refine import expand_to_speech_boundary
from moment_retrieval.search import search_chunks
from moment_retrieval.search_service import (
    SearchService,
    SEMANTIC_PENDING,
    retrieve_semantic_hits,
    semantic_error_code,
)
from moment_retrieval.staged_search import StagedSearchCoordinator
from moment_retrieval.edit_domain import (
    EditPlanError,
    TimelineMap,
    edit_plan_from_intuitive,
    edit_plan_from_kept_ranges,
    edit_plan_from_legacy,
    ms_to_seconds,
    make_effective_export_plan,
)
from moment_retrieval.share import ShareError, export_index, import_index, relink_video
from moment_retrieval.vector_index import VectorIndex
from moment_retrieval.application import DOCUMENTS
from moment_retrieval.save_service import save_document
from moment_retrieval.ui_experiment import UIExperimentRecorder, compare_ui_runs
from moment_retrieval.subtitles import map_subtitles
from moment_retrieval.ui_assets import _APP_CSS, _INTUITIVE_EDITOR_JS
from moment_retrieval.transcript_types import parse_segment

PREVIEW_DIR = config.CACHE_ROOT / "previews"
THUMBNAIL_DIR = config.CACHE_ROOT / "thumbnails"
ALL_VIDEOS_IMAGE = Path("assets/all_videos.svg")
VIDEO_UNAVAILABLE_IMAGE = Path("assets/video_unavailable.svg")
DEFAULT_CLIPS_DIR = "clips"
APP_PORT = 7860
PREVIEW_RENDER_TIMEOUT_SEC = 600
PREVIEW_CACHE_MAX_BYTES = DEFAULT_MAX_BYTES
PREVIEW_CACHE_MAX_FILES = DEFAULT_MAX_FILES
PREVIEW_TEMP_MAX_AGE_SEC = DEFAULT_TEMP_MAX_AGE_SEC
# 直感編集のカードグリッドは生HTMLでサムネイルを埋め込むため、Gradioのファイル配信
# キャッシュへ手動でコピーする必要がある。Blocksコンテキスト外でも動作する
# (`Block.__init__`がGRADIO_CACHEを設定するだけで、UIには一切表示しない)。
_THUMB_CACHE_BLOCK = gr.HTML()
INDEX_JOB_PIDFILE = config.CACHE_ROOT / "index_job.pid"
APP_PIDFILE = config.CACHE_ROOT / "app.pid"
ALL_VIDEOS_VALUE = "__all_videos__"

ADJUST_STEPS = [0.1, 1.0, 10.0, 30.0, 60.0, 600.0]

_embedder = None
_index_lock = threading.Lock()
_highlight_export_lock = threading.Lock()
_preview_cache_manager = PreviewCache(
    PREVIEW_DIR,
    max_bytes=PREVIEW_CACHE_MAX_BYTES,
    max_files=PREVIEW_CACHE_MAX_FILES,
    temp_max_age_sec=PREVIEW_TEMP_MAX_AGE_SEC,
    lock_stripes=DEFAULT_LOCK_STRIPES,
)
# Compatibility aliases for existing diagnostics/tests. Ownership lives in
# PreviewCache; app.py no longer manages these collections itself.
_preview_locks = _preview_cache_manager.locks
_preview_protected_outputs = _preview_cache_manager.protected_outputs
_crash_log = None
# 実行中のインデックス処理サブプロセス (停止ボタン用)
_index_state = {"proc": None, "stopped": False}
_ui_experiment_recorder = UIExperimentRecorder(
    config.CACHE_ROOT / "ui-experiment-v1.jsonl"
)
_intuitive_search_coordinator = StagedSearchCoordinator()


def start_ui_experiment(scenario: str, ui_variant: str, cold: bool):
    return {
        "scenario": scenario, "ui_variant": ui_variant, "cold": bool(cold),
        "started": time.perf_counter(),
    }, "計測中です。指定シナリオを最後まで操作してください。"


def complete_ui_experiment(state: dict, actions: float, errors: float, accepted: bool):
    if not state or "started" not in state:
        raise gr.Error("先に計測を開始してください。")
    duration_ms = round((time.perf_counter() - float(state["started"])) * 1000)
    _ui_experiment_recorder.record(
        state["scenario"], state["ui_variant"], state["cold"], duration_ms,
        int(actions or 0), int(errors or 0), bool(accepted),
    )
    report = compare_ui_runs(_ui_experiment_recorder.read())
    status = "候補UIを採用可能" if report["adopt_candidate"] else (
        "必要回数を計測済み・採用条件未達" if report["ready"] else "計測回数が不足"
    )
    return None, f"記録しました: {duration_ms / 1000:.1f}秒。判定: **{status}**"


def _enable_crash_log() -> None:
    """Enable native crash diagnostics only for an actual app process."""
    global _crash_log
    if _crash_log is not None:
        return
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _crash_log = open(
            config.DATA_DIR / "crash_trace.log",
            "a",
            encoding="utf-8",
            errors="replace",
        )
        faulthandler.enable(file=_crash_log)
    except OSError:
        _crash_log = None


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
        name = v.get("display_name") or Path(v["path"]).name
        label = f"{name}  —  {utils.format_timestamp(v['duration'])}"
        choices.append((label, v.get("public_video_id") or v["video_id"]))
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
        f"{video.get('public_video_id') or video.get('video_id')}|{source}|{stamp}".encode("utf-8")
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


def _video_gallery_data(filter_text: str = "", include_all: bool = True):
    """Build cached thumbnail cards once for either video-picker variant."""
    conn = db.get_conn()
    try:
        db.init_db(conn)
        videos = db.list_videos(conn)
    finally:
        conn.close()

    needle = (filter_text or "").strip().casefold()
    items = []
    video_ids = []
    if include_all:
        items.append((str(ALL_VIDEOS_IMAGE.resolve()), f"すべての動画（{len(videos)}本）"))
        video_ids.append(ALL_VIDEOS_VALUE)
    for video in videos:
        name = video.get("display_name") or Path(video["path"]).name
        if needle and needle not in name.casefold():
            continue
        thumbnail = _make_video_thumbnail(video)
        image = thumbnail or str(VIDEO_UNAVAILABLE_IMAGE.resolve())
        caption = f"{name}\n{utils.format_timestamp(video['duration'])}"
        items.append((image, caption))
        video_ids.append(video.get("public_video_id") or video["video_id"])
    return items, video_ids


def build_video_gallery(filter_text: str = "", selected_video_id: str = ALL_VIDEOS_VALUE):
    """サムネイル付きの検索対象メニューと、各カードの内部IDを返す。"""
    items, video_ids = _video_gallery_data(filter_text, include_all=True)

    try:
        selected_index = video_ids.index(selected_video_id)
    except ValueError:
        selected_index = None
    return gr.update(value=items, selected_index=selected_index), video_ids


def _intuitive_video_cards_data(
    filter_text: str = "", *, generate_thumbnails: bool = True
) -> list[dict]:
    """Individual videos (never the ALL card) enriched with picker metadata.

    Filtering happens before thumbnail generation so irrelevant files never
    invoke ffmpeg and each visible card carries its stable video ID directly.
    """
    conn = db.get_conn()
    try:
        db.init_db(conn)
        videos = db.list_videos(conn)
        indexed_ids = db.get_indexed_video_ids(conn)
    finally:
        conn.close()

    needle = (filter_text or "").strip().casefold()
    cards = []
    for video in videos:
        name = video.get("display_name") or Path(video["path"]).name
        if needle and needle not in name.casefold():
            continue
        cached_thumbnail = _thumbnail_path(video)
        thumbnail_path = (
            str(cached_thumbnail.resolve())
            if cached_thumbnail.exists()
            else (_make_video_thumbnail(video) if generate_thumbnails else None)
        )
        cards.append({
            "video_id": video.get("public_video_id") or video["video_id"],
            "name": name,
            "duration": float(video.get("duration") or 0.0),
            "thumbnail_path": thumbnail_path,
            "asr_complete": bool(video.get("asr_complete")),
            "indexed": (video.get("public_video_id") or video["video_id"]) in indexed_ids,
        })
    return cards


def _thumbnail_servable_url(path: str | None) -> str:
    """Convert a local thumbnail (or the placeholder asset) into a URL the raw HTML
    card grid can embed. Raw local paths cannot be used directly in an <img src>
    because Gradio only serves files it knows about (upload dir / its own cache)."""
    resolved = path or str(VIDEO_UNAVAILABLE_IMAGE.resolve())
    cached = _THUMB_CACHE_BLOCK.move_resource_to_block_cache(resolved)
    if not cached:
        return ""
    return "/gradio_api/file=" + Path(cached).as_posix()


def render_intuitive_video_cards(
    cards: list[dict], selected_video_id: str | None = None
) -> str:
    """Pure: badge-annotated card grid markup. `cards` must already be filtered
    and carry a `thumbnail_url` (a servable URL, not a raw filesystem path)."""
    if not cards:
        return '<div class="intuitive-video-card-empty">該当する動画がありません。</div>'

    selected_id = str(selected_video_id) if selected_video_id else None
    parts = ['<div class="intuitive-video-card-grid">']
    for index, card in enumerate(cards):
        video_id = str(card.get("video_id") or "")
        name = str(card.get("name") or "")
        duration = float(card.get("duration") or 0.0)
        escaped_name = html.escape(name)
        escaped_id = html.escape(video_id, quote=True)
        escaped_thumb = html.escape(str(card.get("thumbnail_url") or ""), quote=True)
        badges = []
        if card.get("asr_complete"):
            badges.append('<span class="intuitive-video-badge">文字起こし済み</span>')
        if card.get("indexed"):
            badges.append('<span class="intuitive-video-badge">索引あり</span>')
        selected_class = " is-selected" if selected_id and video_id == selected_id else ""
        parts.append(
            f'<button type="button" class="intuitive-video-card{selected_class}" '
            f'data-index="{index}" data-video-id="{escaped_id}" title="{escaped_name}">'
            '<span class="intuitive-video-thumb">'
            f'<img src="{escaped_thumb}" alt="" loading="lazy">'
            f'<span class="intuitive-video-duration">{utils.format_timestamp(duration)}</span>'
            '</span>'
            f'<span class="intuitive-video-name">{escaped_name}</span>'
            f'<span class="intuitive-video-badges">{"".join(badges)}</span>'
            '</button>'
        )
    parts.append('</div>')
    return "".join(parts)


def build_intuitive_video_cards(
    filter_text: str = "",
    selected_video_id: str = "",
    *,
    generate_thumbnails: bool = True,
) -> str:
    """Return the directly selectable video-card HTML (never the ALL card)."""
    cards = _intuitive_video_cards_data(
        filter_text, generate_thumbnails=generate_thumbnails
    )
    selected_video_id = parse_video_choice(selected_video_id)
    display_cards = [
        {**card, "thumbnail_url": _thumbnail_servable_url(card.get("thumbnail_path"))}
        for card in cards
    ]
    return render_intuitive_video_cards(display_cards, selected_video_id)


def _selected_gallery_index(video_ids: list[str], evt: gr.SelectData) -> int:
    index = evt.index[0] if isinstance(evt.index, (tuple, list)) else evt.index
    if not isinstance(index, int) or not 0 <= index < len(video_ids):
        raise gr.Error("動画を選択できませんでした。一覧を更新してください。")
    return index


def select_video_from_gallery(video_ids: list[str], evt: gr.SelectData):
    """カードを検索対象にして選択中表示を更新する。"""
    index = _selected_gallery_index(video_ids, evt)
    video_id = video_ids[index]
    thumbnail_update, detail = selected_video_info(video_id)
    return video_id, thumbnail_update, detail, gr.update(selected_index=index)


def region_transcript(conn, video_id: str, start: float, end: float) -> str:
    rows = db.get_segments_in_range(conn, video_id, start, end)
    return " ".join(str(row.get("text") or "") for row in rows)


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
            f"{r['score']:.3f}" if r.get("score") is not None else "—",
            r["text"][:60] + ("..." if len(r["text"]) > 60 else ""),
        ]
        for i, r in enumerate(results)
    ]


def _build_intuitive_table(results: list, selected_idx=None) -> list:
    """直感編集の狭い検索panel向けに、同じ結果を4列へ要約する。"""
    rows = []
    for i, result in enumerate(results):
        score = result.get("score")
        match = result.get("match_type") or "—"
        if score is not None:
            match = f"{match} {score:.3f}"
        source_text = str(result.get("text") or "")
        text = source_text[:54]
        if len(source_text) > 54:
            text += "..."
        video_name = result.get("video_name") or result.get("video_id") or "不明な動画"
        rows.append([
            f"{'●' if i == selected_idx else '○'} {i + 1}",
            f"{utils.format_timestamp(result['start'])}～"
            f"{utils.format_timestamp(result['end'])}",
            match,
            f"{video_name}｜{text}",
        ])
    return rows


def _intuitive_search_view_results(search_view) -> list[dict]:
    """Return results from the request-scoped adapter view.

    Plain lists remain accepted for the older direct-selection helpers and
    their characterization tests; staged search always emits the structured
    form so marker clicks can prove which request they belong to.
    """
    if isinstance(search_view, dict):
        results = search_view.get("results")
        return list(results) if isinstance(results, list) else []
    return list(search_view) if isinstance(search_view, list) else []


def _intuitive_search_view(request_id: str, results: list[dict]) -> dict:
    return {"request_id": str(request_id or ""), "results": list(results)}


def render_intuitive_search_marker_projection(search_view) -> str:
    """Serialize transient search hits for the overview marker adapter."""
    request_id = str(
        search_view.get("request_id") or ""
        if isinstance(search_view, dict) else ""
    )
    markers = []
    for result in _intuitive_search_view_results(search_view):
        hit_id = str(result.get("hit_id") or "")
        video_id = str(result.get("video_id") or "")
        if not hit_id or not video_id:
            continue
        try:
            start = float(result.get("evidence_start", result.get("start")))
            end = float(result.get("evidence_end", result.get("end")))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        if end < start:
            start, end = end, start
        match_type = str(result.get("match_type") or "")
        kind = "text" if match_type == "文字一致" else "semantic"
        evidence_midpoint = start + (end - start) / 2.0
        label_text = str(result.get("text") or "").strip().replace("\n", " ")
        if len(label_text) > 48:
            label_text = label_text[:48] + "..."
        markers.append({
            "hit_id": hit_id,
            "video_id": video_id,
            "kind": kind,
            "position": evidence_midpoint,
            "label": (
                f"{match_type or '検索結果'} {utils.format_timestamp(evidence_midpoint)}"
                + (f" {label_text}" if label_text else "")
            ),
        })
    payload = html.escape(
        json.dumps(
            {"request_id": request_id, "hits": markers},
            ensure_ascii=True, separators=(",", ":"),
        ),
        quote=True,
    )
    return (
        '<span data-intuitive-search-marker-projection '
        f'data-request-id="{html.escape(request_id, quote=True)}" '
        f'data-search-markers="{payload}" aria-hidden="true"></span>'
    )


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


def _search_service_for_connection(
    conn, start_sec: float | None = None, end_sec: float | None = None
) -> SearchService:
    """Build the shared text/semantic service for one publication snapshot."""
    service: SearchService

    def semantic_retriever(text, public_id, limit, threshold):
        return retrieve_semantic_hits(
            conn,
            service.publication_snapshot,
            text,
            public_id,
            limit,
            threshold,
            legacy_index_path=config.TEXT_INDEX_PATH,
            generations_dir=config.search_generations_dir(),
            encode_query=lambda value: get_embedder().encode([value]),
            index_loader=VectorIndex.load,
            start_sec=start_sec,
            end_sec=end_sec,
        )

    service = SearchService(conn, semantic_retriever)
    return service


def _decorate_search_results(conn, hits) -> list[dict]:
    results = [hit.to_legacy_dict() for hit in hits]
    videos_by_id = {}
    for video in db.list_videos(conn):
        filename = video.get("display_name") or Path(video["path"]).name
        public_id = video.get("public_video_id") or video["video_id"]
        videos_by_id[public_id] = f"{filename} [{public_id}]"
    for result in results:
        result["video_name"] = videos_by_id.get(
            result["video_id"], result["video_id"]
        )
    return results


def _decorate_staged_search_results(hits) -> list[dict]:
    conn = db.get_conn()
    db.init_db(conn)
    try:
        return _decorate_search_results(conn, hits)
    finally:
        conn.close()


def search_video_results(
    query: str,
    video_choice: str,
    top_k: int,
    min_score: float,
    range_enabled: bool = False,
    range_start: float = 0.0,
    range_end: float = 0.0,
):
    if not query.strip():
        return []
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
        # Compatibility for lightweight adapter/test connections. Production
        # sqlite connections always take the shared SearchService path below.
        if not hasattr(conn, "execute"):
            query_vec = get_embedder().encode([query])
            vindex = VectorIndex.load(config.TEXT_INDEX_PATH, query_vec.shape[1])
            results = search_chunks(
                conn, vindex, query, query_vec, top_k=int(top_k),
                min_score=float(min_score), video_id=video_filter,
                start_sec=start_sec, end_sec=end_sec,
            )
            videos_by_id = {
                video["video_id"]: f"{Path(video['path']).name} [{video['video_id']}]"
                for video in db.list_videos(conn)
            }
            for result in results:
                result["video_name"] = videos_by_id.get(result["video_id"], result["video_id"])
            return results

        service = _search_service_for_connection(conn, start_sec, end_sec)
        text_hits, semantic_hits = service.search(
            query, public_video_id=video_filter, text_limit=int(top_k),
            semantic_limit=int(top_k), min_score=float(min_score),
            start_ms=round(start_sec * 1000) if start_sec is not None else None,
            end_ms=round(end_sec * 1000) if end_sec is not None else None,
        )
        semantic_status = semantic_error_code(service.semantic_error)
        if semantic_status == SEMANTIC_PENDING:
            gr.Info("意味検索の準備中です。文字一致の結果のみ表示します。")
        elif semantic_status:
            gr.Warning("意味検索を利用できないため、文字一致の結果のみ表示します。")
        return _decorate_search_results(conn, (*text_hits, *semantic_hits))
    finally:
        conn.close()


def do_search(
    query: str,
    video_choice: str,
    top_k: int,
    min_score: float,
    range_enabled: bool = False,
    range_start: float = 0.0,
    range_end: float = 0.0,
):
    results = search_video_results(
        query, video_choice, top_k, min_score,
        range_enabled, range_start, range_end,
    )
    if not results:
        if not query.strip():
            return ([], [], *_EMPTY_SELECTION)
        gr.Info("該当するシーンが見つかりませんでした。")
        return ([], [], *_EMPTY_SELECTION)

    # 検索直後は先頭候補(行クリックと同じ処理)を自動で選択する
    table = _build_table(results, selected_idx=0)
    return (table, results, *_select_result(0, results))


def _preview_source_fingerprint(source: Path, source_size: int) -> str:
    """Compatibility wrapper for the pure cache implementation."""
    return PreviewCache.source_fingerprint(source, source_size)


def _preview_source_version(source: Path) -> tuple[int, int, str | None]:
    """Compatibility wrapper for stable stat plus sampled identity."""
    return PreviewCache.source_version(source)


def _configured_preview_cache() -> PreviewCache:
    """Apply app-level overrides while retaining the manager's lock state."""
    _preview_cache_manager.configure(
        directory=PREVIEW_DIR,
        max_bytes=PREVIEW_CACHE_MAX_BYTES,
        max_files=PREVIEW_CACHE_MAX_FILES,
        temp_max_age_sec=PREVIEW_TEMP_MAX_AGE_SEC,
    )
    return _preview_cache_manager


def _preview_cache_path(
    prefix: str,
    video_id: str | None,
    video_path: str,
    ranges: list[tuple[float, float]],
) -> Path:
    """Return a safe cache path unique to a source version and exact ranges."""
    return _configured_preview_cache().cache_path(
        prefix, video_id, video_path, ranges
    )


def _preview_path(
    video_id: str | None, video_path: str, start: float, end: float
) -> Path:
    """Return a source/version/range-specific cache path for one legacy range."""
    return _preview_cache_path(
        "preview", video_id, video_path, [(start, end)]
    )


def _multi_preview_path(
    video_id: str | None,
    video_path: str,
    ranges: list[list[float]] | list[tuple[float, float]],
) -> Path:
    """Return a source/version/ranges-specific cache path for a joined preview."""
    return _preview_cache_path(
        "preview_multi", video_id, video_path,
        [(start, end) for start, end in ranges],
    )


def _preview_lock_for(out: Path) -> threading.Lock:
    """Return one fixed stripe; the lock set cannot grow with cache entries."""
    return _configured_preview_cache().lock_for(out)


def _is_completed_preview_file(path: Path) -> bool:
    return PreviewCache.is_completed_file(path)


def _prune_preview_cache(
    protected: Path | None = None,
    *,
    cleanup_stale_temps: bool = False,
    now: float | None = None,
) -> None:
    _configured_preview_cache().prune(
        protected,
        cleanup_stale_temps=cleanup_stale_temps,
        now=now,
    )


def _touch_preview_cache_file(path: Path) -> None:
    PreviewCache.touch(path)


def _initialize_preview_cache() -> None:
    """Run cache maintenance only during real application startup."""
    _prune_preview_cache(cleanup_stale_temps=True)


def _create_cached_preview(out: Path, renderer) -> str:
    """Render once through a unique temporary file, then atomically publish it."""
    return _configured_preview_cache().create(out, renderer)


def make_preview(
    video_path: str,
    start: float,
    end: float,
    duration: float,
    video_id: str | None = None,
) -> str:
    out = _preview_path(video_id, video_path, start, end)
    return _create_cached_preview(
        out,
        lambda temporary: cut_clip(
            Path(video_path), start, end, temporary,
            pad=0.0, precise=False, duration=duration,
            timeout_sec=PREVIEW_RENDER_TIMEOUT_SEC,
        ),
    )


def _intuitive_preview_path(
    video_id: str, video_path: str, start: float, end: float
) -> Path:
    """動画をまたいで衝突しない、ファイル名として安全な試作プレビュー保存先。"""
    return _preview_cache_path(
        "intuitive", video_id, video_path, [(start, end)]
    )


def make_intuitive_preview(
    video_id: str, video_path: str, start: float, end: float, duration: float
) -> str:
    """直感編集試作用の動画ID別プレビューをローカルに生成する。"""
    out = _intuitive_preview_path(video_id, video_path, start, end)
    return _create_cached_preview(
        out,
        lambda temporary: cut_clip(
            Path(video_path), start, end, temporary,
            pad=0.0, precise=False, duration=duration,
            timeout_sec=PREVIEW_RENDER_TIMEOUT_SEC,
        ),
    )


def render_intuitive_transcript(segments: list[dict], state: dict | None = None) -> str:
    """単語時刻を優先し、安全にエスケープした試作用文字起こしHTMLを返す。"""
    decoration_exclusions = _clip_intuitive_exclusions(state) if state else []

    def classes_for(start: float, end: float, segment: bool = False) -> str:
        # data-start/data-end are the browser-side interaction contract and are
        # serialized to milliseconds below.  Use those same values for the
        # full-render decoration pass so a later JS projection cannot disagree
        # at a sub-millisecond boundary.
        start = float(f"{start:.3f}")
        end = float(f"{end:.3f}")
        classes = ["intuitive-word"]
        if segment:
            classes.append("intuitive-segment")
        if not state:
            return " ".join(classes)
        selected = state.get("selected_word") or {}
        if (
            abs(float(selected.get("start", -999999)) - start) < 0.002
            and abs(float(selected.get("end", -999999)) - end) < 0.002
        ):
            classes.append("is-selected-word")
        if end <= state["overall_start"] or start >= state["overall_end"]:
            classes.append("is-outside-overall")
        if any(
            end > cut["start"] and start < cut["end"]
            for cut in decoration_exclusions
        ):
            classes.append("is-excluded-word")
        contains_start = lambda value: (
            start <= value < end or (start == end and abs(value - start) < 0.002)
        )
        contains_end = lambda value: (
            start < value <= end or (start == end and abs(value - start) < 0.002)
        )
        if contains_start(state["overall_start"]):
            classes.append("marks-overall-start")
        if contains_end(state["overall_end"]):
            classes.append("marks-overall-end")
        pending = state.get("pending_cut_start")
        if pending is not None and contains_start(float(pending)):
            classes.append("marks-pending-cut")
        if any(contains_start(float(cut["start"])) for cut in decoration_exclusions):
            classes.append("marks-exclusion-start")
        if any(contains_end(float(cut["end"])) for cut in decoration_exclusions):
            classes.append("marks-exclusion-end")
        return " ".join(classes)

    def timed_words_for(segment: dict) -> list[tuple[str, float, float]] | None:
        """Return words only when the segment has complete, real word timestamps.

        Falling back missing word timestamps to the segment boundary makes the UI
        look word-accurate even though the ASR data is not. Treat the whole
        segment as the smallest selectable unit unless every stored word has an
        explicit, finite time range.
        """
        words_json = segment.get("words_json")
        if not words_json:
            return None
        try:
            parsed = json.loads(words_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, list) or not parsed:
            return None

        timed_words = []
        for word in parsed:
            if (
                not isinstance(word, dict)
                or "word" not in word
                or "start" not in word
                or "end" not in word
                or isinstance(word.get("start"), bool)
                or isinstance(word.get("end"), bool)
            ):
                return None
            try:
                start = float(word["start"])
                end = float(word["end"])
            except (TypeError, ValueError):
                return None
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                return None
            timed_words.append((str(word["word"]), start, end))
        return timed_words

    def row_time_chip(segment: dict) -> str:
        """Display-only start-time chip for the row; never invents a missing time."""
        raw_start = segment.get("start_sec")
        if raw_start is None:
            return ""
        try:
            row_start = float(raw_start)
        except (TypeError, ValueError):
            return ""
        if not math.isfinite(row_start):
            return ""
        return (
            '<span class="intuitive-ts-chip">'
            f'{utils.format_timestamp(row_start)}</span>'
        )

    rendered_segments = []
    granularities = set()
    first_focusable = True
    for raw_segment in segments:
        segment = dict(raw_segment)
        words = timed_words_for(segment)
        chip = row_time_chip(segment)

        word_spans = []
        for word in words or []:
            text, start, end = word
            label = f"単語時刻: {utils.format_timestamp(start)} - {utils.format_timestamp(end)}"
            accessible_label = f"{label}: {text}"
            tab_index = 0 if first_focusable else -1
            first_focusable = False
            word_spans.append(
                f'<span class="{classes_for(start, end)}" '
                f'role="button" tabindex="{tab_index}" '
                f'data-time-granularity="word" '
                f'data-start="{start:.3f}" data-end="{end:.3f}" '
                f'aria-label="{html.escape(accessible_label, quote=True)}" '
                f'title="{html.escape(label, quote=True)}">'
                f'{html.escape(text)}</span>'
            )

        if word_spans:
            granularities.add("word")
            rendered_segments.append(
                '<div class="intuitive-segment-row">'
                f'{chip}<div class="intuitive-segment-row-content">'
                f'{"".join(word_spans)}</div></div>'
            )
            continue

        granularities.add("segment")
        start = float(segment.get("start_sec") or 0.0)
        end = float(segment.get("end_sec") or start)
        label = (
            "発話区間（単語時刻なし）: "
            f"{utils.format_timestamp(start)} - {utils.format_timestamp(end)}"
        )
        accessible_label = f'{label}: {str(segment.get("text") or "")}'
        tab_index = 0 if first_focusable else -1
        first_focusable = False
        segment_span = (
            f'<span class="{classes_for(start, end, segment=True)}" '
            f'role="button" tabindex="{tab_index}" '
            f'data-time-granularity="segment" '
            f'data-start="{start:.3f}" data-end="{end:.3f}" '
            f'aria-label="{html.escape(accessible_label, quote=True)}" '
            f'title="{html.escape(label, quote=True)}">'
            f'{html.escape(str(segment.get("text") or ""))}</span>'
        )
        rendered_segments.append(
            '<div class="intuitive-segment-row">'
            f'{chip}<div class="intuitive-segment-row-content">'
            f'{segment_span}</div></div>'
        )

    content = "".join(rendered_segments)
    if not content:
        content = '<span class="intuitive-transcript-empty">この区間に文字起こしはありません。</span>'
        granularity_label = "時刻指定できる文字起こしがありません"
    elif granularities == {"word"}:
        granularity_label = "時刻指定: 単語単位（ASRの単語時刻）"
    elif granularities == {"segment"}:
        granularity_label = "時刻指定: 発話区間単位（単語時刻なし）"
    else:
        granularity_label = "時刻指定: 単語単位＋発話区間単位（区間ごとのASR精度）"
    range_label = ""
    if state:
        transcript_start, transcript_end = _intuitive_transcript_bounds(state)
        range_label = (
            '<span class="intuitive-transcript-range">表示中: '
            f'{utils.format_timestamp(transcript_start)} ～ '
            f'{utils.format_timestamp(transcript_end)}</span>'
        )
    return (
        '<div class="intuitive-transcript-copy" aria-label="プレビュー区間の文字起こし">'
        '<div class="intuitive-transcript-granularity">'
        f'<span>{granularity_label}</span>{range_label}</div>'
        f"{content}</div>"
    )


def render_intuitive_overview_timeline(
    duration: float, start: float, end: float
) -> str:
    duration = max(float(duration or 0.0), 0.1)
    left = max(0.0, min(100.0, start / duration * 100.0))
    right = max(left, min(100.0, end / duration * 100.0))
    return f"""
    <div class="intuitive-timeline">
      <div class="intuitive-timeline-scale"><span>00:00:00</span><strong>5-1. 動画全体の概要タイムライン</strong><span>{utils.format_timestamp(duration)}</span></div>
      <div class="intuitive-overview-track" role="img" aria-label="動画全体と拡大表示範囲">
        <div class="intuitive-overview-window" style="left:{left:.4f}%;width:{right - left:.4f}%" title="下段で拡大している範囲"></div>
      </div>
    </div>
    """


def render_intuitive_zoom_timeline(start: float, end: float) -> str:
    midpoint = start + (end - start) / 2.0
    return f"""
    <div class="intuitive-timeline">
      <div class="intuitive-inline-section-title">5-2. 拡大編集タイムライン</div>
      <div class="intuitive-timeline-scale"><span>{utils.format_timestamp(start)}</span><span>{utils.format_timestamp(midpoint)}</span><span>{utils.format_timestamp(end)}</span></div>
      <div class="intuitive-zoom-track" role="img" aria-label="保存範囲、除外範囲、再生位置の試作タイムライン">
        <div class="intuitive-handle start" title="全体開始ハンドル"></div>
        <div class="intuitive-cut-zone" title="途中カットの表示例"></div>
        <div class="intuitive-playhead" title="プレビュー再生位置"></div>
        <div class="intuitive-handle end" title="全体終了ハンドル"></div>
      </div>
      <div class="intuitive-timeline-legend">
        <span class="intuitive-legend-keep">■ 保存範囲</span>
        <span class="intuitive-legend-cut">▨ 途中カット（表示例）</span>
        <span class="intuitive-legend-position">│ 再生位置</span>
        <span>● オレンジ: 全体範囲ハンドル</span>
      </div>
    </div>
    """


def load_intuitive_video(video_choice: str):
    """インデックス済み動画の最初の発話付近を編集画面へ読み込む。"""
    video_id = parse_video_choice(video_choice)
    if not video_id:
        raise gr.Error("直感編集で試す動画を選択してください。")

    conn = db.get_conn()
    try:
        video = db.get_video(conn, video_id)
        if not video:
            raise gr.Error(f"動画が見つかりません: {video_id}")
        first_segment = db.get_first_text_segment(conn, video["video_id"])
        duration = max(float(video.get("duration") or 0.0), 0.0)
        first_start = float(first_segment["start_sec"]) if first_segment else 0.0
        preview_start = max(0.0, first_start - 2.0)
        effective_duration = duration or max(preview_start + 90.0, 90.0)
        preview_end = min(effective_duration, preview_start + 90.0)
        if preview_end <= preview_start:
            raise gr.Error("プレビューできる長さの動画ではありません。")
        segments = db.get_segments_in_range(
            conn, video["video_id"], preview_start, preview_end
        )
    finally:
        conn.close()

    preview_path = make_intuitive_preview(
        video_id, video["path"], preview_start, preview_end, effective_duration
    )
    filename = Path(video["path"]).name
    interval = (
        f"{utils.format_timestamp(preview_start)} - "
        f"{utils.format_timestamp(preview_end)}"
    )
    info = f"**選択動画:** {html.escape(filename)}　｜　**表示区間:** {interval}"
    return (
        gr.update(
            value=preview_path,
            label=(
                f"1. 動画プレビュー（Source timeline）: "
                f"{filename} [{video_id}]"
            ),
        ),
        render_intuitive_transcript(segments),
        info,
        render_intuitive_overview_timeline(effective_duration, preview_start, preview_end),
        render_intuitive_zoom_timeline(preview_start, preview_end),
    )


_INTUITIVE_TOOLS = {
    "overall_start", "overall_end", "exclude_start", "exclude_end",
}
_INTUITIVE_TOOL_LABELS = (
    ("overall_start", "全体開始"),
    ("overall_end", "全体終了"),
    ("exclude_start", "除外開始"),
    ("exclude_end", "除外終了"),
)
_INTUITIVE_VIEWPORT_MIN_SECONDS = 5.0
# A 90-second initial viewport keeps first load quick, but it must not also be a
# hard resize ceiling: users commonly widen the overview window after finding a
# scene.  Ten minutes is a practical upper bound for the generated source
# preview while still making the resize operation useful and predictable.
_INTUITIVE_VIEWPORT_MAX_SECONDS = 600.0
_INTUITIVE_HISTORY_LIMIT = 50


def _intuitive_transcript_bounds(
    state: dict, focus: float | None = None,
) -> tuple[float, float]:
    """Return transcript bounds that exactly match the zoom viewport.

    ``focus`` remains accepted for compatibility with already queued browser
    commands, but changing the playhead no longer creates a second, independent
    transcript window inside the visible timeline range.
    """
    view_start = float(state["viewport_start"])
    view_end = max(view_start, float(state["viewport_end"]))
    return view_start, view_end


def _intuitive_transcript_projection(state: dict) -> str:
    """Small canonical projection used to restyle the existing transcript DOM."""
    selected = state.get("selected_word") or None
    payload = {
        "overall_start": float(state["overall_start"]),
        "overall_end": float(state["overall_end"]),
        "exclusions": [
            [float(cut["start"]), float(cut["end"])]
            for cut in _clip_intuitive_exclusions(state)
        ],
        "pending_cut_start": (
            None
            if state.get("pending_cut_start") is None
            else float(state["pending_cut_start"])
        ),
        "selected_word": (
            None
            if not selected
            else [float(selected["start"]), float(selected["end"])]
        ),
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _set_intuitive_transcript_focus(state: dict, focus: float | None = None) -> None:
    """Store a canonical focus and the viewport-aligned transcript interval."""
    view_start = float(state["viewport_start"])
    view_end = float(state["viewport_end"])
    if focus is None:
        focus = state.get("transcript_focus_sec", view_start)
    try:
        focus = float(focus)
    except (TypeError, ValueError):
        focus = view_start
    if not math.isfinite(focus):
        focus = view_start
    focus = min(max(focus, view_start), view_end)
    start, end = _intuitive_transcript_bounds(state, focus)
    state["transcript_focus_sec"] = focus
    state["transcript_start"] = start
    state["transcript_end"] = end


def _new_intuitive_state(video: dict, start: float, end: float) -> dict:
    duration = max(float(video.get("duration") or end), end)
    state = {
        "revision": 0,
        "nonce": secrets.token_hex(12),
        "video_id": str(video["video_id"]),
        "public_video_id": str(video.get("public_video_id") or video["video_id"]),
        "video_path": str(video["path"]),
        "duration": duration,
        "preview_start": float(start),
        "preview_end": float(end),
        "overall_start": float(start),
        "overall_end": float(end),
        "exclusions": [],
        "active_tool": None,
        "preview_mode": "source",
        "pending_cut_start": None,
        "selected_boundary": None,
        "selected_word": None,
        "viewport_start": float(start),
        "viewport_end": float(end),
        "playhead_sec": float(start),
        "last_command_id": None,
        "last_command_status": "",
        "edit_dirty": False,
        "undo_stack": [],
        "redo_stack": [],
        # OFF by default: this is only the drag-edit lock.  Tool selection and
        # click-to-set-boundary are controlled by active_tool, independently of
        # this flag; handles and empty-track cut drags remain locked.
        "timeline_edit_mode": False,
    }
    state["baseline_plan"] = _intuitive_plan_snapshot(state)
    domain_plan = edit_plan_from_intuitive(state)
    document = DOCUMENTS.open(
        str(video.get("public_video_id") or video["video_id"]),
        str(video.get("source_generation") or "unknown"),
        domain_plan,
    )
    state["document_id"] = document.document_id
    _set_intuitive_transcript_focus(state, start)
    return state


def _intuitive_edit_signature(state: dict) -> tuple:
    """Semantic saved-plan signature; IDs and display-only state are excluded."""
    exclusions = tuple(
        (
            float(cut["start"]),
            float(cut["end"]),
        )
        for cut in _clip_intuitive_exclusions(state)
    )
    return (
        float(state["overall_start"]),
        float(state["overall_end"]),
        exclusions,
    )


def _intuitive_plan_snapshot(state: dict) -> dict:
    """Deep canonical edit plan used by history and the saved baseline."""
    return {
        "overall_start": float(state["overall_start"]),
        "overall_end": float(state["overall_end"]),
        "exclusions": [
            {
                "id": str(cut.get("id") or ""),
                "start": float(cut["start"]),
                "end": float(cut["end"]),
            }
            for cut in _clip_intuitive_exclusions(state)
        ],
    }


def _restore_intuitive_plan(state: dict, snapshot: dict) -> None:
    """Restore only canonical plan fields, preserving the current session."""
    state["overall_start"] = float(snapshot["overall_start"])
    state["overall_end"] = float(snapshot["overall_end"])
    state["exclusions"] = [
        {
            "id": str(cut.get("id") or ""),
            "start": float(cut["start"]),
            "end": float(cut["end"]),
        }
        for cut in snapshot.get("exclusions") or []
    ]


def _clear_intuitive_gesture_state(state: dict) -> None:
    state["pending_cut_start"] = None
    state["active_tool"] = None
    state["selected_boundary"] = None
    state["selected_word"] = None


def _bounded_intuitive_history(stack: list[dict]) -> list[dict]:
    return copy.deepcopy(list(stack)[-_INTUITIVE_HISTORY_LIMIT:])


def _finite_time(value, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}が数値ではありません。") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name}が有限値ではありません。")
    return result


def _clip_intuitive_exclusions(state: dict) -> list[dict]:
    """全体範囲との交差だけを残し、重複区間をID付きで統合する。"""
    lo = float(state["overall_start"])
    hi = float(state["overall_end"])
    clipped = []
    for raw in state.get("exclusions") or []:
        try:
            start = max(lo, _finite_time(raw.get("start"), "除外開始"))
            end = min(hi, _finite_time(raw.get("end"), "除外終了"))
        except (AttributeError, ValueError):
            continue
        if end <= start + 0.001:
            continue
        clipped.append({"id": str(raw.get("id") or "cut"), "start": start, "end": end})
    clipped.sort(key=lambda cut: (cut["start"], cut["end"]))
    merged = []
    for cut in clipped:
        if merged and cut["start"] <= merged[-1]["end"] + 0.001:
            merged[-1]["end"] = max(merged[-1]["end"], cut["end"])
        else:
            merged.append(dict(cut))
    return merged


def _validate_intuitive_exclusion_invariant(state: dict) -> None:
    """Reject a canonical edit plan whose exclusions cover its whole range."""
    exclusions = state.get("exclusions") or []
    if not exclusions:
        return
    overall_span = float(state["overall_end"]) - float(state["overall_start"])
    excluded_span = sum(
        float(cut["end"]) - float(cut["start"]) for cut in exclusions
    )
    if excluded_span >= overall_span - 0.001:
        raise ValueError("全体範囲のすべてを除外することはできません。")


def _set_intuitive_boundary(state: dict, boundary: dict, value: float) -> None:
    value = _finite_time(value, "境界時刻")
    duration = float(state["duration"])
    value = min(max(value, 0.0), duration)
    kind = boundary.get("kind")
    if kind == "overall_start":
        state["overall_start"] = min(value, state["overall_end"] - 0.1)
    elif kind == "overall_end":
        state["overall_end"] = max(value, state["overall_start"] + 0.1)
    elif kind == "pending_cut_start":
        state["pending_cut_start"] = min(
            max(value, state["overall_start"]), state["overall_end"] - 0.1
        )
    elif kind in {"exclusion_start", "exclusion_end"}:
        cut = next(
            (item for item in state["exclusions"] if item["id"] == boundary.get("id")),
            None,
        )
        if not cut:
            raise ValueError("選択中の除外区間が見つかりません。")
        if kind == "exclusion_start":
            cut["start"] = min(
                max(value, state["overall_start"]), cut["end"] - 0.1
            )
        else:
            cut["end"] = max(
                min(value, state["overall_end"]), cut["start"] + 0.1
            )
    else:
        raise ValueError("調整する境界が選択されていません。")
    state["exclusions"] = _clip_intuitive_exclusions(state)
    if (
        state["exclusions"]
        and sum(c["end"] - c["start"] for c in state["exclusions"])
        >= state["overall_end"] - state["overall_start"] - 0.001
    ):
        raise ValueError("全体範囲のすべてを除外することはできません。")


def _remap_intuitive_boundary(state: dict, boundary: dict | None, anchor=None):
    """除外統合後も、同じ時刻を含む生存cutへ選択境界を付け替える。"""
    if not isinstance(boundary, dict):
        return None
    kind = boundary.get("kind")
    if kind not in {"exclusion_start", "exclusion_end"}:
        if kind == "pending_cut_start" and state.get("pending_cut_start") is None:
            return None
        return dict(boundary)
    same = next(
        (cut for cut in state.get("exclusions") or [] if cut["id"] == boundary.get("id")),
        None,
    )
    if same:
        return {"kind": kind, "id": same["id"]}
    try:
        value = _finite_time(anchor, "選択境界")
    except ValueError:
        return None
    survivor = next(
        (cut for cut in state.get("exclusions") or []
         if cut["start"] - 0.001 <= value <= cut["end"] + 0.001),
        None,
    )
    return {"kind": kind, "id": survivor["id"]} if survivor else None


def _apply_intuitive_word_tool(state: dict, word: dict) -> None:
    """選択済み単語と選択済みツールを、操作順に依存せず適用する。"""
    word_start = _finite_time(word.get("start"), "単語開始")
    word_end = _finite_time(word.get("end"), "単語終了")
    if word_end < word_start:
        raise ValueError("単語の時刻範囲が不正です。")
    normalized_word = {"start": word_start, "end": word_end}
    tool = state.get("active_tool")
    if tool not in _INTUITIVE_TOOLS:
        state["selected_word"] = normalized_word
        return
    if tool == "overall_start":
        boundary = {"kind": "overall_start"}
        _set_intuitive_boundary(state, boundary, word_start)
        state["selected_boundary"] = boundary
        state["active_tool"] = None
        state["selected_word"] = None
    elif tool == "overall_end":
        boundary = {"kind": "overall_end"}
        _set_intuitive_boundary(state, boundary, word_end)
        state["selected_boundary"] = boundary
        state["active_tool"] = None
        state["selected_word"] = None
    elif tool == "exclude_start":
        boundary = {"kind": "pending_cut_start"}
        _set_intuitive_boundary(state, boundary, word_start)
        state["selected_boundary"] = boundary
        state["selected_word"] = None
        state["active_tool"] = "exclude_end"
    else:
        pending = state.get("pending_cut_start")
        if pending is None:
            raise ValueError("先に除外開始を指定してください。")
        if word_end <= float(pending) + 0.001:
            raise ValueError("除外終了は除外開始より後にしてください。")
        cut_start = max(float(pending), state["overall_start"])
        cut_end = min(word_end, state["overall_end"])
        if cut_end <= cut_start + 0.001:
            raise ValueError("除外終了は除外開始より後にしてください。")
        cut_id = f"cut-{state['revision'] + 1}-{len(state['exclusions']) + 1}"
        state["exclusions"].append(
            {"id": cut_id, "start": cut_start, "end": cut_end}
        )
        state["exclusions"] = _clip_intuitive_exclusions(state)
        if sum(c["end"] - c["start"] for c in state["exclusions"]) >= (
            state["overall_end"] - state["overall_start"] - 0.001
        ):
            raise ValueError("全体範囲のすべてを除外することはできません。")
        chosen = next(
            (c for c in state["exclusions"] if c["start"] <= cut_end <= c["end"] + 0.001),
            state["exclusions"][-1],
        )
        state["selected_boundary"] = {
            "kind": "exclusion_end", "id": chosen["id"]
        }
        state["pending_cut_start"] = None
        state["selected_word"] = None
        state["active_tool"] = None


def dispatch_intuitive_command(command_json, state: dict | None) -> dict:
    """直感編集の全操作を検証し、単一のcanonical stateへ反映する。"""
    if not isinstance(state, dict) or not state.get("nonce"):
        raise gr.Error("先に動画を読み込んでください。")
    if isinstance(command_json, str) and len(command_json) > 65536:
        raise gr.Error("編集コマンドが長すぎます。")
    try:
        command = json.loads(command_json) if isinstance(command_json, str) else command_json
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise gr.Error("編集コマンドを読み取れませんでした。") from exc
    if not isinstance(command, dict):
        raise gr.Error("編集コマンドの形式が不正です。")
    try:
        revision = int(command.get("revision"))
    except (TypeError, ValueError) as exc:
        raise gr.Error("編集コマンドのrevisionが不正です。") from exc
    if revision != int(state.get("revision", -1)) or command.get("nonce") != state["nonce"]:
        raise gr.Error("古い画面からの操作を破棄しました。最新表示で再操作してください。")
    if (
        state.get("preview_mode", "source") == "result"
        and command.get("type") not in {"set_viewport", "set_tool"}
    ):
        raise gr.Error("編集結果のプレビュー中です。動画を再読み込みして編集を再開してください。")

    initial_plan = _intuitive_plan_snapshot(state)
    initial_edit_signature = _intuitive_edit_signature(initial_plan)
    next_state = copy.deepcopy(state)
    next_state["undo_stack"] = _bounded_intuitive_history(
        state.get("undo_stack") or []
    )
    next_state["redo_stack"] = _bounded_intuitive_history(
        state.get("redo_stack") or []
    )
    next_state["baseline_plan"] = copy.deepcopy(
        state.get("baseline_plan") or initial_plan
    )
    action = command.get("type")
    transcript_focus = None
    try:
        if (
            state.get("preview_mode", "source") != "result"
            and command.get("playhead_sec") is not None
        ):
            source_playhead = _finite_time(
                command.get("playhead_sec"), "プレビュー再生位置"
            )
            preview_start = max(0.0, float(next_state["preview_start"]))
            preview_end = min(
                float(next_state["duration"]), float(next_state["preview_end"])
            )
            next_state["playhead_sec"] = min(
                max(source_playhead, preview_start), max(preview_start, preview_end)
            )
        if action == "undo":
            if not next_state["undo_stack"]:
                raise ValueError("元に戻せる編集はありません。")
            target = next_state["undo_stack"].pop()
            next_state["redo_stack"].append(initial_plan)
            next_state["redo_stack"] = _bounded_intuitive_history(
                next_state["redo_stack"]
            )
            _restore_intuitive_plan(next_state, target)
            _clear_intuitive_gesture_state(next_state)
        elif action == "redo":
            if not next_state["redo_stack"]:
                raise ValueError("やり直せる編集はありません。")
            target = next_state["redo_stack"].pop()
            next_state["undo_stack"].append(initial_plan)
            next_state["undo_stack"] = _bounded_intuitive_history(
                next_state["undo_stack"]
            )
            _restore_intuitive_plan(next_state, target)
            _clear_intuitive_gesture_state(next_state)
        elif action == "set_tool":
            tool = command.get("tool")
            if tool not in _INTUITIVE_TOOLS:
                raise ValueError("不明な編集ツールです。")
            if next_state.get("preview_mode") == "result":
                # 連結済みプレビューの再生時刻を元動画の絶対時刻として扱わない。
                # 元動画への復帰はhandle_intuitive_command側で完了してから、
                # 選択したツールを有効にする。
                next_state["preview_mode"] = "source"
                next_state["active_tool"] = tool
            else:
                next_state["active_tool"] = (
                    None if next_state.get("active_tool") == tool else tool
                )
            if (
                next_state.get("pending_cut_start") is not None
                and next_state.get("active_tool") != "exclude_end"
            ):
                next_state["pending_cut_start"] = None
                if (next_state.get("selected_boundary") or {}).get("kind") == "pending_cut_start":
                    next_state["selected_boundary"] = None
            if next_state.get("active_tool") and next_state.get("selected_word"):
                transcript_focus = next_state["selected_word"].get("start")
                _apply_intuitive_word_tool(next_state, next_state["selected_word"])
        elif action == "set_from_word":
            transcript_focus = _finite_time(command.get("start"), "文字起こし時刻")
            _apply_intuitive_word_tool(next_state, {
                "start": command.get("start"), "end": command.get("end"),
            })
        elif action == "set_from_timeline":
            if next_state.get("active_tool") not in _INTUITIVE_TOOLS:
                raise ValueError("先にタイムライン編集ツールを選んでください。")
            timeline_time = _finite_time(command.get("time"), "タイムライン時刻")
            transcript_focus = timeline_time
            _apply_intuitive_word_tool(
                next_state, {"start": timeline_time, "end": timeline_time}
            )
        elif action == "set_current_position":
            if next_state.get("active_tool") not in _INTUITIVE_TOOLS:
                raise ValueError("先に適用する編集ツールを選んでください。")
            current_time = _finite_time(command.get("time"), "現在の再生位置")
            if not (
                float(next_state["preview_start"]) - 0.001
                <= current_time
                <= float(next_state["preview_end"]) + 0.001
            ):
                raise ValueError("現在の再生位置がプレビュー範囲外です。")
            transcript_focus = current_time
            next_state["playhead_sec"] = current_time
            _apply_intuitive_word_tool(
                next_state, {"start": current_time, "end": current_time}
            )
        elif action == "set_transcript_focus":
            transcript_focus = _finite_time(
                command.get("time"), "文字起こしの表示中心"
            )
            next_state["playhead_sec"] = min(
                max(transcript_focus, next_state["viewport_start"]),
                next_state["viewport_end"],
            )
        elif action == "select_boundary":
            boundary = {"kind": str(command.get("kind"))}
            if command.get("id") is not None:
                boundary["id"] = str(command["id"])
            # 存在確認も兼ね、現在値で再設定する。
            current = _intuitive_boundary_value(next_state, boundary)
            transcript_focus = current
            _set_intuitive_boundary(next_state, boundary, current)
            next_state["selected_boundary"] = boundary
        elif action == "set_boundary":
            boundary = {"kind": str(command.get("kind"))}
            if command.get("id") is not None:
                boundary["id"] = str(command["id"])
            boundary_time = _finite_time(command.get("time"), "境界時刻")
            transcript_focus = boundary_time
            _set_intuitive_boundary(next_state, boundary, boundary_time)
            next_state["selected_boundary"] = _remap_intuitive_boundary(
                next_state, boundary, boundary_time
            )
        elif action == "adjust_selected":
            boundary = next_state.get("selected_boundary")
            if not boundary:
                raise ValueError("先に調整する境界を選択してください。")
            delta = _finite_time(command.get("delta"), "調整幅")
            adjusted_time = _intuitive_boundary_value(next_state, boundary) + delta
            transcript_focus = adjusted_time
            _set_intuitive_boundary(next_state, boundary, adjusted_time)
            next_state["selected_boundary"] = _remap_intuitive_boundary(
                next_state, boundary, adjusted_time
            )
        elif action == "set_selected_time":
            boundary = next_state.get("selected_boundary")
            if not boundary:
                raise ValueError("先に時刻を変更する境界を選択してください。")
            selected_time = _finite_time(command.get("time"), "境界時刻")
            transcript_focus = selected_time
            _set_intuitive_boundary(next_state, boundary, selected_time)
            next_state["selected_boundary"] = _remap_intuitive_boundary(
                next_state, boundary, selected_time
            )
        elif action == "add_exclusion":
            start = _finite_time(command.get("start"), "除外開始")
            end = _finite_time(command.get("end"), "除外終了")
            start, end = sorted((start, end))
            start = max(start, next_state["overall_start"])
            end = min(end, next_state["overall_end"])
            if end <= start + 0.001:
                raise ValueError("全体範囲の内側をドラッグして除外を作成してください。")
            cut_id = f"cut-{next_state['revision'] + 1}-{len(next_state['exclusions']) + 1}"
            next_state["exclusions"].append(
                {"id": cut_id, "start": start, "end": end}
            )
            next_state["exclusions"] = _clip_intuitive_exclusions(next_state)
            if sum(c["end"] - c["start"] for c in next_state["exclusions"]) >= (
                next_state["overall_end"] - next_state["overall_start"] - 0.001
            ):
                raise ValueError("全体範囲のすべてを除外することはできません。")
            chosen = next(
                (c for c in next_state["exclusions"] if c["start"] <= start <= c["end"]),
                next_state["exclusions"][-1],
            )
            next_state["selected_boundary"] = {
                "kind": "exclusion_end", "id": chosen["id"]
            }
            transcript_focus = (start + end) / 2.0
        elif action == "remove_exclusion":
            cut_id = str(command.get("id") or "")
            if not cut_id or not any(
                str(cut.get("id")) == cut_id for cut in next_state["exclusions"]
            ):
                raise ValueError("削除する途中カットが見つかりません。")
            next_state["exclusions"] = [
                cut for cut in next_state["exclusions"]
                if str(cut.get("id")) != cut_id
            ]
            if (next_state.get("selected_boundary") or {}).get("id") == cut_id:
                next_state["selected_boundary"] = None
            next_state["pending_cut_start"] = None
            if next_state.get("active_tool") == "exclude_end":
                next_state["active_tool"] = None
            if (next_state.get("selected_boundary") or {}).get("kind") == "pending_cut_start":
                next_state["selected_boundary"] = None
        elif action == "clear_exclusions":
            next_state["exclusions"] = []
            next_state["pending_cut_start"] = None
            if next_state.get("active_tool") == "exclude_end":
                next_state["active_tool"] = None
            if (next_state.get("selected_boundary") or {}).get("kind") in {
                "pending_cut_start", "exclusion_start", "exclusion_end",
            }:
                next_state["selected_boundary"] = None
        elif action == "set_viewport":
            start = _finite_time(command.get("start"), "表示開始")
            end = _finite_time(command.get("end"), "表示終了")
            if end < start:
                start, end = end, start
            lo, hi = 0.0, float(next_state["duration"])
            min_span = min(_INTUITIVE_VIEWPORT_MIN_SECONDS, hi)
            max_span = min(_INTUITIVE_VIEWPORT_MAX_SECONDS, hi)
            center = (start + end) / 2.0
            span = min(max(end - start, min_span), max_span)
            start, end = center - span / 2.0, center + span / 2.0
            if start < lo:
                end, start = min(hi, span), lo
            if end > hi:
                start, end = max(lo, hi - span), hi
            next_state["viewport_start"], next_state["viewport_end"] = start, end
            next_state["playhead_sec"] = min(
                max(float(next_state.get("playhead_sec", start)), start), end
            )
            if next_state.get("preview_mode") == "result":
                next_state["preview_mode"] = "source"
                next_state["active_tool"] = None
        elif action == "set_timeline_edit_mode":
            next_state["timeline_edit_mode"] = bool(command.get("enabled"))
        elif action == "fit_overall_to_viewport":
            # viewport is display-only by design and never changes the save
            # range on its own; this is the explicit, one-shot "apply what
            # I'm looking at" action the user asks for instead. The usual
            # post-processing below (duration clamp, exclusion clipping,
            # selected_boundary remap) applies exactly as it does for any
            # other overall_start/overall_end change.
            next_state["overall_start"] = float(next_state["viewport_start"])
            next_state["overall_end"] = float(next_state["viewport_end"])
        else:
            raise ValueError("不明な編集コマンドです。")
    except ValueError as exc:
        raise gr.Error(str(exc)) from exc

    next_state["overall_start"] = max(0.0, min(next_state["overall_start"], next_state["duration"]))
    next_state["overall_end"] = max(
        next_state["overall_start"] + 0.1,
        min(next_state["overall_end"], next_state["duration"]),
    )
    next_state["exclusions"] = _clip_intuitive_exclusions(next_state)
    try:
        # This is the final canonical-plan invariant and intentionally runs
        # after overall clamping plus exclusion clipping/merging.  Every edit
        # path (including fit_overall_to_viewport) must pass it before state is
        # committed and revision is advanced.
        _validate_intuitive_exclusion_invariant(next_state)
    except ValueError as exc:
        raise gr.Error(str(exc)) from exc
    next_state["selected_boundary"] = _remap_intuitive_boundary(
        next_state, next_state.get("selected_boundary")
    )
    next_state["playhead_sec"] = min(
        max(float(next_state.get("playhead_sec", next_state["viewport_start"])),
            next_state["viewport_start"]),
        next_state["viewport_end"],
    )
    _set_intuitive_transcript_focus(next_state, transcript_focus)
    final_edit_signature = _intuitive_edit_signature(next_state)
    if action not in {"undo", "redo"} and final_edit_signature != initial_edit_signature:
        next_state["undo_stack"].append(initial_plan)
        next_state["undo_stack"] = _bounded_intuitive_history(
            next_state["undo_stack"]
        )
        next_state["redo_stack"] = []
    next_state["edit_dirty"] = (
        final_edit_signature
        != _intuitive_edit_signature(next_state["baseline_plan"])
    )
    command_id = str(command.get("command_id") or "")[:128]
    if command_id:
        next_state["last_command_id"] = command_id
        next_state["last_command_status"] = "success"
    next_state["revision"] = int(next_state["revision"]) + 1
    if next_state.get("document_id"):
        try:
            DOCUMENTS.sync_adapter_plan(
                next_state["document_id"], edit_plan_from_intuitive(next_state)
            )
        except Exception:
            pass
    return next_state


def _intuitive_boundary_value(state: dict, boundary: dict) -> float:
    kind = boundary.get("kind")
    if kind in {"overall_start", "overall_end", "pending_cut_start"}:
        key = kind
        value = state.get(key)
        if value is None:
            raise ValueError("境界がまだ設定されていません。")
        return float(value)
    if kind in {"exclusion_start", "exclusion_end"}:
        cut = next(
            (item for item in state.get("exclusions") or [] if item["id"] == boundary.get("id")),
            None,
        )
        if not cut:
            raise ValueError("選択中の除外区間が見つかりません。")
        return float(cut["start" if kind.endswith("start") else "end"])
    raise ValueError("不明な境界です。")


def intuitive_state_to_clip_plan(state: dict) -> dict:
    try:
        plan = edit_plan_from_intuitive(state)
    except EditPlanError as exc:
        raise gr.Error(str(exc)) from exc
    return {
        "base_start": ms_to_seconds(plan.overall.start_ms),
        "base_end": ms_to_seconds(plan.overall.end_ms),
        "ranges": [
            [ms_to_seconds(item.start_ms), ms_to_seconds(item.end_ms)]
            for item in plan.kept_ranges
        ],
    }


def _intuitive_edit_summary(state: dict) -> tuple[float, float, int, float, float]:
    """Return values derived from canonical edit state without caching them in it."""
    start = float(state["overall_start"])
    end = float(state["overall_end"])
    exclusions = _clip_intuitive_exclusions(state)
    total = max(0.0, end - start)
    removed = sum(max(0.0, cut["end"] - cut["start"]) for cut in exclusions)
    return start, end, len(exclusions), removed, max(0.0, total - removed)


def render_intuitive_summary(state: dict) -> str:
    start, end, count, removed, completed = _intuitive_edit_summary(state)
    return (
        '<div class="intuitive-edit-summary" aria-live="polite">'
        '<span class="intuitive-stat">'
        f'<strong>全体</strong> {utils.format_timestamp(start)} ～ '
        f'{utils.format_timestamp(end)}（{end - start:.1f}秒）'
        '</span>'
        '<span class="intuitive-stat">'
        f'<strong>途中カット</strong> {count}箇所 / {removed:.1f}秒'
        '</span>'
        '<span class="intuitive-stat">'
        f'<strong>完成予定</strong> {completed:.1f}秒'
        '</span>'
        '</div>'
    )


def render_intuitive_exclusion_list(state: dict) -> str:
    exclusions = _clip_intuitive_exclusions(state)
    result_mode = state.get("preview_mode", "source") == "result"
    selected = state.get("selected_boundary") or {}
    selected_id = (
        str(selected.get("id"))
        if selected.get("kind") in {"exclusion_start", "exclusion_end"}
        else None
    )
    rows = []
    for index, cut in enumerate(exclusions, 1):
        cut_id = html.escape(str(cut["id"]), quote=True)
        selected_class = " is-selected" if str(cut["id"]) == selected_id else ""
        disabled = " disabled" if result_mode else ""
        rows.append(
            f'<div class="intuitive-exclusion-row{selected_class}">'
            f'<span class="intuitive-exclusion-index">{index}</span>'
            f'<span>{utils.format_timestamp(cut["start"])} ～ '
            f'{utils.format_timestamp(cut["end"])}</span>'
            f'<span class="intuitive-exclusion-length">{cut["end"] - cut["start"]:.1f}秒</span>'
            f'<button type="button" data-intuitive-remove-exclusion="{cut_id}"'
            f'{disabled}>削除</button></div>'
        )
    if not rows:
        body = '<div class="intuitive-exclusion-empty">途中カットはありません。</div>'
    else:
        body = "".join(rows)
    clear_disabled = " disabled" if result_mode or not exclusions else ""
    return (
        '<details class="intuitive-exclusion-list" open>'
        f'<summary>途中カット一覧（{len(exclusions)}箇所）</summary>'
        f'<div class="intuitive-exclusion-list-body">{body}'
        f'<button type="button" class="intuitive-clear-exclusions" '
        f'data-intuitive-clear-exclusions{clear_disabled}>すべて削除</button>'
        '</div></details>'
    )


def intuitive_selected_time_update(state: dict):
    boundary = state.get("selected_boundary")
    if not boundary:
        return gr.update(value=None, interactive=False)
    try:
        value = _intuitive_boundary_value(state, boundary)
    except ValueError:
        return gr.update(value=None, interactive=False)
    return gr.update(
        value=round(value, 3),
        interactive=state.get("preview_mode", "source") != "result",
    )


_INTUITIVE_BOUNDARY_LABELS = {
    "overall_start": "全体開始",
    "overall_end": "全体終了",
    "pending_cut_start": "除外開始（確定待ち）",
    "exclusion_start": "除外開始",
    "exclusion_end": "除外終了",
}


def _intuitive_selected_boundary_label(state: dict) -> str | None:
    """Short "<種別> <時刻>" label for the currently selected boundary, or None."""
    boundary = state.get("selected_boundary")
    if not boundary:
        return None
    try:
        value = _intuitive_boundary_value(state, boundary)
    except ValueError:
        return None
    label = _INTUITIVE_BOUNDARY_LABELS.get(boundary.get("kind"), "境界")
    return f"{label} {utils.format_timestamp(value)}"


def render_intuitive_toolbar(state: dict) -> str:
    revision = int(state.get("revision", 0))
    nonce = html.escape(str(state.get("nonce", "")), quote=True)
    last_command_id = html.escape(
        str(state.get("last_command_id") or ""), quote=True
    )
    last_command_status = html.escape(
        str(state.get("last_command_status") or ""), quote=True
    )
    active = state.get("active_tool")
    result_mode = state.get("preview_mode", "source") == "result"
    can_undo = bool(state.get("undo_stack")) and not result_mode
    can_redo = bool(state.get("redo_stack")) and not result_mode
    has_selected_boundary = bool(state.get("selected_boundary"))
    selected_boundary_kind = html.escape(
        str((state.get("selected_boundary") or {}).get("kind") or ""),
        quote=True,
    )
    transcript_start, transcript_end = _intuitive_transcript_bounds(state)
    transcript_projection = html.escape(
        _intuitive_transcript_projection(state), quote=True
    )
    buttons = "".join(
        f'<button type="button" class="intuitive-tool-button'
        f'{" is-selected" if active == key else ""}" data-intuitive-tool="{key}" '
        f'aria-pressed="{str(active == key).lower()}">'
        f"{label}</button>"
        for key, label in _INTUITIVE_TOOL_LABELS
    )
    if result_mode:
        status = "編集ツールを選ぶと元動画へ戻り、その編集を続けられます。"
    elif active == "exclude_end" and state.get("pending_cut_start") is not None:
        status = "除外開始を設定済みです。終了にする文字起こし範囲またはタイムライン位置を選んでください。"
    elif state.get("selected_word"):
        status = "文字起こし範囲を選択済みです。適用する編集ツールを選んでください。"
    elif active:
        status = f"{dict(_INTUITIVE_TOOL_LABELS).get(active)}にする文字起こし範囲またはタイムライン位置を選んでください。"
    else:
        status = "時刻付きの文字起こし範囲、または編集ツールを選んでください。"
    boundary_label = _intuitive_selected_boundary_label(state)
    selected_html = (
        f'<span class="intuitive-selected-boundary-chip">選択中: {html.escape(boundary_label)}</span>'
        if boundary_label else
        '<span class="intuitive-selected-boundary-chip is-empty">未選択</span>'
    )
    return (
        f'<div class="intuitive-toolbox" data-intuitive-root data-revision="{revision}" '
        f'data-nonce="{nonce}" data-preview-mode="{state.get("preview_mode", "source")}" '
        f'data-last-command-id="{last_command_id}" '
        f'data-last-command-status="{last_command_status}" '
        f'data-active-tool="{html.escape(str(active or ""), quote=True)}" '
        f'data-edit-dirty="{str(bool(state.get("edit_dirty", False))).lower()}" '
        f'data-can-undo="{str(can_undo).lower()}" '
        f'data-can-redo="{str(can_redo).lower()}" '
        f'data-has-selected-boundary="{str(has_selected_boundary).lower()}" '
        f'data-selected-boundary-kind="{selected_boundary_kind}" '
        f'data-timeline-edit-mode="{str(bool(state.get("timeline_edit_mode", False))).lower()}" '
        f'data-transcript-start="{transcript_start:.3f}" '
        f'data-transcript-end="{transcript_end:.3f}" '
        f'data-transcript-projection="{transcript_projection}" '
        f'data-viewport-start="{float(state["viewport_start"]):.3f}" '
        f'data-viewport-end="{float(state["viewport_end"]):.3f}" '
        f'data-preview-start="{float(state["preview_start"]):.3f}" '
        f'data-preview-end="{float(state["preview_end"]):.3f}">'
        f'<div class="intuitive-tool-heading"><strong>3. 文字起こし編集</strong>'
        f'<span class="intuitive-history-actions">'
        f'<button type="button" data-intuitive-history="undo"'
        f'{"" if can_undo else " disabled"} aria-label="元に戻す (Ctrl+Z)">↶ 元に戻す</button>'
        f'<button type="button" data-intuitive-history="redo"'
        f'{"" if can_redo else " disabled"} aria-label="やり直す (Ctrl+Y)">↷ やり直す</button>'
        f'</span></div>'
        f'<div class="intuitive-tool-buttons">{buttons}</div>'
        f'<div class="intuitive-tool-status" role="status" aria-live="polite">'
        f'{selected_html}{status}</div></div>'
    )


def render_intuitive_state_overview(state: dict) -> str:
    duration = max(float(state["duration"]), 0.1)
    overall_left = max(0.0, min(100.0, state["overall_start"] / duration * 100.0))
    overall_right = max(overall_left, min(100.0, state["overall_end"] / duration * 100.0))
    viewport_left = max(0.0, min(100.0, state["viewport_start"] / duration * 100.0))
    viewport_right = max(viewport_left, min(100.0, state["viewport_end"] / duration * 100.0))
    viewport_span = max(0.0, state["viewport_end"] - state["viewport_start"])
    min_span = min(_INTUITIVE_VIEWPORT_MIN_SECONDS, duration)
    max_span = min(_INTUITIVE_VIEWPORT_MAX_SECONDS, duration)
    viewport_start_min = max(0.0, state["viewport_end"] - max_span)
    viewport_start_max = max(
        viewport_start_min, state["viewport_end"] - min_span
    )
    viewport_end_min = min(duration, state["viewport_start"] + min_span)
    viewport_end_max = min(duration, state["viewport_start"] + max_span)
    public_video_id = html.escape(
        str(state.get("public_video_id") or state["video_id"]), quote=True
    )
    return f"""
    <div class="intuitive-timeline" data-intuitive-overview data-duration="{duration:.3f}"
         data-public-video-id="{public_video_id}"
         data-preview-start="{state['preview_start']:.3f}" data-preview-end="{state['preview_end']:.3f}"
         data-viewport-start="{state['viewport_start']:.3f}" data-viewport-end="{state['viewport_end']:.3f}"
         data-viewport-min-span="{min_span:.3f}" data-viewport-max-span="{max_span:.3f}">
      <div class="intuitive-timeline-scale"><span>00:00:00</span><strong>5-1. 動画全体の概要タイムライン</strong><span>{utils.format_timestamp(duration)}</span></div>
      <div class="intuitive-overview-track" role="group" aria-label="全体編集範囲と拡大表示範囲">
        <div class="intuitive-overall-window" style="left:{overall_left:.4f}%;width:{overall_right - overall_left:.4f}%" title="全体編集範囲"></div>
        <div class="intuitive-search-marker-layer" data-intuitive-search-marker-layer
             aria-label="この動画の検索ヒット"></div>
        <div class="intuitive-overview-window" style="left:{viewport_left:.4f}%;width:{viewport_right - viewport_left:.4f}%" title="拡大表示範囲">
          <span class="intuitive-viewport-interaction">
            <span class="intuitive-viewport-grip start" data-viewport-drag="start"
                  role="slider" tabindex="0" aria-label="表示範囲の開始"
                  aria-valuemin="{viewport_start_min:.3f}" aria-valuemax="{viewport_start_max:.3f}"
                  aria-valuenow="{state['viewport_start']:.3f}"
                  aria-valuetext="{utils.format_timestamp(state['viewport_start'])}"></span>
            <span class="intuitive-viewport-move" data-viewport-drag="move"
                  role="slider" tabindex="0" aria-label="表示範囲を移動"
                  aria-valuemin="0" aria-valuemax="{max(0.0, duration - viewport_span):.3f}"
                  aria-valuenow="{state['viewport_start']:.3f}"
                  aria-valuetext="{utils.format_timestamp(state['viewport_start'])} ～ {utils.format_timestamp(state['viewport_end'])}"></span>
            <span class="intuitive-viewport-grip end" data-viewport-drag="end"
                  role="slider" tabindex="0" aria-label="表示範囲の終了"
                  aria-valuemin="{viewport_end_min:.3f}"
                  aria-valuemax="{viewport_end_max:.3f}" aria-valuenow="{state['viewport_end']:.3f}"
                  aria-valuetext="{utils.format_timestamp(state['viewport_end'])}"></span>
          </span>
        </div>
      </div>
      <div class="intuitive-viewport-summary">
        <span>表示範囲（保存範囲は変わりません）: <strong data-viewport-summary>{utils.format_timestamp(state['viewport_start'])} ～ {utils.format_timestamp(state['viewport_end'])}（{viewport_span:.1f}秒）</strong></span>
        <span class="intuitive-search-marker-legend" data-intuitive-search-marker-legend hidden>
          <span><i class="text" aria-hidden="true"></i>文字一致</span>
          <span><i class="semantic" aria-hidden="true"></i>意味検索</span>
        </span>
        <span>両端をドラッグして{min_span:.0f}～{max_span:.0f}秒に変更 / 中央をドラッグして移動</span>
      </div>
    </div>
    """


def render_intuitive_state_zoom(state: dict) -> str:
    view_start, view_end = float(state["viewport_start"]), float(state["viewport_end"])
    span = max(view_end - view_start, 0.1)
    midpoint = view_start + span / 2.0
    duration = max(float(state["duration"]), 0.1)
    minimap_left = max(0.0, min(100.0, view_start / duration * 100.0))
    minimap_right = max(
        minimap_left, min(100.0, view_end / duration * 100.0)
    )
    result_mode = state.get("preview_mode", "source") == "result"
    drag_edit_enabled = bool(state.get("timeline_edit_mode", False))
    slider_disabled = result_mode or not drag_edit_enabled

    def slider_attributes(
        kind: str, value: float, label: str, cut: dict | None = None
    ) -> str:
        if kind == "overall_start":
            minimum, maximum = 0.0, max(0.0, float(state["overall_end"]) - 0.1)
        elif kind == "overall_end":
            minimum = min(float(state["duration"]), float(state["overall_start"]) + 0.1)
            maximum = float(state["duration"])
        elif kind == "exclusion_start" and cut:
            minimum = float(state["overall_start"])
            maximum = max(minimum, float(cut["end"]) - 0.1)
        elif kind == "exclusion_end" and cut:
            minimum = min(
                float(state["overall_end"]), float(cut["start"]) + 0.1
            )
            maximum = float(state["overall_end"])
        else:
            minimum, maximum = float(state["overall_start"]), float(state["overall_end"])
        return (
            f'role="slider" tabindex="{-1 if slider_disabled else 0}" '
            f'aria-disabled="{str(slider_disabled).lower()}" '
            f'aria-label="{html.escape(label, quote=True)}" '
            f'aria-valuemin="{minimum:.3f}" aria-valuemax="{maximum:.3f}" '
            f'aria-valuenow="{float(value):.3f}" '
            f'aria-valuetext="{utils.format_timestamp(float(value))}" '
            f'data-boundary-time="{float(value):.3f}"'
        )

    def percent(value):
        return max(0.0, min(100.0, (float(value) - view_start) / span * 100.0))

    overlays = []
    selected_boundary = state.get("selected_boundary") or {}
    selected_cut_id = (
        str(selected_boundary.get("id"))
        if selected_boundary.get("kind") in {"exclusion_start", "exclusion_end"}
        else None
    )
    for cut in state.get("exclusions") or []:
        left, right = percent(cut["start"]), percent(cut["end"])
        if right <= 0 or left >= 100 or right <= left:
            continue
        cut_id = html.escape(str(cut["id"]), quote=True)
        selected_class = " is-selected-cut" if str(cut["id"]) == selected_cut_id else ""
        overlays.append(
            f'<div class="intuitive-cut-zone{selected_class}" style="left:{left:.4f}%;width:{right-left:.4f}%" '
            f'data-cut-id="{cut_id}" title="途中カット">'
            f'<span class="intuitive-cut-handle start" data-boundary-kind="exclusion_start" data-cut-id="{cut_id}" '
            f'{slider_attributes("exclusion_start", cut["start"], "途中カットの開始", cut)}></span>'
            f'<span class="intuitive-cut-handle end" data-boundary-kind="exclusion_end" data-cut-id="{cut_id}" '
            f'{slider_attributes("exclusion_end", cut["end"], "途中カットの終了", cut)}></span></div>'
        )
    handles = []
    for kind, value, css_class in (
        ("overall_start", state["overall_start"], "start"),
        ("overall_end", state["overall_end"], "end"),
    ):
        position = percent(value)
        if 0 <= position <= 100:
            handles.append(
                f'<div class="intuitive-handle {css_class}" style="left:{position:.4f}%" '
                f'data-boundary-kind="{kind}" {slider_attributes(kind, value, dict(_INTUITIVE_BOUNDARY_LABELS).get(kind, kind))} '
                f'title="{kind}"></div>'
            )
    transcript_start, transcript_end = _intuitive_transcript_bounds(state)
    transcript_left = percent(transcript_start)
    transcript_right = percent(transcript_end)
    playhead_position = percent(state.get("playhead_sec", view_start))
    timeline_tools = "".join(
        f'<button type="button" class="intuitive-tool-button'
        f'{" is-selected" if state.get("active_tool") == key else ""}" '
        f'data-intuitive-tool="{key}" '
        f'aria-pressed="{str(state.get("active_tool") == key).lower()}" '
        f'title="{label}にする位置を選びます">'
        f'{label}</button>'
        for key, label in _INTUITIVE_TOOL_LABELS
    )
    drag_edit_label = (
        "ドラッグ編集: ON" if drag_edit_enabled else "ドラッグ編集: OFF"
    )
    drag_edit_hint = (
        "境界ハンドルのドラッグと、空白範囲のドラッグによる途中カット追加ができます。"
        if drag_edit_enabled else
        "境界ハンドルと空白範囲のドラッグをロックしています。位置クリックによるツール適用は利用できます。"
    )
    return f"""
    <div class="intuitive-timeline" data-intuitive-zoom data-view-start="{view_start:.3f}" data-view-end="{view_end:.3f}"
         data-preview-start="{state['preview_start']:.3f}" data-preview-end="{state['preview_end']:.3f}"
         data-preview-mode="{html.escape(state.get('preview_mode', 'source'), quote=True)}"
         data-timeline-edit-mode="{str(drag_edit_enabled).lower()}"
         data-overall-start="{state['overall_start']:.3f}" data-overall-end="{state['overall_end']:.3f}">
      <div class="intuitive-detail-minimap" role="img"
           aria-label="元動画全体に対する現在の詳細表示範囲">
        <span>00:00:00</span>
        <span class="intuitive-detail-minimap-track">
          <span class="intuitive-detail-minimap-window"
                style="left:{minimap_left:.4f}%;width:{minimap_right - minimap_left:.4f}%"></span>
        </span>
        <span>{utils.format_timestamp(duration)}</span>
      </div>
      <div class="intuitive-timeline-toolbox" aria-label="拡大タイムライン編集ツール（共通境界ツール）">
        <strong class="intuitive-inline-section-title">5-2. 拡大編集</strong>
        <button type="button" class="intuitive-edit-mode-toggle{' is-on' if drag_edit_enabled else ''}"
                data-intuitive-toggle-edit-mode
                aria-pressed="{str(drag_edit_enabled).lower()}"
                title="{drag_edit_hint}">{drag_edit_label}</button>
        <button type="button" class="intuitive-fit-overall-button"
                data-intuitive-fit-overall
                title="保存範囲を表示範囲に合わせる">
          表示範囲を保存範囲へ
        </button>
        <div class="intuitive-tool-buttons">{timeline_tools}</div>
      </div>
      <div class="intuitive-timeline-scale"><span>{utils.format_timestamp(view_start)}</span><span>{utils.format_timestamp(midpoint)}</span><span>{utils.format_timestamp(view_end)}</span></div>
      <div class="intuitive-zoom-track" role="group" aria-label="保存範囲、除外範囲、再生位置">
        <div class="intuitive-transcript-window" style="left:{transcript_left:.4f}%;width:{max(0.0, transcript_right - transcript_left):.4f}%"
             title="文字起こし表示: {utils.format_timestamp(transcript_start)} ～ {utils.format_timestamp(transcript_end)}"></div>
        <div class="intuitive-zoom-overall" style="left:{percent(state['overall_start']):.4f}%;width:{max(0.0, percent(state['overall_end']) - percent(state['overall_start'])):.4f}%"></div>
        {''.join(overlays)}{''.join(handles)}
        <div class="intuitive-playhead" style="left:{playhead_position:.4f}%" title="プレビュー再生位置"></div>
        <div class="intuitive-zoom-hint">全体範囲内をドラッグして途中カットを追加</div>
      </div>
      <div class="intuitive-timeline-legend"><span class="intuitive-legend-keep">■ 保存範囲</span>
        <span class="intuitive-legend-cut">▨ 途中カット</span><span class="intuitive-legend-position">│ 再生位置</span>
        <span class="intuitive-legend-transcript">▔ 文字起こし表示</span>
        <span>青枠: viewport / 薄青: 全体編集範囲</span></div>
    </div>
    """


def _refresh_intuitive_source(state: dict):
    start, end = float(state["viewport_start"]), float(state["viewport_end"])
    _set_intuitive_transcript_focus(state)
    transcript_start, transcript_end = _intuitive_transcript_bounds(state)
    conn = db.get_conn()
    try:
        segments = db.get_segments_in_range(
            conn, state["video_id"], transcript_start, transcript_end
        )
    finally:
        conn.close()
    preview_path = make_intuitive_preview(
        state["video_id"], state["video_path"], start, end, state["duration"]
    )
    refreshed = {
        **state,
        "preview_start": start,
        "preview_end": end,
        "active_tool": state.get("active_tool"),
        "preview_mode": "source",
    }
    filename = Path(state["video_path"]).name
    interval = f"{utils.format_timestamp(start)} - {utils.format_timestamp(end)}"
    return (
        refreshed,
        gr.update(
            value=preview_path,
            label=(
                f"1. 動画プレビュー（Source timeline）: "
                f"{filename} [{state['video_id']}]"
            ),
        ),
        render_intuitive_transcript(segments, refreshed),
        f"**表示モード:** 元動画プレビュー（Source timeline）　｜　"
        f"**選択動画:** {html.escape(filename)}　｜　**表示区間:** {interval}",
    )


def _render_intuitive_transcript_for_state(state: dict) -> str:
    _set_intuitive_transcript_focus(state)
    transcript_start, transcript_end = _intuitive_transcript_bounds(state)
    conn = db.get_conn()
    try:
        segments = db.get_segments_in_range(
            conn, state["video_id"], transcript_start, transcript_end
        )
    finally:
        conn.close()
    return render_intuitive_transcript(segments, state)


def _intuitive_render_outputs(state: dict, source_updates=None):
    preview, transcript, info = source_updates or (gr.update(), gr.update(), gr.update())
    return (
        state,
        render_intuitive_toolbar(state),
        render_intuitive_state_overview(state),
        render_intuitive_state_zoom(state),
        preview,
        transcript,
        info,
        render_intuitive_summary(state),
        render_intuitive_exclusion_list(state),
        intuitive_selected_time_update(state),
    )


def handle_intuitive_command(command_json: str, state: dict):
    try:
        command = json.loads(command_json) if isinstance(command_json, str) else dict(command_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        command = {}
    command_id = str(command.get("command_id") or "")[:128]
    resume_for_tool = bool(
        isinstance(state, dict)
        and state.get("preview_mode", "source") == "result"
        and command.get("type") == "set_tool"
    )
    try:
        next_state = dispatch_intuitive_command(command_json, state)
    except Exception as exc:
        # HTML→Gradio bridgeはrevisionの更新を完了通知として直列実行する。
        # ここでcallback自体を例外で失敗させると、JS側のawaitRevisionは
        # 「revisionが進むまで」待ち続けたまま二度と満たされず、以降の
        # 全コマンドがcommandQueueに溜まったまま送信されなくなる
        # (revisionが進まない = FIFOが完全に停止するデッドロック)。
        # dispatch_intuitive_command内のバリデーション失敗(ValueError→
        # gr.Errorに変換済み)はもちろん、想定外の例外(gr.Errorでないもの)も
        # ここで必ず捕まえ、編集内容を変えずrevisionだけ進めて安全に復帰する。
        gr.Warning(
            str(exc) if isinstance(exc, gr.Error) else
            f"予期しないエラーが発生しました。操作を取り消します。({type(exc).__name__})"
        )
        if not isinstance(state, dict):
            raise
        next_state = copy.deepcopy(state)
        next_state["revision"] = int(state.get("revision", 0)) + 1
        if command_id:
            next_state["last_command_id"] = command_id
            next_state["last_command_status"] = (
                "validation_error" if isinstance(exc, gr.Error)
                else "unexpected_error"
            )
        return _intuitive_render_outputs(next_state)
    if command.get("type") == "fit_overall_to_viewport":
        gr.Info(
            "保存範囲を "
            f"{utils.format_timestamp(next_state['overall_start'])}〜"
            f"{utils.format_timestamp(next_state['overall_end'])} に設定しました。"
        )
    if command.get("type") == "set_viewport" or resume_for_tool:
        try:
            next_state, preview, transcript, info = _refresh_intuitive_source(next_state)
        except Exception as exc:
            gr.Warning(
                "表示区間のプレビュー更新に失敗しました。元の表示区間へ戻します。"
                f" ({type(exc).__name__})"
            )
            recovered = copy.deepcopy(state)
            recovered["revision"] = int(state.get("revision", 0)) + 1
            if command_id:
                recovered["last_command_id"] = command_id
                recovered["last_command_status"] = "unexpected_error"
            return _intuitive_render_outputs(recovered)
        return _intuitive_render_outputs(next_state, (preview, transcript, info))
    # The viewport did not change, so the transcript words are identical.
    # Keep that potentially large DOM in place and let the browser apply the
    # small canonical decoration projection carried by the toolbar update.
    return _intuitive_render_outputs(next_state)


def sync_intuitive_editor(sync_token: str, state: dict):
    """Read-only canonical redraw queued behind any in-flight editor command."""
    if not isinstance(state, dict) or not state.get("nonce"):
        raise gr.Error("先に動画を読み込んでください。")
    safe_token = html.escape(str(sync_token or "")[:128], quote=True)
    sync_ack = (
        f'<span data-intuitive-sync-token="{safe_token}" aria-hidden="true"></span>'
    )
    return (*_intuitive_render_outputs(state), sync_ack)


def adjust_intuitive_boundary(step: float, direction: float, state: dict):
    command = {
        "type": "adjust_selected",
        "delta": _finite_time(step or 1.0, "調整幅") * float(direction),
        "revision": state.get("revision") if state else None,
        "nonce": state.get("nonce") if state else None,
    }
    try:
        next_state = dispatch_intuitive_command(command, state)
        return _intuitive_render_outputs(
            next_state,
            (gr.update(), _render_intuitive_transcript_for_state(next_state), gr.update()),
        )
    except gr.Error as exc:
        gr.Warning(str(exc))
        if not isinstance(state, dict):
            raise
        return _intuitive_render_outputs(state)


def load_intuitive_editor(video_choice: str):
    preview, transcript, info, _, _ = load_intuitive_video(video_choice)
    video_id = parse_video_choice(video_choice)
    conn = db.get_conn()
    try:
        video = db.get_video(conn, video_id)
        first_segment = (
            db.get_first_text_segment(conn, video["video_id"])
            if video else None
        )
    finally:
        conn.close()
    if not video:
        raise gr.Error(f"動画が見つかりません: {video_id}")
    first_start = float(first_segment["start_sec"]) if first_segment else 0.0
    start = max(0.0, first_start - 2.0)
    end = min(float(video.get("duration") or start + 90.0), start + 90.0)
    state = _new_intuitive_state(video, start, end)
    transcript = _render_intuitive_transcript_for_state(state)
    return (
        state, preview, transcript,
        "**表示モード:** 元動画プレビュー（Source timeline）　｜　" + info,
        render_intuitive_toolbar(state),
        render_intuitive_state_overview(state),
        render_intuitive_state_zoom(state),
        render_intuitive_summary(state),
        render_intuitive_exclusion_list(state),
        intuitive_selected_time_update(state),
    )


def load_intuitive_editor_with_search_target(video_choice: str):
    """Load an edit session and synchronize search to that individual video."""
    loaded = load_intuitive_editor(video_choice)
    video_id = parse_video_choice(video_choice)
    return (*loaded, gr.update(value=video_id or ALL_VIDEOS_VALUE))


def select_intuitive_video_from_card(command_json: str, filter_text: str):
    """Select a visible HTML card without an index-coupled hidden Gallery."""
    try:
        command = json.loads(command_json or "")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise gr.Error("動画を選択できませんでした。一覧を更新してください。") from exc
    if not isinstance(command, dict):
        raise gr.Error("動画を選択できませんでした。一覧を更新してください。")
    video_id = parse_video_choice(str(command.get("video_id") or ""))
    if video_id == ALL_VIDEOS_VALUE:
        raise gr.Error("直感編集では個別の動画を選択してください。")
    if not video_id:
        raise gr.Error("動画を選択できませんでした。一覧を更新してください。")
    cards_html = build_intuitive_video_cards(
        filter_text, video_id, generate_thumbnails=False
    )
    return (
        video_id, cards_html,
        *load_intuitive_editor_with_search_target(video_id),
    )


def refresh_intuitive_video_picker(
    filter_text: str,
    selected_video_id: str,
    current_search_target: str | None = None,
):
    """Refresh cards and both fallback/search dropdowns without rebuilding thumbnails."""
    cards = _intuitive_video_cards_data(filter_text)
    cards_html = render_intuitive_video_cards(
        [
            {**card, "thumbnail_url": _thumbnail_servable_url(card.get("thumbnail_path"))}
            for card in cards
        ],
        parse_video_choice(selected_video_id),
    )
    choices = [
        (
            f'{card["name"]}  —  {utils.format_timestamp(card["duration"])}',
            card["video_id"],
        )
        for card in cards
    ]
    search_choices = [("すべての動画", ALL_VIDEOS_VALUE), *choices]
    search_update = {"choices": search_choices}
    if current_search_target is not None:
        preserved_target = current_search_target or ALL_VIDEOS_VALUE
        if preserved_target not in {value for _label, value in search_choices}:
            all_choices = list_video_choices()
            preserved_choice = next(
                (choice for choice in all_choices if choice[1] == preserved_target),
                None,
            )
            if preserved_choice is not None:
                search_choices.append(preserved_choice)
            else:
                preserved_target = ALL_VIDEOS_VALUE
        search_update["value"] = preserved_target
    return (
        cards_html,
        gr.update(choices=choices),
        gr.update(**search_update),
    )


def _load_intuitive_search_result(index: int, results: list[dict]):
    if not results or index < 0 or index >= len(results):
        raise gr.Error("検索結果を選択してください。")
    result = results[index]
    conn = db.get_conn()
    try:
        video = db.get_video(conn, result["video_id"])
        if not video:
            raise gr.Error(f"動画が見つかりません: {result['video_id']}")
        overall_start, overall_end = expand_to_speech_boundary(
            conn, result["video_id"], result["start"], result["end"]
        )
        duration = float(video.get("duration") or overall_end)
        if not math.isfinite(duration) or duration < 0.1:
            raise gr.Error("検索結果の動画が短すぎるか、長さを取得できません。")
        overall_start = max(0.0, min(float(overall_start), duration - 0.1))
        overall_end = max(
            overall_start + 0.1,
            min(float(overall_end), duration),
        )
        desired_start = max(0.0, overall_start - 10.0)
        desired_end = min(duration, overall_end + 10.0)
        if desired_end - desired_start > 90.0:
            center = (overall_start + overall_end) / 2.0
            desired_start, desired_end = center - 45.0, center + 45.0
            if desired_start < 0:
                desired_start, desired_end = 0.0, min(duration, 90.0)
            elif desired_end > duration:
                desired_start, desired_end = max(0.0, duration - 90.0), duration
        segments = db.get_segments_in_range(
            conn, video["video_id"], desired_start, desired_end
        )
    finally:
        conn.close()

    state = _new_intuitive_state(video, desired_start, desired_end)
    state["overall_start"] = float(overall_start)
    state["overall_end"] = float(overall_end)
    # A search result opens as a fresh, clean edit document.  The constructor
    # initially snapshots the padded preview viewport, so replace that baseline
    # after applying the speech-expanded edit range.  Otherwise a later
    # display-only command would mark the untouched result dirty.
    state["baseline_plan"] = _intuitive_plan_snapshot(state)
    state["edit_dirty"] = False
    preview_path = make_intuitive_preview(
        state["video_id"], state["video_path"], desired_start, desired_end, state["duration"]
    )
    filename = Path(state["video_path"]).name
    interval = f"{utils.format_timestamp(desired_start)} - {utils.format_timestamp(desired_end)}"
    return (
        state,
        gr.update(
            value=preview_path,
            label=(
                f"1. 動画プレビュー（Source timeline）: "
                f"{filename} [{state['video_id']}]"
            ),
        ),
        render_intuitive_transcript(segments, state),
        f"**表示モード:** 元動画プレビュー（Source timeline）　｜　"
        f"**検索結果:** {html.escape(filename)}　｜　**表示区間:** {interval}",
        render_intuitive_toolbar(state),
        render_intuitive_state_overview(state),
        render_intuitive_state_zoom(state),
        render_intuitive_summary(state),
        render_intuitive_exclusion_list(state),
        intuitive_selected_time_update(state),
    )


def do_intuitive_search(
    query: str, video_choice: str, top_k: int, min_score: float,
    current_state: dict | None = None,
):
    results = search_video_results(query, video_choice, top_k, min_score)
    if not results:
        if query.strip():
            gr.Info("該当するシーンが見つかりませんでした。")
        return (
            [], [], current_state,
            gr.update(), gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(), gr.update(), gr.update(),
        )
    return (
        _build_intuitive_table(results, selected_idx=0),
        results,
        *_load_intuitive_search_result(0, results),
    )


def do_intuitive_search_staged(
    query: str,
    video_choice: str,
    top_k: int,
    min_score: float,
    request: gr.Request = None,
):
    """Render normalized text hits first, then append semantic hits.

    Search owns a separate concurrency lane from editor mutations.  Results do
    not change the open edit document until the user explicitly selects a row.
    A per-session request token prevents a slow older embedding request from
    overwriting a newer query.
    """
    query = str(query or "").strip()
    session_id = getattr(request, "session_hash", None)
    if not query:
        request_id = _intuitive_search_coordinator.registry.begin(session_id)
        search_view = _intuitive_search_view(request_id, [])
        yield (
            [], search_view, "検索する文字・フレーズを入力してください。",
            render_intuitive_search_marker_projection(search_view),
        )
        return

    video_filter = parse_video_choice(video_choice)
    semantic_limit = max(1, int(top_k))
    text_limit = max(20, semantic_limit)
    threshold = float(min_score)

    def service_factory(conn):
        db.init_db(conn)
        return _search_service_for_connection(conn)

    for stage in _intuitive_search_coordinator.search(
        query,
        session_id=session_id,
        connection_factory=db.get_conn,
        service_factory=service_factory,
        public_video_id=video_filter,
        text_limit=text_limit,
        semantic_limit=semantic_limit,
        min_score=threshold,
    ):
        results = _decorate_staged_search_results(stage.hits)
        text_count = len(stage.text_hits)
        semantic_count = len(stage.semantic_hits)
        if not stage.complete:
            status = f"文字一致: {text_count}件。意味検索を実行中…"
        elif stage.publication_changed:
            status = (
                "検索中にインデックスが更新されました。"
                "文字一致のみ表示しています。再検索してください。"
            )
        elif stage.semantic_status == SEMANTIC_PENDING:
            status = (
                f"文字一致: {text_count}件。意味検索は準備中のため、"
                "文字一致のみ表示しています。"
            )
        elif stage.semantic_status:
            status = (
                f"文字一致: {text_count}件。意味検索を利用できないため、"
                "文字一致のみ表示しています。"
            )
        else:
            status = (
                f"文字一致: {text_count}件 / 意味検索: {semantic_count}件"
            )
        search_view = _intuitive_search_view(stage.request_id, results)
        yield (
            _build_intuitive_table(results),
            search_view,
            status,
            render_intuitive_search_marker_projection(search_view),
        )


def _select_intuitive_search_result(index: int, search_view):
    results = _intuitive_search_view_results(search_view)
    if index < 0 or index >= len(results):
        raise gr.Error("検索結果を選択してください。")
    return (
        _build_intuitive_table(results, selected_idx=index),
        *_load_intuitive_search_result(index, results),
    )


def on_intuitive_search_select(search_view, evt: gr.SelectData):
    raw_index = evt.index if evt and evt.index is not None else 0
    index = raw_index[0] if isinstance(raw_index, (tuple, list)) else raw_index
    return _select_intuitive_search_result(int(index), search_view)


def on_intuitive_search_marker(command_json: str, search_view, request: gr.Request = None):
    """Resolve a marker by stable hit ID, then use the row-selection path."""
    try:
        command = json.loads(command_json or "")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise gr.Error("検索マーカーを選択できませんでした。再検索してください。") from exc
    if not isinstance(command, dict):
        raise gr.Error("検索マーカーを選択できませんでした。再検索してください。")
    view_request_id = str(
        search_view.get("request_id") or ""
        if isinstance(search_view, dict) else ""
    )
    command_request_id = str(command.get("request_id") or "")
    session_id = getattr(request, "session_hash", None)
    if (
        not view_request_id
        or command_request_id != view_request_id
        or not _intuitive_search_coordinator.registry.is_current(
            session_id, command_request_id
        )
    ):
        raise gr.Error("検索結果が更新されています。現在の候補を選び直してください。")
    hit_id = str(command.get("hit_id") or "")
    results = _intuitive_search_view_results(search_view)
    index = next(
        (i for i, result in enumerate(results) if str(result.get("hit_id") or "") == hit_id),
        -1,
    )
    if index < 0:
        raise gr.Error("検索結果が更新されています。現在の候補を選び直してください。")
    return _select_intuitive_search_result(index, search_view)


def preview_intuitive_editor(state: dict):
    if not state:
        raise gr.Error("先に動画を読み込んでください。")
    ctx = {
        "video_id": state["video_id"],
        "video_path": state["video_path"],
        "duration": state["duration"],
    }
    preview, info, _ = preview_clip_plan(
        state["overall_start"], state["overall_end"], ctx,
        intuitive_state_to_clip_plan(state),
    )
    result_state = {
        **state,
        "active_tool": None,
        "pending_cut_start": None,
        "selected_word": None,
        "selected_boundary": (
            None
            if (state.get("selected_boundary") or {}).get("kind") == "pending_cut_start"
            else state.get("selected_boundary")
        ),
        "preview_mode": "result",
        "revision": state["revision"] + 1,
    }
    result_label = (
        "1. 動画プレビュー（Result timeline）: "
        f"{Path(state['video_path']).name} [{state['video_id']}]"
    )
    preview = (
        {**preview, "label": result_label}
        if isinstance(preview, dict)
        else gr.update(value=preview, label=result_label)
    )
    return _intuitive_render_outputs(
        result_state,
        (
            preview,
            gr.update(),
            "**表示モード:** 編集結果プレビュー（Result timeline）　｜　"
            + info.replace("\n", "　"),
        ),
    )


def return_intuitive_source(state: dict, result_time_sec: float | None = None):
    if not state:
        raise gr.Error("先に動画を読み込んでください。")
    source_state = {
        **state,
        "active_tool": None,
        "preview_mode": "source",
        "revision": int(state.get("revision", 0)) + 1,
    }
    if result_time_sec is not None and state.get("preview_mode") == "result":
        try:
            mapping = TimelineMap.from_plan(edit_plan_from_intuitive(state))
            source_ms = mapping.result_to_source(round(float(result_time_sec) * 1000))
            source_time = ms_to_seconds(source_ms)
            span = max(5.0, float(state["viewport_end"]) - float(state["viewport_start"]))
            source_state["viewport_start"] = max(
                0.0, min(float(state["duration"]) - span, source_time - span / 2)
            )
            source_state["viewport_end"] = min(
                float(state["duration"]), source_state["viewport_start"] + span
            )
            source_state["playhead_sec"] = source_time
        except (ValueError, EditPlanError):
            pass
    source_state, preview, transcript, info = _refresh_intuitive_source(source_state)
    return _intuitive_render_outputs(source_state, (preview, transcript, info))


def save_intuitive_editor(
    state: dict, precise: bool, out_dir: str, filename: str, include_srt: bool = False
):
    if not state:
        raise gr.Error("先に動画を読み込んでください。")
    requested_name = (filename or "").strip()
    if requested_name and (
        Path(requested_name).name != requested_name
        or requested_name in {".", ".."}
        or "/" in requested_name
        or "\\" in requested_name
    ):
        raise gr.Error("ファイル名にはフォルダやパスを含めないでください。")
    ctx = {
        "video_id": state["video_id"],
        "video_path": state["video_path"],
        "duration": state["duration"],
    }
    source = Path(state["video_path"])
    if source.is_file() and state.get("document_id"):
        domain_plan = edit_plan_from_intuitive(state)
        DOCUMENTS.sync_adapter_plan(state["document_id"], domain_plan)
        output_dir = Path((out_dir or "").strip() or DEFAULT_CLIPS_DIR)
        output_name = requested_name or (
            f"{state['video_id']}_{int(state['overall_start'])}_{int(state['overall_end'])}.mp4"
        )
        if not output_name.lower().endswith(".mp4"):
            output_name += ".mp4"
        subtitle_text = None
        subtitle_warnings = []
        if include_srt:
            conn = db.get_conn()
            try:
                transcript_segments = []
                for row in db.get_segments(conn, state["video_id"]):
                    try:
                        transcript_segments.append(
                            parse_segment(row, domain_plan.source_duration_ms)
                        )
                    except ValueError as exc:
                        subtitle_warnings.append(str(exc))
            finally:
                conn.close()
            subtitle_result = map_subtitles(
                transcript_segments, make_effective_export_plan(domain_plan)
            )
            subtitle_text = subtitle_result.to_srt()
            subtitle_warnings.extend(subtitle_result.warnings)
        result = save_document(
            state["document_id"], source, output_dir / output_name, bool(precise),
            subtitle_text=subtitle_text, warnings=subtitle_warnings, cutter=cut_clips,
        )
        saved_path = str(result.video_path.resolve())
    else:
        saved_path = on_save(
            state["overall_start"], state["overall_end"], ctx,
            intuitive_state_to_clip_plan(state), precise, out_dir, filename,
        )
    saved_state = copy.deepcopy(state)
    saved_state["baseline_plan"] = _intuitive_plan_snapshot(saved_state)
    saved_state["edit_dirty"] = False
    return saved_path, saved_state, render_intuitive_toolbar(saved_state)


def get_region_sentences(conn, video_id: str, lo: float, hi: float) -> list:
    return db.get_segments_in_range(conn, video_id, lo, hi)


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
    preview = make_preview(
        video["path"], start, end, duration, video_id=r["video_id"]
    )
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

    preview = make_preview(
        video["path"], start, end, duration, video_id=video_id
    )
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
    preview = make_preview(
        ctx["video_path"], start, end, ctx["duration"],
        video_id=ctx.get("video_id"),
    )
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
        try:
            domain_plan = edit_plan_from_kept_ranges(start, end, plan["ranges"])
        except EditPlanError:
            return [[start, end]]
        return [
            [ms_to_seconds(item.start_ms), ms_to_seconds(item.end_ms)]
            for item in domain_plan.kept_ranges
        ]
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
        preview = make_preview(
            ctx["video_path"], ranges[0][0], ranges[0][1], ctx["duration"],
            video_id=ctx.get("video_id"),
        )
    else:
        out = _multi_preview_path(
            ctx.get("video_id"), ctx["video_path"], ranges
        )
        preview = _create_cached_preview(
            out,
            lambda temporary: cut_clips(
                Path(ctx["video_path"]), ranges, temporary,
                precise=False, duration=ctx["duration"],
                timeout_sec=PREVIEW_RENDER_TIMEOUT_SEC,
            ),
        )
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

def do_index(
    video_path: str,
    asr_model: str,
    force: bool,
    batch_infer: bool = True,
    llm_analysis: bool = False,
    llm_model: str = "",
):
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
        if llm_analysis:
            selected_llm_model = (llm_model or config.LLM_ANALYSIS_MODEL).strip()
            if not selected_llm_model:
                raise gr.Error("LLM解析を有効にする場合はOllamaモデル名を指定してください。")
            cmd += ["--llm-analysis", "--llm-model", selected_llm_model]
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


def format_latest_llm_analysis(video_choice: str) -> str:
    """Render the latest successful derived analysis without exposing ASR rows."""
    video_id = parse_video_choice(video_choice)
    if not video_id:
        return "動画を選択してください。"
    conn = db.get_conn()
    try:
        db.init_db(conn)
        revision = db.get_active_transcript_revision(conn, video_id)
        if revision is None:
            return "この動画には有効な文字起こしがありません。"
        runs = db.list_analysis_runs(conn, video_id, revision)
        if not runs:
            return "この文字起こしにはLLM解析結果がありません。"
        latest = runs[0]
        ready = next((item for item in runs if item["status"] == "ready"), None)
        notices = []
        if latest["status"] == "failed":
            notices.append(
                "> 最新の解析は失敗しました: "
                + html.escape(str(latest.get("error_message") or "原因不明"))
            )
        elif latest["status"] in {"pending", "running"}:
            notices.append("> 最新の解析は処理中です。")
        if ready is None:
            return "\n\n".join(notices or ["解析結果はまだ利用できません。"])

        tags = ready.get("tags") or []
        chapters = db.get_analysis_chapters(conn, ready["analysis_run_id"])
        result = ready.get("result") or {}
        lines = notices + [
            "### 動画全体の要約",
            html.escape(str(ready.get("summary") or "要約はありません。")),
            "",
            "### タグ",
            " / ".join(f"`{html.escape(str(tag))}`" for tag in tags)
            if tags else "タグはありません。",
        ]
        if result:
            coverage = result.get("segment_coverage_ratio")
            coverage_text = (
                f"{float(coverage) * 100:.1f}%"
                if isinstance(coverage, (int, float))
                else "未計測"
            )
            lines += [
                "",
                "### 解析品質",
                f"- 文字起こしセグメント網羅率: **{coverage_text}**",
                "- 解析窓: "
                f"{int(result.get('window_count') or 0)} / "
                f"章: {int(result.get('chapter_count') or len(chapters))}",
                "- 方式: "
                f"`{html.escape(str(ready.get('prompt_version') or '不明'))}` / "
                f"モデル: `{html.escape(str(ready.get('model') or '不明'))}`",
            ]
        lines += ["", "### 時間付きの章"]
        if not chapters:
            lines.append("章は生成されませんでした。")
        for chapter in chapters:
            start = utils.format_timestamp(float(chapter["start_sec"]))
            end = utils.format_timestamp(float(chapter["end_sec"]))
            title = html.escape(str(chapter.get("title") or "無題"))
            summary = html.escape(str(chapter.get("summary") or ""))
            chapter_tags = " / ".join(
                f"`{html.escape(str(tag))}`"
                for tag in chapter.get("tags", [])
            )
            lines.append(f"- **{start}–{end}　{title}**")
            if summary:
                lines.append(f"  - {summary}")
            if chapter_tags:
                lines.append(f"  - {chapter_tags}")
        return "\n".join(lines)
    finally:
        conn.close()


def _has_ready_llm_analysis(video_choice: str) -> bool:
    """Return whether the active transcript has a reusable ready analysis."""
    video_id = parse_video_choice(video_choice)
    if not video_id:
        return False
    conn = db.get_conn()
    try:
        db.init_db(conn)
        revision = db.get_active_transcript_revision(conn, video_id)
        if revision is None:
            return False
        return db.get_latest_ready_analysis_run(conn, video_id, revision) is not None
    finally:
        conn.close()


def list_llm_result_video_choices() -> list[tuple[str, str]]:
    """List videos while making reusable summary state visible in the UI."""
    conn = db.get_conn()
    try:
        db.init_db(conn)
        choices = []
        for video in db.list_videos(conn):
            video_id = video.get("public_video_id") or video["video_id"]
            revision = db.get_active_transcript_revision(conn, video_id)
            ready = (
                db.get_latest_ready_analysis_run(conn, video_id, revision)
                if revision is not None else None
            )
            status = "要約済み" if ready is not None else "未要約"
            name = video.get("display_name") or Path(video["path"]).name
            label = (
                f"[{status}] {name}  —  "
                f"{utils.format_timestamp(float(video.get('duration') or 0.0))}"
            )
            choices.append((label, str(video_id)))
        return choices
    finally:
        conn.close()


def load_llm_target_preview(video_choice: str):
    """Show the selected source video and its reusable analysis state."""
    video_id = parse_video_choice(video_choice)
    if not video_id:
        return gr.update(value=None), "**対象動画:** 未選択"
    conn = db.get_conn()
    try:
        db.init_db(conn)
        video = db.get_video(conn, video_id)
        revision = db.get_active_transcript_revision(conn, video_id)
        ready = (
            db.get_latest_ready_analysis_run(conn, video_id, revision)
            if revision is not None else None
        )
    finally:
        conn.close()
    if not video:
        return gr.update(value=None), "対象動画の情報を取得できませんでした。"
    source = Path(video["path"])
    if not source.is_file():
        return gr.update(value=None), "対象動画の元ファイルが見つかりません。"
    name = html.escape(str(video.get("display_name") or source.name))
    duration = utils.format_timestamp(float(video.get("duration") or 0.0))
    transcript_status = "文字起こし済み" if revision is not None else "文字起こしなし"
    summary_status = "要約済み" if ready is not None else "未要約"
    detail = (
        f"**対象動画:** {name}  \n"
        f"**長さ:** {duration}　｜　{transcript_status}　｜　{summary_status}"
    )
    return gr.update(value=str(source.resolve())), detail


def sync_llm_video_selection(video_choice: str):
    """Keep both LLM work tabs on one video and refresh both previews."""
    preview, detail = load_llm_target_preview(video_choice)
    return gr.update(value=video_choice), preview, detail, preview, detail


def load_summary_highlight_workspace(video_choice: str):
    """Load one saved summary and expose highlight generation when reusable."""
    summary = format_latest_llm_analysis(video_choice)
    ready = _has_ready_llm_analysis(video_choice)
    if ready:
        highlight_markdown, choices = _latest_highlight_view(video_choice)
        status = (
            "✅ 保存済みの要約・時間付き章をそのまま利用できます。"
            "再要約せず見どころ候補を生成できます。"
        )
    else:
        highlight_markdown, choices = (
            "この動画には利用できる保存済み要約がありません。"
            "先に上の「この動画をローカルLLMで解析」を実行してください。",
            [],
        )
        status = "保存済み要約がないため、見どころ候補はまだ生成できません。"
    return (
        summary,
        status,
        highlight_markdown,
        gr.update(value=choices[0][1] if choices else ""),
        gr.update(interactive=ready),
    )


def do_existing_llm_analysis(video_choice: str, model: str):
    """Run retryable local analysis in a separate process for an existing video."""
    video_id = parse_video_choice(video_choice)
    if not video_id:
        raise gr.Error("解析する動画を選択してください。")
    selected_model = (model or config.LLM_ANALYSIS_MODEL).strip()
    if not selected_model:
        raise gr.Error("Ollamaモデル名を指定してください。")
    if not _index_lock.acquire(blocking=False):
        raise gr.Error(
            "別の動画追加またはLLM解析が実行中です。完了までお待ちください。"
        )

    import os
    import sys

    command = [
        sys.executable,
        "analyze_transcript.py",
        "--video-id",
        video_id,
        "--model",
        selected_model,
    ]
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }
    process = None
    log_lines = ["ローカルLLM解析を開始しました。"]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            cwd=str(Path(__file__).parent),
        )
        INDEX_JOB_PIDFILE.write_text(str(process.pid), encoding="utf-8")
        _index_state["proc"] = process
        _index_state["stopped"] = False
        yield "\n".join(log_lines), gr.update()
        for line in iter(process.stdout.readline, ""):
            text = line.rstrip()
            if text:
                log_lines.append(text)
                yield "\n".join(log_lines), gr.update()
        code = process.wait()
        if code == 0:
            yield "\n".join(log_lines), format_latest_llm_analysis(video_choice)
        elif _index_state["stopped"]:
            log_lines.append(
                "LLM解析を停止しました。既存の文字起こし・検索結果は変更されていません。"
            )
            yield "\n".join(log_lines), gr.update()
        else:
            log_lines.append(
                "解析に失敗しました。既存の文字起こし・検索結果は変更されていません。"
            )
            yield "\n".join(log_lines), gr.update()
    finally:
        INDEX_JOB_PIDFILE.unlink(missing_ok=True)
        _index_state["proc"] = None
        _index_lock.release()


def _latest_highlight_view(video_choice: str) -> tuple[str, list[tuple[str, str]]]:
    """Return escaped inline-radio cards and stable candidate IDs."""
    video_id = parse_video_choice(video_choice)
    if not video_id:
        return "動画を選択してください。", []
    conn = db.get_conn()
    try:
        db.init_db(conn)
        revision = db.get_active_transcript_revision(conn, video_id)
        if revision is None:
            return "この動画には有効な文字起こしがありません。", []
        runs = db.list_highlight_runs(conn, video_id, revision)
        if not runs:
            return "この文字起こしには見どころ候補がありません。", []
        latest = runs[0]
        ready = next((run for run in runs if run and run["status"] == "ready"), None)
        notices = []
        if latest and latest["status"] == "failed":
            notices.append(
                '<div class="highlight-candidate-notice is-error">'
                "最新の候補生成は失敗しました: "
                + html.escape(str(latest.get("error_message") or "原因不明"))
                + "</div>"
            )
        elif latest and latest["status"] in {"pending", "running"}:
            notices.append(
                '<div class="highlight-candidate-notice">'
                "最新の候補生成は処理中です。</div>"
            )
        if ready is None:
            return "".join(
                notices or [
                    '<div class="highlight-candidate-empty">'
                    "候補はまだ利用できません。</div>"
                ]
            ), []

        candidates = db.get_highlight_candidates(conn, ready["highlight_run_id"])
        result = ready.get("result") or {}
        generation_mode = str(result.get("generation_mode") or "summary")
        generation_description = (
            "自然言語クエリ検索"
            if generation_mode == "query" else "要約から自動選定"
        )
        requested = int(
            result.get("requested_count") or ready.get("requested_count") or 0
        )
        query_html = ""
        if generation_mode == "query" and result.get("query"):
            query_html = (
                "<li>クエリ: "
                f"{html.escape(str(result['query']))}</li>"
            )
        parts = [
            '<div class="highlight-candidate-view">',
            *notices,
            '<section class="highlight-candidate-quality">',
            "<h3>見どころ候補</h3>",
            "<p>文字起こしだけを使った候補です。映像だけの出来事、表情、"
            "音の盛り上がりは評価していません。必ずプレビューで確認してください。</p>",
            "<details><summary>生成品質を表示</summary><ul>",
            f"<li>生成方式: <strong>{generation_description}</strong></li>",
            query_html,
            f"<li>候補: <strong>{len(candidates)}件</strong> / 要求: {requested}件</li>",
            "<li>候補尺: "
            f"{float(result.get('duration_min') or 0.0):.1f}〜"
            f"{float(result.get('duration_max') or 0.0):.1f}秒 "
            f"（中央値 {float(result.get('duration_median') or 0.0):.1f}秒）</li>",
            f"<li>重複抑制: {int(result.get('overlap_suppressed_count') or 0)}件 / "
            f"最小尺へ自動拡張: {int(result.get('boundary_expanded_count') or 0)}件 / "
            f"境界警告: {int(result.get('boundary_warning_count') or 0)}件 / "
            f"最小尺未達: {int(result.get('below_min_duration_count') or 0)}件</li>",
            "<li>segment根拠: "
            + ("全候補で確認済み" if result.get("all_segment_linked") else "未確認")
            + "</li>",
            f"<li>隔離した不正ASR segment: "
            f"{int(result.get('invalid_segment_count') or 0)}件</li>",
            "</ul></details></section>",
            '<fieldset class="highlight-candidate-card-list">',
            '<legend class="highlight-candidate-legend">候補を選択</legend>',
        ]
        choices: list[tuple[str, str]] = []
        for ordinal, candidate in enumerate(candidates, start=1):
            start = float(candidate["start_sec"])
            end = float(candidate["end_sec"])
            title = html.escape(str(candidate.get("title") or "無題"))
            summary = html.escape(str(candidate.get("summary") or ""))
            reason = html.escape(str(candidate.get("reason") or ""))
            category = html.escape(str(candidate.get("category") or "未分類"))
            tags = "".join(
                '<span class="highlight-candidate-tag">'
                f"{html.escape(str(tag))}</span>"
                for tag in candidate.get("tags", [])
            )
            duration = end - start
            candidate_id = str(candidate["highlight_candidate_id"])
            escaped_candidate_id = html.escape(candidate_id, quote=True)
            checked = " checked" if ordinal == 1 else ""
            parts.extend([
                '<label class="highlight-candidate-card'
                + (" is-selected" if ordinal == 1 else "") + '">',
                '<span class="highlight-candidate-heading">',
                f'<input type="radio" name="highlight-candidate-inline" '
                f'value="{escaped_candidate_id}"{checked}>',
                '<strong class="highlight-candidate-title">'
                f"{ordinal}. {utils.format_timestamp(start)}–"
                f"{utils.format_timestamp(end)}（{duration:.1f}秒）　{title}"
                "</strong></span>",
                '<ul class="highlight-candidate-description">',
                f"<li>分類: {category}</li>",
            ])
            if summary:
                parts.append(f"<li>内容: {summary}</li>")
            if reason:
                parts.append(f"<li>選定理由: {reason}</li>")
            if tags:
                parts.append(f"<li>タグ: {tags}</li>")
            if candidate.get("boundary_warning"):
                parts.append(
                    '<li class="highlight-candidate-warning">⚠ 最大尺内で前後関係を'
                    "完結できない可能性があります。プレビュー後に編集画面で境界を"
                    "調整してください。</li>"
                )
            parts.extend(["</ul>", "</label>"])
            label = (
                f"{ordinal}. {utils.format_timestamp(start)}–"
                f"{utils.format_timestamp(end)}  {str(candidate.get('title') or '無題')}"
            )
            choices.append((label, candidate_id))
        parts.extend(["</fieldset>", "</div>"])
        return "".join(parts), choices
    finally:
        conn.close()


def load_latest_highlight_view(video_choice: str):
    cards, choices = _latest_highlight_view(video_choice)
    return cards, gr.update(value=choices[0][1] if choices else "")


def _resolve_highlight_candidate(video_choice: str, candidate_id: str):
    video_id = parse_video_choice(video_choice)
    if not video_id:
        raise gr.Error("動画を選択してください。")
    if not candidate_id:
        raise gr.Error("見どころ候補を選択してください。")
    conn = db.get_conn()
    try:
        db.init_db(conn)
        revision = db.get_active_transcript_revision(conn, video_id)
        if revision is None:
            raise gr.Error("この動画には有効な文字起こしがありません。")
        run = db.get_latest_ready_highlight_run(conn, video_id, revision)
        if run is None:
            raise gr.Error("利用できる見どころ候補がありません。")
        candidate = next(
            (
                item for item in db.get_highlight_candidates(
                    conn, run["highlight_run_id"]
                )
                if item["highlight_candidate_id"] == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise gr.Error("候補が更新されています。一覧を再表示してください。")
        video = db.get_video(conn, video_id)
        if not video:
            raise gr.Error("候補の元動画が見つかりません。")
        return video_id, video, candidate
    finally:
        conn.close()


def preview_highlight_candidate(video_choice: str, candidate_id: str):
    video_id, video, candidate = _resolve_highlight_candidate(
        video_choice, candidate_id
    )
    start = float(candidate["start_sec"])
    end = float(candidate["end_sec"])
    duration = float(video.get("duration") or end)
    preview = make_preview(
        video["path"], start, end, duration, video_id=video_id,
    )
    filename = Path(video["path"]).name
    detail = (
        f"**{html.escape(str(candidate.get('title') or '無題'))}**　｜　"
        f"{utils.format_timestamp(start)}–{utils.format_timestamp(end)}　｜　"
        f"{end - start:.1f}秒\n\n"
        f"{html.escape(str(candidate.get('summary') or ''))}"
    )
    return gr.update(
        value=preview,
        label=f"見どころ候補プレビュー: {filename}",
    ), detail


def _load_highlight_candidate_editor(video_choice: str, candidate_id: str):
    """Open one fitted candidate as a clean intuitive Edit plan."""
    video_id, video, candidate = _resolve_highlight_candidate(
        video_choice, candidate_id
    )
    overall_start = float(candidate["start_sec"])
    overall_end = float(candidate["end_sec"])
    duration = float(video.get("duration") or overall_end)
    if not 0 <= overall_start < overall_end <= duration:
        raise gr.Error("候補の時刻が元動画の範囲外です。候補を再生成してください。")
    viewport_start = max(0.0, overall_start - 10.0)
    viewport_end = min(duration, overall_end + 10.0)
    conn = db.get_conn()
    try:
        segments = db.get_segments_in_range(
            conn, video["video_id"], viewport_start, viewport_end,
        )
    finally:
        conn.close()
    state = _new_intuitive_state(video, overall_start, overall_end)
    state["preview_start"] = viewport_start
    state["preview_end"] = viewport_end
    state["viewport_start"] = viewport_start
    state["viewport_end"] = viewport_end
    state["playhead_sec"] = overall_start
    _set_intuitive_transcript_focus(state, overall_start)
    preview = make_intuitive_preview(
        state["video_id"], state["video_path"], viewport_start, viewport_end,
        state["duration"],
    )
    filename = Path(state["video_path"]).name
    interval = (
        f"{utils.format_timestamp(viewport_start)} - "
        f"{utils.format_timestamp(viewport_end)}"
    )
    info = (
        "**表示モード:** 元動画プレビュー（Source timeline）　｜　"
        f"**見どころ候補:** {html.escape(filename)}　｜　**表示区間:** {interval}"
    )
    return (
        state,
        gr.update(
            value=preview,
            label=f"1. 動画プレビュー（Source timeline）: {filename} [{video_id}]",
        ),
        render_intuitive_transcript(segments, state),
        info,
        render_intuitive_toolbar(state),
        render_intuitive_state_overview(state),
        render_intuitive_state_zoom(state),
        render_intuitive_summary(state),
        render_intuitive_exclusion_list(state),
        intuitive_selected_time_update(state),
    )


def load_highlight_candidate_into_editor(video_choice: str, candidate_id: str):
    loaded = _load_highlight_candidate_editor(video_choice, candidate_id)
    video_id = parse_video_choice(video_choice)
    gr.Info("候補を編集画面へ読み込みました。検索・編集・切り抜きタブで確認できます。")
    return (
        gr.update(value=video_id),
        *loaded,
        gr.update(value=video_id),
    )


def _create_query_highlight_run(
    video_choice: str,
    query: str,
    requested_count: int,
    min_duration_sec: float,
    max_duration_sec: float,
) -> dict:
    """Persist deterministic candidates derived from shared local search."""
    video_id = parse_video_choice(video_choice)
    normalized_query = str(query or "").strip()
    if not video_id:
        raise gr.Error("候補を生成する動画を選択してください。")
    if not normalized_query:
        raise gr.Error("検索したい内容を自然言語で入力してください。")
    requested_count = int(requested_count)
    min_duration_sec = float(min_duration_sec)
    max_duration_sec = float(max_duration_sec)
    if not 3 <= requested_count <= 10:
        raise gr.Error("候補件数は3〜10件で指定してください。")
    if (
        not math.isfinite(min_duration_sec)
        or not math.isfinite(max_duration_sec)
        or not 0 < min_duration_sec <= max_duration_sec
    ):
        raise gr.Error("候補尺は 0 < 最小尺 <= 最大尺 で指定してください。")

    hits = search_video_results(
        normalized_query,
        video_id,
        max(20, requested_count * 4),
        config.MIN_SCORE,
    )
    if not hits:
        raise gr.Error("この動画ではクエリに該当する場面が見つかりませんでした。")

    conn = db.get_conn()
    try:
        db.init_db(conn)
        revision = db.get_active_transcript_revision(conn, video_id)
        if revision is None:
            raise gr.Error("この動画には有効な文字起こしがありません。")
        analysis = db.get_latest_ready_analysis_run(conn, video_id, revision)
        if analysis is None:
            raise gr.Error("先にこの動画のLLM要約を作成してください。")
        source_segments = db.get_segments(
            conn, video_id, transcript_revision=revision
        )
        valid_segments = valid_source_segments(source_segments)
        chapters = db.get_analysis_chapters(conn, analysis["analysis_run_id"])
        candidates, suppressed = build_query_highlight_candidates(
            valid_segments,
            chapters,
            hits,
            normalized_query,
            requested_count=requested_count,
            min_duration_sec=min_duration_sec,
            max_duration_sec=max_duration_sec,
        )
        if not candidates:
            raise gr.Error("検索結果から有効な切り抜き範囲を作成できませんでした。")
        run_id = db.create_highlight_run(
            conn,
            video_id,
            revision,
            analysis["analysis_run_id"],
            provider="local-search",
            model="text+BGE-M3",
            prompt_version=QUERY_PROMPT_VERSION,
            requested_count=requested_count,
            min_duration_sec=min_duration_sec,
            max_duration_sec=max_duration_sec,
            commit=False,
        )
        durations = [
            float(candidate["end_sec"]) - float(candidate["start_sec"])
            for candidate in candidates
        ]
        result = {
            "generation_mode": "query",
            "query": normalized_query,
            "requested_count": requested_count,
            "candidate_count": len(candidates),
            "source_chapter_count": len(chapters),
            "invalid_segment_count": len(source_segments) - len(valid_segments),
            "duration_min": min(durations),
            "duration_median": statistics.median(durations),
            "duration_max": max(durations),
            "below_min_duration_count": sum(
                duration < min_duration_sec for duration in durations
            ),
            "boundary_expanded_count": sum(
                bool(candidate.get("boundary_expanded"))
                for candidate in candidates
            ),
            "boundary_warning_count": sum(
                bool(candidate.get("boundary_warning"))
                for candidate in candidates
            ),
            "overlap_suppressed_count": suppressed,
            "all_segment_linked": True,
            "prompt_version": QUERY_PROMPT_VERSION,
        }
        db.replace_highlight_candidates(
            conn, run_id, candidates, commit=False
        )
        db.update_highlight_run(
            conn, run_id, status="ready", result=result, commit=False
        )
        conn.commit()
        return {"highlight_run_id": run_id, **result}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def do_highlight_generation(
    generation_mode: str,
    video_choice: str,
    model: str,
    query: str,
    requested_count: int,
    min_duration_sec: float,
    max_duration_sec: float,
):
    """Dispatch the selected candidate source while keeping one UI contract."""
    if generation_mode == "summary":
        yield from do_existing_highlight_analysis(
            video_choice,
            model,
            requested_count,
            min_duration_sec,
            max_duration_sec,
        )
        return
    if generation_mode != "query":
        raise gr.Error("候補の作り方を選択してください。")
    log_lines = ["自然言語クエリで文字一致・意味検索を実行しています。"]
    yield "\n".join(log_lines), gr.update(), gr.update()
    result = _create_query_highlight_run(
        video_choice,
        query,
        requested_count,
        min_duration_sec,
        max_duration_sec,
    )
    log_lines.append(
        f"検索から見どころ候補を{result['candidate_count']}件作成しました。"
    )
    markdown, choices = _latest_highlight_view(video_choice)
    yield (
        "\n".join(log_lines),
        markdown,
        gr.update(value=choices[0][1] if choices else ""),
    )


def highlight_generation_mode_update(generation_mode: str):
    return gr.update(visible=generation_mode == "query")


def _highlight_export_context(video_choice: str) -> tuple[dict, list[dict]]:
    video_id = parse_video_choice(video_choice)
    if not video_id:
        raise gr.Error("動画を選択してください。")
    conn = db.get_conn()
    try:
        db.init_db(conn)
        revision = db.get_active_transcript_revision(conn, video_id)
        if revision is None:
            raise gr.Error("この動画には有効な文字起こしがありません。")
        run = db.get_latest_ready_highlight_run(conn, video_id, revision)
        if run is None:
            raise gr.Error("保存できる見どころ候補がありません。")
        candidates = db.get_highlight_candidates(conn, run["highlight_run_id"])
        chapters = db.get_analysis_chapters(conn, run["analysis_run_id"])
        chapter_titles = {
            int(chapter["ordinal"]): str(chapter.get("title") or "").strip()
            for chapter in chapters
        }
        candidates = [
            {
                **candidate,
                "export_title": (
                    chapter_titles.get(int(candidate["source_chapter_ordinal"]))
                    or str(candidate.get("title") or "").strip()
                    or "見どころ"
                ),
            }
            for candidate in candidates
        ]
        video = db.get_video(conn, video_id)
        if not video or not Path(video["path"]).is_file():
            raise gr.Error("候補の元動画が見つかりません。")
        return video, candidates
    finally:
        conn.close()


_WINDOWS_RESERVED_FILENAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _safe_highlight_filename_part(
    value: str, *, fallback: str, max_length: int
) -> str:
    """Return a readable filename part that is safe on Windows."""
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or ""))
    sanitized = re.sub(r"\s+", " ", sanitized).strip(" .")
    if not sanitized:
        sanitized = fallback
    sanitized = sanitized[:max_length].rstrip(" .") or fallback
    if sanitized.upper() in _WINDOWS_RESERVED_FILENAMES:
        sanitized = f"_{sanitized}"
    return sanitized


def _available_highlight_output_path(
    output_dir: Path, video_name: str, chapter_title: str
) -> Path:
    safe_video_name = _safe_highlight_filename_part(
        Path(video_name).stem,
        fallback="動画",
        max_length=64,
    )
    safe_chapter_title = _safe_highlight_filename_part(
        chapter_title,
        fallback="見どころ",
        max_length=96,
    )
    stem = f"{safe_video_name}_{safe_chapter_title}"
    candidate = output_dir / f"{stem}.mp4"
    suffix = 2
    while candidate.exists() or candidate.with_name(
        f".{candidate.name}.cut-video-claim"
    ).exists():
        candidate = output_dir / f"{stem}_{suffix}.mp4"
        suffix += 1
    return candidate


def export_highlight_candidates(
    video_choice: str,
    selected_candidate_id: str,
    export_scope: str,
    output_dir_text: str,
    precise: bool,
):
    """Cut selected/generated candidates locally with atomic final publish."""
    if not _highlight_export_lock.acquire(blocking=False):
        raise gr.Error("別の見どころ候補を保存中です。完了までお待ちください。")
    outputs: list[str] = []
    log_lines: list[str] = []
    try:
        video, candidates = _highlight_export_context(video_choice)
        if export_scope == "selected":
            candidates = [
                candidate for candidate in candidates
                if candidate["highlight_candidate_id"] == selected_candidate_id
            ]
            if not candidates:
                raise gr.Error("保存する候補を選択してください。")
        elif export_scope != "all":
            raise gr.Error("保存対象を選択してください。")
        output_dir = Path(
            str(output_dir_text or "").strip()
            or str(config.ARTIFACT_ROOT / "highlights")
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        log_lines.append(f"{len(candidates)}件の候補をローカル保存します。")
        yield "\n".join(log_lines), outputs
        video_name = str(
            video.get("display_name") or Path(video["path"]).name
        )
        for ordinal, candidate in enumerate(candidates, start=1):
            start = float(candidate["start_sec"])
            end = float(candidate["end_sec"])
            output = _available_highlight_output_path(
                output_dir,
                video_name,
                str(candidate.get("export_title") or candidate.get("title") or "見どころ"),
            )
            temporary = output.with_name(
                f".{output.stem}.{secrets.token_hex(4)}.partial.mp4"
            )
            claim = output.with_name(f".{output.name}.cut-video-claim")
            try:
                try:
                    claim.touch(exist_ok=False)
                except FileExistsError as exc:
                    raise gr.Error(
                        "同じ候補の保存処理が競合しました。もう一度実行してください。"
                    ) from exc
                cut_clip(
                    Path(video["path"]),
                    start,
                    end,
                    temporary,
                    pad=0.0,
                    precise=bool(precise),
                    duration=float(video.get("duration") or end),
                )
                if output.exists():
                    raise gr.Error(
                        f"保存先に同名ファイルが作成されました: {output.name}"
                    )
                temporary.replace(output)
            finally:
                temporary.unlink(missing_ok=True)
                claim.unlink(missing_ok=True)
            outputs.append(str(output.resolve()))
            log_lines.append(
                f"{ordinal}/{len(candidates)} 保存完了: {output.name}"
            )
            yield "\n".join(log_lines), list(outputs)
    finally:
        _highlight_export_lock.release()


def do_existing_highlight_analysis(
    video_choice: str,
    model: str,
    requested_count: int,
    min_duration_sec: float,
    max_duration_sec: float,
):
    """Generate segment-linked candidates in a separate local process."""
    video_id = parse_video_choice(video_choice)
    if not video_id:
        raise gr.Error("候補を生成する動画を選択してください。")
    selected_model = (model or config.LLM_HIGHLIGHT_MODEL).strip()
    if not selected_model:
        raise gr.Error("Ollamaモデル名を指定してください。")
    try:
        requested_count = int(requested_count)
        min_duration_sec = float(min_duration_sec)
        max_duration_sec = float(max_duration_sec)
    except (TypeError, ValueError) as exc:
        raise gr.Error("候補件数と候補尺を数値で指定してください。") from exc
    if not 3 <= requested_count <= 10:
        raise gr.Error("候補件数は3〜10件で指定してください。")
    if (
        not math.isfinite(min_duration_sec)
        or not math.isfinite(max_duration_sec)
        or not 0 < min_duration_sec <= max_duration_sec
    ):
        raise gr.Error("候補尺は 0 < 最小尺 <= 最大尺 となるよう指定してください。")
    if not _index_lock.acquire(blocking=False):
        raise gr.Error(
            "別の動画追加・LLM解析・候補生成が実行中です。完了までお待ちください。"
        )

    import os
    import sys

    command = [
        sys.executable,
        "generate_highlights.py",
        "--video-id", video_id,
        "--model", selected_model,
        "--count", str(requested_count),
        "--min-duration", str(min_duration_sec),
        "--max-duration", str(max_duration_sec),
    ]
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }
    process = None
    log_lines = ["ローカルLLMで見どころ候補の生成を開始しました。"]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            cwd=str(Path(__file__).parent),
        )
        INDEX_JOB_PIDFILE.write_text(str(process.pid), encoding="utf-8")
        _index_state["proc"] = process
        _index_state["stopped"] = False
        yield "\n".join(log_lines), gr.update(), gr.update()
        for line in iter(process.stdout.readline, ""):
            text = line.rstrip()
            if text:
                log_lines.append(text)
                yield "\n".join(log_lines), gr.update(), gr.update()
        code = process.wait()
        if code == 0:
            markdown, choices = _latest_highlight_view(video_choice)
            yield (
                "\n".join(log_lines),
                markdown,
                gr.update(value=choices[0][1] if choices else ""),
            )
        elif _index_state["stopped"]:
            log_lines.append(
                "候補生成を停止しました。文字起こし・検索・編集内容は変更されていません。"
            )
            yield "\n".join(log_lines), gr.update(), gr.update()
        else:
            log_lines.append(
                "候補生成に失敗しました。文字起こし・検索・編集内容は変更されていません。"
            )
            yield "\n".join(log_lines), gr.update(), gr.update()
    finally:
        INDEX_JOB_PIDFILE.unlink(missing_ok=True)
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
    _remove_app_pidfile()
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

def do_export(video_choice: str, privacy_confirmed: bool = False):
    video_id = parse_video_choice(video_choice)
    if not video_id:
        raise gr.Error("エクスポートする動画を選択してください。")
    if privacy_confirmed is not True:
        raise gr.Error(
            "全文文字起こし等を含むことと、個人情報を確認したことに同意してください。"
        )
    try:
        out_path = export_index(video_id, confirm_sensitive=True)
    except ShareError as e:
        raise gr.Error(str(e))
    gr.Info(f"エクスポートしました: {out_path}")
    return str(out_path), f"保存先: {out_path}"


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


def do_relink(video_choice: str, source_path: str):
    public_id = parse_video_choice(video_choice)
    if not public_id:
        raise gr.Error("再関連付けする動画を選択してください。")
    try:
        video = relink_video(public_id, Path((source_path or "").strip()))
    except ShareError as exc:
        raise gr.Error(str(exc)) from exc
    return f"再関連付けしました: {video['display_name']}"


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








_INTUITIVE_COLLAPSE_VIDEO_PICKER_JS = r"""() => {
  const picker = document.getElementById('intuitive-video-picker');
  const accordion = picker ? picker.closest('.gr-accordion') || picker : null;
  const content = accordion
    ? accordion.querySelector(':scope > [data-testid="accordion-content"]')
    : null;
  const button = accordion
    ? accordion.querySelector(':scope > button.label-wrap')
    : null;
  if (content && button && getComputedStyle(content).display !== 'none') button.click();
  if (picker) picker.classList.remove('is-intuitive-reselecting');
  const focusEditor = () => {
    const editor = document.getElementById('intuitive-mode-row');
    if (editor) editor.scrollIntoView({block: 'start', behavior: 'auto'});
  };
  requestAnimationFrame(focusEditor);
  setTimeout(focusEditor, 120);
  setTimeout(focusEditor, 300);
  const syncSearchMarkers = () => document.dispatchEvent(
    new CustomEvent('cut-video:sync-search-markers')
  );
  requestAnimationFrame(syncSearchMarkers);
  setTimeout(syncSearchMarkers, 120);
}"""


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

    with gr.Tab(
        "従来版（検索・切り抜き）",
        visible=config.ENABLE_LEGACY_UI,
        elem_id="legacy-search-cut-tab",
    ):
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

    with gr.Tab(
        "検索・編集・切り抜き",
        id="intuitive-main",
        elem_id="intuitive-editor-tab",
    ):
        intuitive_state = gr.State(None)
        # An initially open Accordion does not emit an expand event.
        # Seed directly selectable cards so the picker is immediately usable.
        # First paint uses only cached thumbnails. Missing thumbnails are made
        # when the user expands, filters or explicitly refreshes the picker,
        # so a newly added large library cannot block application startup.
        _INTUITIVE_INITIAL_CARDS = _intuitive_video_cards_data(
            "", generate_thumbnails=False
        )
        _INTUITIVE_INITIAL_CARDS_HTML = render_intuitive_video_cards([
            {**card, "thumbnail_url": _thumbnail_servable_url(card.get("thumbnail_path"))}
            for card in _INTUITIVE_INITIAL_CARDS
        ])

        with gr.Accordion(
            "サムネイルから編集する動画を選ぶ", open=True,
            elem_id="intuitive-video-picker",
        ) as intuitive_video_picker:
            with gr.Row(elem_classes=["intuitive-compact-row"]):
                intuitive_video_filter = gr.Textbox(
                    label="ファイル名で絞り込み",
                    placeholder="ファイル名の一部を入力",
                    scale=4,
                    elem_id="intuitive-video-filter",
                )
                intuitive_video_filter_btn = gr.Button(
                    "絞り込む", scale=1, elem_id="intuitive-video-filter-button",
                )
                intuitive_reload_btn = gr.Button(
                    "一覧更新", scale=1, elem_id="intuitive-reload-videos",
                )
            intuitive_video_gallery_html = gr.HTML(
                value=_INTUITIVE_INITIAL_CARDS_HTML,
                label="動画一覧（選ぶと自動で編集を開始）",
                elem_id="intuitive-video-card-grid",
            )
            # A tiny hidden bridge carries the clicked card's stable video ID
            # into Gradio. No parallel Gallery or index coupling is required.
            intuitive_video_card_command = gr.Textbox(
                value="",
                show_label=False,
                container=False,
                elem_id="intuitive-video-card-command",
            )
            intuitive_video_card_submit = gr.Button(
                "select-card",
                elem_id="intuitive-video-card-submit",
            )
            with gr.Row(elem_classes=["intuitive-fallback-picker"]):
                intuitive_video_select = gr.Dropdown(
                    choices=list_video_choices_only(),
                    label="サムネイルを表示できない場合の動画選択",
                    scale=5,
                    elem_id="intuitive-video-select",
                )
                intuitive_load_btn = gr.Button(
                    "選択動画を再読み込み", variant="secondary", scale=1,
                    elem_id="intuitive-load-video",
                )
        intuitive_search_results = gr.State({"request_id": "", "results": []})
        with gr.Group(elem_id="intuitive-header"):
            with gr.Row(elem_id="intuitive-mode-row"):
                intuitive_video_info = gr.Markdown(
                    "**選択動画:** 未選択　｜　**表示区間:** 未読み込み",
                    elem_id="intuitive-video-info",
                    scale=4,
                )
                intuitive_summary = gr.HTML(
                    '<div class="intuitive-edit-summary">動画を読み込むと編集内容を表示します。</div>',
                    elem_id="intuitive-edit-summary",
                    scale=5,
                )
                intuitive_reselect_video_btn = gr.Button(
                    "動画を選び直す", scale=0, min_width=118, size="sm",
                    elem_id="intuitive-reselect-video",
                )
                intuitive_preview_result_btn = gr.Button(
                    "編集結果を確認", scale=0, min_width=138,
                    elem_id="intuitive-preview-result",
                )
                intuitive_return_source_btn = gr.Button(
                    "元動画へ戻る", scale=0, min_width=126,
                    elem_id="intuitive-return-source",
                )

        with gr.Row(equal_height=True, elem_id="intuitive-workspace-row"):
            with gr.Column(
                scale=5, min_width=480, elem_id="intuitive-preview-panel",
            ):
                intuitive_preview = gr.Video(
                    label="1. 動画プレビュー（Source timeline）",
                    autoplay=False,
                    interactive=False,
                    height=320,
                    elem_id="intuitive-preview-video",
                )
            with gr.Column(
                scale=4, min_width=400, elem_id="intuitive-search-panel",
            ):
                gr.Markdown(
                    "**2. 文字クエリー検索**",
                    elem_classes=["intuitive-panel-heading"],
                )
                with gr.Row(elem_classes=["intuitive-search-primary"]):
                    intuitive_query = gr.Textbox(
                        label="検索したい文字・フレーズ",
                        placeholder="文字・フレーズを検索",
                        show_label=False,
                        container=False,
                        scale=4,
                        elem_id="intuitive-search-query",
                    )
                    intuitive_search_btn = gr.Button(
                        "検索", variant="primary", scale=0, min_width=72,
                        elem_id="intuitive-search-button",
                    )
                with gr.Row(elem_classes=["intuitive-search-options"]):
                    intuitive_search_target = gr.Dropdown(
                        choices=list_video_choices(), value=ALL_VIDEOS_VALUE,
                        label="検索対象", show_label=False, container=False,
                        elem_id="intuitive-search-target",
                    )
                intuitive_result_table = gr.Dataframe(
                    headers=["#", "時刻", "方式", "動画・根拠"],
                    interactive=False,
                    label="検索結果（行を選ぶと即プレビュー）", show_label=False,
                    elem_id="intuitive-search-results",
                )
                intuitive_search_status = gr.Markdown(
                    "検索すると、文字一致を先に表示し、意味検索結果を後から追加します。",
                    elem_id="intuitive-search-status",
                )
            with gr.Column(
                scale=3, min_width=280,
                elem_id="intuitive-transcript-panel",
            ):
                intuitive_toolbar = gr.HTML(
                    '<div class="intuitive-toolbox"><strong>3. 文字起こし編集</strong>'
                    '<div class="intuitive-tool-buttons">'
                    '<button disabled>全体開始</button><button disabled>全体終了</button>'
                    '<button disabled>除外開始</button><button disabled>除外終了</button></div>'
                    '<div class="intuitive-tool-status" role="status" aria-live="polite">'
                    '動画を読み込んでください。</div></div>',
                    elem_id="intuitive-toolbox",
                    elem_classes=["intuitive-tool-header"],
                )
                intuitive_transcript = gr.HTML(
                    render_intuitive_transcript([]),
                    elem_id="intuitive-transcript-words",
                )

        # 全体の俯瞰と詳細操作は同じ EditPlan を見る二つの表示です。
        # Gradio の Tab 切替には callback を結び付けず、再生位置・選択境界・
        # active tool・revision・Undo/Redo を含む canonical state を維持します。
        with gr.Tabs(elem_id="intuitive-timeline-tabs"):
            with gr.Tab(
                "① 全体を決める",
                elem_id="intuitive-overall-range-tab",
            ):
                gr.HTML(
                    '<div class="intuitive-overall-range-actions">'
                    '<span>青枠で詳細表示する範囲を合わせた後、保存範囲へ反映できます。</span>'
                    '<button type="button" class="intuitive-fit-overall-button" '
                    'data-intuitive-fit-overall>表示範囲を保存範囲として適用</button>'
                    '</div>',
                    elem_id="intuitive-overall-range-actions",
                )
                gr.HTML(
                    '<div class="intuitive-overall-adjust-controls" '
                    'data-intuitive-overall-controls>'
                    '<div class="intuitive-overall-boundary-picker" role="group" '
                    'aria-label="微調整する全体境界">'
                    '<span>調整する境界</span>'
                    '<button type="button" data-intuitive-select-overall-boundary="overall_start" '
                    'aria-pressed="false">全体開始</button>'
                    '<button type="button" data-intuitive-select-overall-boundary="overall_end" '
                    'aria-pressed="false">全体終了</button></div>'
                    '<fieldset class="intuitive-overall-adjust-steps">'
                    '<legend>調整幅（秒）</legend>'
                    '<label><input type="radio" name="intuitive-overall-adjust-step" '
                    'value="0.1" data-intuitive-overall-step>0.1</label>'
                    '<label><input type="radio" name="intuitive-overall-adjust-step" '
                    'value="1" data-intuitive-overall-step checked>1</label>'
                    '<label><input type="radio" name="intuitive-overall-adjust-step" '
                    'value="10" data-intuitive-overall-step>10</label>'
                    '<label><input type="radio" name="intuitive-overall-adjust-step" '
                    'value="30" data-intuitive-overall-step>30</label>'
                    '<label><input type="radio" name="intuitive-overall-adjust-step" '
                    'value="60" data-intuitive-overall-step>60</label>'
                    '<label><input type="radio" name="intuitive-overall-adjust-step" '
                    'value="600" data-intuitive-overall-step>600</label>'
                    '</fieldset>'
                    '<div class="intuitive-overall-adjust-actions" role="group" '
                    'aria-label="選択した全体境界を微調整">'
                    '<button type="button" data-intuitive-overall-adjust="-1" disabled>前へ</button>'
                    '<button type="button" data-intuitive-overall-adjust="1" disabled>後ろへ</button>'
                    '</div></div>',
                    elem_id="intuitive-overall-adjust-controls",
                )
                intuitive_overview_timeline = gr.HTML(
                    render_intuitive_overview_timeline(1.0, 0.0, 1.0),
                    elem_id="intuitive-overview-timeline",
                )
                intuitive_search_marker_projection = gr.HTML(
                    render_intuitive_search_marker_projection(
                        {"request_id": "", "results": []}
                    ),
                    elem_id="intuitive-search-marker-projection",
                )

            with gr.Tab(
                "② 詳細編集（任意）",
                elem_id="intuitive-detail-edit-tab",
            ):
                intuitive_zoom_timeline = gr.HTML(
                    render_intuitive_zoom_timeline(0.0, 1.0),
                    elem_id="intuitive-zoom-timeline",
                )
                with gr.Row(
                    equal_height=True,
                    elem_id="intuitive-edit-controls-row",
                ):
                    with gr.Column(scale=8, min_width=790):
                        with gr.Row(
                            elem_classes=["intuitive-compact-row"],
                            elem_id="intuitive-boundary-controls",
                        ):
                            # 選択中の境界（種別＋時刻）は境界ツールのステータス行
                            # （.intuitive-tool-status、render_intuitive_toolbar）に統合済み。
                            # この行は調整操作だけのコンパクトな1行にする。
                            intuitive_adjust_step = gr.Radio(
                                choices=ADJUST_STEPS,
                                value=1.0,
                                label="調整幅 (秒)",
                                interactive=True,
                                scale=4,
                                min_width=360,
                                container=False,
                                elem_id="intuitive-adjust-step",
                            )
                            intuitive_selected_time = gr.Number(
                                value=None,
                                label="選択境界の時刻 (秒)",
                                interactive=False,
                                show_label=False,
                                container=False,
                                scale=2,
                                min_width=135,
                                elem_id="intuitive-selected-time",
                                elem_classes=["intuitive-control-group-start"],
                            )
                            intuitive_apply_time_btn = gr.Button(
                                "時刻を適用", scale=0, min_width=82, size="sm",
                                elem_id="intuitive-apply-time",
                            )
                            intuitive_apply_current_btn = gr.Button(
                                "現在位置", scale=0, min_width=86, size="sm",
                                elem_id="intuitive-apply-current",
                                elem_classes=["intuitive-control-group-start"],
                            )
                            intuitive_before_btn = gr.Button(
                                "前へ", interactive=True, scale=0, min_width=68, size="sm",
                                elem_id="intuitive-adjust-before",
                            )
                            intuitive_after_btn = gr.Button(
                                "後ろへ", interactive=True, scale=0, min_width=68, size="sm",
                                elem_id="intuitive-adjust-after",
                            )
                    with gr.Column(
                        scale=3,
                        min_width=300,
                        elem_id="intuitive-exclusion-panel",
                    ):
                        intuitive_exclusion_list = gr.HTML(
                            '<details class="intuitive-exclusion-list" open><summary>途中カット一覧（0箇所）</summary>'
                            '<div class="intuitive-exclusion-list-body"><div class="intuitive-exclusion-empty">'
                            '途中カットはありません。</div></div></details>',
                            elem_id="intuitive-exclusion-list",
                        )
        with gr.Group(elem_id="intuitive-save-bar"):
            with gr.Row(elem_classes=["intuitive-compact-row"]):
                intuitive_out_dir = gr.Textbox(
                    value=DEFAULT_CLIPS_DIR, label="保存先", show_label=False,
                    container=False, scale=2, elem_id="intuitive-out-dir",
                )
                intuitive_folder_btn = gr.Button(
                    "参照", scale=0, min_width=80, elem_id="intuitive-folder-browse",
                )
                intuitive_filename = gr.Textbox(
                    value="", label="ファイル名（空なら自動）",
                    placeholder="ファイル名（空なら自動）", show_label=False,
                    container=False, scale=2, elem_id="intuitive-filename",
                )
                intuitive_precise = gr.Checkbox(
                    value=True, label="フレーム精度", scale=1,
                    elem_id="intuitive-precise",
                )
                intuitive_srt = gr.Checkbox(
                    value=False, label="SRT字幕も保存", scale=1,
                    elem_id="intuitive-srt",
                )
                intuitive_save_btn = gr.Button(
                    "保存", variant="primary", scale=1,
                    elem_id="intuitive-save-button",
                )
            intuitive_saved_path = gr.Textbox(
                label="保存結果", interactive=False, lines=1,
                show_label=False, container=False,
                elem_id="intuitive-saved-path",
            )

        with gr.Accordion("UI比較計測（匿名・ローカル保存）", open=False):
            gr.Markdown(
                "検索語・動画名・パスは記録しません。各UI・各シナリオを最低10回記録すると、"
                "設計書の採用条件を自動判定します。"
            )
            intuitive_experiment_state = gr.State(None)
            with gr.Row():
                intuitive_experiment_scenario = gr.Dropdown(
                    choices=[("文字一致", "text_search"), ("意味検索", "semantic_search"),
                             ("検索なし手動", "manual_clip")],
                    value="text_search", label="シナリオ",
                )
                intuitive_experiment_variant = gr.Radio(
                    choices=[("既存Gradio", "gradio"), ("候補UI", "candidate")],
                    value="candidate", label="UI",
                )
                intuitive_experiment_cold = gr.Checkbox(value=False, label="Cold条件")
                intuitive_experiment_start = gr.Button("計測開始")
            with gr.Row():
                intuitive_experiment_actions = gr.Number(value=0, minimum=0, label="操作数")
                intuitive_experiment_errors = gr.Number(value=0, minimum=0, label="エラー数")
                intuitive_experiment_accepted = gr.Checkbox(value=True, label="シナリオ完了")
                intuitive_experiment_complete = gr.Button("完了を記録")
            intuitive_experiment_status = gr.Markdown("未計測")
            intuitive_experiment_start.click(
                start_ui_experiment,
                inputs=[intuitive_experiment_scenario, intuitive_experiment_variant,
                        intuitive_experiment_cold],
                outputs=[intuitive_experiment_state, intuitive_experiment_status],
            )
            intuitive_experiment_complete.click(
                complete_ui_experiment,
                inputs=[intuitive_experiment_state, intuitive_experiment_actions,
                        intuitive_experiment_errors, intuitive_experiment_accepted],
                outputs=[intuitive_experiment_state, intuitive_experiment_status],
            )

        intuitive_command_json = gr.Textbox(
            value="", elem_id="intuitive-command-json",
        )
        intuitive_command_submit = gr.Button(
            "command", elem_id="intuitive-command-submit",
        )
        intuitive_sync_token = gr.Textbox(
            value="", elem_id="intuitive-sync-token",
        )
        intuitive_sync_submit = gr.Button(
            "sync", elem_id="intuitive-sync-submit",
        )
        intuitive_sync_ack = gr.HTML(
            value='<span data-intuitive-sync-token="" aria-hidden="true"></span>',
            elem_id="intuitive-sync-ack",
        )
        intuitive_search_marker_command = gr.Textbox(
            value="", elem_id="intuitive-search-marker-command",
        )
        intuitive_search_marker_submit = gr.Button(
            "select-search-marker", elem_id="intuitive-search-marker-submit",
        )
        intuitive_result_time = gr.Number(value=0.0, visible=False)
        intuitive_render_outputs = [
            intuitive_state,
            intuitive_toolbar,
            intuitive_overview_timeline,
            intuitive_zoom_timeline,
            intuitive_preview,
            intuitive_transcript,
            intuitive_video_info,
            intuitive_summary,
            intuitive_exclusion_list,
            intuitive_selected_time,
        ]

        intuitive_load_outputs = [
            intuitive_state,
            intuitive_preview,
            intuitive_transcript,
            intuitive_video_info,
            intuitive_toolbar,
            intuitive_overview_timeline,
            intuitive_zoom_timeline,
            intuitive_summary,
            intuitive_exclusion_list,
            intuitive_selected_time,
        ]
        intuitive_load_and_search_outputs = [
            *intuitive_load_outputs,
            intuitive_search_target,
        ]
        intuitive_load_btn.click(
            load_intuitive_editor_with_search_target,
            inputs=[intuitive_video_select],
            outputs=intuitive_load_and_search_outputs,
            concurrency_id="intuitive-editor-state",
            concurrency_limit=1,
        ).success(fn=None, js=_INTUITIVE_COLLAPSE_VIDEO_PICKER_JS)
        # `.input` reacts only to a user's fallback selection.  Gallery output
        # may update the Dropdown value too, without starting a duplicate load.
        intuitive_video_select.input(
            load_intuitive_editor_with_search_target,
            inputs=[intuitive_video_select],
            outputs=intuitive_load_and_search_outputs,
            trigger_mode="always_last",
            show_progress="minimal",
            concurrency_id="intuitive-editor-state",
            concurrency_limit=1,
        ).success(fn=None, js=_INTUITIVE_COLLAPSE_VIDEO_PICKER_JS)
        intuitive_video_picker.expand(
            build_intuitive_video_cards,
            inputs=[intuitive_video_filter, intuitive_video_select],
            outputs=[intuitive_video_gallery_html],
        )
        intuitive_video_filter_btn.click(
            build_intuitive_video_cards,
            inputs=[intuitive_video_filter, intuitive_video_select],
            outputs=[intuitive_video_gallery_html],
        )
        intuitive_video_filter.submit(
            build_intuitive_video_cards,
            inputs=[intuitive_video_filter, intuitive_video_select],
            outputs=[intuitive_video_gallery_html],
        )
        intuitive_reload_btn.click(
            refresh_intuitive_video_picker,
            inputs=[
                intuitive_video_filter, intuitive_video_select,
                intuitive_search_target,
            ],
            outputs=[
                intuitive_video_gallery_html,
                intuitive_video_select,
                intuitive_search_target,
            ],
        )
        intuitive_video_card_submit.click(
            select_intuitive_video_from_card,
            inputs=[intuitive_video_card_command, intuitive_video_filter],
            outputs=[
                intuitive_video_select,
                intuitive_video_gallery_html,
                *intuitive_load_and_search_outputs,
            ],
            trigger_mode="always_last",
            show_progress="minimal",
            concurrency_id="intuitive-editor-state",
            concurrency_limit=1,
        ).success(
            fn=None,
            js=_INTUITIVE_COLLAPSE_VIDEO_PICKER_JS,
        )
        intuitive_search_outputs = [
            intuitive_result_table,
            intuitive_search_results,
            intuitive_search_status,
            intuitive_search_marker_projection,
        ]
        intuitive_search_btn.click(
            do_intuitive_search_staged,
            inputs=[
                intuitive_query, intuitive_search_target,
                gr.State(5), gr.State(config.MIN_SCORE),
            ],
            outputs=intuitive_search_outputs,
            trigger_mode="always_last",
            concurrency_id="intuitive-search",
            concurrency_limit=2,
        ).success(fn=None, js=_INTUITIVE_COLLAPSE_VIDEO_PICKER_JS)
        intuitive_query.submit(
            do_intuitive_search_staged,
            inputs=[
                intuitive_query, intuitive_search_target,
                gr.State(5), gr.State(config.MIN_SCORE),
            ],
            outputs=intuitive_search_outputs,
            trigger_mode="always_last",
            concurrency_id="intuitive-search",
            concurrency_limit=2,
        ).success(fn=None, js=_INTUITIVE_COLLAPSE_VIDEO_PICKER_JS)
        intuitive_result_table.select(
            on_intuitive_search_select,
            inputs=[intuitive_search_results],
            outputs=[
                intuitive_result_table,
                intuitive_state,
                intuitive_preview,
                intuitive_transcript,
                intuitive_video_info,
                intuitive_toolbar,
                intuitive_overview_timeline,
                intuitive_zoom_timeline,
                intuitive_summary,
                intuitive_exclusion_list,
                intuitive_selected_time,
            ],
            concurrency_id="intuitive-editor-state",
            concurrency_limit=1,
        ).success(fn=None, js=_INTUITIVE_COLLAPSE_VIDEO_PICKER_JS)
        intuitive_search_marker_submit.click(
            on_intuitive_search_marker,
            inputs=[intuitive_search_marker_command, intuitive_search_results],
            outputs=[
                intuitive_result_table,
                intuitive_state,
                intuitive_preview,
                intuitive_transcript,
                intuitive_video_info,
                intuitive_toolbar,
                intuitive_overview_timeline,
                intuitive_zoom_timeline,
                intuitive_summary,
                intuitive_exclusion_list,
                intuitive_selected_time,
            ],
            concurrency_id="intuitive-editor-state",
            concurrency_limit=1,
        ).success(fn=None, js=_INTUITIVE_COLLAPSE_VIDEO_PICKER_JS)
        intuitive_command_submit.click(
            handle_intuitive_command,
            inputs=[intuitive_command_json, intuitive_state],
            outputs=intuitive_render_outputs,
            concurrency_id="intuitive-editor-state",
            concurrency_limit=1,
        )
        intuitive_sync_submit.click(
            sync_intuitive_editor,
            inputs=[intuitive_sync_token, intuitive_state],
            outputs=[*intuitive_render_outputs, intuitive_sync_ack],
            concurrency_id="intuitive-editor-state",
            concurrency_limit=1,
        )
        intuitive_preview_result_btn.click(
            preview_intuitive_editor,
            inputs=[intuitive_state],
            outputs=intuitive_render_outputs,
            concurrency_id="intuitive-editor-state",
            concurrency_limit=1,
        )
        intuitive_return_source_btn.click(
            return_intuitive_source,
            inputs=[intuitive_state, intuitive_result_time],
            outputs=intuitive_render_outputs,
            js="""(state, _time) => {
              const video = document.querySelector('#intuitive-preview-video video');
              return [state, video ? Number(video.currentTime || 0) : 0];
            }""",
            concurrency_id="intuitive-editor-state",
            concurrency_limit=1,
        )
        intuitive_save_btn.click(
            save_intuitive_editor,
            inputs=[
                intuitive_state, intuitive_precise,
                intuitive_out_dir, intuitive_filename, intuitive_srt,
            ],
            outputs=[intuitive_saved_path, intuitive_state, intuitive_toolbar],
            concurrency_id="intuitive-editor-state",
            concurrency_limit=1,
        )
        intuitive_folder_btn.click(
            browse_folder,
            inputs=[intuitive_out_dir],
            outputs=[intuitive_out_dir],
        )
        demo.load(fn=None, js=_INTUITIVE_EDITOR_JS)

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
        with gr.Row():
            llm_analysis_chk = gr.Checkbox(
                value=False,
                label="文字起こし後に要約・タグ・章を生成（実験・ローカルOllama）",
            )
            llm_model_box = gr.Textbox(
                value=config.LLM_ANALYSIS_MODEL,
                label="Ollamaモデル",
                placeholder="例: qwen3:8b",
            )
        gr.Markdown(
            "LLM解析は既定で無効です。有効時も文字起こし・検索の完了後に実行され、"
            "解析失敗で動画登録は取り消されません。外部クラウドには送信しません。"
            "初回だけ `setup_ollama.bat` を実行してOllamaとモデルを準備してください。"
        )
        index_log = gr.Textbox(label="進捗ログ", interactive=False, lines=10)

        video_browse_btn.click(browse_video, inputs=[new_video_box], outputs=[new_video_box])
        index_btn.click(
            do_index,
            inputs=[
                new_video_box,
                asr_model_dd,
                force_chk,
                batch_infer_chk,
                llm_analysis_chk,
                llm_model_box,
            ],
            outputs=[index_log, video_select],
            concurrency_id="library-index-io",
            concurrency_limit=1,
        )
        stop_btn.click(stop_indexing)

    with gr.Tab("LLM要約・見どころ"):
        gr.Markdown(
            "## LLM要約・見どころ\n"
            "文字起こし済み動画の要約を作成・確認し、その保存済み要約から"
            "見どころ候補を作成します。処理はローカルOllamaだけを使用します。"
        )
        with gr.Tabs(elem_id="llm-workspace-tabs"):
            with gr.Tab("① 要約を作る・確認する"):
                gr.Markdown(
                    "保存済みの要約・タグ・時間付き章を確認できます。"
                    "要約がない動画は、この画面から別プロセスで作成できます。"
                )
                with gr.Row():
                    llm_result_video = gr.Dropdown(
                        choices=list_llm_result_video_choices(),
                        label="対象動画",
                        scale=3,
                    )
                    llm_workspace_model_box = gr.Textbox(
                        value=config.LLM_ANALYSIS_MODEL,
                        label="要約に使うOllamaモデル",
                        placeholder="例: qwen3:8b",
                        scale=1,
                    )
                    llm_result_reload = gr.Button("動画一覧を更新", scale=1)
                with gr.Row(equal_height=True):
                    llm_summary_source_preview = gr.Video(
                        label="対象動画のプレビュー",
                        autoplay=False,
                        interactive=False,
                        height=280,
                        scale=3,
                    )
                    llm_summary_source_detail = gr.Markdown(
                        "**対象動画:** 未選択",
                        container=True,
                        scale=2,
                    )
                with gr.Row():
                    llm_result_show = gr.Button("保存済み結果を表示")
                    llm_result_analyze = gr.Button(
                        "この動画をローカルLLMで解析",
                        variant="primary",
                    )
                llm_result_log = gr.Textbox(
                    label="LLM解析ログ",
                    interactive=False,
                    lines=4,
                )
                llm_result_markdown = gr.Markdown(
                    "動画を選択して保存済み結果を表示してください。"
                )
            with gr.Tab("② 要約から見どころを作る・切り抜く"):
                gr.Markdown(
                    "保存済みの章から候補章を選び、選んだ章の元文字起こしへ戻って"
                    "内容が収まる範囲を作ります。映像・表情・音の盛り上がりは評価しません。"
                    "候補はプレビュー・編集でき、確認後に選択候補または全候補をまとめて保存できます。"
                )
                with gr.Row():
                    llm_highlight_video = gr.Dropdown(
                        choices=list_llm_result_video_choices(),
                        label="対象動画",
                        scale=3,
                    )
                    llm_highlight_model_box = gr.Textbox(
                        value=config.LLM_HIGHLIGHT_MODEL,
                        label="候補生成に使うOllamaモデル",
                        placeholder="例: qwen3:8b",
                        scale=1,
                    )
                    llm_highlight_reload = gr.Button("動画一覧を更新", scale=1)
                with gr.Row(equal_height=True):
                    llm_highlight_source_preview = gr.Video(
                        label="対象動画のプレビュー",
                        autoplay=False,
                        interactive=False,
                        height=280,
                        scale=3,
                    )
                    llm_highlight_source_detail = gr.Markdown(
                        "**対象動画:** 未選択",
                        container=True,
                        scale=2,
                    )
                highlight_source_status = gr.Markdown(
                    "要約生成済みの動画を上で選ぶと、保存済み要約を再利用できます。"
                )
                highlight_generation_mode = gr.Radio(
                    choices=[
                        ("要約から自動選定", "summary"),
                        ("自然言語クエリで検索", "query"),
                    ],
                    value="summary",
                    label="候補の作り方",
                )
                highlight_query = gr.Textbox(
                    label="検索したい内容",
                    placeholder="例: 配信環境の改善について具体的に説明している場面",
                    lines=2,
                    visible=False,
                )
                with gr.Row():
                    highlight_count = gr.Number(
                        value=config.LLM_HIGHLIGHT_COUNT,
                        minimum=3,
                        maximum=10,
                        precision=0,
                        label="候補件数（3〜10）",
                    )
                    highlight_min_duration = gr.Number(
                        value=config.LLM_HIGHLIGHT_MIN_DURATION_SEC,
                        minimum=1,
                        label="最小尺（秒・目安）",
                    )
                    highlight_max_duration = gr.Number(
                        value=config.LLM_HIGHLIGHT_MAX_DURATION_SEC,
                        minimum=1,
                        label="最大尺（秒・話の完結を優先）",
                    )
                with gr.Row():
                    highlight_result_show = gr.Button("保存済み候補を表示")
                    highlight_result_generate = gr.Button(
                        "見どころ候補を生成",
                        variant="primary",
                        interactive=False,
                    )
                highlight_result_log = gr.Textbox(
                    label="見どころ候補生成ログ",
                    interactive=False,
                    lines=4,
                )
                highlight_result_markdown = gr.HTML(
                    '<div class="highlight-candidate-empty">'
                    "先に動画と保存済みLLM解析結果を選択してください。</div>",
                    elem_id="highlight-candidate-cards",
                )
                # Inline radio cards write their stable candidate ID here.
                # Keeping one Gradio bridge makes preview/edit/export callbacks
                # independent from the rendered card order.
                highlight_candidate_select = gr.Textbox(
                    value="",
                    show_label=False,
                    container=False,
                    elem_id="highlight-candidate-selection",
                )
                with gr.Row():
                    highlight_preview_btn = gr.Button("選択候補をプレビュー")
                    highlight_edit_btn = gr.Button(
                        "選択候補を編集画面で開く",
                        variant="secondary",
                    )
                highlight_preview = gr.Video(
                    label="見どころ候補プレビュー",
                    autoplay=False,
                    interactive=False,
                    height=360,
                )
                highlight_preview_detail = gr.Markdown(
                    "候補を選び、プレビューボタンを押してください。"
                )
                with gr.Accordion("3. 作成した候補を切り抜いて保存", open=True):
                    highlight_export_scope = gr.Radio(
                        choices=[
                            ("選択候補のみ", "selected"),
                            ("表示中の全候補", "all"),
                        ],
                        value="selected",
                        label="保存対象",
                    )
                    with gr.Row():
                        highlight_export_dir = gr.Textbox(
                            value=str(config.ARTIFACT_ROOT / "highlights"),
                            label="保存先フォルダ",
                            scale=3,
                        )
                        highlight_export_precise = gr.Checkbox(
                            value=True,
                            label="フレーム精度で保存",
                            scale=1,
                        )
                    highlight_export_btn = gr.Button(
                        "候補を切り抜いて保存",
                        variant="primary",
                    )
                    highlight_export_log = gr.Textbox(
                        label="保存ログ",
                        interactive=False,
                        lines=4,
                    )
                    highlight_export_files = gr.File(
                        label="保存したファイル",
                        file_count="multiple",
                        interactive=False,
                    )
            llm_result_reload.click(
                lambda: (
                    gr.update(choices=list_llm_result_video_choices()),
                    gr.update(choices=list_llm_result_video_choices()),
                ),
                outputs=[llm_result_video, llm_highlight_video],
            )
            llm_highlight_reload.click(
                lambda: (
                    gr.update(choices=list_llm_result_video_choices()),
                    gr.update(choices=list_llm_result_video_choices()),
                ),
                outputs=[llm_result_video, llm_highlight_video],
            )
            llm_result_video.input(
                sync_llm_video_selection,
                inputs=[llm_result_video],
                outputs=[
                    llm_highlight_video,
                    llm_summary_source_preview,
                    llm_summary_source_detail,
                    llm_highlight_source_preview,
                    llm_highlight_source_detail,
                ],
                show_progress="minimal",
            ).then(
                load_summary_highlight_workspace,
                inputs=[llm_result_video],
                outputs=[
                    llm_result_markdown,
                    highlight_source_status,
                    highlight_result_markdown,
                    highlight_candidate_select,
                    highlight_result_generate,
                ],
                show_progress="minimal",
            )
            llm_highlight_video.input(
                sync_llm_video_selection,
                inputs=[llm_highlight_video],
                outputs=[
                    llm_result_video,
                    llm_summary_source_preview,
                    llm_summary_source_detail,
                    llm_highlight_source_preview,
                    llm_highlight_source_detail,
                ],
                show_progress="minimal",
            ).then(
                load_summary_highlight_workspace,
                inputs=[llm_highlight_video],
                outputs=[
                    llm_result_markdown,
                    highlight_source_status,
                    highlight_result_markdown,
                    highlight_candidate_select,
                    highlight_result_generate,
                ],
                show_progress="minimal",
            )
            llm_result_show.click(
                load_summary_highlight_workspace,
                inputs=[llm_result_video],
                outputs=[
                    llm_result_markdown,
                    highlight_source_status,
                    highlight_result_markdown,
                    highlight_candidate_select,
                    highlight_result_generate,
                ],
            )
            llm_analysis_event = llm_result_analyze.click(
                do_existing_llm_analysis,
                inputs=[llm_result_video, llm_workspace_model_box],
                outputs=[llm_result_log, llm_result_markdown],
                concurrency_id="library-index-io",
                concurrency_limit=1,
            )
            llm_analysis_event.success(
                load_summary_highlight_workspace,
                inputs=[llm_result_video],
                outputs=[
                    llm_result_markdown,
                    highlight_source_status,
                    highlight_result_markdown,
                    highlight_candidate_select,
                    highlight_result_generate,
                ],
            )
            highlight_result_show.click(
                load_latest_highlight_view,
                inputs=[llm_highlight_video],
                outputs=[highlight_result_markdown, highlight_candidate_select],
            )
            highlight_generation_mode.change(
                highlight_generation_mode_update,
                inputs=[highlight_generation_mode],
                outputs=[highlight_query],
            )
            highlight_result_generate.click(
                do_highlight_generation,
                inputs=[
                    highlight_generation_mode,
                    llm_highlight_video,
                    llm_highlight_model_box,
                    highlight_query,
                    highlight_count,
                    highlight_min_duration,
                    highlight_max_duration,
                ],
                outputs=[
                    highlight_result_log,
                    highlight_result_markdown,
                    highlight_candidate_select,
                ],
                concurrency_id="library-index-io",
                concurrency_limit=1,
            )
            highlight_preview_btn.click(
                preview_highlight_candidate,
                inputs=[llm_highlight_video, highlight_candidate_select],
                outputs=[highlight_preview, highlight_preview_detail],
                concurrency_id="highlight-preview-io",
                concurrency_limit=1,
            )
            highlight_edit_btn.click(
                load_highlight_candidate_into_editor,
                inputs=[llm_highlight_video, highlight_candidate_select],
                outputs=[
                    intuitive_video_select,
                    *intuitive_load_and_search_outputs,
                ],
                concurrency_id="intuitive-editor-state",
                concurrency_limit=1,
            )
            highlight_export_btn.click(
                export_highlight_candidates,
                inputs=[
                    llm_highlight_video,
                    highlight_candidate_select,
                    highlight_export_scope,
                    highlight_export_dir,
                    highlight_export_precise,
                ],
                outputs=[highlight_export_log, highlight_export_files],
                concurrency_id="highlight-export-io",
                concurrency_limit=1,
            )

    with gr.Tab("インデックスの共有"):
        gr.Markdown(
            "文字起こし済みのインデックスをzipファイルとして書き出し/読み込みし、"
            "他のPCと再文字起こしなしで共有できます。\n\n"
            "**注意:** 共有zipには、全文文字起こし、単語時刻、検索チャンク、"
            "埋め込みベクトルが含まれます。動画本体、元ファイル名、送信元PCの"
            "パス、旧内部IDは含めません。インポート後は元動画の再関連付けが必要です。"
        )
        with gr.Row():
            gr.Markdown("### エクスポート")
        with gr.Row():
            export_video_select = gr.Dropdown(
                choices=list_video_choices_only(), label="エクスポートする動画", scale=3
            )
            export_reload_btn = gr.Button("動画リスト更新", scale=1)
        export_privacy_confirm = gr.Checkbox(
            label="共有内容を確認し、文字起こしに個人情報・機密情報がないことを確認しました",
            value=False,
        )
        export_btn = gr.Button("エクスポート", variant="primary")
        export_file = gr.File(label="ダウンロード", interactive=False)
        export_path_box = gr.Textbox(label="保存先パス", interactive=False)

        with gr.Row():
            gr.Markdown("### インポート")
        import_file = gr.File(label="インポートするzipファイル", file_types=[".zip"])
        import_btn = gr.Button("インポート", variant="primary")
        import_log = gr.Textbox(label="進捗ログ", interactive=False, lines=8)

        with gr.Row():
            gr.Markdown("### 元動画の再関連付け")
        relink_video_select = gr.Dropdown(
            choices=list_video_choices_only(), label="共有インデックスの動画"
        )
        with gr.Row():
            relink_source_path = gr.Textbox(label="対応する元動画のパス", scale=3)
            relink_browse_btn = gr.Button("ファイル参照", scale=1)
            relink_btn = gr.Button("検証して再関連付け", variant="primary", scale=1)
        relink_status = gr.Textbox(label="再関連付け結果", interactive=False)

        export_reload_btn.click(
            lambda: gr.update(choices=list_video_choices_only()), outputs=[export_video_select]
        )
        export_btn.click(
            do_export,
            inputs=[export_video_select, export_privacy_confirm],
            outputs=[export_file, export_path_box],
            concurrency_id="library-index-io",
            concurrency_limit=1,
        )
        import_btn.click(
            do_import,
            inputs=[import_file],
            outputs=[import_log, video_select],
            concurrency_id="library-index-io",
            concurrency_limit=1,
        )
        relink_browse_btn.click(
            browse_video, inputs=[relink_source_path], outputs=[relink_source_path]
        )
        relink_btn.click(
            do_relink, inputs=[relink_video_select, relink_source_path],
            outputs=[relink_status], concurrency_id="library-index-io", concurrency_limit=1,
        )

# Gradio 6.19 never clears its loading overlay when any Tab is render=False.
# Keep the opt-in legacy tab rendered but invisible, then explicitly select the
# main editor on the implicit top-level Tabs container.
_top_level_tabs = next(
    child for child in demo.children if isinstance(child, gr.Tabs)
)
_top_level_tabs.selected = "intuitive-main"


def _already_running(port: int = APP_PORT) -> bool:
    return _app_is_healthy(port, timeout_sec=2.0, attempts=3)


def _app_is_healthy(
    port: int = APP_PORT,
    *,
    timeout_sec: float = 1.5,
    attempts: int = 2,
) -> bool:
    """Return True only when this Cut_Video Gradio app answers over HTTP."""
    import json
    import time
    import urllib.error
    import urllib.request

    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{int(port)}/config",
                timeout=max(0.1, float(timeout_sec)),
            ) as response:
                status = response.status
                payload = json.load(response)
            if (
                status == 200
                and payload.get("mode") == "blocks"
                and payload.get("title") == "動画シーン検索"
            ):
                return True
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
            urllib.error.URLError,
        ):
            pass
        if attempt + 1 < attempts:
            time.sleep(0.25)
    return False


def _port_is_open(port: int = APP_PORT) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _write_app_pidfile(port: int = APP_PORT) -> None:
    """Atomically record the exact process allowed to be cleaned next startup."""
    import json
    import os

    APP_PIDFILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "port": int(port),
        "app_path": str(Path(__file__).resolve()),
    }
    temporary = APP_PIDFILE.with_suffix(APP_PIDFILE.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, APP_PIDFILE)


def _remove_app_pidfile() -> None:
    """Remove the PID record only when it still belongs to this process."""
    import json
    import os

    try:
        payload = json.loads(APP_PIDFILE.read_text(encoding="utf-8"))
        if int(payload.get("pid", -1)) != os.getpid():
            return
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return
    APP_PIDFILE.unlink(missing_ok=True)


def _stale_app_process(pid: int, port: int) -> bool:
    """Verify that pid both owns the port and runs this project's app.py."""
    import os
    import subprocess

    if os.name != "nt" or pid <= 0 or pid == os.getpid():
        return False
    script = (
        f"$targetPid = {int(pid)}; "
        f"$owners = @(Get-NetTCPConnection -LocalPort {int(port)} -State Listen "
        "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess); "
        f"$proc = Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' "
        "-ErrorAction SilentlyContinue; "
        "if ($proc -and ($owners -contains $targetPid)) { $proc.CommandLine }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    tokens = (result.stdout or "").replace('"', " ").replace("'", " ").split()
    return any(Path(token).name.casefold() == "app.py" for token in tokens)


def _cleanup_stale_app_instance(port: int = APP_PORT) -> bool:
    """Stop only a recorded Cut_Video process that owns an unresponsive port."""
    import json
    import os
    import subprocess
    import time

    if not _port_is_open(port) or _app_is_healthy(
        port, timeout_sec=1.0, attempts=1
    ):
        return False

    try:
        payload = json.loads(APP_PIDFILE.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        recorded_port = int(payload["port"])
        recorded_path = Path(payload["app_path"]).resolve()
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        APP_PIDFILE.unlink(missing_ok=True)
        return False

    if (
        pid == os.getpid()
        or recorded_port != int(port)
        or recorded_path != Path(__file__).resolve()
        or not _stale_app_process(pid, port)
    ):
        APP_PIDFILE.unlink(missing_ok=True)
        return False

    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    for _ in range(30):
        if not _port_is_open(port):
            APP_PIDFILE.unlink(missing_ok=True)
            print(f"応答不能だった前回のWebUI (PID {pid}) を停止しました。")
            return True
        time.sleep(0.1)
    return False


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

    # コマンドラインを確認し、このアプリが起動する長時間ジョブだけを停止する。
    # (PID再利用で app.py 自身や無関係なpythonプロセスを誤って停止しないため)
    try:
        check = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.SubprocessError, OSError):
        return
    command_line = (check.stdout or "").lower()
    allowed_jobs = ("index_video.py", "analyze_transcript.py")
    if any(job_name in command_line for job_name in allowed_jobs):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        print(f"前回の残留バックグラウンド処理 (PID {pid}) を停止しました。")


if __name__ == "__main__":
    if _already_running():
        import webbrowser

        print(f"アプリは既に起動しています: http://127.0.0.1:{APP_PORT}")
        print("(二重起動を防ぐため、このプロセスは終了します)")
        webbrowser.open(f"http://127.0.0.1:{APP_PORT}")
        raise SystemExit(0)

    if _port_is_open(APP_PORT) and not _cleanup_stale_app_instance(APP_PORT):
        print(f"[ERROR] 127.0.0.1:{APP_PORT} は使用中ですが、"
              "Cut_Videoから正常な応答がありません。")
        print("タスクマネージャーで以前の app.py を終了するか、"
              "このポートを使っている別アプリを終了してから再実行してください。")
        raise SystemExit(1)

    _enable_crash_log()
    _cleanup_stale_index_job()
    _initialize_preview_cache()
    _write_app_pidfile(APP_PORT)
    try:
        demo.launch(
            server_name="127.0.0.1",
            server_port=APP_PORT,
            inbrowser=True,
            css=_APP_CSS,
        )
    finally:
        _remove_app_pidfile()
