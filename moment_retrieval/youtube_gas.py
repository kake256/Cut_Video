"""Run the comment auto-reply on Google Apps Script so it works while this PC is off.

The app creates a GAS project in the user's own Google account through the Apps
Script API, writes ``gas/Code.js`` plus a generated ``Config.gs`` (the reply
settings) and ``Secret.gs`` (the channel login from 「返信用に連携」, so a
brand-account channel works even though GAS runs as the managing personal
account), and deploys it once as a web app (a status page).  The one-time consent
is given by running ``setup`` in the script editor, opened as the owning account
(see ``setup_url``); ``setup`` then registers its own time-driven trigger.  This
assumes the OAuth consent screen is in production; in testing mode the refresh
token expires after 7 days.  Later setting changes only rewrite the project files: installable triggers run
the latest saved code, so no new version is needed for them.

Two steps stay with the user because Google allows no other way: turning on the
Apps Script API in their own settings, and approving the script's access.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from . import config, youtube_auto_reply

SCOPES = [
    "https://www.googleapis.com/auth/script.projects",
    "https://www.googleapis.com/auth/script.deployments",
]
USER_SETTINGS_URL = "https://script.google.com/home/usersettings"
PROJECT_TITLE = "CUT コメント自動返信"
GAS_DIR = Path(__file__).resolve().parent / "gas"
_ENABLE_API_URL = re.compile(
    r"https://console\.(?:developers|cloud)\.google\.com/apis/api/script\.googleapis\.com/\S*?project=\d+")


class GasError(RuntimeError):
    def __init__(self, message: str, *, url: str = ""):
        super().__init__(message)
        self.url = url  # a page the user has to open to fix this


def token_path() -> Path:
    return config.LIBRARY_ROOT / "youtube_gas_token.json"


def state_path() -> Path:
    return config.LIBRARY_ROOT / "youtube_gas_reply.json"


def load_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def editor_url(script_id: str) -> str:
    return f"https://script.google.com/d/{script_id}/edit"


def setup_url(state: dict) -> str:
    """The script editor, opened as the account that owns it.

    The web app (``/exec``) always runs as the browser's default account, so it cannot be
    opened when several Google accounts are signed in and the owner is not the first.
    The editor honours the chosen account, and ``setup`` is the function it selects first.
    """
    target = editor_url(state["script_id"])
    email = state.get("owner_email")
    if not email:
        return target
    return target + "?" + urlencode({"authuser": email})


def config_source(settings: youtube_auto_reply.Settings) -> str:
    values = {
        "enabled": bool(settings.enabled and settings.run_on == "gas"),
        "templates": settings.templates,
        "daily_limit": settings.daily_limit,
        "interval_minutes": settings.interval_minutes,
        "include_past": settings.include_past,
        "since": settings.since,
        "min_gap_minutes": youtube_auto_reply.MIN_GAP_MINUTES,
        "max_reply_chars": youtube_auto_reply.MAX_REPLY_CHARS,
        "max_pages_new": youtube_auto_reply.MAX_THREAD_PAGES,
        "max_pages_past": youtube_auto_reply.MAX_THREAD_PAGES_PAST,
    }
    return ("// CUTの自動投稿アプリが書き込む設定です。変更はアプリの「コメント返信」タブで行ってください。\n"
            "var CUT_CONFIG = " + json.dumps(values, ensure_ascii=False, indent=2) + ";\n")


def secret_source() -> str:
    """The channel login from 「返信用に連携」, so GAS can reply as a brand-account channel too.

    GAS itself runs as the managing personal account; the refresh token lets it act as
    the channel the user picked.  Stored only in the user's own (private) script project.
    """
    try:
        token = json.loads(youtube_auto_reply.token_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        token = {}
    keys = ("client_id", "client_secret", "refresh_token")
    if not isinstance(token, dict) or not all(token.get(key) for key in keys):
        raise GasError("先に「返信用に連携」を押し、返信に使うチャンネルを選んでログインしてください。")
    values = {key: token[key] for key in keys}
    values["token_uri"] = token.get("token_uri") or "https://oauth2.googleapis.com/token"
    return ("// コメント返信に使うチャンネルのログイン情報です。このスクリプトを他人と共有しないでください。\n"
            "// アプリで「返信用に連携」をやり直すと書き換わります。\n"
            "var CUT_CREDENTIALS = " + json.dumps(values, indent=2) + ";\n")


def project_files(settings: youtube_auto_reply.Settings) -> list[dict]:
    return [
        {"name": "appsscript", "type": "JSON",
         "source": (GAS_DIR / "appsscript.json").read_text(encoding="utf-8")},
        {"name": "Code", "type": "SERVER_JS", "source": (GAS_DIR / "Code.js").read_text(encoding="utf-8")},
        {"name": "Config", "type": "SERVER_JS", "source": config_source(settings)},
        {"name": "Secret", "type": "SERVER_JS", "source": secret_source()},
    ]


def _service(service_factory: Callable | None, *, interactive: bool):
    if service_factory is not None:
        return service_factory()
    from googleapiclient.discovery import build

    creds = youtube_auto_reply.oauth_credentials(
        token_path(), SCOPES, interactive=interactive,
        login_message="GASを使うためのGoogleログインが必要です。「GASで動かす」を押してください。")
    return build("script", "v1", credentials=creds, cache_discovery=False)


def _explain(exc: Exception) -> GasError:
    text = str(exc)
    if "usersettings" in text or "has not enabled the Apps Script API" in text:
        return GasError(
            "Apps Script APIがオフになっています。開いたページで「Google Apps Script API」をオンにしてから、"
            "もう一度「GASで動かす」を押してください（最初の1回だけです）。", url=USER_SETTINGS_URL)
    match = _ENABLE_API_URL.search(text)
    if match or ("script.googleapis.com" in text and ("disabled" in text or "has not been used" in text)):
        return GasError(
            "連携に使っているGoogle CloudプロジェクトでApps Script APIが無効です。開いたページで「有効にする」を"
            "押し、数分待ってから、もう一度「GASで動かす」を押してください。",
            url=match.group(0) if match else "https://console.cloud.google.com/apis/library/script.googleapis.com")
    if "insufficient" in text.lower() and "scope" in text.lower():
        return GasError("GASを作る許可が足りません。「GASの連携を解除」してから、もう一度「GASで動かす」を押してください。")
    return GasError(f"GASの作成・更新に失敗しました（{type(exc).__name__}: {text[:200]}）。")


def _web_url(deployment: dict) -> str:
    for entry in deployment.get("entryPoints") or []:
        url = (entry.get("webApp") or {}).get("url")
        if url:
            return str(url)
    return ""


def deploy(settings: youtube_auto_reply.Settings, *, service_factory: Callable | None = None,
           now: datetime | None = None) -> dict:
    """Create the project on first use, write the files and (re)deploy the web app."""
    secret_source()  # the channel login must exist before anything is created
    state = load_state()
    service = _service(service_factory, interactive=True)
    projects = service.projects()
    try:
        if not state.get("script_id"):
            created = projects.create(body={"title": PROJECT_TITLE}).execute()
            state = {"script_id": str(created["scriptId"])}
            _save_state(state)  # a retry after a later failure must not create a second project
        script_id = state["script_id"]
        if not state.get("owner_email"):
            creator = projects.get(scriptId=script_id).execute().get("creator") or {}
            state["owner_email"] = str(creator.get("email") or "")
        projects.updateContent(scriptId=script_id, body={"files": project_files(settings)}).execute()
        version = projects.versions().create(
            scriptId=script_id, body={"description": "CUTから更新"}).execute()
        deployment_config = {"versionNumber": version["versionNumber"], "manifestFileName": "appsscript",
                             "description": "CUT コメント自動返信"}
        if state.get("deployment_id"):
            deployment = projects.deployments().update(
                scriptId=script_id, deploymentId=state["deployment_id"],
                body={"deploymentConfig": deployment_config}).execute()
        else:
            deployment = projects.deployments().create(scriptId=script_id, body=deployment_config).execute()
    except GasError:
        raise
    except Exception as exc:
        raise _explain(exc) from exc
    state["deployment_id"] = str(deployment.get("deploymentId") or state.get("deployment_id") or "")
    state["web_url"] = _web_url(deployment) or state.get("web_url", "")
    state["editor_url"] = editor_url(script_id)
    state["setup_url"] = setup_url(state)
    state["synced_at"] = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    _save_state(state)
    if not state["web_url"]:
        raise GasError("GASのWebアプリのURLを取得できませんでした。もう一度「GASで動かす」を押してください。")
    return state


def sync(settings: youtube_auto_reply.Settings, *, service_factory: Callable | None = None,
         now: datetime | None = None) -> bool:
    """Push changed settings to an existing project.  False when there is none yet."""
    state = load_state()
    if not state.get("script_id"):
        return False
    service = _service(service_factory, interactive=False)
    try:
        service.projects().updateContent(
            scriptId=state["script_id"], body={"files": project_files(settings)}).execute()
    except GasError:
        raise
    except Exception as exc:
        raise _explain(exc) from exc
    state["synced_at"] = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    _save_state(state)
    return True


def disconnect() -> None:
    """Forget the GAS login (the project in the user's account is left alone)."""
    if token_path().is_file():
        token_path().unlink()
