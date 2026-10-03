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

from moment_retrieval import agent_runner, channel_policy, youtube_upload  # noqa: E402
from moment_retrieval.auto_pipeline import AutoPipeline, PipelineError  # noqa: E402

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
    if channel is None:
        return "**YouTube:** 未連携です。「YouTubeアカウントを連携」を押してください。"
    return f"**YouTube:** 連携中 — {html.escape(str(channel.get('title') or channel.get('id')))}"


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


def auto_channel_rows() -> list[list[str]]:
    return [
        [item.get("name", ""), item.get("key", ""), item.get("permission_note", ""),
         str(item.get("added_at", ""))[:10]]
        for item in channel_policy.list_channels()
    ]


def auto_channel_choices() -> list[tuple[str, str]]:
    return [(f"{item.get('name')}（{item.get('key')}）", item.get("key")) for item in channel_policy.list_channels()]


def auto_add_channel(url: str, note: str):
    try:
        channel = channel_policy.resolve_channel(url)
        channel_policy.add_channel(channel, note)
    except ValueError as exc:
        raise gr.Error(str(exc)) from exc
    except Exception as exc:
        raise gr.Error(f"チャンネル情報を取得できませんでした: {exc}") from exc
    gr.Info(f"許可済みチャンネルに追加しました: {channel.name}")
    return auto_channel_rows(), gr.update(choices=auto_channel_choices(), value=None), "", ""


def auto_remove_channel(key: str):
    if not key:
        raise gr.Error("削除するチャンネルを選択してください。")
    channel_policy.remove_channel(key)
    return auto_channel_rows(), gr.update(choices=auto_channel_choices(), value=None)


def auto_save_limit(limit) -> str:
    try:
        value = channel_policy.save_settings(int(limit))
    except (TypeError, ValueError) as exc:
        raise gr.Error(str(exc)) from exc
    return f"1日の公開上限を{value['daily_limit']}本にしました。"


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
        allowed, reason = channel_policy.can_publish(job.source_channel_key)
        state = _AUTO_STATE_LABELS.get(job.state, job.state)
        step = f" / {job.step}" if job.state == "running" and job.step else ""
        parts.append(f"### {html.escape(job.job_id)} — {state}{html.escape(step)}")
        parts.append(f"- 元動画: {html.escape(job.source)}")
        if job.source_channel:
            parts.append(f"- チャンネル: {html.escape(job.source_channel)}")
        for upload in job.uploads:
            status = "公開中" if upload.get("privacy_status") == "public" else "非公開"
            link = upload.get("watch_url") or upload.get("studio_url")
            parts.append(f"- [{status}] {html.escape(str(upload.get('title', '')))} — {link}")
            if status == "非公開" and allowed:
                publishable.append(
                    (f"{upload.get('title')}（{job.job_id}）", f"{job.job_id}|{upload.get('video_id')}")
                )
        if job.uploads and not allowed:
            parts.append(f"- 公開: {html.escape(reason)}")
        recent = "\n".join(job.log[-8:])
        # Blank lines around the HTML block keep the next job's heading rendered as Markdown.
        parts.append(f"\n<details><summary>ログ</summary>\n\n```\n{recent}\n```\n\n</details>\n")
    running = [(job.job_id, job.job_id) for job in jobs if job.state in {"queued", "running"}]
    return (
        "\n".join(parts),
        gr.update(choices=publishable, value=publishable[0][1] if publishable else None),
        gr.update(choices=running, value=running[0][1] if running else None),
    )


def auto_submit(sources: str, agent: str, clip_count, min_sec, max_sec, layout: str, upload: bool):
    lines = [line.strip() for line in str(sources or "").splitlines() if line.strip()]
    if not lines:
        raise gr.Error("切り抜きたい動画のURLかファイルのパスを入力してください。")
    submitted = []
    for line in lines[:10]:
        try:
            job = _auto_pipeline().submit(
                line, agent, clip_count=int(clip_count), min_duration_sec=float(min_sec),
                max_duration_sec=float(max_sec), layout=layout, upload=bool(upload),
            )
        except PipelineError as exc:
            raise gr.Error(f"{line}: {exc}") from exc
        submitted.append(job.job_id)
    gr.Info(f"{len(submitted)}件のジョブを開始しました。")
    return ("", *auto_jobs_view())


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
    with gr.Column():
        gr.Markdown(
            "動画のURL（YouTube / Twitch など）か、動画フォルダ内のファイルを貼り付けると、"
            "文字起こし → AIによる場面選び → 縦型ショートの書き出し → YouTubeへ**非公開**アップロード"
            "まで自動で行います。公開は、許可済みチャンネルの動画だけ下の「公開する」で行えます。\n\n"
            "**注意:** 文字起こしは選んだAI（Codex / Claude）へ送られます。"
            "他人の配信は、配信者の許可や切り抜きガイドラインを確認したものだけを許可済みにしてください。"
        )
        with gr.Accordion("① アカウント連携", open=True):
            auto_account_md = gr.Markdown("")
            auto_agents_md = gr.Markdown("")
            auto_client_file = gr.File(
                label="OAuthクライアントJSON（Google Cloudからダウンロードしたもの）",
                file_types=[".json"], type="filepath",
            )
            with gr.Row():
                auto_connect_btn = gr.Button("YouTubeアカウントを連携", variant="primary")
                auto_disconnect_btn = gr.Button("連携を解除")
                auto_account_refresh_btn = gr.Button("状態を更新")
        with gr.Accordion("② 許可済みチャンネル（公開してよい配信者）", open=False):
            auto_channels_df = gr.Dataframe(
                headers=["チャンネル", "ID", "許可の根拠", "登録日"],
                value=[], interactive=False, wrap=True,
            )
            with gr.Row():
                auto_channel_url = gr.Textbox(label="チャンネルか動画のURL", scale=3)
                auto_channel_note = gr.Textbox(
                    label="許可の根拠（例: 配信者のガイドラインURL、許可をもらった日時）", scale=3,
                )
                auto_channel_add_btn = gr.Button("追加", scale=1)
            with gr.Row():
                auto_channel_remove = gr.Dropdown(choices=[], label="削除するチャンネル", scale=3)
                auto_channel_remove_btn = gr.Button("削除", scale=1)
            with gr.Row():
                auto_limit = gr.Number(
                    value=channel_policy.DEFAULT_DAILY_LIMIT, precision=0,
                    label="1日の公開上限（本）", scale=1,
                )
                auto_limit_btn = gr.Button("上限を保存", scale=1)
                auto_limit_md = gr.Markdown("", scale=2)
        with gr.Accordion("③ 切り抜きたい動画", open=True):
            auto_sources = gr.Textbox(
                label="URLかファイルのパス（1行に1つ、最大10件）", lines=3,
                placeholder="https://www.youtube.com/watch?v=...\nhttps://www.twitch.tv/videos/...",
            )
            with gr.Row():
                auto_agent = gr.Radio(
                    choices=[("Codex", "codex"), ("Claude Code", "claude")],
                    value="codex", label="場面を選ぶAI",
                )
                auto_clip_count = gr.Slider(1, 10, value=3, step=1, label="切り抜き本数")
                auto_layout = gr.Radio(
                    choices=[("ぼかし背景", "blur"), ("切り取り", "crop")], value="blur", label="縦型レイアウト",
                )
            with gr.Row():
                auto_min_sec = gr.Number(value=20, label="最短（秒）")
                auto_max_sec = gr.Number(value=60, label="最長（秒）")
                auto_upload = gr.Checkbox(value=True, label="YouTubeへ非公開アップロードする")
            auto_start_btn = gr.Button("自動切り抜きを開始", variant="primary")
        with gr.Accordion("④ ジョブ一覧", open=True):
            with gr.Row():
                auto_publish_select = gr.Dropdown(choices=[], label="公開する動画", scale=3)
                auto_publish_btn = gr.Button("公開する", variant="primary", scale=1)
            with gr.Row():
                auto_cancel_select = gr.Dropdown(choices=[], label="実行中のジョブ", scale=3)
                auto_cancel_btn = gr.Button("停止", variant="stop", scale=1)
            auto_jobs_md = gr.Markdown("まだジョブはありません。")
            auto_timer = gr.Timer(5)

        auto_job_outputs = [auto_jobs_md, auto_publish_select, auto_cancel_select]
        demo.load(auto_account_status, outputs=[auto_account_md])
        demo.load(auto_agent_status, outputs=[auto_agents_md])
        demo.load(auto_channel_rows, outputs=[auto_channels_df])
        demo.load(lambda: gr.update(choices=auto_channel_choices()), outputs=[auto_channel_remove])
        demo.load(lambda: channel_policy.settings()["daily_limit"], outputs=[auto_limit])
        demo.load(auto_jobs_view, outputs=auto_job_outputs, show_progress="hidden")
        auto_timer.tick(auto_jobs_view, outputs=auto_job_outputs, show_progress="hidden")
        auto_client_file.upload(auto_install_client_secret, inputs=[auto_client_file], outputs=[auto_account_md])
        auto_connect_btn.click(auto_connect_account, outputs=[auto_account_md], concurrency_id="youtube-account")
        auto_disconnect_btn.click(auto_disconnect_account, outputs=[auto_account_md])
        auto_account_refresh_btn.click(auto_account_status, outputs=[auto_account_md])
        auto_channel_add_btn.click(
            auto_add_channel, inputs=[auto_channel_url, auto_channel_note],
            outputs=[auto_channels_df, auto_channel_remove, auto_channel_url, auto_channel_note],
        )
        auto_channel_remove_btn.click(
            auto_remove_channel, inputs=[auto_channel_remove],
            outputs=[auto_channels_df, auto_channel_remove],
        )
        auto_limit_btn.click(auto_save_limit, inputs=[auto_limit], outputs=[auto_limit_md])
        auto_start_btn.click(
            auto_submit,
            inputs=[auto_sources, auto_agent, auto_clip_count, auto_min_sec, auto_max_sec, auto_layout, auto_upload],
            outputs=[auto_sources, *auto_job_outputs],
        )
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
