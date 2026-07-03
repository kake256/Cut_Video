#!/usr/bin/env python
"""動画シーン検索GUI (Gradio)。

    python app.py

「検索・切り抜き」タブ: 検索 → 結果選択 → プレビュー → 手動調整 → 保存。
「動画の追加」タブ: 新規動画の文字起こし〜インデックス化をWebUIから実行。
"""
import faulthandler
import threading
from pathlib import Path

import gradio as gr

# ネイティブ層のクラッシュ(access violation等)の原因追跡用
_crash_log = open("data/crash_trace.log", "a", encoding="utf-8", errors="replace")
faulthandler.enable(file=_crash_log)

from cut_clip import cut_clip
from moment_retrieval import config, db, utils
from moment_retrieval.downloader import DownloadError, download_video
from moment_retrieval.embedder import TextEmbedder
from moment_retrieval.refine import expand_to_speech_boundary
from moment_retrieval.share import ShareError, export_index, import_index
from moment_retrieval.vector_index import VectorIndex

PREVIEW_DIR = Path("data/previews")
DEFAULT_CLIPS_DIR = "clips"

ADJUST_STEPS = [0.1, 1.0, 10.0, 30.0, 60.0, 600.0]

_embedder = None
_index_lock = threading.Lock()


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
    choices = ["(すべての動画)"]
    for v in videos:
        name = Path(v["path"]).name
        choices.append(f"{v['video_id']}  ({name}, {utils.format_timestamp(v['duration'])})")
    return choices


def list_video_choices_only() -> list:
    """「(すべての動画)」を除いた個別動画の選択肢一覧(共有タブ用)。"""
    return [c for c in list_video_choices() if not c.startswith("(")]


def parse_video_choice(choice: str) -> str:
    if not choice or choice.startswith("("):
        return None
    return choice.split()[0]


def region_transcript(conn, video_id: str, start: float, end: float) -> str:
    rows = conn.execute(
        "SELECT text FROM asr_segments WHERE video_id = ? AND end_sec > ? AND start_sec < ? "
        "ORDER BY start_sec",
        (video_id, start, end),
    ).fetchall()
    return " ".join(r["text"] for r in rows)


def do_search(query: str, video_choice: str, top_k: int, min_score: float):
    if not query.strip():
        return [], gr.update(choices=[], value=None), []
    if not config.TEXT_INDEX_PATH.exists():
        raise gr.Error("インデックスがありません。「動画の追加」タブで動画を登録してください。")

    video_filter = parse_video_choice(video_choice)

    conn = db.get_conn()
    db.init_db(conn)

    query_vec = get_embedder().encode([query])
    vindex = VectorIndex.load(config.TEXT_INDEX_PATH, query_vec.shape[1])
    fetch_k = int(top_k) * 5 if video_filter else int(top_k)
    scores, ids = vindex.search(query_vec, top_k=fetch_k)

    chunks = db.get_chunks_by_ids(conn, [int(i) for i in ids if i != -1])

    results = []
    for cid, score in zip(ids, scores):
        cid = int(cid)
        if cid == -1 or cid not in chunks or float(score) < min_score:
            continue
        c = chunks[cid]
        if video_filter and c["video_id"] != video_filter:
            continue
        results.append(
            {
                "video_id": c["video_id"],
                "start": c["start_sec"],
                "end": c["end_sec"],
                "score": float(score),
                "text": c["text"],
            }
        )
        if len(results) >= int(top_k):
            break

    conn.close()
    if not results:
        gr.Info("該当するシーンが見つかりませんでした。")
        return [], gr.update(choices=[], value=None), []

    table = [
        [
            i + 1,
            r["video_id"],
            utils.format_timestamp(r["start"]),
            utils.format_timestamp(r["end"]),
            f"{r['score']:.3f}",
            r["text"][:60] + ("..." if len(r["text"]) > 60 else ""),
        ]
        for i, r in enumerate(results)
    ]
    choices = [
        f"{i + 1}. {utils.format_timestamp(r['start'])} {r['text'][:30]}"
        for i, r in enumerate(results)
    ]
    return table, gr.update(choices=choices, value=choices[0]), results


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


def on_select(selection: str, results: list):
    if not selection or not results:
        return (None, gr.update(), gr.update(), gr.update(), gr.update(),
                None, "", "", [], gr.update(), gr.update())
    idx = int(selection.split(".")[0]) - 1
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
        f"動画: {video['path']}\n"
        f"ヒット区間: {utils.format_timestamp(r['start'])} - {utils.format_timestamp(r['end'])}\n"
        f"拡張後: {utils.format_timestamp(start)} - {utils.format_timestamp(end)} "
        f"(長さ {end - start:.1f} 秒)"
    )
    ctx = {"video_path": video["path"], "duration": duration, "video_id": r["video_id"]}
    choices = _sentence_choices(sents)
    return (
        preview,
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
    )


def refresh_preview(start: float, end: float, ctx: dict):
    if not ctx:
        raise gr.Error("先に検索結果を選択してください。")
    if end <= start:
        raise gr.Error("終了は開始より後にしてください。")
    preview = make_preview(ctx["video_path"], start, end, ctx["duration"])
    conn = db.get_conn()
    transcript = region_transcript(conn, ctx["video_id"], start, end)
    conn.close()
    info = (
        f"動画: {ctx['video_path']}\n"
        f"区間: {utils.format_timestamp(start)} - {utils.format_timestamp(end)} "
        f"(長さ {end - start:.1f} 秒)"
    )
    return preview, info, transcript


def adjust_time(value: float, ctx: dict, delta: float):
    hi = ctx["duration"] if ctx else None
    v = (value or 0) + delta
    v = max(0.0, v)
    if hi is not None:
        v = min(v, hi)
    return round(v, 1)


def always_refresh(start: float, end: float, ctx: dict):
    """選択済みならプレビュー・区間情報・文字起こしを更新する。"""
    if not ctx or end <= start:
        return gr.update(), gr.update(), gr.update()
    return refresh_preview(start, end, ctx)


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


def on_save(start: float, end: float, ctx: dict, precise: bool, out_dir: str, filename: str):
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

    cut_clip(
        Path(ctx["video_path"]), start, end, out,
        pad=0.0, precise=precise, duration=ctx["duration"],
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
            env=env,
            cwd=str(Path(__file__).parent),
        )
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
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
        if code == 0:
            gr.Info("インデックス作成が完了しました。")
            yield "\n".join(log_lines), gr.update(choices=list_video_choices())
        else:
            log_lines.append(f"エラー: インデックス処理が異常終了しました (exit code {code})")
            yield "\n".join(log_lines), gr.update()
    finally:
        _index_lock.release()


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


def build_adjust_row(label: str, target_number, ctx_state, preview_io, slider):
    with gr.Row():
        gr.Markdown(f"**{label}**")
        for step in [-s for s in reversed(ADJUST_STEPS)] + ADJUST_STEPS:
            btn = gr.Button(f"{step:+g}", size="sm", min_width=40)
            btn.click(
                adjust_time,
                inputs=[target_number, ctx_state, gr.State(step)],
                outputs=[target_number],
            ).then(
                lambda v: gr.update(value=v), inputs=[target_number], outputs=[slider]
            ).then(
                always_refresh,
                inputs=preview_io["inputs"],
                outputs=preview_io["outputs"],
            )


with gr.Blocks(title="動画シーン検索") as demo:
    gr.Markdown("# 動画シーン検索・切り抜き")

    with gr.Tab("検索・切り抜き"):
        results_state = gr.State([])
        ctx_state = gr.State(None)

        with gr.Row():
            video_select = gr.Dropdown(
                choices=list_video_choices(),
                value="(すべての動画)",
                label="検索対象の動画",
                scale=3,
            )
            reload_btn = gr.Button("動画リスト更新", scale=1)

        with gr.Row():
            query_box = gr.Textbox(
                label="検索クエリ", placeholder="例: 奨学金について話しているところ", scale=4
            )
            search_btn = gr.Button("検索", variant="primary", scale=1)
        with gr.Row():
            top_k = gr.Slider(1, 20, value=5, step=1, label="候補数")
            min_score = gr.Slider(0.0, 1.0, value=config.MIN_SCORE, step=0.01, label="類似度閾値")

        result_table = gr.Dataframe(
            headers=["#", "動画", "開始", "終了", "スコア", "テキスト"],
            interactive=False,
            label="検索結果",
        )
        result_select = gr.Radio(choices=[], label="切り抜く候補を選択")

        preview_video = gr.Video(label="プレビュー", autoplay=True)

        info_box = gr.Textbox(label="選択中の区間", interactive=False, lines=3)
        transcript_box = gr.Textbox(label="区間の文字起こし", interactive=False, lines=4)

        sentences_state = gr.State([])
        gr.Markdown("**文字起こしの文で区間を調整** (文を選ぶと開始/終了がその文に合います)")
        with gr.Row():
            start_sent_dd = gr.Dropdown(choices=[], label="この文から (開始)")
            end_sent_dd = gr.Dropdown(choices=[], label="この文まで (終了)")

        start_slider = gr.Slider(0, 1, value=0, step=0.1, label="開始 (シークバー)")
        end_slider = gr.Slider(0, 1, value=1, step=0.1, label="終了 (シークバー)")

        with gr.Row():
            start_num = gr.Number(value=0, label="開始 (秒)", precision=1)
            end_num = gr.Number(value=0, label="終了 (秒)", precision=1)

        preview_io = {
            "inputs": [start_num, end_num, ctx_state],
            "outputs": [preview_video, info_box, transcript_box],
        }
        build_adjust_row("開始を調整", start_num, ctx_state, preview_io, start_slider)
        build_adjust_row("終了を調整", end_num, ctx_state, preview_io, end_slider)

        update_btn = gr.Button("プレビュー更新")

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
        search_btn.click(
            do_search,
            inputs=[query_box, video_select, top_k, min_score],
            outputs=[result_table, result_select, results_state],
        )
        query_box.submit(
            do_search,
            inputs=[query_box, video_select, top_k, min_score],
            outputs=[result_table, result_select, results_state],
        )
        reload_btn.click(lambda: gr.update(choices=list_video_choices()), outputs=[video_select])
        result_select.change(
            on_select,
            inputs=[result_select, results_state],
            outputs=[
                preview_video, start_num, end_num, start_slider, end_slider,
                ctx_state, info_box, transcript_box,
                sentences_state, start_sent_dd, end_sent_dd,
            ],
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
        # シークバー(ユーザーが離した時のみ発火)→数値欄へ反映→自動プレビュー
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
        # 数値欄の直接編集もシークバーに反映
        start_num.input(lambda v: gr.update(value=v), inputs=[start_num], outputs=[start_slider])
        end_num.input(lambda v: gr.update(value=v), inputs=[end_num], outputs=[end_slider])
        # 数値を直接入力してEnterで確定した場合もプレビューへ反映
        start_num.submit(
            always_refresh, inputs=preview_io["inputs"], outputs=preview_io["outputs"]
        )
        end_num.submit(
            always_refresh, inputs=preview_io["inputs"], outputs=preview_io["outputs"]
        )

        update_btn.click(
            refresh_preview,
            inputs=[start_num, end_num, ctx_state],
            outputs=[preview_video, info_box, transcript_box],
        )
        folder_btn.click(browse_folder, inputs=[out_dir_box], outputs=[out_dir_box])
        save_btn.click(
            on_save,
            inputs=[start_num, end_num, ctx_state, precise_chk, out_dir_box, filename_box],
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
        index_log = gr.Textbox(label="進捗ログ", interactive=False, lines=10)

        video_browse_btn.click(browse_video, inputs=[new_video_box], outputs=[new_video_box])
        index_btn.click(
            do_index,
            inputs=[new_video_box, asr_model_dd, force_chk, batch_infer_chk],
            outputs=[index_log, video_select],
        )

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


if __name__ == "__main__":
    demo.launch(server_name="127.0.0.1", server_port=7860, inbrowser=True)
