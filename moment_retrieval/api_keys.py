"""API keys for the AIs that pick highlights, entered in the GUI instead of a login.

Codex and Claude Code normally run on the user's ChatGPT / Claude sign-in.  A
person who receives this app can instead paste their own API key in the
「設定」tab; it is stored in ``<DATA_DIR>/api_keys.json`` (``data/`` is not in
git) and handed only to the CLI process CUT starts, never to the MCP server or
any other child process.

Priority: a key saved in the GUI, then the usual environment variable, then the
CLI's own sign-in (no key at all).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from . import config

FILE_NAME = "api_keys.json"

# provider -> label, the agent that uses it, environment variables read as a fallback,
# and the environment variables the CLI reads the key from.
PROVIDERS = {
    "anthropic": {
        "label": "Claude（Anthropic）",
        "agent": "claude",
        "env": ("ANTHROPIC_API_KEY",),
        "inject": ("ANTHROPIC_API_KEY",),
        "test_url": "https://api.anthropic.com/v1/models?limit=1",
    },
    "openai": {
        "label": "Codex（OpenAI）",
        "agent": "codex",
        # ``codex exec`` reads CODEX_API_KEY; OPENAI_API_KEY is the common name people already have.
        "env": ("CODEX_API_KEY", "OPENAI_API_KEY"),
        "inject": ("CODEX_API_KEY", "OPENAI_API_KEY"),
        "test_url": "https://api.openai.com/v1/models",
    },
}
AGENT_PROVIDER = {item["agent"]: name for name, item in PROVIDERS.items()}
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-.:]{20,300}$")


class ApiKeyError(ValueError):
    pass


def path() -> Path:
    return Path(config.DATA_DIR) / FILE_NAME


def _load() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {name: str(value) for name, value in data.items()
            if name in PROVIDERS and isinstance(value, str) and value} if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".api_keys_", suffix=".json", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _check_provider(provider: str) -> dict:
    if provider not in PROVIDERS:
        raise ApiKeyError(f"未対応のAIです: {provider}")
    return PROVIDERS[provider]


def normalize(key: str) -> str:
    key = str(key or "").strip()
    if not _KEY_PATTERN.fullmatch(key):
        raise ApiKeyError("APIキーの形式が正しくありません（空白や改行が入っていないか確認してください）。")
    return key


def save_key(provider: str, key: str) -> None:
    _check_provider(provider)
    data = _load()
    data[provider] = normalize(key)
    _write(data)


def delete_key(provider: str) -> bool:
    _check_provider(provider)
    data = _load()
    if provider not in data:
        return False
    del data[provider]
    _write(data)
    return True


def resolve(provider: str) -> tuple[str, str]:
    """(key, source) with source 'saved', the environment variable name, or '' when none."""
    item = _check_provider(provider)
    saved = _load().get(provider, "")
    if saved:
        return saved, "saved"
    for name in item["env"]:
        value = os.environ.get(name, "").strip()
        if value:
            return value, name
    return "", ""


def has_key(agent: str) -> bool:
    provider = AGENT_PROVIDER.get(agent)
    return bool(provider and resolve(provider)[0])


def mask(key: str) -> str:
    key = str(key or "")
    if len(key) <= 12:
        return "●" * len(key)
    return f"{key[:7]}…{key[-4:]}"


def describe(provider: str) -> str:
    key, source = resolve(provider)
    if not key:
        return "未設定（CLIのログインを使います）"
    if source == "saved":
        return f"保存済み（{mask(key)}）"
    return f"環境変数 {source} を使用中（{mask(key)}）"


def agent_env(agent: str, base: dict | None = None) -> dict:
    """Environment for the Codex / Claude Code process, with the key added when one is set."""
    env = dict(os.environ if base is None else base)
    provider = AGENT_PROVIDER.get(agent)
    if provider:
        key, _source = resolve(provider)
        if key:
            for name in PROVIDERS[provider]["inject"]:
                env[name] = key
    return env


def test_key(provider: str, key: str = "", *, opener: Callable = urllib.request.urlopen) -> tuple[bool, str]:
    """Ask the provider's model list (no tokens used) whether the key is accepted."""
    item = _check_provider(provider)
    key = normalize(key) if str(key or "").strip() else resolve(provider)[0]
    if not key:
        return False, "テストするAPIキーがありません。入力するか、先に保存してください。"
    if provider == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        headers = {"Authorization": f"Bearer {key}"}
    request = urllib.request.Request(item["test_url"], headers=headers, method="GET")
    try:
        with opener(request, timeout=15) as response:
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "キーが無効です（401）。コピーし間違いや失効を確認してください。"
        if exc.code == 403:
            return False, "キーに権限がありません（403）。組織やプロジェクトの設定を確認してください。"
        if exc.code == 429:
            return True, "キーは有効ですが、利用上限に達しています（429）。残高や上限を確認してください。"
        return False, f"接続に失敗しました（HTTP {exc.code}）。"
    except (OSError, ValueError) as exc:
        return False, f"接続できませんでした（{type(exc).__name__}）。ネットワークを確認してください。"
    if 200 <= int(status) < 300:
        return True, f"接続できました。{item['label']} のAPIキーは有効です（{mask(key)}）。"
    return False, f"接続に失敗しました（HTTP {status}）。"
