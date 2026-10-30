"""Original stream titles for library videos, so pickers can say what a video is.

Titles come from yt-dlp's metadata: saved when a URL is downloaded, and fetched
again (metadata only, no media) for older downloads whose file name still
carries the Twitch VOD id or YouTube video id.  Stored as a small JSON file next
to the library database; a missing title is never an error.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from . import config

_TWITCH = re.compile(r"(?:twitch\.tv/videos/|(?:^|_)v)(\d{6,})(?=$|[./?&_])")
_YOUTUBE = re.compile(r"(?:[?&]v=|youtu\.be/|shorts/|(?:^|[_/]))([A-Za-z0-9_-]{11})(?=$|[.?&])")
_lock = threading.Lock()


def _store_path() -> Path:
    return Path(config.LIBRARY_ROOT) / "source_titles.json"


def source_key(text: str) -> str | None:
    """``twitch:<id>`` or ``youtube:<id>`` from a URL or a downloaded file name."""
    value = str(text or "")
    name = Path(value).name if "://" not in value else value
    twitch = _TWITCH.search(name)
    if twitch:
        return f"twitch:{twitch.group(1)}"
    youtube = _YOUTUBE.search(name)
    if youtube:
        return f"youtube:{youtube.group(1)}"
    return None


def source_url(key: str) -> str | None:
    kind, _, ident = str(key).partition(":")
    if kind == "twitch":
        return f"https://www.twitch.tv/videos/{ident}"
    if kind == "youtube":
        return f"https://www.youtube.com/watch?v={ident}"
    return None


def load() -> dict[str, dict]:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(url_or_name: str, title: str, upload_date: str | None = None) -> None:
    key = source_key(url_or_name)
    title = " ".join(str(title or "").split())
    if not key or not title:
        return
    with _lock:
        data = load()
        data[key] = {"title": title, "upload_date": str(upload_date or "")}
        path = _store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, path)


def fetch_missing(names: list[str], *, extract=None) -> int:
    """Fetch titles (metadata only) for names whose source has none yet; returns how many."""
    known = load()
    keys = []
    for name in names:
        key = source_key(name)
        if key and key not in known and key not in keys:
            keys.append(key)
    if not keys:
        return 0
    if extract is None:
        from yt_dlp import YoutubeDL

        def extract(url):
            with YoutubeDL({"quiet": True, "skip_download": True, "no_warnings": True}) as ydl:
                return ydl.extract_info(url, download=False, process=False)
    found = 0
    for key in keys:
        try:
            info = extract(source_url(key)) or {}
        except Exception:  # deleted VOD, offline, rate limit: try again next time
            continue
        if info.get("title"):
            record(source_url(key), info["title"], info.get("upload_date"))
            found += 1
    return found
