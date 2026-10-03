"""Detect and set up the AIs that pick highlights (Codex, Claude Code, local Ollama).

Installs and sign-ins run in a separate, visible console window so the user
sees every step (and any prompt) and can close it at any time.  Nothing here
runs without an explicit button press in the GUI.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Callable

from . import agent_runner, config

REPO_ROOT = Path(__file__).resolve().parents[1]
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"

# Fixed commands only; nothing user-supplied is ever spliced in.
INSTALL_COMMANDS = {
    "codex": "npm install -g @openai/codex && codex login",
    "claude_npm": "npm install -g @anthropic-ai/claude-code && claude auth login",
    "claude_native": (
        'powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"'
        " && claude auth login"
    ),
    "node": "winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements",
}
LOGIN_COMMANDS = {"codex": "codex login", "claude": "claude auth login"}


def _quiet(command: list[str], runner: Callable = subprocess.run, timeout: float = 20):
    try:
        return runner(command, capture_output=True, text=True, encoding="utf-8",
                      errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def codex_status(*, runner: Callable = subprocess.run) -> dict:
    exe = agent_runner.find_executable("codex")
    if not exe:
        return {"installed": False, "ready": False, "detail": "未インストール"}
    result = _quiet([exe, "login", "status"], runner)
    ready = bool(result and result.returncode == 0 and "logged in" in (result.stdout + result.stderr).lower())
    return {"installed": True, "ready": ready, "detail": "ログイン済み" if ready else "未ログイン"}


def claude_status(*, runner: Callable = subprocess.run) -> dict:
    exe = agent_runner.find_executable("claude")
    if not exe:
        return {"installed": False, "ready": False, "detail": "未インストール"}
    result = _quiet([exe, "auth", "status", "--json"], runner)
    try:
        ready = bool(result and json.loads(result.stdout).get("loggedIn"))
    except (ValueError, AttributeError):
        ready = False
    return {"installed": True, "ready": ready, "detail": "ログイン済み" if ready else "未ログイン"}


def find_ollama() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    candidates = [
        REPO_ROOT.parent / "dependencies" / "ollama" / "app" / "ollama.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Ollama" / "ollama.exe",
    ]
    return next((str(path) for path in candidates if path.is_file()), None)


def ollama_models(*, opener: Callable = urllib.request.urlopen) -> list[str] | None:
    """Model names served by a running local Ollama, or None when it is not running."""
    try:
        with opener(OLLAMA_TAGS_URL, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return [str(item.get("name")) for item in data.get("models", []) if item.get("name")]


def ollama_status(*, opener: Callable = urllib.request.urlopen) -> dict:
    models = ollama_models(opener=opener)
    installed = find_ollama() is not None or models is not None
    if models is None:
        return {"installed": installed, "ready": False,
                "detail": "起動していません" if installed else "未インストール", "models": []}
    wanted = config.LLM_ANALYSIS_MODEL
    ready = wanted in models
    return {"installed": True, "ready": ready, "models": models,
            "detail": f"利用可（{wanted}）" if ready else f"モデル {wanted} が未取得"}


def all_status() -> dict:
    return {"codex": codex_status(), "claude": claude_status(), "local": ollama_status()}


def _open_console(command: str, *, popen: Callable = subprocess.Popen) -> None:
    """Run a fixed command in a new console window the user can watch."""
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    popen(["cmd", "/k", command], cwd=str(REPO_ROOT), creationflags=flags)


def install(agent: str, *, popen: Callable = subprocess.Popen) -> str:
    """Start the installer for one AI in a new window; returns what was started."""
    has_npm = shutil.which("npm") is not None
    if agent == "codex":
        if not has_npm:
            _open_console(INSTALL_COMMANDS["node"], popen=popen)
            return "Node.js（Codexのインストールに必要）のインストールを開始しました。終わったらアプリを再起動して、もう一度押してください。"
        _open_console(INSTALL_COMMANDS["codex"], popen=popen)
        return "Codexのインストールを開始しました。終わるとブラウザでChatGPTへのログインが開きます。"
    if agent == "claude":
        _open_console(INSTALL_COMMANDS["claude_npm" if has_npm else "claude_native"], popen=popen)
        return "Claude Codeのインストールを開始しました。終わるとブラウザでClaudeへのログインが開きます。"
    if agent == "local":
        _open_console(f'"{REPO_ROOT / "setup_ollama.bat"}"', popen=popen)
        return f"ローカルAI（Ollama と {config.LLM_ANALYSIS_MODEL}、数GB）のセットアップを開始しました。"
    raise ValueError(f"unknown agent: {agent}")


def login(agent: str, *, popen: Callable = subprocess.Popen) -> str:
    if agent not in LOGIN_COMMANDS:
        raise ValueError(f"unknown agent: {agent}")
    exe = agent_runner.find_executable(agent)
    if not exe:
        return "先にインストールしてください。"
    # Use the detected binary: Codex's desktop app bundles a CLI that is not on PATH.
    subcommand = LOGIN_COMMANDS[agent].split(" ", 1)[1]
    _open_console(f'"{exe}" {subcommand}', popen=popen)
    return "ログイン用のウィンドウを開きました。ブラウザでログインを済ませてください。"
