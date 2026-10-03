"""Help the user prepare Google Cloud for YouTube uploads with the gcloud CLI.

Automated: sign-in (gcloud's own browser flow), creating a project and
enabling YouTube Data API v3.  Google offers no public API for the OAuth
consent screen or desktop OAuth clients, so those pages are opened in the
browser for the user to finish.  Every call that changes the user's Google
account is triggered by an explicit GUI action.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import webbrowser
from pathlib import Path
from typing import Callable

from . import config

YOUTUBE_API = "youtube.googleapis.com"
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


class SetupError(RuntimeError):
    pass


def _state_path() -> Path:
    return config.LIBRARY_ROOT / "youtube_gcp_project.json"


def find_gcloud() -> str | None:
    return shutil.which("gcloud")


def _run(args: list[str], *, timeout: float = 180, runner: Callable = subprocess.run) -> str:
    gcloud = find_gcloud()
    if not gcloud:
        raise SetupError("Google Cloud SDK（gcloud）が見つかりません。インストールしてから再度お試しください。")
    result = runner(
        [gcloud, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip().splitlines()
        detail = " ".join(message[-2:])[-300:]
        if "Terms of Service" in detail or "terms of service" in detail.lower():
            raise SetupError(
                "Google Cloudの利用規約に未同意です。https://console.cloud.google.com/ を一度開いて同意してから再実行してください。"
            )
        raise SetupError(f"gcloudの実行に失敗しました: {detail}")
    return result.stdout


def active_account(*, runner: Callable = subprocess.run) -> str | None:
    accounts = json.loads(_run(["auth", "list", "--format=json"], runner=runner) or "[]")
    return next((item.get("account") for item in accounts if item.get("status") == "ACTIVE"), None)


def login(*, runner: Callable = subprocess.run) -> str | None:
    """gcloud's own browser sign-in; returns the account now active."""
    _run(["auth", "login", "--brief", "--quiet"], timeout=600, runner=runner)
    return active_account(runner=runner)


def saved_project() -> str | None:
    try:
        value = json.loads(_state_path().read_text(encoding="utf-8")).get("project_id")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and _PROJECT_ID.fullmatch(value) else None


def create_project_and_enable_api(*, runner: Callable = subprocess.run) -> dict:
    """Create (or reuse) the CUT project and enable YouTube Data API v3 on it."""
    account = active_account(runner=runner)
    if not account:
        raise SetupError("先に「Googleにログイン」を行ってください。")
    project_id = saved_project()
    created = False
    if project_id is None:
        project_id = "cut-youtube-" + secrets.token_hex(3)
        _run(["projects", "create", project_id, "--name=CUT-YouTube", "--quiet"], runner=runner)
        created = True
        _state_path().parent.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps({"project_id": project_id, "account": account}), encoding="utf-8")
    _run(["services", "enable", YOUTUBE_API, f"--project={project_id}", "--quiet"], timeout=300, runner=runner)
    return {"project_id": project_id, "account": account, "created": created}


def console_pages(project_id: str) -> list[tuple[str, str]]:
    base = "https://console.cloud.google.com/auth"
    return [
        ("同意画面（アプリ名・メール）", f"{base}/branding?project={project_id}"),
        ("テストユーザー（自分のアカウントを追加）", f"{base}/audience?project={project_id}"),
        ("OAuthクライアント作成（種類: デスクトップアプリ）", f"{base}/clients/create?project={project_id}"),
    ]


def open_console_pages(project_id: str, *, opener: Callable = webbrowser.open) -> list[tuple[str, str]]:
    pages = console_pages(project_id)
    for _label, url in pages:
        opener(url)
    return pages
