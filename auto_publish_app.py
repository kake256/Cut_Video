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
import re
import socket
import threading
import webbrowser
from datetime import datetime
from functools import partial
from pathlib import Path
from urllib.parse import quote

os.chdir(Path(__file__).resolve().parent)

# onnxruntime (Silero VAD, used when finishing relistens each clip) fails to load its
# DLL on Windows once gradio is imported, so load it first.
try:
    import onnxruntime  # noqa: E402,F401
except ImportError:
    pass

import gradio as gr  # noqa: E402

from moment_retrieval import (  # noqa: E402
    agent_runner, ai_setup, api_keys, config, gcloud_setup, youtube_auto_reply, youtube_gas, youtube_upload,
)
from moment_retrieval.auto_pipeline import (  # noqa: E402
    LIBRARY_PREFIX, SHORTS_MAX_SEC, AutoPipeline, PipelineError, library_videos,
)

APP_PORT = int(os.environ.get("CUT_AUTO_PUBLISH_PORT", "7870"))

# Gradio's multiselect dropdown ignores clicks on its arrow and stays open after a pick.
_LIBRARY_ARROW_JS = """() => {
  if (window.__cutLibraryArrow) return;
  window.__cutLibraryArrow = true;
  document.addEventListener('mousedown', (event) => {
    const icon = event.target.closest('#auto-library .icon-wrap');
    if (!icon) return;
    event.preventDefault();
    const input = icon.closest('#auto-library').querySelector('input');
    if (document.activeElement === input) { input.blur(); return; }
    input.focus();
    input.click();
  }, true);
}"""
_LIBRARY_CLOSE_JS = """() => {
  const active = document.activeElement;
  if (active && active.closest('#auto-library')) active.blur();
}"""
# Operable panels get a light gray fill; fields inside stay white so they read as inputs.
APP_CSS = """
#auto-library .icon-wrap { cursor: pointer; }
/* 「JS本数」 sits on the checkbox's line: label and a small box side by side, no tall card. */
#auto-jsdraw-count { padding: 4px 8px; }
#auto-jsdraw-count label { display: flex; align-items: center; gap: 8px; }
#auto-jsdraw-count label > span { margin: 0; white-space: nowrap; }
#auto-jsdraw-count input { width: 52px; min-width: 0; padding: 4px 6px; }
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
        f"[「対象」ページ](https://console.cloud.google.com/auth/audience?project={project})"
        if project else "Google Cloud Consoleの「対象」ページ"
    )
    return (
        f"**配布用:** クライアントを `{html.escape(target.name)}` としてアプリフォルダに同梱しました。"
        "このファイルだけを相手に渡し、CUTのフォルダ直下（start.batと同じ場所）に置いてもらえば、"
        "相手は「YouTubeアカウントを連携」だけで使えます。"
        f"{audience}で「アプリを公開」して本番環境にしておくと、テストユーザーの登録が不要になり、"
        "ログインが7日で切れることもなくなります（審査前は最大100人）。"
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
            "「このアプリは確認されていません」と出た場合は「詳細」→「（安全ではないページ）に移動」で進めます。"
            "「アクセスをブロック」と出た場合は、クライアントがテスト中のままです。配布した人に、"
            "Google Cloud Consoleの「対象」ページで「アプリを公開」するか、このGoogleアカウントを"
            "テストユーザーに追加してもらってください。"
        )
    return auto_account_status()


def auto_disconnect_account() -> str:
    youtube_upload.disconnect_account()
    return auto_account_status()


_AUTO_REPLIER = youtube_auto_reply.BackgroundReplier()
_REPLY_STATUS_LABELS = {"replied": "返信済み", "error": "失敗"}


def reply_status() -> str:
    settings = youtube_auto_reply.load_settings()
    state = "有効" if settings.enabled else "無効"
    if settings.run_on == "gas":
        gas = youtube_gas.load_state()
        lines = [
            f"**コメント返信:** {state} / 実行場所: GAS（パソコンが起動していなくても返信します） / "
            f"{settings.interval_minutes}分ごとに確認 / 1日{settings.daily_limit}件まで",
            f"[スクリプトを開く（初回は「実行」で許可）]({gas.get('setup_url') or gas.get('editor_url', '')}) / "
            f"[状態の確認ページ（返信ログのスプレッドシートもここから）]({gas.get('web_url', '')})",
        ]
        return "\n\n".join(lines)
    login = "連携中" if youtube_auto_reply.is_connected() else "未連携（「返信用に連携」を押してください）"
    lines = [
        f"**コメント返信:** {state} / 実行場所: このパソコン（アプリを開いている間だけ） / ログイン: {login} / "
        f"今日 {youtube_auto_reply.replies_today()}/{settings.daily_limit}件 / "
        f"{settings.interval_minutes}分ごとに確認",
    ]
    if _AUTO_REPLIER.last_message:
        lines.append(f"前回の自動実行: {html.escape(_AUTO_REPLIER.last_message)}")
    return "\n\n".join(lines)


def _push_to_gas() -> str:
    """Send changed settings to GAS when it is the one replying; '' when nothing to report."""
    settings = youtube_auto_reply.load_settings()
    if settings.run_on != "gas":
        return ""
    try:
        youtube_gas.sync(settings)
    except (youtube_gas.GasError, youtube_auto_reply.AutoReplyError) as exc:
        return f"\n\n**GASへの反映に失敗しました:** {html.escape(str(exc))}"
    except Exception as exc:
        return f"\n\n**GASへの反映に失敗しました:** {type(exc).__name__}"
    return "\n\nGASにも反映しました。"


def reply_gas_enable() -> str:
    """Create/update the GAS project and open its consent page; the browser opens on this PC."""
    problems = youtube_upload.setup_problems()
    if problems:
        return "**GAS:** " + " ".join(problems)
    settings = youtube_auto_reply.load_settings()
    previous = settings.run_on

    def restore():
        youtube_auto_reply.save_settings(settings.enabled, settings.templates, settings.daily_limit,
                                         settings.interval_minutes, run_on=previous)

    try:
        settings = youtube_auto_reply.save_settings(
            settings.enabled, settings.templates, settings.daily_limit, settings.interval_minutes, run_on="gas")
        state = youtube_gas.deploy(settings)
    except youtube_gas.GasError as exc:
        restore()
        if exc.url:
            webbrowser.open(exc.url)
        return f"**GAS:** {html.escape(str(exc))}"
    except Exception as exc:
        restore()
        return f"**GAS:** 準備に失敗しました（{type(exc).__name__}: {html.escape(str(exc))[:200]}）。"
    setup_link = state.get("setup_url") or state.get("editor_url", "")
    webbrowser.open(setup_link)
    owner = html.escape(state.get("owner_email") or "スクリプトを作った個人のGoogleアカウント")
    return (
        f"**GAS:** スクリプトを用意し、ブラウザで開きました（{owner} で開きます。アカウントを聞かれたらこれを選択）。"
        "上部の「実行」を押してください（関数は最初から「setup」が選ばれています）。"
        "「承認が必要です」→「権限を確認」→ アカウントを選択 →「詳細」→「CUT コメント自動返信（安全ではないページ）に移動」→"
        "「許可」と進み、下の実行ログに「準備完了: （チャンネル名）として…」と出れば完了です。"
        f"\n\nブラウザが開かない場合は[こちら]({setup_link})。"
        "\n\n" + reply_status()
    )


def reply_gas_stop() -> str:
    settings = youtube_auto_reply.load_settings()
    youtube_auto_reply.save_settings(settings.enabled, settings.templates, settings.daily_limit,
                                     settings.interval_minutes, run_on="pc")
    try:  # tell GAS to stop replying; its trigger then finds nothing to do
        youtube_gas.sync(youtube_auto_reply.load_settings())
        note = "GAS側の返信は止めました。"
    except Exception as exc:
        note = (f"GAS側を止められませんでした（{type(exc).__name__}）。"
                "スクリプトを開き、左の「トリガー」から autoReply を削除してください。")
    return f"**GAS:** このパソコンで返信する設定に戻しました。{note}\n\n" + reply_status()


def reply_log_markdown() -> str:
    rows = youtube_auto_reply.recent_log()
    if not rows:
        return "まだ返信していません。"
    lines = ["| 日時 | 状態 | 相手 | コメント | 返信 |", "|---|---|---|---|---|"]
    for row in rows:
        when = (youtube_auto_reply.parse_time(row["created_at"]) or datetime.now()).astimezone()
        cells = [when.strftime("%m/%d %H:%M"), _REPLY_STATUS_LABELS.get(row["status"], row["status"]),
                 row["author"], row["comment_text"][:60], row["reply_text"][:60] or row["error"][:60]]
        lines.append("| " + " | ".join(
            html.escape(str(cell)).replace("|", "／").replace("\n", " ") for cell in cells) + " |")
    return "\n".join(lines)


def reply_templates_view():
    """The registered replies (line breaks shown as-is) and the choices for deletion."""
    templates = youtube_auto_reply.load_settings().templates
    if not templates:
        return "まだ定型文がありません。下の欄に入力して「追加」を押してください。", gr.update(choices=[], value=None)
    blocks = []
    for number, text in enumerate(templates, start=1):
        quoted = "<br>".join(html.escape(line) for line in text.split("\n"))
        blocks.append(f"**{number}.**<br>{quoted}")
    choices = [(f"{number}. {text.splitlines()[0][:40]}", str(number - 1))
               for number, text in enumerate(templates, start=1)]
    return "\n\n".join(blocks), gr.update(choices=choices, value=None)


def reply_load_settings():
    settings = youtube_auto_reply.load_settings()
    return settings.enabled, settings.include_past, settings.daily_limit, settings.interval_minutes


def reply_save_settings(enabled: bool, include_past: bool, daily_limit, interval_minutes) -> str:
    templates = youtube_auto_reply.load_settings().templates
    try:
        youtube_auto_reply.save_settings(enabled, templates, daily_limit, interval_minutes,
                                         include_past=include_past)
    except youtube_auto_reply.AutoReplyError as exc:
        return f"**コメント返信:** {exc}"
    return reply_status() + _push_to_gas()


def reply_add_template(text: str):
    try:
        youtube_auto_reply.add_template(text)
    except youtube_auto_reply.AutoReplyError as exc:
        return (f"**コメント返信:** {exc}", gr.update(), *reply_templates_view())
    return (reply_status() + _push_to_gas(), "", *reply_templates_view())


def reply_remove_template(index):
    try:
        youtube_auto_reply.remove_template(index)
    except youtube_auto_reply.AutoReplyError as exc:
        return (f"**コメント返信:** {exc}", gr.update(), *reply_templates_view())
    return (reply_status() + _push_to_gas(), youtube_auto_reply.load_settings().enabled,
            *reply_templates_view())


def reply_connect() -> str:
    problems = youtube_upload.setup_problems()
    if problems:
        return "**コメント返信:** " + " ".join(problems)
    try:
        youtube_auto_reply.connect_account()
    except Exception as exc:
        return f"**コメント返信:** 連携に失敗しました（{type(exc).__name__}: {html.escape(str(exc))[:200]}）。"
    return reply_status() + _push_to_gas()  # GAS replies with this login too


def reply_disconnect() -> str:
    youtube_auto_reply.disconnect_account()
    return reply_status()


def reply_run_now():
    try:
        message = youtube_auto_reply.run_once(manual=True).message
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"[:300]
    return f"**手動実行:** {html.escape(message)}\n\n" + reply_status(), reply_log_markdown()


def auto_agent_status() -> str:
    status = ai_setup.all_status()
    short = {"codex": "Codex", "claude": "Claude Code", "local": "ローカルAI"}
    return " / ".join(
        f"{short[name]}: {'利用可' if item['ready'] else item['detail']}" for name, item in status.items()
    )


_AUTO_STATE_LABELS = {
    "queued": "待機中", "running": "実行中", "done": "完了", "failed": "失敗", "cancelled": "停止",
}


CARD_JOBS = 10  # newest jobs shown as cards; jobs with unposted clips are always shown


def clip_cards() -> list[dict]:
    """Finished jobs as cards, read from the job files so the daily batch's jobs appear too."""
    from moment_retrieval import made_clips

    groups: dict[str, list] = {}
    for clip in made_clips.list_clips():
        groups.setdefault(clip.job_id, []).append(clip)
    return [
        {"job_id": job_id, "source": clips[0].job_source, "batch": clips[0].batch, "clips": clips}
        for index, (job_id, clips) in enumerate(groups.items())
        if index < CARD_JOBS or any(not clip.receipt for clip in clips)
    ]


def cards_signature(cards=None) -> str:
    """Changes only when clips are added, uploaded or change privacy, so the cards
    (and any ticked checkboxes) are not rebuilt on every progress refresh.

    The publish queue's state per clip is part of it too, so queuing, publishing
    or failing shows up in the list (and the queued ones leave the checkboxes)."""
    from moment_retrieval import publish_queue

    cards = clip_cards() if cards is None else cards
    queued = publish_queue.latest_by_path()

    def queue_state(clip):
        item = queued.get(publish_queue._key(clip.path)) or {}
        return [item.get("state"), item.get("not_before")]

    return json.dumps([
        [card["job_id"], [[str(clip.path)] + [(clip.receipt or {}).get(key) for key in
                                              ("video_id", "privacy_status", "upload_status", "missing_on_youtube",
                                               "publish_at")]
                          + queue_state(clip)
                          for clip in card["clips"]]]
        for card in cards
    ], ensure_ascii=False)


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
            video = _auto_pipeline().video_download_status(job.job_id)
            video_line = f"<br><small>並行中の動画ダウンロード: {html.escape(video)}</small>" if video else ""
            parts.append(f"- **{html.escape(job.job_id)}** {state}{html.escape(step)} — "
                         f"{html.escape(job.source)}<br><small>{html.escape(last)}</small>{video_line}")
        running_md = "\n".join(parts)
    else:
        running_md = "実行中のジョブはありません。"
    running = [(job.job_id, job.job_id) for job in active]
    return (
        running_md,
        cards_signature(),
        gr.update(choices=running, value=running[0][1] if running else None),
        _job_history_markdown(jobs),
    )


API_AGENT_CHOICE = "API 1回呼び出し"


def model_choices(agent: str) -> list[tuple[str, str]]:
    if agent == agent_runner.API_AGENT:
        return [("Sonnet 5.5（固定・約0.1ドル）", "")]
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


LEDGER_GAS_FILE = Path(__file__).resolve().parent / "moment_retrieval" / "gas_ledger.js"


def ledger_gas_source() -> str:
    try:
        return LEDGER_GAS_FILE.read_text(encoding="utf-8")
    except OSError:
        return "// moment_retrieval/gas_ledger.js が見つかりません"


def ledger_status() -> str:
    from moment_retrieval import shared_ledger

    state = shared_ledger.status()
    if not state["enabled"]:
        return ("### ⚪ 共有台帳はオフです\n"
                "自分の名前と、一緒に使う人と共通の「送信用URL」を入れて「この設定で始める」を押すと、"
                "ほかの人が着手した動画や、作った・投稿した場面を、自分の候補から外すようになります。")
    name = html.escape(state["name"])
    source = "（環境変数で設定）" if state["from_env"] else ""
    where = "送信用URL" if state["url"] else f"フォルダ `{html.escape(state['folder'])}`"
    if not state["reachable"]:
        hint = ("インターネットにつながっているか、送信用URLのデプロイが残っているか確認してください。"
                if state["url"] else "Googleドライブなどの同期アプリが起動しているか確認してください。")
        text = (f"### ⚠️ 共有台帳に届きません{source}\n"
                f"{where}　自分の名前: **{name}**\n\n{hint}"
                "届かない間の記録はこのPCにためておき、届いたときにまとめて送ります。")
    else:
        others = "、".join(f"**{html.escape(item)}**" for item in state["others"]) or "まだいません（相手が記録すると表示されます）"
        text = (f"### ✅ 共有台帳を使っています{source}\n"
                f"{where}　自分の名前: **{name}**\n\n一緒に使っている人: {others}")
    if state["waiting"]:
        text += f"\n\nまだ送れていない記録が {state['waiting']} 件あります。"
    return text


def ledger_share_guide() -> str:
    from moment_retrieval import shared_ledger

    state = shared_ledger.status()
    if not (state["enabled"] and state["reachable"]):
        return ""
    if state["url"]:
        return ("#### 相手を招待する\n"
                "相手のCUTの「分担（共有台帳）」タブに、この送信用URLを貼ってもらいます"
                "（名前は相手のものにします）。URLを知っている人は誰でも書き込めるので、"
                "一緒に使う人にだけ直接渡してください。\n\n"
                f"`{html.escape(state['url'])}`")
    folder = html.escape(Path(state["folder"]).name)
    return (
        "#### 相手を招待する\n"
        f"1. 「{folder}」フォルダを相手と共有します（Googleドライブなら右クリック→「共有」→**編集者**）。\n"
        f"2. 相手は共有された「{folder}」を自分のPCに同期させます（Googleドライブなら「ショートカットを追加」）。\n"
        "3. 相手のCUTでこのタブを開き、同じフォルダを選んで始めます。"
    )


def ledger_preview(folder: str) -> str:
    """For the folder option: what pressing its start button will do."""
    target = str(folder or "").strip().strip('"')
    if not target:
        return ""
    path = Path(target)
    try:
        if path.is_dir():
            return "このフォルダはすでにあります。ここに参加します。"
        if path.parent.is_dir():
            return "このフォルダは、始めるときに新しく作ります。"
    except OSError:
        pass
    return "⚠️ この場所が見つかりません。同期アプリが起動しているか確認してください。"


def ledger_form_values():
    """(送信用URL, フォルダ, 名前, 状態, フォルダの確認, 招待の手順)"""
    from moment_retrieval import shared_ledger

    saved = shared_ledger.saved_settings()
    url = str(saved.get("url") or "")
    folder = str(saved.get("folder") or "") or shared_ledger.suggested_folder()
    name = str(saved.get("name") or "") or shared_ledger.default_name()
    return (url, folder, name, ledger_status(), ledger_preview(folder), ledger_share_guide())


def ledger_pick_folder(current: str) -> str:
    """Open a folder dialog on this PC (the app runs locally); keep the text when cancelled."""
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(title="共有台帳に使う同期フォルダを選ぶ", initialdir=current or None,
                                         mustexist=False)
        root.destroy()
    except Exception:
        raise gr.Error("フォルダ選択の画面を開けませんでした。場所を直接入力してください。")
    return str(Path(chosen)) if chosen else current


def ledger_start_url(url: str, name: str):
    from moment_retrieval import shared_ledger

    try:
        shared_ledger.save_url(url, name)
    except shared_ledger.LedgerSettingsError as exc:
        raise gr.Error(str(exc)) from exc
    gr.Info("共有台帳を始めました。次のジョブから使います。")
    return ledger_form_values()


def ledger_start_folder(folder: str, name: str):
    from moment_retrieval import shared_ledger

    try:
        saved = shared_ledger.save_settings(folder, name)
    except shared_ledger.LedgerSettingsError as exc:
        raise gr.Error(str(exc)) from exc
    gr.Info(f"共有台帳を {saved.folder} で始めました。次のジョブから使います。")
    return ledger_form_values()


def ledger_backfill() -> str:
    from moment_retrieval import used_ranges

    result = used_ranges.send_past_uploads_to_ledger()
    if not result["enabled"]:
        return "先に上で共有台帳を始めてください。"
    skipped = f"（URLの分からない動画の {result['no_url']} 件は送れませんでした）" if result["no_url"] else ""
    if not result["sent"]:
        return f"送れる投稿済みの切り抜きはありませんでした{skipped}。"
    if not result["delivered"]:
        return (f"{result['sent']} 件を送ろうとしましたが、台帳に届きませんでした。このPCにためておき、"
                f"次につながったときに送ります{skipped}。")
    return f"✅ これまでの投稿済みの切り抜き {result['sent']} 件を台帳に送りました{skipped}。"


def ledger_clear():
    from moment_retrieval import shared_ledger

    shared_ledger.clear_settings()
    return ledger_form_values()


def api_key_status(provider: str) -> str:
    return f"**{api_keys.PROVIDERS[provider]['label']}:** {html.escape(api_keys.describe(provider))}"


def api_key_save(provider: str, key: str):
    """Save the typed key; the box is cleared so the key is never shown again."""
    if not str(key or "").strip():
        raise gr.Error("APIキーを入力してから「保存」を押してください。")
    try:
        api_keys.save_key(provider, key)
    except api_keys.ApiKeyError as exc:
        raise gr.Error(str(exc)) from exc
    gr.Info(f"{api_keys.PROVIDERS[provider]['label']} のAPIキーを保存しました。")
    return "", api_key_status(provider), ai_setup_view()


def api_key_test(provider: str, key: str) -> str:
    """Test the typed key, or the saved / environment key when the box is empty."""
    try:
        ok, message = api_keys.test_key(provider, key)
    except api_keys.ApiKeyError as exc:
        ok, message = False, str(exc)
    return api_key_status(provider) + f"\n\n{'✅' if ok else '⚠️'} {html.escape(message)}"


def api_key_delete(provider: str):
    removed = api_keys.delete_key(provider)
    note = "保存したキーを削除しました。" if removed else "保存したキーはありません。"
    return "", api_key_status(provider) + f"\n\n{note}", ai_setup_view()


def save_claude_uses_key(enabled: bool) -> str:
    """Opt in (or back out) of starting Claude Code with the saved Anthropic key."""
    api_keys.set_claude_cli_uses_key(bool(enabled))
    gr.Info("Claude Code をAPIキー（従量課金）で動かします。" if enabled
            else "Claude Code はログインの枠で動かします（キーはAPI 1回呼び出しとJS描画だけに使います）。")
    return ai_setup_view()



# ---------------------------------------------------------------- JS描画ショート（Claude API）

def jsdraw_video_choices() -> list[tuple[str, str]]:
    """文字起こし済みで、元の動画ファイルがこのPCにあるもの（新しい順）。"""
    from moment_retrieval import db

    conn = db.get_conn()
    try:
        videos = db.list_videos(conn)
    finally:
        conn.close()
    return [(Path(str(v["path"])).name, str(v["path"])) for v in reversed(videos) if Path(str(v["path"])).is_file()]


def _jsdraw_seconds(text: str) -> float:
    """「38:50」「0:38:50」「2330」を秒にする。"""
    parts = str(text or "").strip().split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        raise gr.Error(f"時刻の形式が正しくありません: {text}（例 0:38:50）") from None
    if not nums or len(nums) > 3:
        raise gr.Error(f"時刻の形式が正しくありません: {text}（例 0:38:50）")
    sec = 0.0
    for n in nums:
        sec = sec * 60 + n
    return sec


def jsdraw_run(video: str, start: str, end: str, title: str, story: str, model: str):
    """1本作る。進み具合を流しながら、最後に動画を返す。"""
    import queue

    from moment_retrieval import jsdraw_short

    if not video:
        raise gr.Error("動画を選んでください。")
    a, b = _jsdraw_seconds(start), _jsdraw_seconds(end)
    if b <= a:
        raise gr.Error("終わりは始めより後にしてください。")
    if not api_keys.resolve("anthropic")[0]:
        raise gr.Error("AnthropicのAPIキーがありません。「設定」タブの「APIキーで使う」で保存してください。")
    lines: list[str] = []
    done: "queue.Queue[tuple[str, object]]" = queue.Queue()

    def progress(msg: str) -> None:
        if lines and lines[-1].split("（")[0] == msg.split("（")[0]:
            lines[-1] = msg  # 同じ工程の待ち時間表示は上書き
        else:
            lines.append(msg)

    def work() -> None:
        try:
            done.put(("ok", jsdraw_short.make_short(video, a, b, model=model, title=title.strip(),
                                                     story=story.strip(), progress=progress)))
        except Exception as exc:  # noqa: BLE001 — 画面に出して終える
            done.put(("error", exc))

    threading.Thread(target=work, daemon=True).start()
    while True:
        try:
            kind, value = done.get(timeout=2)
            break
        except queue.Empty:
            yield "\n".join(f"- {line}" for line in lines[-8:]), None
    if kind == "error":
        yield "\n".join(f"- {line}" for line in lines[-8:]) + f"\n\n⚠️ {html.escape(str(value))}", None
        return
    result = value
    yield (f"✅ できました：`{result['mp4']}`\n\nAPIの費用：約 {result['usd']} ドル"
           f"（生成 {result['usage'][0]['usd']} ＋ 直し {result['usage'][1]['usd']}）", result["mp4"])


def jsdraw_short_labels() -> dict:
    from moment_retrieval import jsdraw_short

    return jsdraw_short.MODEL_LABELS


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


_DATE_PREFIX = re.compile(r"^(\d{4})(\d{2})(\d{2})_")


def _video_date(name: str, upload_date: str = "") -> str:
    """YYYYMMDD from the downloaded file name (its stream date), else from the source metadata."""
    match = _DATE_PREFIX.match(name)
    if match:
        return "".join(match.groups())
    return upload_date if re.fullmatch(r"\d{8}", upload_date or "") else ""


def _content_hints(video_ids: list[str], limit: int = 2) -> dict[str, list[str]]:
    """A few highlight titles per video, as a clue to its content when no title is known."""
    from moment_retrieval import db

    hints: dict[str, list[str]] = {}
    conn = db.get_conn()
    try:
        for video_id in video_ids:
            revision = db.get_active_transcript_revision(conn, video_id)
            run = db.get_latest_ready_highlight_run(conn, video_id, revision) if revision else None
            if run:
                titles = [str(c.get("title") or "").strip() for c in db.get_highlight_candidates(conn, run["highlight_run_id"])]
                # Search-made candidates are titled after the query, which says nothing here.
                hints[video_id] = [title for title in titles if title and "に関する場面" not in title][:limit]
    finally:
        conn.close()
    return hints


def library_choices() -> list[tuple[str, str]]:
    """Newest first, labelled by stream date, title (or highlight titles) and length."""
    from moment_retrieval import source_titles

    videos = library_videos()
    titles = source_titles.load()
    hints = _content_hints([video["video_id"] for video in videos])
    rows = []
    for video in videos:
        name = str(video["name"] or "")
        meta = titles.get(source_titles.source_key(name) or "", {})
        date = _video_date(name, str(meta.get("upload_date") or ""))
        what = str(meta.get("title") or "") or " / ".join(hints.get(video["video_id"], []))             or Path(name).stem.removesuffix(".audio")
        if len(what) > 40:
            what = what[:39] + "…"
        day = f"{int(date[4:6])}/{int(date[6:8])}" if date else "日付不明"
        if date and date[:4] != str(datetime.now().year):
            day = f"{date[:4]}/{day}"
        note = "" if video["source_available"] else "（共有・元動画は自動でダウンロード）"
        rows.append((date, f"{day}｜{what}　{_duration_label(video['duration_sec'])}{note}", video["video_id"]))
    rows.sort(key=lambda row: row[0], reverse=True)
    return [(label, video_id) for _date, label, video_id in rows]


_titles_fetch_started = False


def fetch_missing_titles_in_background() -> None:
    """Once per run, look up stream titles for older downloads (metadata only)."""
    global _titles_fetch_started
    if _titles_fetch_started:
        return
    _titles_fetch_started = True
    from moment_retrieval import source_titles

    names = [str(video["name"] or "") for video in library_videos()]
    threading.Thread(target=source_titles.fetch_missing, args=(names,), daemon=True).start()


def refresh_library():
    fetch_missing_titles_in_background()
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
    _write_ui_settings({"min_sec": min_sec, "max_sec": max_sec, "layout": layout, "effort": effort,
                        "finish": bool(finish),
                        "upload": upload if upload in {"private", "unlisted", "none"} else "private"})
    return "保存しました。次回からこの設定で開きます。"


def _write_ui_settings(updates: dict) -> None:
    """Merge into the saved defaults, so saving one part keeps the others (e.g. the last AI)."""
    path = _ui_settings_path()
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    saved = {**(saved if isinstance(saved, dict) else {}), **updates}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")


def _saved_agent() -> tuple[str, str]:
    """The AI and model chosen last time, or Codex with its default model."""
    try:
        saved = json.loads(_ui_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    agent = saved.get("agent") if isinstance(saved, dict) else None
    if agent not in agent_runner.PIPELINE_AGENTS:
        return "codex", default_model("codex")
    model = saved.get("model")
    try:
        model, _effort = agent_runner.validate_model(agent, model if isinstance(model, str) else "", "")
    except agent_runner.AgentError:
        model = default_model(agent)
    return agent, model


JSDRAW_DEFAULT_MODEL = "fable"
# The cost depends on the route (login CLI or API), so the note under the box states it, not these labels.
JSDRAW_MODEL_CHOICES = [("Fable 5.2", "fable"), ("Opus 5.5", "opus"), ("Sonnet 5.5", "sonnet")]


def _saved_jsdraw() -> tuple[bool, str]:
    """Whether the JS-drawn Short was on last time, and its drawing model."""
    try:
        saved = json.loads(_ui_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    model = saved.get("jsdraw_model")
    return (saved.get("jsdraw") is True,
            model if model in {value for _label, value in JSDRAW_MODEL_CHOICES} else JSDRAW_DEFAULT_MODEL)


def remember_jsdraw(enabled: bool, model: str) -> None:
    if model not in {value for _label, value in JSDRAW_MODEL_CHOICES}:
        model = JSDRAW_DEFAULT_MODEL
    try:
        _write_ui_settings({"jsdraw": bool(enabled), "jsdraw_model": model})
    except OSError:
        pass


JSDRAW_DEFAULT_COUNT = 3


def _clamp_jsdraw_count(count, clip_count=10) -> int:
    """1 <= JS本数 <= 本数 (a bad value falls back to the default)."""
    try:
        clip_count = max(1, min(10, int(float(clip_count))))
    except (TypeError, ValueError):
        clip_count = 10
    try:
        count = int(float(count))
    except (TypeError, ValueError):
        count = JSDRAW_DEFAULT_COUNT
    return max(1, min(count, clip_count))


def _saved_jsdraw_count() -> int:
    try:
        saved = json.loads(_ui_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return _clamp_jsdraw_count((saved if isinstance(saved, dict) else {}).get("jsdraw_count", JSDRAW_DEFAULT_COUNT))


def remember_jsdraw_count(count, clip_count=10) -> None:
    try:
        _write_ui_settings({"jsdraw_count": _clamp_jsdraw_count(count, clip_count)})
    except OSError:
        pass


def jsdraw_count_update(enabled: bool, count, clip_count):
    """「JS本数」: shown only while the box is checked, never above 本数."""
    return gr.update(visible=bool(enabled), maximum=_clamp_jsdraw_count(clip_count),
                     value=_clamp_jsdraw_count(count, clip_count))


def apply_jsdraw_count_settings(clip_count=3):
    enabled = _saved_jsdraw()[0] and _jsdraw_available(_saved_agent()[0])
    return jsdraw_count_update(enabled, _saved_jsdraw_count(), clip_count)


def _jsdraw_available(agent: str) -> bool:
    from moment_retrieval import jsdraw_pipeline

    return jsdraw_pipeline.available_for(agent)


def jsdraw_controls(agent: str, enabled: bool):
    """(checkbox, 描画モデル, note) for the chosen highlight AI: the drawing follows that AI.

    The note says how the Short will be drawn; the model choice is hidden for Codex (it uses
    the Codex model picked above) and the box is unavailable for the local AI without a key.
    """
    from moment_retrieval import jsdraw_pipeline

    available = jsdraw_pipeline.available_for(agent)
    enabled = bool(enabled) and available
    note = jsdraw_pipeline.note_for(agent)
    return (gr.update(value=enabled, interactive=available),
            gr.update(visible=enabled and jsdraw_pipeline.backend_for(agent) != "codex"),
            gr.update(value=f'<small style="color: var(--body-text-color-subdued)">{html.escape(note)}</small>',
                      visible=enabled or not available))


def apply_jsdraw_settings():
    enabled, model = _saved_jsdraw()
    box, radio, note = jsdraw_controls(_saved_agent()[0], enabled)
    return box, {**radio, "value": model}, note


def on_jsdraw_change(enabled: bool, agent: str = "claude"):
    _box, radio, note = jsdraw_controls(agent, enabled)
    return radio, note


def _require_jsdraw_ready(enabled: bool, agent: str = "api") -> None:
    """Only drawing through the Claude API needs a saved key; the login CLIs draw on their plan."""
    if not enabled:
        return
    from moment_retrieval import jsdraw_pipeline

    if not jsdraw_pipeline.available_for(agent):
        raise gr.Error(jsdraw_pipeline.NO_KEY_MESSAGE)


def remember_agent(agent: str, model: str) -> None:
    """Keep the AI and model picked on the main tab as next time's default."""
    if agent not in agent_runner.PIPELINE_AGENTS:
        return
    try:
        model, _effort = agent_runner.validate_model(agent, model, "")
    except agent_runner.AgentError:
        return
    try:
        _write_ui_settings({"agent": agent, "model": model})
    except OSError:
        pass  # a read-only folder only loses the remembered choice


def apply_agent_settings():
    agent, model = _saved_agent()
    choices = model_choices(agent)
    if model not in {value for _label, value in choices}:
        # e.g. an Ollama model that was removed; keep the saved name selectable rather than drop it silently
        choices = choices + [(model, model)] if model and agent == "local" else choices
        model = model if model and agent == "local" else default_model(agent)
    return (gr.update(value=agent), gr.update(choices=choices, value=model),
            gr.update(visible=agent == "codex"))


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


def _require_agent_ready(agent: str) -> None:
    """Stop before queueing when the chosen CLI is missing or signed out (its jobs would fail later)."""
    if agent == agent_runner.API_AGENT:
        from moment_retrieval import oneshot_selector

        if not oneshot_selector.has_key():
            raise gr.Error(oneshot_selector.NO_KEY_MESSAGE)
        return
    checks = {"codex": ai_setup.codex_status, "claude": ai_setup.claude_status}
    if agent not in checks:
        return
    status = checks[agent]()
    if status["ready"]:
        return
    label, button = {"codex": ("Codex", "Codex"), "claude": ("Claude Code", "Claude")}[agent]
    if not status["installed"]:
        raise gr.Error(f"{label} がインストールされていません。「設定」タブの「{button}」ボタンで準備してください。")
    raise gr.Error(
        f"{label} にログインしていません（ログインの期限切れを含む）。「設定」タブの「{button}」ボタンを押し、"
        "開いたウィンドウとブラウザでログインし直してから、もう一度開始してください。"
    )


def auto_submit(mode: str, sources: str, library_selection, agent: str, model: str, clip_count, effort: str,
                min_sec, max_sec, layout: str, upload: bool, focus: str = "", finish: bool = False,
                preload_transcript: bool = True, jsdraw: bool = False, jsdraw_model: str = JSDRAW_DEFAULT_MODEL,
                jsdraw_count=JSDRAW_DEFAULT_COUNT):
    # Only the input that is shown counts; a leftover value in the hidden one is ignored.
    if mode == "library":
        lines = [LIBRARY_PREFIX + video_id for video_id in (library_selection or [])]
        if not lines:
            raise gr.Error("文字起こし済みの動画を選んでください。")
    else:
        lines = [line.strip() for line in str(sources or "").splitlines() if line.strip()]
        if not lines:
            if re.match(r"\s*https?://", str(focus or "")):
                raise gr.Error("URLが「探したい場面」の欄に入っています。上の「動画のURL」の欄に入れ直してください。")
            raise gr.Error("動画のURLを入力してください。")
    _require_agent_ready(agent)
    _require_jsdraw_ready(jsdraw, agent)
    submitted = []
    for line in lines[:10]:
        try:
            job = _auto_pipeline().submit(
                line, agent, clip_count=int(clip_count), min_duration_sec=float(min_sec),
                max_duration_sec=float(max_sec), layout=layout, **_upload_args(upload),
                model=model or "", effort=(effort or "") if agent == "codex" else "", focus=focus or "",
                finish=bool(finish), preload_transcript=bool(preload_transcript),
                jsdraw=bool(jsdraw), jsdraw_model=jsdraw_model or JSDRAW_DEFAULT_MODEL,
                jsdraw_count=_clamp_jsdraw_count(jsdraw_count, clip_count),
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
                        min_sec, max_sec, layout: str, upload: bool, focus: str = "", finish: bool = False,
                        preload_transcript: bool = True, jsdraw: bool = False,
                        jsdraw_model: str = JSDRAW_DEFAULT_MODEL, jsdraw_count=JSDRAW_DEFAULT_COUNT):
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
                preload_transcript=bool(preload_transcript),
                jsdraw=bool(jsdraw) and bool(start_clipping), jsdraw_model=jsdraw_model or JSDRAW_DEFAULT_MODEL,
                jsdraw_count=_clamp_jsdraw_count(jsdraw_count, clip_count),
            )
        except PipelineError as exc:
            log += f"\n元動画のダウンロードを開始できませんでした: {exc}"
            continue
        what = "続けて切り抜き・アップロードまで進めます" if start_clipping else "切り抜きは行いません"
        log += (f"\n元動画のダウンロードを開始しました（{job.job_id}）。"
                f"共有された文字起こしに関連付けます（{what}）。")
    choices = library_choices()
    return log, gr.update(choices=choices), gr.update(choices=choices)


def _sync_job_uploads(video_id: str, receipt: dict) -> None:
    """Keep the in-memory job (and its file) in step with a privacy change.

    Only finished jobs whose saved state matches memory are written back, so a job
    another process (e.g. the daily batch) is working on is never overwritten.
    """
    from moment_retrieval.auto_pipeline import TERMINAL

    pipeline = _auto_pipeline()
    with pipeline.lock:
        for job in pipeline.jobs.values():
            for upload in job.uploads:
                if upload.get("video_id") != video_id:
                    continue
                upload["privacy_status"] = receipt.get("privacy_status")
                path = config.CACHE_ROOT / "auto_jobs" / f"{job.job_id}.json"
                try:
                    saved_state = json.loads(path.read_text(encoding="utf-8")).get("state")
                except (OSError, ValueError):
                    saved_state = None
                if job.state in TERMINAL and saved_state == job.state:
                    pipeline._save(job)


def upload_clips(paths, privacy: str):
    """Upload the checked clips one by one, streaming progress; uploaded ones are skipped."""
    from moment_retrieval import made_clips

    picked = [str(item) for item in (paths or []) if item]
    if not picked:
        raise gr.Error("アップロードする動画にチェックを入れてください。")
    privacy = "unlisted" if privacy == "unlisted" else "private"  # the YouTube setting; "none" -> private
    log: list[str] = []
    done = 0
    for number, path in enumerate(picked, start=1):
        log.append(f"- [{number}/{len(picked)}] {html.escape(Path(path).name)}")
        yield "\n".join(log), gr.update()
        try:
            for message, receipt in made_clips.upload(path, privacy):
                if not message:
                    continue
                line = "  - " + html.escape(message.strip())
                if message.startswith("  アップロード中") and "アップロード中" in log[-1]:
                    log[-1] = line
                else:
                    log.append(line)
                if receipt and not message.startswith("アップロード済み"):
                    done += 1
                yield "\n".join(log), gr.update()
        except youtube_upload.UploadError as exc:
            log.append(f"  - 失敗: {html.escape(str(exc))}")
        except Exception as exc:
            log.append(f"  - 失敗: {type(exc).__name__}: {html.escape(str(exc))[:200]}")
        yield "\n".join(log), cards_signature()
    if done:
        gr.Info(f"{done}本をアップロードしました。")


def publish_clips_from_ui(paths, interval_hours=None):
    """Schedule the checked clips on YouTube to go public one at a time, ``interval_hours`` apart.

    The first one is published by the app right away; the rest get a YouTube publish time
    (private until then), so they go public even while the app or the PC is off."""
    from moment_retrieval import publish_queue

    picked = [str(item) for item in (paths or []) if item]
    if not picked:
        raise gr.Error("公開する動画にチェックを入れてください。")
    try:
        hours = float(interval_hours) if interval_hours is not None and float(interval_hours) > 0 else None
    except (TypeError, ValueError):
        hours = None
    hours = hours or publish_queue.DEFAULT_INTERVAL_HOURS
    added, skipped = publish_queue.enqueue(picked, interval_hours=hours)
    scheduled, kept = publish_queue.schedule_waiting(paths=[item["path"] for item in added]) if added else ([], [])
    lines = []
    if added:
        first = datetime.fromtimestamp(added[0]["not_before"])
        lines.append(f"- {len(added)}本を公開待ちに入れました。{hours:g}時間おきに1本ずつ公開します"
                     f"（最初は{first:%m/%d %H:%M}ごろ、全部終わるまで約{max(0, len(added) - 1) * hours:g}時間）。")
        if scheduled:
            lines.append(f"- {len(scheduled)}本はYouTubeに公開予約しました。予定時刻まで非公開になり、"
                         "アプリやPCを止めていても予定時刻に公開されます。")
        now_count = len(added) - len(scheduled) - len(kept)
        if now_count:
            lines.append(f"- {now_count}本はすぐ公開の順番なので、アプリがこのまま公開します。")
        lines += [f"- YouTubeに予約できず、アプリを開いている間に公開します: {html.escape(item['title'])}"
                  f"（{html.escape(item['message'])}）" for item in kept]
        gr.Info(f"{len(added)}本を公開待ちに入れました。")
    lines += [f"- 公開待ちに入れませんでした: {html.escape(name)}: {html.escape(reason)}" for name, reason in skipped]
    return "\n".join(lines), cards_signature()


def publish_queue_markdown() -> str:
    from moment_retrieval import publish_queue

    info = publish_queue.summary()
    if not info["remaining"] and not info["recent"]:
        return "公開待ちの動画はありません。"
    lines = []
    if info["remaining"]:
        by_app = sum(1 for item in info["pending"] if item.get("state") != "scheduled")
        note = ("YouTubeに公開予約済みなので、アプリを閉じていても公開されます" if not by_app else
                f"うち{by_app}本はYouTubeに予約しておらず、アプリを開いている間だけ進みます")
        lines.append(f"**公開待ち 残り{info['remaining']}本**・次の公開予定 "
                     f"{datetime.fromtimestamp(info['next_at']):%m/%d %H:%M}ごろ（{note}）")
        for item in info["pending"]:
            label = publish_queue.STATE_LABELS.get(item.get("state"), item.get("state"))
            lines.append(f"- {label} {datetime.fromtimestamp(item['not_before']):%m/%d %H:%M} "
                         f"{html.escape(item['title'])}")
    else:
        lines.append("公開待ちの動画はありません。")
    if info["recent"]:
        lines.append("\n最近の結果:")
        for item in info["recent"]:
            label = publish_queue.STATE_LABELS.get(item.get("state"), item.get("state"))
            when = f"{datetime.fromtimestamp(item['finished_at']):%m/%d %H:%M}" if item.get("finished_at") else ""
            note = f" — {html.escape(item['message'])}" if item.get("message") else ""
            lines.append(f"- {label} {when} {html.escape(item['title'])}{note}")
    return "\n".join(lines)


def publish_queue_cancel():
    """Cancel the waiting clips; ones scheduled on YouTube get their publish time removed."""
    from moment_retrieval import publish_queue

    count, failed = publish_queue.cancel_waiting()
    lines = [f"- 公開待ちの{count}本を取り消しました（YouTubeの公開予約も外しました）。"]
    lines += [f"- 取り消せませんでした: {html.escape(title)}: {html.escape(reason)}" for title, reason in failed]
    return "\n".join(lines), publish_queue_markdown(), cards_signature()


def publish_queue_move_to_youtube():
    """Register clips still waiting in the app's queue as YouTube publish times."""
    from moment_retrieval import publish_queue

    scheduled, kept = publish_queue.schedule_waiting()
    lines = [f"- {len(scheduled)}本をYouTubeに公開予約しました（予定時刻まで非公開）。" if scheduled else
             "- YouTubeに移す公開待ちはありません（すぐ公開の順番の1本はアプリが公開します）。"]
    lines += [f"- 予約できませんでした: {html.escape(item['title'])}（{html.escape(item['message'])}）" for item in kept]
    return "\n".join(lines), publish_queue_markdown(), cards_signature()


def change_clip_privacy(paths, privacy: str):
    """Set the checked uploaded clips to one privacy; failures are listed instead of stopping the rest."""
    from moment_retrieval import made_clips

    picked = [str(item) for item in (paths or []) if item]
    if not picked:
        raise gr.Error("公開設定を変える動画にチェックを入れてください。")
    if privacy not in youtube_upload.PRIVACY_CHOICES:
        raise gr.Error("変更先の公開設定を選んでください。")
    label = youtube_upload.PRIVACY_LABELS.get(privacy, privacy)
    done, failed = [], []
    for path in picked:
        try:
            receipt = made_clips.change_privacy(path, privacy)
        except youtube_upload.UploadError as exc:
            failed.append((Path(path).name, str(exc)))
            continue
        except Exception as exc:
            failed.append((Path(path).name, f"{type(exc).__name__}: {exc}"[:200]))
            continue
        _sync_job_uploads(str(receipt["video_id"]), receipt)
        done.append(receipt["watch_url"])
    if done:
        gr.Info(f"{len(done)}本を{label}にしました。")
    lines = [f"- {label}にしました: {url}" for url in done]
    reasons = {reason for _name, reason in failed}
    if failed and len(reasons) == 1:  # one reason is enough when all failed the same way
        lines.append(f"- {len(failed)}本を{label}にできませんでした: {html.escape(reasons.pop())}")
    else:
        lines += [f"- {label}にできませんでした: {html.escape(name)}: {html.escape(reason)}" for name, reason in failed]
    return "\n".join(lines), cards_signature()


def clip_publish_status(receipt: dict | None, item: dict | None) -> str:
    """The clip list's state badge: the publish queue's state first, then YouTube's."""
    from moment_retrieval import made_clips

    label = made_clips.status_label(receipt)
    public = bool(receipt) and receipt.get("privacy_status") == "public"
    state = (item or {}).get("state")
    if not public and state == "queued":
        return f"**[公開待ち {datetime.fromtimestamp(float(item['not_before'])):%m/%d %H:%M}]**（今は{label}）"
    if not public and state == "scheduled":
        return f"**[公開予約 {datetime.fromtimestamp(float(item['not_before'])):%m/%d %H:%M}]**"
    if not public and state == "running":
        return f"**[公開中…]**（今は{label}）"
    if not public and state == "failed":
        reason = html.escape(str(item.get("message") or "")[:80])
        return f"**[公開に失敗]**（今は{label}{'・' + reason if reason else ''}）"
    scheduled = made_clips.scheduled_publish_at(receipt)
    if scheduled is not None:
        return f"**[公開予約 {scheduled:%m/%d %H:%M}]**"
    if public:
        return f"**[{label}]**"
    return f"[{label}]"


def refresh_clip_statuses():
    from moment_retrieval import made_clips

    paths = [str(clip.path) for card in clip_cards() for clip in card["clips"] if clip.receipt]
    try:
        count = made_clips.refresh_statuses(paths) if paths else 0
    except youtube_upload.UploadError as exc:
        return f"YouTubeの状態を確認できませんでした: {html.escape(str(exc))}", gr.update()
    except Exception as exc:
        return f"YouTubeの状態を確認できませんでした（{type(exc).__name__}）。", gr.update()
    message = f"{count}本の公開設定と処理状況をYouTubeから取り直しました。" if count else "投稿済みの動画がありません。"
    return message, cards_signature()


def refresh_clip_statuses_quietly():
    """On page load: bring the labels in line with YouTube (changes made in Studio etc.), silently."""
    _message, signature = refresh_clip_statuses()
    return signature


def repost_list(max_views, min_days):
    from moment_retrieval import repost

    try:
        found = repost.candidates(max_views=int(max_views or repost.DEFAULT_MAX_VIEWS),
                                  min_age_days=float(min_days or repost.DEFAULT_MIN_AGE_DAYS))
    except Exception as exc:
        return gr.update(choices=[], value=[]), f"一覧を作れませんでした（{type(exc).__name__}）。"
    choices = [(f"{item['title']}（{item['views']}回・{datetime.fromtimestamp(item['published_at']):%m/%d}投稿）",
                item["video_id"]) for item in found]
    message = f"{len(found)}本あります。再投稿する動画を選んでください。" if found else "当てはまる動画はありません。"
    return gr.update(choices=choices, value=[]), message


def repost_queue_markdown() -> str:
    from moment_retrieval import repost

    items = [item for item in repost.load_queue() if item.get("state") != "cancelled"]
    if not items:
        return "再投稿の予約はありません。"
    lines = []
    for item in sorted(items, key=lambda item: item["not_before"]):
        when = (f"{datetime.fromtimestamp(item['not_before']):%m/%d %H:%M}以降"
                if item.get("state") == "queued" else "")
        note = f" — {html.escape(item['message'])}" if item.get("message") else ""
        lines.append(f"- {repost.STATE_LABELS.get(item.get('state'), item.get('state'))} {when} "
                     f"{html.escape(item['title'])}{note}")
    return "\n".join(lines)


def repost_enqueue(video_ids, interval_hours, max_views, min_days):
    from moment_retrieval import repost

    if not video_ids:
        return "動画を選んでください。", repost_queue_markdown(), gr.update()
    wanted = set(video_ids)
    found = [item for item in repost.candidates(max_views=int(max_views or repost.DEFAULT_MAX_VIEWS),
                                                min_age_days=float(min_days or repost.DEFAULT_MIN_AGE_DAYS))
             if item["video_id"] in wanted]
    added = repost.enqueue(found, interval_hours=float(interval_hours or repost.DEFAULT_INTERVAL_HOURS))
    hours = float(interval_hours or repost.DEFAULT_INTERVAL_HOURS)
    return (f"{len(added)}本を予約しました。アプリを開いている間に、{hours:g}時間おきに1本ずつ再投稿します"
            f"（全部終わるまで約{max(0, len(added) - 1) * hours:g}時間）。",
            repost_queue_markdown(), gr.update(choices=[], value=[]))


def repost_enqueue_all(interval_hours, max_views, min_days):
    """Queue every clip the current conditions list (the list need not be built first)."""
    from moment_retrieval import repost

    found = repost.candidates(max_views=int(max_views or repost.DEFAULT_MAX_VIEWS),
                              min_age_days=float(min_days or repost.DEFAULT_MIN_AGE_DAYS))
    if not found:
        return "当てはまる動画はありません。", repost_queue_markdown(), gr.update()
    return repost_enqueue([item["video_id"] for item in found], interval_hours, max_views, min_days)


def repost_cancel():
    from moment_retrieval import repost

    return f"待ちの{repost.cancel_waiting()}本を取り消しました。", repost_queue_markdown()


def refresh_everything():
    """The top "更新" button: re-read jobs (incl. the daily batch's), clips, their YouTube state and the video list."""
    fetch_missing_titles_in_background()
    refresh_clip_statuses()
    choices = library_choices()
    return (summary_status(), *auto_jobs_view(), gr.update(choices=choices), gr.update(choices=choices))


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
        refresh_all_btn = gr.Button("最新の状態に更新", scale=0, min_width=190)
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
                        choices=[], multiselect=True, scale=5, elem_id="auto-library",
                        label="文字起こし済みの動画（複数選択可・編集用CUTで処理した動画も含む）",
                    )
                    auto_library_refresh = gr.Button("一覧を更新", scale=1)
            with gr.Row():
                auto_agent = gr.Radio(
                    choices=[("Codex", "codex"), ("Claude Code", "claude"), ("ローカルAI", "local"),
                             (API_AGENT_CHOICE, agent_runner.API_AGENT)], value="codex",
                    label="見どころを選ぶAI", scale=5,
                )
                auto_model = gr.Dropdown(
                    choices=model_choices("codex"), value=agent_runner.DEFAULT_CODEX_MODEL,
                    label="モデル", scale=2,
                )
                auto_clip_count = gr.Slider(1, 10, value=3, step=1, label="本数", scale=2)
            # One line: the box, the drawing model (only when checked, not for Codex) and a short note.
            with gr.Row(equal_height=True, elem_id="auto-jsdraw-row"):
                auto_jsdraw = gr.Checkbox(
                    value=False, label="JS描画ショート", container=False, scale=1, min_width=170,
                )
                auto_jsdraw_model = gr.Radio(
                    choices=JSDRAW_MODEL_CHOICES, value=JSDRAW_DEFAULT_MODEL, show_label=False, container=False,
                    visible="hidden", scale=4, min_width=360,
                )
                # How many of the picks get JS drawing (the AI's js_score decides which).
                auto_jsdraw_count = gr.Number(
                    value=JSDRAW_DEFAULT_COUNT, minimum=1, maximum=3, step=1, precision=0, label="JS本数",
                    visible=False, scale=0, min_width=140, elem_id="auto-jsdraw-count",
                )
            auto_jsdraw_note = gr.Markdown("", visible=False, container=False, elem_id="auto-jsdraw-note")
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
                    auto_preload = gr.Checkbox(
                        value=True, label="文字起こしを最初に渡す（往復を減らす・Claude Code/Codex）",
                    )
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
            with gr.Accordion("公開待ち（チェックした動画をYouTubeに公開予約し、1本ずつ間隔をあけて公開します）", open=True):
                with gr.Row():
                    publish_interval = gr.Number(value=1, minimum=0.1, label="公開の間隔（時間）", scale=1)
                    publish_queue_refresh_btn = gr.Button("公開待ちを更新", size="sm", scale=1)
                    publish_queue_move_btn = gr.Button("アプリの公開待ちをYouTubeの予約に移す", size="sm", scale=1)
                    publish_queue_cancel_btn = gr.Button("公開待ちを取り消す", size="sm", scale=1)
                publish_queue_md = gr.Markdown(publish_queue_markdown)
            publish_queue_refresh_btn.click(publish_queue_markdown, outputs=[publish_queue_md])
            publish_queue_move_btn.click(publish_queue_move_to_youtube,
                                         outputs=[publish_result_md, publish_queue_md, auto_uploads_sig],
                                         concurrency_id="youtube-publish", concurrency_limit=1)
            publish_queue_cancel_btn.click(publish_queue_cancel,
                                           outputs=[publish_result_md, publish_queue_md, auto_uploads_sig],
                                           concurrency_id="youtube-publish", concurrency_limit=1)

            @gr.render(inputs=[auto_uploads_sig])
            def render_clip_cards(signature):
                from moment_retrieval import made_clips, publish_queue

                queued = publish_queue.latest_by_path()

                cards = clip_cards()
                if not cards:
                    return
                with gr.Row():
                    gr.Markdown("**作った動画**（未投稿はチェックしてアップロード、アップロード済みはリンクから確認し、チェックして公開できます）",
                                scale=4)
                    status_btn = gr.Button("YouTubeの状態を確認", scale=1, size="sm")
                status_btn.click(refresh_clip_statuses, outputs=[publish_result_md, auto_uploads_sig],
                                 concurrency_id="youtube-publish", concurrency_limit=1)
                for card in cards:
                    job_id, clips = card["job_id"], card["clips"]
                    with gr.Group():
                        how = "（書き出しのみ・毎日の自動処理など）" if card["batch"] else ""
                        lines = [f"**{html.escape(job_id)}**{how} — 元動画: {html.escape(card['source'])}"]
                        waiting_paths = set()
                        for clip in clips:
                            item = queued.get(publish_queue._key(clip.path))
                            status = clip_publish_status(clip.receipt, item)
                            if item is not None and item.get("state") in publish_queue.PENDING:
                                waiting_paths.add(str(clip.path))
                            if clip.receipt:
                                video_id = html.escape(str(clip.receipt["video_id"]))
                                links = (f"[ショートで見る](https://www.youtube.com/shorts/{video_id})・"
                                         f"[Studioで編集](https://studio.youtube.com/video/{video_id}/edit)")
                            else:
                                links = (f'<a href="/gradio_api/file={quote(str(clip.path))}" target="_blank">'
                                         "再生して確認</a>")
                            lines.append(f"- {status} {html.escape(clip.title)} — {links}")
                        gr.Markdown("\n".join(lines), padding=True)
                        unposted = [(clip.title, str(clip.path)) for clip in clips if not clip.receipt]
                        if unposted:
                            # Uploaded with the 切り抜き tab's YouTube setting, like a manual run.
                            to_upload = gr.CheckboxGroup(choices=unposted, label="アップロードする動画")
                            with gr.Row():
                                upload_picked = gr.Button("チェックした動画をアップロード")
                                upload_all = gr.Button("このジョブの全部をアップロード")
                            upload_picked.click(
                                upload_clips, inputs=[to_upload, auto_upload],
                                outputs=[publish_result_md, auto_uploads_sig],
                                concurrency_id="youtube-upload", concurrency_limit=1,
                            )
                            upload_all.click(
                                partial(upload_clips, tuple(path for _title, path in unposted)),
                                inputs=[auto_upload], outputs=[publish_result_md, auto_uploads_sig],
                                concurrency_id="youtube-upload", concurrency_limit=1,
                            )
                        private = [(clip.title, str(clip.path)) for clip in clips
                                   if clip.receipt and clip.receipt.get("privacy_status") != "public"
                                   and not clip.receipt.get("missing_on_youtube")
                                   # Waiting in the app's queue or scheduled on YouTube: not offered again.
                                   and str(clip.path) not in waiting_paths
                                   and made_clips.scheduled_publish_at(clip.receipt) is None]
                        if private:
                            picked = gr.CheckboxGroup(choices=private, label="公開する動画")
                            with gr.Row():
                                publish_picked = gr.Button("チェックした動画を公開")
                                publish_all = gr.Button("このジョブの全部を公開")
                            publish_picked.click(
                                publish_clips_from_ui, inputs=[picked, publish_interval],
                                outputs=[publish_result_md, auto_uploads_sig],
                                concurrency_id="youtube-publish", concurrency_limit=1,
                            ).then(publish_queue_markdown, outputs=[publish_queue_md])
                            publish_all.click(
                                partial(publish_clips_from_ui, tuple(path for _title, path in private)),
                                inputs=[publish_interval], outputs=[publish_result_md, auto_uploads_sig],
                                concurrency_id="youtube-publish", concurrency_limit=1,
                            ).then(publish_queue_markdown, outputs=[publish_queue_md])

            with gr.Accordion("ジョブの履歴（ログ）", open=False):
                auto_history_md = gr.Markdown("まだジョブはありません。")
            with gr.Accordion("実行中のジョブを停止", open=False):
                with gr.Row():
                    auto_cancel_select = gr.Dropdown(choices=[], label="実行中のジョブ", scale=4)
                    auto_cancel_btn = gr.Button("停止", variant="stop", scale=1)
            with gr.Accordion("伸びていない動画の再投稿", open=False):
                gr.Markdown(
                    "投稿から日数が経っても再生回数が少ない動画を、旧動画を**非公開**にして（削除はしません）"
                    "投稿し直します。まとめて上げると表示されにくいので、1本ずつ間隔を空けます。"
                )
                with gr.Row():
                    repost_max_views = gr.Number(value=30, precision=0, label="再生回数がこれ未満")
                    repost_min_days = gr.Number(value=3, label="投稿から何日以上")
                    repost_interval = gr.Number(value=3, label="再投稿の間隔（時間）")
                repost_list_btn = gr.Button("一覧を作る")
                repost_status_md = gr.Markdown()
                repost_picked = gr.CheckboxGroup(choices=[], label="再投稿する動画")
                with gr.Row():
                    repost_go_btn = gr.Button("選んだ動画を再投稿の予約に入れる", variant="primary")
                    repost_all_btn = gr.Button("条件に当てはまる全部を再投稿の予約に入れる")
                    repost_cancel_btn = gr.Button("待ちの予約を取り消す")
                repost_queue_md = gr.Markdown(repost_queue_markdown)
                repost_list_btn.click(repost_list, inputs=[repost_max_views, repost_min_days],
                                      outputs=[repost_picked, repost_status_md])
                repost_go_btn.click(repost_enqueue,
                                    inputs=[repost_picked, repost_interval, repost_max_views, repost_min_days],
                                    outputs=[repost_status_md, repost_queue_md, repost_picked])
                repost_all_btn.click(repost_enqueue_all,
                                     inputs=[repost_interval, repost_max_views, repost_min_days],
                                     outputs=[repost_status_md, repost_queue_md, repost_picked])
                repost_cancel_btn.click(repost_cancel, outputs=[repost_status_md, repost_queue_md])
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

        with gr.Tab("コメント返信"):
            gr.Markdown(
                "自分のチャンネルの動画に付いた未返信のコメントへ、登録した定型文で自動返信します（承認なし）。"
                "自分のコメントや、自分が既に返信したコメントには返信しません。"
                "YouTubeのAPIにハートを付ける機能はないため、ハートはYouTube Studioで付けてください。"
                f"1回の確認で返信するのは1件まで、返信どうしの間隔は{youtube_auto_reply.MIN_GAP_MINUTES}分以上あけ、"
                "1日の上限を超えては返信しません。このパソコンで動かす場合はアプリを開いている間だけ動きます"
                "（パソコンを起動していなくても返信したい場合は、下の「GASで動かす」を使います）。"
            )
            reply_status_md = gr.Markdown("")
            with gr.Row():
                reply_connect_btn = gr.Button("返信用に連携", variant="primary")
                reply_disconnect_btn = gr.Button("返信用の連携を解除")
            gr.Markdown(
                "<small>返信にはコメント管理の許可（youtube.force-ssl）が必要なため、アップロード用とは別に連携します。"
                "OAuthクライアントは「設定」タブで読み込んだものを使います。</small>"
            )
            gr.Markdown("### 定型文")
            gr.Markdown(
                "<small>登録した中から毎回ランダムに選びます（直前と同じ文は避けます）。"
                "改行を含む文もそのまま返信します。{name} は相手の名前に置き換わります。</small>"
            )
            reply_templates_md = gr.Markdown("")
            reply_new_template = gr.Textbox(label="追加する定型文（改行できます）", lines=3,
                                            placeholder="コメントありがとうございます！")
            reply_add_btn = gr.Button("追加", variant="primary")
            with gr.Row():
                reply_remove_select = gr.Dropdown(choices=[], label="削除する定型文", scale=4)
                reply_remove_btn = gr.Button("削除", variant="stop", scale=1)
            gr.Markdown("### 返信の設定")
            reply_enabled = gr.Checkbox(value=False, label="自動返信を有効にする")
            reply_include_past = gr.Checkbox(
                value=True,
                label="過去の未返信コメントも対象にする（古いものから順に。オフにすると設定を初めて保存した後のコメントだけ）",
            )
            with gr.Row():
                reply_daily_limit = gr.Number(value=youtube_auto_reply.DEFAULT_DAILY_LIMIT, precision=0,
                                              minimum=1, maximum=youtube_auto_reply.MAX_DAILY_LIMIT,
                                              label="1日の返信上限（件）")
                reply_interval = gr.Number(value=youtube_auto_reply.DEFAULT_INTERVAL_MINUTES, precision=0,
                                           minimum=youtube_auto_reply.MIN_INTERVAL_MINUTES,
                                           label="確認の間隔（分）")
            with gr.Row():
                reply_save_btn = gr.Button("設定を保存", variant="primary")
                reply_run_btn = gr.Button("今すぐ1回実行")
                reply_refresh_btn = gr.Button("状態を更新")
            gr.Markdown("### パソコンを起動していなくても返信する（GAS）")
            gr.Markdown(
                "「GASで動かす」を押すと、あなたのGoogleアカウントに返信用のスクリプト（Google Apps Script）を作り、"
                "Googleのサーバー上で上と同じ設定のまま返信を続けます。返信には「返信用に連携」で選んだチャンネルの"
                "ログインを使うため、ブランドアカウントのチャンネルでも動きます（先に「返信用に連携」を済ませてください）。"
                "初回だけ、ブラウザでGoogleへのログインと許可を求められます（2回とも、ブランドアカウントではなく"
                "管理している個人のGoogleアカウントを選びます）。"
                "「Apps Script APIがオフ」と表示されたら、開いたページのスイッチを1回オンにしてから、もう一度押してください。"
                "定型文や設定を変えると、GASにも自動で反映します。"
            )
            with gr.Row():
                reply_gas_btn = gr.Button("GASで動かす（作成・更新）", variant="primary")
                reply_gas_stop_btn = gr.Button("このパソコンで動かすに戻す")
            reply_gas_md = gr.Markdown("")
            gr.Markdown("### 直近の返信ログ（このパソコンで返信した分）")
            reply_log_md = gr.Markdown("")

        with gr.Tab("分担（共有台帳）"):
            ledger_md = gr.Markdown("")
            gr.Markdown(
                "<small>同じ配信を何人かで切り抜くときに、ほかの人が着手した動画、作った・投稿した場面を"
                "自分の候補から外します。全員が同じ「送信用URL」に記録を送り、ほかの人の記録もそこから受け取ります"
                "（YouTube／TwitchのURLから取り込んだ動画が対象）。</small>"
            )
            ledger_name = gr.Textbox(label="自分の名前", placeholder="taro",
                                     info="一緒に使う人と重ならない名前にします（英数字がおすすめ）")
            ledger_url = gr.Textbox(label="送信用URL", placeholder="https://script.google.com/macros/s/…/exec",
                                    info="一緒に使う人から受け取ったURLを貼ります。自分が最初の1人なら、下の「送信用URLの作り方」で作ります")
            with gr.Row():
                ledger_start_btn = gr.Button("この設定で始める", variant="primary", scale=3)
                ledger_refresh_btn = gr.Button("状態を更新", scale=1, min_width=100)
                ledger_clear_btn = gr.Button("オフにする", scale=1, min_width=100)
            ledger_share_md = gr.Markdown("")
            with gr.Accordion("台帳を使う前に投稿した切り抜きも送る", open=False):
                gr.Markdown("<small>このPCでこれまでに投稿した切り抜きの範囲を、まとめて台帳に送ります。"
                            "URLから取り込んだ動画の分だけが送れます。何度押しても結果は変わりません。</small>")
                ledger_backfill_btn = gr.Button("これまでの投稿済みの切り抜きを送る")
                ledger_backfill_md = gr.Markdown("")
            with gr.Accordion("送信用URLの作り方（最初の1人だけ・5分ほど）", open=False):
                gr.Markdown(
                    "1. <a href='https://script.google.com/home/projects/create' target='_blank'>"
                    "Google Apps Script の新しいプロジェクト</a>を開きます（自分のGoogleアカウントで）。\n"
                    "2. 最初から入っている文字をすべて消し、下のコードを右上のボタンでコピーして貼り付け、**保存**します（Ctrl+S）。\n"
                    "3. 右上の「デプロイ」→「新しいデプロイ」→ 歯車から「ウェブアプリ」を選び、"
                    "「次のユーザーとして実行: **自分**」「アクセスできるユーザー: **全員**」にして「デプロイ」を押します。"
                    "初回は許可を求められるので、自分のアカウントで許可します"
                    "（「このアプリは確認されていません」と出たら「詳細」→「移動」）。\n"
                    "4. 表示された「ウェブアプリのURL」（…/exec で終わるもの）をコピーして、上の「送信用URL」に貼ります。\n\n"
                    "コードを直したときは「デプロイ」→「デプロイを管理」→ 鉛筆 →「新バージョン」でデプロイすると、"
                    "URLを変えずに反映できます（「新しいデプロイ」だとURLが変わります）。\n\n"
                    "記録は自分のGoogleドライブのスプレッドシート「CUT 共有台帳」にたまります。"
                    "URLを知っている人は誰でも書き込めるので、一緒に使う人にだけ渡してください。"
                )
                gr.Textbox(value=ledger_gas_source(), label="Apps Script に貼るコード（右上のボタンでコピー）",
                           interactive=False, lines=12, max_lines=12, buttons=["copy"])
            with gr.Accordion("URLの代わりに同期フォルダを使う（パソコン版Googleドライブ・OneDriveなど）", open=False):
                with gr.Row(equal_height=True):
                    ledger_folder = gr.Textbox(label="同期フォルダ", placeholder=r"G:\マイドライブ\CUT台帳", scale=4)
                    ledger_pick_btn = gr.Button("フォルダを選ぶ", scale=1, min_width=120)
                ledger_preview_md = gr.Markdown("")
                ledger_folder_btn = gr.Button("このフォルダで始める")
        with gr.Tab("JS描画ショート"):
            gr.Markdown(
                "配信の一区間から、絵をJavaScriptで描いた縦型ショート（約55秒）をClaude APIで作ります。"
                "話の要約・場面の絵・字幕・音声の切り詰めまで自動です。「設定」タブで保存した**Anthropic**のAPIキーを使い、"
                "費用はキーの持ち主にかかります（1本 Fable 約0.5ドル／Sonnet 約0.1ドル。半額のBatch処理で、1本10〜15分ほど）。"
            )
            with gr.Row():
                jsdraw_video = gr.Dropdown(choices=[], label="動画（文字起こし済み・このPCにあるもの）", scale=5)
                jsdraw_reload = gr.Button("一覧を更新", scale=1, min_width=100)
            with gr.Row():
                jsdraw_start = gr.Textbox(label="区間の始め（元配信の時刻）", placeholder="0:38:50")
                jsdraw_end = gr.Textbox(label="区間の終わり", placeholder="0:40:40",
                                        info="1〜5分の区間から、話の流れが伝わる約55秒をAIが選びます")
            jsdraw_title = gr.Textbox(label="題材（任意）", placeholder="借金ルート突入")
            jsdraw_story = gr.Textbox(label="話のあらすじ（任意）", lines=2,
                                      placeholder="空欄ならAIが文字起こしから流れを読み取ります")
            jsdraw_model = gr.Radio(
                choices=[(label, key) for key, label in jsdraw_short_labels().items()],
                value="fable", label="モデル",
            )
            jsdraw_btn = gr.Button("作る", variant="primary")
            jsdraw_log = gr.Markdown("")
            jsdraw_out = gr.Video(label="できた動画", height=480)
            jsdraw_reload.click(lambda: gr.update(choices=jsdraw_video_choices()), outputs=[jsdraw_video])
            jsdraw_btn.click(jsdraw_run, inputs=[jsdraw_video, jsdraw_start, jsdraw_end, jsdraw_title,
                                                 jsdraw_story, jsdraw_model],
                             outputs=[jsdraw_log, jsdraw_out])
            demo.load(lambda: gr.update(choices=jsdraw_video_choices()), outputs=[jsdraw_video])
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
            with gr.Accordion("APIキーで使う（ログインの代わり）", open=False):
                gr.Markdown(
                    "<small>ChatGPTやClaudeのアカウントでログインする代わりに、自分のAPIキーで"
                    "Codex／Claude Codeを動かせます（CLIのインストールは必要、ログインは不要）。"
                    "Claude Code にキーを使うのは、下の「Claude Code もAPIキーで動かす」をオンにしたときだけです。"
                    "キーはこのパソコンの <code>data/api_keys.json</code> にだけ保存され、gitには入りません。"
                    "保存したキーは環境変数（ANTHROPIC_API_KEY・CODEX_API_KEY・OPENAI_API_KEY）より優先され、"
                    "利用料金はキーの持ち主のAPIアカウントにかかります。</small>"
                )
                api_key_widgets = []
                for provider, item in api_keys.PROVIDERS.items():
                    key_status = gr.Markdown("")
                    with gr.Row():
                        key_box = gr.Textbox(label=f"{item['label']} のAPIキー", type="password",
                                             placeholder="貼り付けて「保存」（保存済みのキーは表示しません）",
                                             scale=4)
                        key_save = gr.Button("保存", variant="primary", scale=1, min_width=80)
                        key_test = gr.Button("接続テスト", scale=1, min_width=80)
                        key_delete = gr.Button("削除", scale=1, min_width=80)
                    api_key_widgets.append((provider, key_status, key_box, key_save, key_test, key_delete))
                claude_uses_key = gr.Checkbox(
                    value=False, label="Claude Code もAPIキーで動かす（従量課金）",
                    info="オフ（標準）のときは、Claude（Anthropic）のキーは「API 1回呼び出し」とJS描画だけに使い、"
                         "Claude Code はログインの枠で動かします。",
                )
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
    # The labels come from the saved receipts; refresh them from YouTube once per page load.
    demo.load(refresh_clip_statuses_quietly, outputs=[auto_uploads_sig], show_progress="hidden")
    demo.load(gcp_status, outputs=[gcp_status_md])
    auto_timer.tick(auto_jobs_view, outputs=auto_job_outputs, show_progress="hidden")
    auto_timer.tick(publish_queue_markdown, outputs=[publish_queue_md], show_progress="hidden")
    # .input (user picks only): the saved choice restored on load must not be reset to the default model.
    auto_agent.input(on_agent_change, inputs=[auto_agent], outputs=[auto_model, auto_effort]).then(
        remember_agent, inputs=[auto_agent, auto_model], show_progress="hidden")
    auto_model.input(remember_agent, inputs=[auto_agent, auto_model], show_progress="hidden")
    demo.load(apply_agent_settings, outputs=[auto_agent, auto_model, auto_effort])
    jsdraw_count_args = {"inputs": [auto_jsdraw, auto_jsdraw_count, auto_clip_count],
                         "outputs": [auto_jsdraw_count], "show_progress": "hidden"}
    auto_jsdraw.input(on_jsdraw_change, inputs=[auto_jsdraw, auto_agent],
                      outputs=[auto_jsdraw_model, auto_jsdraw_note]).then(
        remember_jsdraw, inputs=[auto_jsdraw, auto_jsdraw_model], show_progress="hidden").then(
        jsdraw_count_update, **jsdraw_count_args)
    auto_agent.input(jsdraw_controls, inputs=[auto_agent, auto_jsdraw],
                     outputs=[auto_jsdraw, auto_jsdraw_model, auto_jsdraw_note]).then(
        jsdraw_count_update, **jsdraw_count_args)
    auto_jsdraw_model.input(remember_jsdraw, inputs=[auto_jsdraw, auto_jsdraw_model], show_progress="hidden")
    demo.load(apply_jsdraw_settings, outputs=[auto_jsdraw, auto_jsdraw_model, auto_jsdraw_note])
    demo.load(apply_jsdraw_count_settings, inputs=[auto_clip_count], outputs=[auto_jsdraw_count])
    # Only what the user types is saved; lowering 本数 caps the shown value without forgetting it.
    auto_jsdraw_count.input(remember_jsdraw_count, inputs=[auto_jsdraw_count, auto_clip_count],
                            show_progress="hidden")
    auto_clip_count.change(jsdraw_count_update, **jsdraw_count_args)
    auto_client_file.upload(auto_install_client_secret, inputs=[auto_client_file], outputs=[auto_account_md])
    gcp_login_btn.click(gcp_login, outputs=[gcp_status_md], concurrency_id="gcp-setup")
    gcp_code_btn.click(gcp_submit_code, inputs=[gcp_code], outputs=[gcp_status_md, gcp_code],
                       concurrency_id="gcp-setup")
    gcp_create_btn.click(gcp_create_project, inputs=[gcp_confirm], outputs=[gcp_result_md],
                         concurrency_id="gcp-setup").then(gcp_status, outputs=[gcp_status_md])
    gcp_pages_btn.click(gcp_open_pages, outputs=[gcp_result_md])
    bundle_btn.click(bundle_client, outputs=[bundle_md])
    ledger_outputs = [ledger_url, ledger_folder, ledger_name, ledger_md, ledger_preview_md, ledger_share_md]
    demo.load(ledger_form_values, outputs=ledger_outputs)
    ledger_folder.input(ledger_preview, inputs=[ledger_folder], outputs=[ledger_preview_md], show_progress="hidden")
    ledger_pick_btn.click(ledger_pick_folder, inputs=[ledger_folder], outputs=[ledger_folder],
                          concurrency_id="ledger-dialog").then(
        ledger_preview, inputs=[ledger_folder], outputs=[ledger_preview_md])
    ledger_start_btn.click(ledger_start_url, inputs=[ledger_url, ledger_name], outputs=ledger_outputs)
    ledger_folder_btn.click(ledger_start_folder, inputs=[ledger_folder, ledger_name], outputs=ledger_outputs)
    ledger_refresh_btn.click(ledger_form_values, outputs=ledger_outputs)
    ledger_clear_btn.click(ledger_clear, outputs=ledger_outputs)
    ledger_backfill_btn.click(ledger_backfill, outputs=[ledger_backfill_md]).then(ledger_status, outputs=[ledger_md])
    demo.load(ai_setup_view, outputs=[ai_status_md])
    for provider, key_status, key_box, key_save, key_test, key_delete in api_key_widgets:
        demo.load(partial(api_key_status, provider), outputs=[key_status])
        key_save.click(partial(api_key_save, provider), inputs=[key_box],
                       outputs=[key_box, key_status, ai_status_md]).then(summary_status, outputs=[status_md])
        key_test.click(partial(api_key_test, provider), inputs=[key_box], outputs=[key_status])
        key_delete.click(partial(api_key_delete, provider), outputs=[key_box, key_status, ai_status_md]).then(
            summary_status, outputs=[status_md])
    demo.load(lambda: gr.update(value=api_keys.claude_cli_uses_key()), outputs=[claude_uses_key])
    claude_uses_key.input(save_claude_uses_key, inputs=[claude_uses_key], outputs=[ai_status_md]).then(
        summary_status, outputs=[status_md])
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
                auto_min_sec, auto_max_sec, auto_layout, auto_upload, auto_focus, auto_finish, auto_preload, auto_jsdraw,
                auto_jsdraw_model, auto_jsdraw_count],
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
    # Close the list after each pick (open it again to add more), and let the arrow open it.
    auto_library.select(None, None, None, js=_LIBRARY_CLOSE_JS)
    demo.load(None, None, None, js=_LIBRARY_ARROW_JS)
    share_export_btn.click(export_shared_index, inputs=[share_export_video, share_include_url],
                           outputs=[share_export_file, share_export_md], concurrency_id="library-share")
    share_import_btn.click(
        import_shared_index,
        inputs=[share_import_file, share_start, auto_agent, auto_model, auto_clip_count, auto_effort,
                auto_min_sec, auto_max_sec, auto_layout, auto_upload, auto_focus, auto_finish, auto_preload, auto_jsdraw,
                auto_jsdraw_model, auto_jsdraw_count],
        outputs=[share_import_log, auto_library, share_export_video],
        concurrency_id="library-share",
    ).then(auto_jobs_view, outputs=auto_job_outputs)
    auto_cancel_btn.click(auto_cancel, inputs=[auto_cancel_select], outputs=auto_job_outputs)
    refresh_all_btn.click(refresh_everything,
                          outputs=[status_md, *auto_job_outputs, auto_library, share_export_video])
    demo.load(reply_load_settings, outputs=[reply_enabled, reply_include_past, reply_daily_limit, reply_interval])
    demo.load(reply_templates_view, outputs=[reply_templates_md, reply_remove_select])
    demo.load(reply_status, outputs=[reply_status_md])
    demo.load(reply_log_markdown, outputs=[reply_log_md])
    reply_save_btn.click(reply_save_settings,
                         inputs=[reply_enabled, reply_include_past, reply_daily_limit, reply_interval],
                         outputs=[reply_status_md])
    reply_add_btn.click(reply_add_template, inputs=[reply_new_template],
                        outputs=[reply_status_md, reply_new_template, reply_templates_md, reply_remove_select])
    reply_remove_btn.click(reply_remove_template, inputs=[reply_remove_select],
                           outputs=[reply_status_md, reply_enabled, reply_templates_md, reply_remove_select])
    reply_connect_btn.click(reply_connect, outputs=[reply_status_md], concurrency_id="youtube-account")
    reply_disconnect_btn.click(reply_disconnect, outputs=[reply_status_md])
    reply_gas_btn.click(reply_gas_enable, outputs=[reply_gas_md], concurrency_id="youtube-account").then(
        reply_status, outputs=[reply_status_md])
    reply_gas_stop_btn.click(reply_gas_stop, outputs=[reply_gas_md]).then(reply_status, outputs=[reply_status_md])
    reply_run_btn.click(reply_run_now, outputs=[reply_status_md, reply_log_md], concurrency_id="youtube-reply")
    reply_refresh_btn.click(lambda: (reply_status(), reply_log_markdown()),
                            outputs=[reply_status_md, reply_log_md])


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
    _AUTO_REPLIER.start()
    from moment_retrieval import repost

    repost.start_worker()
    from moment_retrieval import publish_queue

    publish_queue.start_worker(on_published=lambda receipt: _sync_job_uploads(str(receipt["video_id"]), receipt))
    if args.browser:
        demo.launch(server_name="127.0.0.1", server_port=APP_PORT, inbrowser=True, css=APP_CSS,
                    allowed_paths=[str(config.ARTIFACT_ROOT)])
        return
    demo.launch(server_name="127.0.0.1", server_port=APP_PORT, inbrowser=False, css=APP_CSS,
                allowed_paths=[str(config.ARTIFACT_ROOT)], prevent_thread_lock=True)

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
