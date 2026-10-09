#!/usr/bin/env python
"""CUT 自動投稿: paste a link -> AI picks clips -> Shorts -> private YouTube upload.

A separate app from the editor (``app.py``).  It shares the same library
(``data/``) and output folder (``clips/``) and runs on its own port.
Publishing stays a one-click action limited to allow-listed channels.
"""
from __future__ import annotations

import html
import json
import os
import socket
import threading
import webbrowser
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)

import gradio as gr  # noqa: E402

from moment_retrieval import agent_runner, ai_setup, config, gcloud_setup, youtube_upload  # noqa: E402
from moment_retrieval.auto_pipeline import (  # noqa: E402
    LIBRARY_PREFIX, SHORTS_MAX_SEC, AutoPipeline, PipelineError, library_videos,
)

APP_PORT = int(os.environ.get("CUT_AUTO_PUBLISH_PORT", "7870"))
# Operable panels get a light gray fill; fields inside stay white so they read as inputs.
APP_CSS = """
.gradio-container {
    --body-background-fill: #ffffff;
    --block-background-fill: #f1f3f5;
    --block-border-color: #dde1e6;
    --input-background-fill: #ffffff;
}
.dark .gradio-container {
    --body-background-fill: #0f1115;
    --block-background-fill: #1d2026;
    --input-background-fill: #121418;
}
"""



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
    if channel.get("needs_relink"):
        return ("**YouTube:** 連携中ですが、公開するには追加の許可が必要です。"
                f"「設定」タブで「YouTubeアカウントを連携」をやり直してください。{source}")
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
    status = ai_setup.all_status()
    short = {"codex": "Codex", "claude": "Claude Code", "local": "ローカルAI"}
    return " / ".join(
        f"{short[name]}: {'利用可' if item['ready'] else item['detail']}" for name, item in status.items()
    )


_AUTO_STATE_LABELS = {
    "queued": "待機中", "running": "実行中", "done": "完了", "failed": "失敗", "cancelled": "停止",
}


def uploads_signature(jobs) -> str:
    """Changes only when uploads or their public/private state change, so the publish
    cards (and any ticked checkboxes) are not rebuilt on every progress refresh."""
    return json.dumps([
        [job.job_id, [[u.get("video_id"), u.get("privacy_status")] for u in job.uploads]]
        for job in jobs if job.uploads
    ])


def _job_history_markdown(jobs) -> str:
    lines = []
    for job in jobs:
        state = _AUTO_STATE_LABELS.get(job.state, job.state)
        uploads = f"・アップロード {len(job.uploads)}本" if job.uploads else ""
        if getattr(job, "focus", ""):
            uploads += f"・探した場面: {html.escape(job.focus)}"
        recent = "\n".join(job.log[-8:])
        lines.append(
            f"**{html.escape(job.job_id)}** — {state}{uploads}　{html.escape(job.source)}\n\n"
            f"<details><summary>ログ</summary>\n\n```\n{recent}\n```\n\n</details>\n"
        )
    return "\n".join(lines) or "まだジョブはありません。"


def auto_jobs_view():
    """Running jobs stay visible; finished ones go to the collapsed history."""
    jobs = _auto_pipeline().list_jobs()[:10]
    active = [job for job in jobs if job.state in {"queued", "running"}]
    if active:
        parts = []
        for job in active:
            state = _AUTO_STATE_LABELS.get(job.state, job.state)
            step = f"（{job.step}）" if job.step else ""
            last = job.log[-1] if job.log else ""
            parts.append(f"- **{html.escape(job.job_id)}** {state}{html.escape(step)} — "
                         f"{html.escape(job.source)}<br><small>{html.escape(last)}</small>")
        running_md = "\n".join(parts)
    else:
        running_md = "実行中のジョブはありません。"
    running = [(job.job_id, job.job_id) for job in active]
    return (
        running_md,
        uploads_signature(jobs),
        gr.update(choices=running, value=running[0][1] if running else None),
        _job_history_markdown(jobs),
    )


def model_choices(agent: str) -> list[tuple[str, str]]:
    if agent == "claude":
        return list(agent_runner.CLAUDE_MODELS)
    if agent == "local":
        models = ai_setup.ollama_models() or []
        return [(name, name) for name in models] or [(config.LLM_ANALYSIS_MODEL, config.LLM_ANALYSIS_MODEL)]
    return agent_runner.codex_models()


def default_model(agent: str) -> str:
    if agent == "codex":
        return agent_runner.DEFAULT_CODEX_MODEL
    if agent == "local":
        return config.LLM_ANALYSIS_MODEL
    return ""


_AI_ORDER = (("codex", "Codex"), ("claude", "Claude Code"), ("local", "ローカルAI（Ollama・無料・PC内で完結）"))


def ai_setup_view() -> str:
    status = ai_setup.all_status()
    lines = ["| AI | 状態 |", "|---|---|"]
    for key, label in _AI_ORDER:
        item = status[key]
        mark = "✅" if item["ready"] else ("⚠️" if item["installed"] else "—")
        lines.append(f"| {label} | {mark} {html.escape(item['detail'])} |")
    return "\n".join(lines)


def ai_prepare(agent: str) -> str:
    """Install, sign in, or confirm readiness with a single button per AI."""
    return ai_setup.prepare(agent) + "\n\n" + ai_setup_view()


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
        youtube_part = "YouTube: 未連携（「設定」タブで連携するとアップロードまで自動。未連携なら書き出しまで）"
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


_UI_SETTINGS_DEFAULTS = {"min_sec": 20, "max_sec": SHORTS_MAX_SEC, "layout": "blur",
                         "effort": agent_runner.DEFAULT_EFFORT, "finish": True, "upload": "unlisted"}
UPLOAD_CHOICES = [("非公開でアップロード", "private"), ("限定公開でアップロード", "unlisted"), ("アップロードしない", "none")]


def _upload_args(upload) -> dict:
    """The upload control's value as submit() arguments (a bare bool is the old checkbox)."""
    if upload is True or upload in (None, "private"):
        return {"upload": True, "privacy": "private"}
    if upload == "unlisted":
        return {"upload": True, "privacy": "unlisted"}
    return {"upload": False, "privacy": "private"}


def _ui_settings_path() -> Path:
    return config.LIBRARY_ROOT / "auto_publish_ui.json"


def load_ui_settings() -> dict:
    """Saved defaults for length, layout and reasoning effort (invalid values fall back)."""
    try:
        saved = json.loads(_ui_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    settings = dict(_UI_SETTINGS_DEFAULTS)
    try:
        min_sec, max_sec = float(saved.get("min_sec", settings["min_sec"])), float(saved.get("max_sec", settings["max_sec"]))
        if 5 <= min_sec <= max_sec <= SHORTS_MAX_SEC:
            settings.update(min_sec=min_sec, max_sec=max_sec)
    except (TypeError, ValueError):
        pass
    if saved.get("layout") in {"blur", "crop"}:
        settings["layout"] = saved["layout"]
    if saved.get("effort") in agent_runner.EFFORTS:
        settings["effort"] = saved["effort"]
    if isinstance(saved.get("finish"), bool):
        settings["finish"] = saved["finish"]
    if saved.get("upload") in {"private", "unlisted", "none"}:
        settings["upload"] = saved["upload"]
    return settings


def apply_ui_settings():
    settings = load_ui_settings()
    return (settings["effort"], settings["min_sec"], settings["max_sec"], settings["layout"], settings["finish"],
            settings["upload"])


def save_ui_settings(effort: str, min_sec, max_sec, layout: str, finish: bool = True, upload: str = "private") -> str:
    try:
        min_sec, max_sec = float(min_sec), float(max_sec)
    except (TypeError, ValueError):
        raise gr.Error("長さは数値で入力してください。")
    if not 5 <= min_sec <= max_sec <= SHORTS_MAX_SEC:
        raise gr.Error(f"長さは 5秒 <= 最短 <= 最長 <= {SHORTS_MAX_SEC}秒 で指定してください。")
    if layout not in {"blur", "crop"} or effort not in agent_runner.EFFORTS:
        raise gr.Error("レイアウトと推論の強さを選んでください。")
    path = _ui_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"min_sec": min_sec, "max_sec": max_sec, "layout": layout, "effort": effort,
                                "finish": bool(finish),
                                "upload": upload if upload in {"private", "unlisted", "none"} else "private"},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return "保存しました。次回からこの設定で開きます。"


def on_mode_change(mode: str):
    url = mode == "url"
    return gr.update(visible=url), gr.update(visible=not url), gr.update(visible=url)


def auto_download_only(sources: str):
    """Download and transcribe URLs (or index local files) without clipping or uploading."""
    lines = [line.strip() for line in str(sources or "").splitlines() if line.strip()]
    if not lines:
        raise gr.Error("ダウンロードしたい動画のURLを入力してください。")
    submitted = []
    for line in lines[:10]:
        try:
            # The AI is not called in link-only jobs; any valid agent satisfies validation.
            job = _auto_pipeline().submit(line, "codex", link_only=True)
        except PipelineError as exc:
            raise gr.Error(f"{line}: {exc}") from exc
        submitted.append(job.job_id)
    gr.Info(f"{len(submitted)}件のダウンロードと文字起こしを開始しました。")
    return ("", *auto_jobs_view())


def auto_submit(mode: str, sources: str, library_selection, agent: str, model: str, clip_count, effort: str,
                min_sec, max_sec, layout: str, upload: bool, focus: str = "", finish: bool = False):
    # Only the input that is shown counts; a leftover value in the hidden one is ignored.
    if mode == "library":
        lines = [LIBRARY_PREFIX + video_id for video_id in (library_selection or [])]
        if not lines:
            raise gr.Error("文字起こし済みの動画を選んでください。")
    else:
        lines = [line.strip() for line in str(sources or "").splitlines() if line.strip()]
        if not lines:
            raise gr.Error("動画のURLを入力してください。")
    submitted = []
    for line in lines[:10]:
        try:
            job = _auto_pipeline().submit(
                line, agent, clip_count=int(clip_count), min_duration_sec=float(min_sec),
                max_duration_sec=float(max_sec), layout=layout, **_upload_args(upload),
                model=model or "", effort=(effort or "") if agent == "codex" else "", focus=focus or "",
                finish=bool(finish),
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
                        min_sec, max_sec, layout: str, upload: bool, focus: str = "", finish: bool = False):
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
    if not new_ids:
        log += "\n元動画のダウンロードは行いませんでした（新しく使える動画がありません。元動画URLのない共有zipは、元動画をこのPCに用意してください）。"
    for video_id in new_ids:
        conn = db.get_conn()
        try:
            origin = source_origin.origin_url_for_video(conn, db.get_video(conn, video_id) or {})
        finally:
            conn.close()
        if not origin:
            continue
        # The original video is always fetched from the bundled URL; clipping is optional.
        try:
            job = _auto_pipeline().submit(
                origin, agent, clip_count=int(clip_count), min_duration_sec=float(min_sec),
                max_duration_sec=float(max_sec), layout=layout, **_upload_args(upload),
                model=model or "", effort=(effort or "") if agent == "codex" else "",
                link_only=not start_clipping, focus=focus or "", finish=bool(finish),
            )
        except PipelineError as exc:
            log += f"\n元動画のダウンロードを開始できませんでした: {exc}"
            continue
        what = "続けて切り抜き・アップロードまで進めます" if start_clipping else "切り抜きは行いません"
        log += (f"\n元動画のダウンロードを開始しました（{job.job_id}）。"
                f"共有された文字起こしに関連付けます（{what}）。")
    choices = library_choices()
    return log, gr.update(choices=choices), gr.update(choices=choices)


def publish_now(job_id: str, video_ids):
    """Publish the chosen clips right away; failures are listed instead of stopping the rest."""
    ids = [str(item) for item in (video_ids or []) if item]
    if not ids:
        raise gr.Error("公開する動画にチェックを入れてください。")
    done, failed = [], []
    for video_id in ids:
        try:
            result = _auto_pipeline().publish(job_id, video_id)
            done.append(result["watch_url"])
        except (PipelineError, youtube_upload.UploadError) as exc:
            failed.append(f"{video_id}: {exc}")
        except Exception as exc:
            failed.append(f"{video_id}: {type(exc).__name__}: {exc}")
    if done:
        gr.Info(f"{len(done)}本を公開しました。")
    lines = [f"- 公開しました: {url}" for url in done]
    if failed:
        # One reason is enough when every clip failed the same way (e.g. a missing permission).
        reasons = {item.split(": ", 1)[1] for item in failed}
        if len(reasons) == 1:
            lines.append(f"- {len(failed)}本を公開できませんでした: {reasons.pop()}")
        else:
            lines += [f"- 公開できませんでした: {item}" for item in failed]
    return ("\n".join(lines), *auto_jobs_view())


def auto_cancel(job_id: str):
    if not job_id or not _auto_pipeline().cancel(job_id):
        raise gr.Error("停止できるジョブがありません。")
    gr.Info("停止を要求しました。")
    return auto_jobs_view()


# Close the tab if the browser allows it; otherwise replace the page with a notice
# so the user does not see a connection error once the server stops.
_QUIT_JS = """() => {
    try { window.open('', '_self'); window.close(); } catch (e) {}
    setTimeout(() => {
        document.body.innerHTML =
            '<div style="display:flex;align-items:center;justify-content:center;'
            + 'height:100vh;font-family:sans-serif;font-size:1.4rem;color:#888;">'
            + 'アプリを終了しました。ブラウザで開いている場合は、このタブを閉じてください。</div>';
    }, 200);
}"""


def shutdown_app():
    """Stop running jobs (and their child processes), then end this server process."""
    pipeline = _auto_pipeline()
    for job in pipeline.list_jobs():
        if job.state in {"queued", "running"}:
            pipeline.cancel(job.job_id)
    # Let this response reach the browser before the process exits.
    threading.Timer(3.0, lambda: os._exit(0)).start()
    return gr.update(visible=True, value="**アプリを終了しました。ブラウザで開いている場合は、このタブを閉じてください。**")


with gr.Blocks(title="CUT 自動投稿") as demo:
    with gr.Row():
        gr.Markdown("# CUT 自動投稿")
        quit_btn = gr.Button("アプリを終了", variant="stop", scale=0, min_width=120)
        quit_confirm_btn = gr.Button("本当に終了する（実行中のジョブも停止）", variant="stop",
                                     visible=False, scale=0)
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
    status_md = gr.Markdown("")
    with gr.Tabs():
        with gr.Tab("切り抜き"):
            auto_mode = gr.Radio(
                choices=[("新しい動画（URL）", "url"), ("文字起こし済みの動画", "library")],
                value="url", label="切り抜く動画",
            )
            with gr.Group(visible=True) as auto_url_group:
                auto_sources = gr.Textbox(
                    label="動画のURL（YouTube / Twitch）またはファイルのパス　※1行に1つ",
                    lines=2, placeholder="https://www.youtube.com/watch?v=...",
                )
            with gr.Group(visible=False) as auto_library_group:
                with gr.Row():
                    auto_library = gr.Dropdown(
                        choices=[], multiselect=True, scale=5,
                        label="文字起こし済みの動画（複数選択可・編集用CUTで処理した動画も含む）",
                    )
                    auto_library_refresh = gr.Button("一覧を更新", scale=1)
            with gr.Row():
                auto_agent = gr.Radio(
                    choices=[("Codex", "codex"), ("Claude Code", "claude"), ("ローカルAI", "local")], value="codex",
                    label="見どころを選ぶAI", scale=2,
                )
                auto_model = gr.Dropdown(
                    choices=model_choices("codex"), value=agent_runner.DEFAULT_CODEX_MODEL,
                    label="モデル", scale=2,
                )
                auto_clip_count = gr.Slider(1, 10, value=3, step=1, label="本数", scale=2)
            auto_focus = gr.Textbox(
                label="探したい場面（任意）　空欄なら見どころ全般から選びます",
                placeholder="例: 謎解きができなくてキレている箇所",
                max_length=200,
            )
            with gr.Accordion("長さ・レイアウト・仕上げ・推論の強さ・アップロード", open=False):
                with gr.Row():
                    auto_effort = gr.Dropdown(
                        choices=list(agent_runner.EFFORTS), value=agent_runner.DEFAULT_EFFORT,
                        label="推論の強さ（Codex）",
                    )
                    auto_min_sec = gr.Number(value=20, minimum=5, maximum=SHORTS_MAX_SEC, label="最短（秒）")
                    auto_max_sec = gr.Number(
                        value=SHORTS_MAX_SEC, minimum=5, maximum=SHORTS_MAX_SEC,
                        label=f"最長（秒）※ショートの上限 {SHORTS_MAX_SEC}秒",
                    )
                with gr.Row():
                    auto_layout = gr.Radio(
                        choices=[("ぼかし背景", "blur"), ("切り取り", "crop")], value="blur", label="縦型レイアウト",
                    )
                    auto_finish = gr.Checkbox(
                        value=True, label="仕上げる（引きのタイトル・字幕のタイミング調整・効果音）",
                    )
                    auto_upload = gr.Radio(choices=UPLOAD_CHOICES, value="unlisted", label="YouTube")
                with gr.Row():
                    auto_settings_save = gr.Button("長さ・レイアウト・仕上げ・推論の強さ・YouTubeを既定として保存", scale=1)
                    auto_settings_md = gr.Markdown("", scale=2)
            with gr.Row():
                auto_start_btn = gr.Button("切り抜いてアップロード", variant="primary", size="lg", scale=3)
                auto_download_btn = gr.Button("ダウンロードと文字起こしだけ", size="lg", scale=1)
            gr.Markdown(
                "<small>権利者から切り抜きの許可を得た動画だけに使ってください。文字起こしは選んだAIへ送られます。"
                "アップロードは**非公開**か**限定公開**（リンクを知っている人だけが見られる）で、"
                "公開は下のジョブの一覧かYouTube Studioで行います。</small>"
            )
            gr.Markdown("### ジョブ")
            auto_jobs_md = gr.Markdown("実行中のジョブはありません。")
            auto_uploads_sig = gr.State("[]")
            publish_result_md = gr.Markdown("")

            @gr.render(inputs=[auto_uploads_sig])
            def render_publish_cards(signature):
                entries = json.loads(signature or "[]")
                if not entries:
                    return
                gr.Markdown("**アップロードした動画**（リンクから確認し、チェックして公開できます）")
                for job_id, _uploads in entries:
                    job = _auto_pipeline().jobs.get(job_id)
                    if job is None:
                        continue
                    with gr.Group():
                        lines = [f"**{html.escape(job_id)}** — 元動画: {html.escape(job.source)}"]
                        for upload in job.uploads:
                            video_id = str(upload.get("video_id") or "")
                            status = youtube_upload.PRIVACY_LABELS.get(str(upload.get("privacy_status")), "非公開")
                            watch = f"https://www.youtube.com/watch?v={video_id}"
                            studio = f"https://studio.youtube.com/video/{video_id}/edit"
                            lines.append(f"- [{status}] {html.escape(str(upload.get('title', '')))} — "
                                         f"[YouTubeで見る]({watch})・[Studioで編集]({studio})")
                        gr.Markdown("\n".join(lines), padding=True)
                        private = [(str(u.get("title", "")), str(u.get("video_id")))
                                   for u in job.uploads if u.get("privacy_status") != "public"]
                        if not private:
                            continue
                        picked = gr.CheckboxGroup(choices=private, label="公開する動画")
                        with gr.Row():
                            publish_picked = gr.Button("チェックした動画を公開")
                            publish_all = gr.Button("このジョブの全部を公開")
                        publish_picked.click(
                            lambda selection, job_id=job_id: publish_now(job_id, selection),
                            inputs=[picked], outputs=[publish_result_md, *auto_job_outputs],
                            concurrency_id="youtube-publish", concurrency_limit=1,
                        )
                        publish_all.click(
                            lambda job_id=job_id, ids=tuple(v for _t, v in private): publish_now(job_id, ids),
                            outputs=[publish_result_md, *auto_job_outputs],
                            concurrency_id="youtube-publish", concurrency_limit=1,
                        )

            with gr.Accordion("ジョブの履歴（ログ）", open=False):
                auto_history_md = gr.Markdown("まだジョブはありません。")
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
            gr.Markdown("<small>元動画のURLが入ったzipなら、読み込み後に元動画を自動でダウンロードして関連付けます。</small>")
            share_start = gr.Checkbox(
                value=True,
                label="続けて「切り抜き」タブの設定で切り抜き・アップロードまで進める",
            )
            share_import_btn = gr.Button("読み込む", variant="primary")
            share_import_log = gr.Textbox(label="読み込みログ", interactive=False, lines=6)

        with gr.Tab("設定"):
            gr.Markdown("### 見どころを選ぶAI")
            ai_status_md = gr.Markdown("")
            gr.Markdown(
                "<small>使いたいAIのボタンを押すと、未インストールならインストール、未ログインならログインを"
                "別ウィンドウで進めます（準備済みなら何もしません）。終わったらもう一度押すと状態が更新されます。"
                "ローカルAIはアカウント不要ですが、数GBのダウンロードとメモリ16GB程度が必要です。</small>"
            )
            with gr.Row():
                ai_codex_btn = gr.Button("Codex")
                ai_claude_btn = gr.Button("Claude")
                ai_local_btn = gr.Button("ローカル")
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

    auto_job_outputs = [auto_jobs_md, auto_uploads_sig, auto_cancel_select, auto_history_md]
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
    demo.load(ai_setup_view, outputs=[ai_status_md])
    for button, agent in ((ai_codex_btn, "codex"), (ai_claude_btn, "claude"), (ai_local_btn, "local")):
        button.click(lambda agent=agent: ai_prepare(agent), outputs=[ai_status_md]).then(
            summary_status, outputs=[status_md])
    auto_connect_btn.click(auto_connect_account, outputs=[auto_account_md],
                           concurrency_id="youtube-account").then(summary_status, outputs=[status_md])
    auto_disconnect_btn.click(auto_disconnect_account, outputs=[auto_account_md]).then(
        summary_status, outputs=[status_md])
    auto_account_refresh_btn.click(auto_account_status, outputs=[auto_account_md]).then(
        summary_status, outputs=[status_md])
    auto_start_btn.click(
        auto_submit,
        inputs=[auto_mode, auto_sources, auto_library, auto_agent, auto_model, auto_clip_count, auto_effort,
                auto_min_sec, auto_max_sec, auto_layout, auto_upload, auto_focus, auto_finish],
        outputs=[auto_sources, auto_library, *auto_job_outputs],
    )
    auto_mode.change(on_mode_change, inputs=[auto_mode],
                     outputs=[auto_url_group, auto_library_group, auto_download_btn])
    demo.load(apply_ui_settings,
              outputs=[auto_effort, auto_min_sec, auto_max_sec, auto_layout, auto_finish, auto_upload])
    auto_settings_save.click(save_ui_settings,
                             inputs=[auto_effort, auto_min_sec, auto_max_sec, auto_layout, auto_finish, auto_upload],
                             outputs=[auto_settings_md])
    auto_download_btn.click(auto_download_only, inputs=[auto_sources],
                            outputs=[auto_sources, *auto_job_outputs]).then(
        refresh_library, outputs=[auto_library, share_export_video])
    demo.load(refresh_library, outputs=[auto_library, share_export_video])
    auto_library_refresh.click(refresh_library, outputs=[auto_library, share_export_video])
    share_export_btn.click(export_shared_index, inputs=[share_export_video, share_include_url],
                           outputs=[share_export_file, share_export_md], concurrency_id="library-share")
    share_import_btn.click(
        import_shared_index,
        inputs=[share_import_file, share_start, auto_agent, auto_model, auto_clip_count, auto_effort,
                auto_min_sec, auto_max_sec, auto_layout, auto_upload, auto_focus, auto_finish],
        outputs=[share_import_log, auto_library, share_export_video],
        concurrency_id="library-share",
    ).then(auto_jobs_view, outputs=auto_job_outputs)
    auto_cancel_btn.click(auto_cancel, inputs=[auto_cancel_select], outputs=auto_job_outputs)


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _disable_console_quick_edit() -> None:
    """A click in the console starts text selection (QuickEdit), which blocks every write
    to it and freezes the server. Turn that off for this app's own console window."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            enable_quick_edit, enable_extended_flags = 0x0040, 0x0080
            kernel32.SetConsoleMode(handle, (mode.value & ~enable_quick_edit) | enable_extended_flags)
    except (AttributeError, OSError):
        pass


def _running_jobs() -> int:
    return sum(1 for job in _auto_pipeline().list_jobs() if job.state in {"queued", "running"})


def _open_window(url: str, *, on_close=None) -> bool:
    """Show the UI in its own window (pywebview).  False when that is not possible."""
    try:
        import webview
    except ImportError:
        return False
    webview.settings["ALLOW_DOWNLOADS"] = True  # share zips and exported files
    window = webview.create_window("CUT 自動投稿", url, width=1280, height=920, min_size=(900, 600),
                                   text_select=True)

    def closing():
        if on_close is None or not _running_jobs():
            return True
        return window.create_confirmation_dialog(
            "CUT 自動投稿", "実行中のジョブがあります。止めて終了しますか？")

    window.events.closing += closing
    try:
        webview.start()
    except Exception as exc:  # e.g. the WebView2 runtime is missing
        print(f"専用ウィンドウを開けなかったため、ブラウザで開きます: {exc}")
        return False
    if on_close is not None:
        on_close()
    return True


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="CUT 自動投稿")
    parser.add_argument("--browser", action="store_true", help="専用ウィンドウではなくブラウザで開く")
    args = parser.parse_args(argv)
    url = f"http://127.0.0.1:{APP_PORT}"
    _disable_console_quick_edit()
    if _port_in_use(APP_PORT):
        print(f"自動投稿アプリは既に起動しています: {url}")
        # A second window onto the running app; closing it leaves that app running.
        if args.browser or not _open_window(url):
            webbrowser.open(url)
        raise SystemExit(0)
    if args.browser:
        demo.launch(server_name="127.0.0.1", server_port=APP_PORT, inbrowser=True, css=APP_CSS)
        return
    demo.launch(server_name="127.0.0.1", server_port=APP_PORT, inbrowser=False, css=APP_CSS,
                prevent_thread_lock=True)

    def stop():
        pipeline = _auto_pipeline()
        for job in pipeline.list_jobs():
            if job.state in {"queued", "running"}:
                pipeline.cancel(job.job_id)
        os._exit(0)

    if not _open_window(url, on_close=stop):
        webbrowser.open(url)
        demo.block_thread()


if __name__ == "__main__":
    main()
