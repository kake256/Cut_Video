#!/usr/bin/env python
"""CUT 自動投稿: paste a link -> AI picks clips -> Shorts -> private YouTube upload.

A separate app from the editor (``app.py``).  It shares the same library
(``data/``) and output folder (``clips/``) and runs on its own port.
Publishing stays a one-click action limited to allow-listed channels.
"""
from __future__ import annotations

import html
import os
import socket
import webbrowser
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

import gradio as gr  # noqa: E402

from moment_retrieval import agent_runner, gcloud_setup, youtube_upload  # noqa: E402
from moment_retrieval.auto_pipeline import (  # noqa: E402
    LIBRARY_PREFIX, AutoPipeline, PipelineError, library_videos,
)

APP_PORT = int(os.environ.get("CUT_AUTO_PUBLISH_PORT", "7870"))



_AUTO_PIPELINE: AutoPipeline | None = None


def _auto_pipeline() -> AutoPipeline:
    global _AUTO_PIPELINE
    if _AUTO_PIPELINE is None:
        _AUTO_PIPELINE = AutoPipeline()
    return _AUTO_PIPELINE


def auto_account_status() -> str:
    problems = youtube_upload.setup_problems()
    if problems:
        return (
            "**YouTube:** 準備が必要です。Google CloudでOAuthクライアント（種類: デスクトップアプリ）を作り、"
            "ダウンロードしたJSONを下の「OAuthクライアントJSON」から読み込んでください"
            "（手順: docs/YOUTUBE_UPLOAD.md）。"
        )
    try:
        channel = youtube_upload.connected_channel()
    except Exception as exc:  # expired/revoked tokens must not break the tab
        return f"**YouTube:** 連携を確認できませんでした（{type(exc).__name__}）。連携し直してください。"
    source = "（アプリ同梱のクライアントを使用）" if youtube_upload.client_source() == "bundled" else ""
    if channel is None:
        return f"**YouTube:** 未連携です。「YouTubeアカウントを連携」を押してください。{source}"
    return f"**YouTube:** 連携中 — {html.escape(str(channel.get('title') or channel.get('id')))}{source}"


def bundle_client() -> str:
    try:
        target = youtube_upload.bundle_own_client()
    except youtube_upload.UploadError as exc:
        return f"**配布用:** {exc}"
    project = gcloud_setup.saved_project()
    audience = (
        f"[テストユーザーの管理ページ](https://console.cloud.google.com/auth/audience?project={project})"
        if project else "Google Cloud Consoleの「対象」ページ"
    )
    return (
        f"**配布用:** クライアントを `{html.escape(target.name)}` としてアプリフォルダに同梱しました。"
        "このフォルダごと渡せば、相手は「YouTubeアカウントを連携」だけで使えます。"
        f"審査前は、使う人のGoogleアカウントを{audience}でテストユーザー（最大100人）に追加してください。"
        "このファイルは公開リポジトリ（GitHub）には載せないでください（.gitignore済み）。"
    )


def gcp_status() -> str:
    if not gcloud_setup.find_gcloud():
        return "**Google Cloud:** gcloud（Google Cloud SDK）が見つかりません。手作業の手順は docs/YOUTUBE_UPLOAD.md を参照してください。"
    try:
        account = gcloud_setup.active_account()
    except gcloud_setup.SetupError as exc:
        return f"**Google Cloud:** {exc}"
    project = gcloud_setup.saved_project()
    lines = [f"**Google Cloud:** ログイン中のアカウント: {html.escape(account or '未ログイン')}"]
    lines.append(f"CUT用プロジェクト: {html.escape(project) if project else '未作成'}")
    return " / ".join(lines)


def gcp_login() -> str:
    try:
        url = gcloud_setup.start_login()
    except gcloud_setup.SetupError as exc:
        return f"**Google Cloud:** {exc}"
    return (
        "**Google Cloud:** ブラウザでログイン画面を開きました（開かない場合は"
        f"[こちらのリンク]({url})）。使うGoogleアカウントでログインして許可すると確認コードが表示されるので、"
        "下の「確認コード」に貼り付けて「コードを送信」を押してください。"
    )


def gcp_submit_code(code: str):
    try:
        gcloud_setup.finish_login(code)
    except gcloud_setup.SetupError as exc:
        return f"**Google Cloud:** {exc}", gr.update()
    return gcp_status(), ""


def gcp_create_project(confirmed: bool) -> str:
    if confirmed is not True:
        return "**Google Cloud:** プロジェクトの作成に同意するチェックを入れてください。"
    try:
        result = gcloud_setup.create_project_and_enable_api()
    except gcloud_setup.SetupError as exc:
        return f"**Google Cloud:** {exc}"
    action = "作成し" if result["created"] else "既存のものを使い"
    return (
        f"**Google Cloud:** プロジェクト {html.escape(result['project_id'])} を{action}、"
        "YouTube Data API v3 を有効にしました。次に「設定ページを開く」を押してください。"
    )


def gcp_open_pages() -> str:
    project = gcloud_setup.saved_project()
    if not project:
        return "**Google Cloud:** 先にプロジェクトを作成してください。"
    pages = gcloud_setup.open_console_pages(project)
    steps = "\n".join(f"{index}. [{label}]({url})" for index, (label, url) in enumerate(pages, start=1))
    return (
        "ブラウザで次のページを開きました。上から順に設定してください。\n\n" + steps + "\n\n"
        "1ではアプリ名（例: CUT）とメールを入力、2では対象を「外部」にして自分のGoogleアカウントをテストユーザーに追加、"
        "3では種類「デスクトップアプリ」でクライアントを作成し、JSONをダウンロードして下の欄で読み込みます。"
    )


def auto_install_client_secret(uploaded) -> str:
    path = getattr(uploaded, "name", None) or uploaded
    if not path:
        return auto_account_status()
    try:
        youtube_upload.install_client_secret(Path(str(path)))
    except youtube_upload.UploadError as exc:
        return f"**YouTube:** {exc}"
    return "**YouTube:** OAuthクライアントを読み込みました。「YouTubeアカウントを連携」を押してください。"


def auto_connect_account() -> str:
    """Show problems inline instead of an error popup; the browser opens on this PC."""
    if youtube_upload.setup_problems():
        return auto_account_status()
    try:
        youtube_upload.connect_account()
    except Exception as exc:
        return (
            f"**YouTube:** 連携に失敗しました（{type(exc).__name__}: {html.escape(str(exc))[:200]}）。"
            "OAuth同意画面のテストユーザーに、使うGoogleアカウントを追加したか確認してください。"
        )
    return auto_account_status()


def auto_disconnect_account() -> str:
    youtube_upload.disconnect_account()
    return auto_account_status()


def auto_agent_status() -> str:
    found = agent_runner.available_agents()
    return " / ".join(
        f"{agent_runner.AGENT_LABELS[name]}: {'利用可' if ok else '見つかりません'}"
        for name, ok in found.items()
    )


_AUTO_STATE_LABELS = {
    "queued": "待機中", "running": "実行中", "done": "完了", "failed": "失敗", "cancelled": "停止",
}


def auto_jobs_view():
    jobs = _auto_pipeline().list_jobs()[:10]
    if not jobs:
        return "まだジョブはありません。", gr.update(choices=[], value=None), gr.update(choices=[], value=None)
    parts = []
    publishable: list[tuple[str, str]] = []
    for job in jobs:
        state = _AUTO_STATE_LABELS.get(job.state, job.state)
        step = f" / {job.step}" if job.state == "running" and job.step else ""
        parts.append(f"### {html.escape(job.job_id)} — {state}{html.escape(step)}")
        parts.append(f"- 元動画: {html.escape(job.source)}")
        if job.source_channel:
            parts.append(f"- チャンネル: {html.escape(job.source_channel)}")
        if job.source_video_key:
            parts.append(f"- 動画ID: {html.escape(job.source_video_key)}")
        for upload in job.uploads:
            status = "公開中" if upload.get("privacy_status") == "public" else "非公開"
            link = upload.get("watch_url") or upload.get("studio_url")
            parts.append(f"- [{status}] {html.escape(str(upload.get('title', '')))} — {link}")
            if status == "非公開":
                publishable.append(
                    (f"{upload.get('title')}（{job.job_id}）", f"{job.job_id}|{upload.get('video_id')}")
                )
        recent = "\n".join(job.log[-8:])
        # Blank lines around the HTML block keep the next job's heading rendered as Markdown.
        parts.append(f"\n<details><summary>ログ</summary>\n\n```\n{recent}\n```\n\n</details>\n")
    running = [(job.job_id, job.job_id) for job in jobs if job.state in {"queued", "running"}]
    return (
        "\n".join(parts),
        gr.update(choices=publishable, value=publishable[0][1] if publishable else None),
        gr.update(choices=running, value=running[0][1] if running else None),
    )


def model_choices(agent: str) -> list[tuple[str, str]]:
    if agent == "claude":
        return list(agent_runner.CLAUDE_MODELS)
    return agent_runner.codex_models()


def default_model(agent: str) -> str:
    return agent_runner.DEFAULT_CODEX_MODEL if agent == "codex" else ""


def on_agent_change(agent: str):
    return (
        gr.update(choices=model_choices(agent), value=default_model(agent)),
        gr.update(visible=agent == "codex"),
    )


def summary_status() -> str:
    """One line for the main tab: is everything needed for a run ready?"""
    youtube = auto_account_status().replace("**YouTube:** ", "")
    if youtube.startswith("連携中"):
        youtube_part = f"YouTube: {youtube}"
    else:
        youtube_part = "YouTube: 未連携（「設定」タブで連携すると非公開アップロードまで自動。未連携なら書き出しまで）"
    return f"{youtube_part}　|　AI: {auto_agent_status()}"


def _duration_label(seconds: float) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def library_choices() -> list[tuple[str, str]]:
    choices = []
    for video in library_videos():
        note = "" if video["source_available"] else "（共有・元動画は自動でダウンロード）"
        choices.append((f"{video['name']}　{_duration_label(video['duration_sec'])}{note}", video["video_id"]))
    return choices


def refresh_library():
    choices = library_choices()
    return gr.update(choices=choices), gr.update(choices=choices)


def auto_submit(sources: str, library_selection, agent: str, model: str, clip_count, effort: str,
                min_sec, max_sec, layout: str, upload: bool):
    lines = [line.strip() for line in str(sources or "").splitlines() if line.strip()]
    lines += [LIBRARY_PREFIX + video_id for video_id in (library_selection or [])]
    if not lines:
        raise gr.Error("動画のURLを入力するか、文字起こし済みの動画を選んでください。")
    submitted = []
    for line in lines[:10]:
        try:
            job = _auto_pipeline().submit(
                line, agent, clip_count=int(clip_count), min_duration_sec=float(min_sec),
                max_duration_sec=float(max_sec), layout=layout, upload=bool(upload),
                model=model or "", effort=(effort or "") if agent == "codex" else "",
            )
        except PipelineError as exc:
            raise gr.Error(f"{line}: {exc}") from exc
        submitted.append(job.job_id)
    gr.Info(f"{len(submitted)}件のジョブを開始しました。")
    return ("", gr.update(value=[]), *auto_jobs_view())


def export_shared_index(video_id: str, include_url: bool):
    from moment_retrieval.share import ShareError, export_index

    if not video_id:
        raise gr.Error("書き出す動画を選んでください。")
    try:
        path = export_index(video_id, confirm_sensitive=True, include_source_url=bool(include_url))
    except ShareError as exc:
        raise gr.Error(str(exc)) from exc
    return str(path.resolve()), f"書き出しました: {path.resolve()}"


def import_shared_index(uploaded, start_clipping: bool, agent: str, model: str, clip_count, effort: str,
                        min_sec, max_sec, layout: str, upload: bool):
    """Import a share zip; optionally start a job that downloads, relinks and clips it."""
    from moment_retrieval import db, source_origin
    from moment_retrieval.share import ShareError, import_index

    path = getattr(uploaded, "name", None) or uploaded
    if not path:
        raise gr.Error("共有zipを選んでください。")
    before = {item["video_id"] for item in library_videos()}
    try:
        messages = list(import_index(Path(str(path))))
    except ShareError as exc:
        raise gr.Error(str(exc)) from exc
    log = "\n".join(messages)
    new_ids = [item["video_id"] for item in library_videos() if item["video_id"] not in before]
    if start_clipping:
        if not new_ids:
            log += "\n切り抜きは開始していません（新しく使える動画がありません。元動画URLのない共有zipは、元動画をこのPCに用意してください）。"
        for video_id in new_ids:
            conn = db.get_conn()
            try:
                origin = source_origin.origin_url_for_video(conn, db.get_video(conn, video_id) or {})
            finally:
                conn.close()
            if not origin:
                continue
            try:
                job = _auto_pipeline().submit(
                    origin, agent, clip_count=int(clip_count), min_duration_sec=float(min_sec),
                    max_duration_sec=float(max_sec), layout=layout, upload=bool(upload),
                    model=model or "", effort=(effort or "") if agent == "codex" else "",
                )
            except PipelineError as exc:
                log += f"\n切り抜きを開始できませんでした: {exc}"
                continue
            log += f"\n切り抜きジョブを開始しました（{job.job_id}）。元動画をダウンロードして共有された文字起こしに関連付けます。"
    choices = library_choices()
    return log, gr.update(choices=choices), gr.update(choices=choices)


def auto_publish(selection: str):
    if not selection or "|" not in selection:
        raise gr.Error("公開する動画を選択してください。")
    job_id, video_id = selection.split("|", 1)
    try:
        result = _auto_pipeline().publish(job_id, video_id)
    except (PipelineError, youtube_upload.UploadError) as exc:
        raise gr.Error(str(exc)) from exc
    except Exception as exc:
        raise gr.Error(f"公開に失敗しました: {exc}") from exc
    gr.Info(f"公開しました: {result['watch_url']}")
    return auto_jobs_view()


def auto_cancel(job_id: str):
    if not job_id or not _auto_pipeline().cancel(job_id):
        raise gr.Error("停止できるジョブがありません。")
    gr.Info("停止を要求しました。")
    return auto_jobs_view()


with gr.Blocks(title="CUT 自動投稿") as demo:
    gr.Markdown("# CUT 自動投稿")
    status_md = gr.Markdown("")
    with gr.Tabs():
        with gr.Tab("切り抜き"):
            auto_sources = gr.Textbox(
                label="動画のURL（YouTube / Twitch）またはファイルのパス　※1行に1つ",
                lines=2, placeholder="https://www.youtube.com/watch?v=...",
            )
            with gr.Row():
                auto_library = gr.Dropdown(
                    choices=[], multiselect=True, scale=5,
                    label="または文字起こし済みの動画から選ぶ（編集用CUTで処理した動画も含む）",
                )
                auto_library_refresh = gr.Button("一覧を更新", scale=1)
            with gr.Row():
                auto_agent = gr.Radio(
                    choices=[("Codex", "codex"), ("Claude Code", "claude")], value="codex",
                    label="見どころを選ぶAI", scale=2,
                )
                auto_model = gr.Dropdown(
                    choices=model_choices("codex"), value=agent_runner.DEFAULT_CODEX_MODEL,
                    label="モデル", scale=2,
                )
                auto_clip_count = gr.Slider(1, 10, value=3, step=1, label="本数", scale=2)
            with gr.Accordion("詳細設定", open=False):
                with gr.Row():
                    auto_effort = gr.Dropdown(
                        choices=list(agent_runner.EFFORTS), value=agent_runner.DEFAULT_EFFORT,
                        label="推論の強さ（Codex）",
                    )
                    auto_min_sec = gr.Number(value=20, label="最短（秒）")
                    auto_max_sec = gr.Number(value=60, label="最長（秒）")
                with gr.Row():
                    auto_layout = gr.Radio(
                        choices=[("ぼかし背景", "blur"), ("切り取り", "crop")], value="blur", label="縦型レイアウト",
                    )
                    auto_upload = gr.Checkbox(value=True, label="YouTubeへ非公開アップロードする")
            auto_start_btn = gr.Button("切り抜いて非公開アップロード", variant="primary", size="lg")
            gr.Markdown(
                "<small>権利者から切り抜きの許可を得た動画だけに使ってください。文字起こしは選んだAIへ送られます。"
                "アップロードは常に**非公開**で、公開は下の「公開する」かYouTube Studioで行います。</small>"
            )
            gr.Markdown("### ジョブ")
            with gr.Row():
                auto_publish_select = gr.Dropdown(choices=[], label="公開する動画", scale=4)
                auto_publish_btn = gr.Button("公開する", scale=1)
            auto_jobs_md = gr.Markdown("まだジョブはありません。")
            with gr.Accordion("実行中のジョブを停止", open=False):
                with gr.Row():
                    auto_cancel_select = gr.Dropdown(choices=[], label="実行中のジョブ", scale=4)
                    auto_cancel_btn = gr.Button("停止", variant="stop", scale=1)
            auto_timer = gr.Timer(5)

        with gr.Tab("インデックスの共有"):
            gr.Markdown(
                "文字起こし（インデックス）をzipで受け渡しします。受け取った人はWhisperで文字起こしをし直さずに"
                "切り抜けます。zipには全文の文字起こしが含まれるので、渡す相手に注意してください。"
            )
            gr.Markdown("### 書き出す")
            with gr.Row():
                share_export_video = gr.Dropdown(choices=[], label="書き出す動画", scale=4)
                share_include_url = gr.Checkbox(value=True, label="元動画のURLを同梱（YouTube / Twitch）", scale=2)
                share_export_btn = gr.Button("共有zipを書き出す", scale=1)
            share_export_file = gr.File(label="書き出したzip", interactive=False, height=80)
            share_export_md = gr.Markdown("")
            gr.Markdown("### 読み込む")
            share_import_file = gr.File(label="共有zip（.vindex.zip）", file_types=[".zip"], type="filepath", height=120)
            share_start = gr.Checkbox(
                value=True,
                label="読み込んだら、元動画をダウンロードして「切り抜き」タブの設定で切り抜き・アップロードまで進める",
            )
            share_import_btn = gr.Button("読み込む", variant="primary")
            share_import_log = gr.Textbox(label="読み込みログ", interactive=False, lines=6)

        with gr.Tab("設定"):
            gr.Markdown("### YouTubeアカウント")
            auto_account_md = gr.Markdown("")
            auto_agents_md = gr.Markdown("")
            auto_client_file = gr.File(
                label="OAuthクライアントJSON（Google Cloudからダウンロードしたもの）",
                file_types=[".json"], type="filepath", height=120,
            )
            with gr.Row():
                auto_connect_btn = gr.Button("YouTubeアカウントを連携", variant="primary")
                auto_disconnect_btn = gr.Button("連携を解除")
                auto_account_refresh_btn = gr.Button("状態を更新")
            with gr.Accordion("Google Cloudの準備（初回のみ）", open=False):
                gcp_status_md = gr.Markdown("")
                gr.Markdown(
                    "ログイン、CUT用プロジェクトの作成、YouTube Data API v3 の有効化を自動で行います。"
                    "同意画面とOAuthクライアントはGoogleの仕様上自動化できないため、設定ページを開きます。"
                )
                with gr.Row():
                    gcp_login_btn = gr.Button("1. Googleにログイン")
                    gcp_create_btn = gr.Button("2. プロジェクト作成とAPI有効化")
                    gcp_pages_btn = gr.Button("3. 設定ページを開く")
                gcp_confirm = gr.Checkbox(
                    value=False,
                    label="ログイン中のGoogleアカウントにCUT用のプロジェクトを作成することに同意します（2の前に）",
                )
                with gr.Row():
                    gcp_code = gr.Textbox(label="確認コード（1のログイン後にブラウザに表示されたもの）",
                                          type="password", scale=4)
                    gcp_code_btn = gr.Button("コードを送信", scale=1)
                gcp_result_md = gr.Markdown("")
            with gr.Accordion("配布用: OAuthクライアントをアプリに同梱", open=False):
                gr.Markdown(
                    "自分のOAuthクライアントをアプリフォルダに同梱すると、このフォルダを受け取った人は"
                    "Google Cloudの作業なしで「YouTubeアカウントを連携」だけで使えます。"
                )
                bundle_btn = gr.Button("このクライアントをアプリに同梱する")
                bundle_md = gr.Markdown("")

    auto_job_outputs = [auto_jobs_md, auto_publish_select, auto_cancel_select]
    demo.load(summary_status, outputs=[status_md])
    demo.load(auto_account_status, outputs=[auto_account_md])
    demo.load(auto_agent_status, outputs=[auto_agents_md])
    demo.load(auto_jobs_view, outputs=auto_job_outputs, show_progress="hidden")
    demo.load(gcp_status, outputs=[gcp_status_md])
    auto_timer.tick(auto_jobs_view, outputs=auto_job_outputs, show_progress="hidden")
    auto_agent.change(on_agent_change, inputs=[auto_agent], outputs=[auto_model, auto_effort])
    auto_client_file.upload(auto_install_client_secret, inputs=[auto_client_file], outputs=[auto_account_md])
    gcp_login_btn.click(gcp_login, outputs=[gcp_status_md], concurrency_id="gcp-setup")
    gcp_code_btn.click(gcp_submit_code, inputs=[gcp_code], outputs=[gcp_status_md, gcp_code],
                       concurrency_id="gcp-setup")
    gcp_create_btn.click(gcp_create_project, inputs=[gcp_confirm], outputs=[gcp_result_md],
                         concurrency_id="gcp-setup").then(gcp_status, outputs=[gcp_status_md])
    gcp_pages_btn.click(gcp_open_pages, outputs=[gcp_result_md])
    bundle_btn.click(bundle_client, outputs=[bundle_md])
    auto_connect_btn.click(auto_connect_account, outputs=[auto_account_md],
                           concurrency_id="youtube-account").then(summary_status, outputs=[status_md])
    auto_disconnect_btn.click(auto_disconnect_account, outputs=[auto_account_md]).then(
        summary_status, outputs=[status_md])
    auto_account_refresh_btn.click(auto_account_status, outputs=[auto_account_md]).then(
        summary_status, outputs=[status_md])
    auto_start_btn.click(
        auto_submit,
        inputs=[auto_sources, auto_library, auto_agent, auto_model, auto_clip_count, auto_effort,
                auto_min_sec, auto_max_sec, auto_layout, auto_upload],
        outputs=[auto_sources, auto_library, *auto_job_outputs],
    )
    demo.load(refresh_library, outputs=[auto_library, share_export_video])
    auto_library_refresh.click(refresh_library, outputs=[auto_library, share_export_video])
    share_export_btn.click(export_shared_index, inputs=[share_export_video, share_include_url],
                           outputs=[share_export_file, share_export_md], concurrency_id="library-share")
    share_import_btn.click(
        import_shared_index,
        inputs=[share_import_file, share_start, auto_agent, auto_model, auto_clip_count, auto_effort,
                auto_min_sec, auto_max_sec, auto_layout, auto_upload],
        outputs=[share_import_log, auto_library, share_export_video],
        concurrency_id="library-share",
    ).then(auto_jobs_view, outputs=auto_job_outputs)
    auto_publish_btn.click(auto_publish, inputs=[auto_publish_select], outputs=auto_job_outputs,
                           concurrency_id="youtube-publish", concurrency_limit=1)
    auto_cancel_btn.click(auto_cancel, inputs=[auto_cancel_select], outputs=auto_job_outputs)


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    if _port_in_use(APP_PORT):
        print(f"自動投稿アプリは既に起動しています: http://127.0.0.1:{APP_PORT}")
        webbrowser.open(f"http://127.0.0.1:{APP_PORT}")
        raise SystemExit(0)
    demo.launch(server_name="127.0.0.1", server_port=APP_PORT, inbrowser=True)
