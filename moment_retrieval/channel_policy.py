"""Which source channels or individual videos may be published, and how often.

Every automatic upload is private. Only clips whose source channel, or exact
source video, the user explicitly registered (with a note on why it is
allowed) offer a one-click "publish" in the GUI, limited per day. Everything
else, including local files, stays private. State lives as small JSON files
under the private library directory.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import config

DEFAULT_DAILY_LIMIT = 3
_KEY = re.compile(r"^(youtube|twitch):[A-Za-z0-9_.@-]{2,100}$")
_VIDEO_KEY = re.compile(r"^(youtube|twitch-video):[A-Za-z0-9_-]{2,100}$")


def _channels_path() -> Path:
    return config.LIBRARY_ROOT / "auto_publish_channels.json"


def _videos_path() -> Path:
    """Keep narrowly scoped permissions separate from legacy channel records."""
    return config.LIBRARY_ROOT / "auto_publish_videos.json"


def _settings_path() -> Path:
    return config.LIBRARY_ROOT / "auto_publish_settings.json"


def _log_path() -> Path:
    return config.LIBRARY_ROOT / "auto_publish_log.json"


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


@dataclass(frozen=True)
class SourceChannel:
    key: str          # "youtube:UC..." or "twitch:<login>"
    name: str
    url: str


@dataclass(frozen=True)
class SourceVideo:
    """A single source video and the channel yt-dlp says uploaded it."""
    key: str
    channel_key: str
    title: str
    url: str


def channel_from_info(info: dict) -> SourceChannel | None:
    """Derive the uploading channel from yt-dlp metadata, if it has one."""
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    if extractor.startswith("youtube"):
        channel_id = str(info.get("channel_id") or "")
        if not channel_id:
            return None
        return SourceChannel(
            f"youtube:{channel_id}",
            str(info.get("channel") or info.get("uploader") or channel_id),
            str(info.get("channel_url") or f"https://www.youtube.com/channel/{channel_id}"),
        )
    if extractor.startswith("twitch"):
        login = str(info.get("uploader_id") or "").lower()
        if not login:
            return None
        return SourceChannel(
            f"twitch:{login}",
            str(info.get("uploader") or login),
            f"https://www.twitch.tv/{login}",
        )
    return None


def video_from_info(info: dict) -> SourceVideo | None:
    """Derive a stable identity for one supported source video from yt-dlp metadata."""
    # A playlist ID is not a video ID. Be conservative if a caller supplies
    # playlist metadata even when yt-dlp's noplaylist option was requested.
    if info.get("_type") in {"playlist", "multi_video"} or info.get("entries"):
        return None
    channel = channel_from_info(info)
    if channel is None:
        return None
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    video_id = str(info.get("id") or "")
    if not video_id:
        return None
    if extractor.startswith("youtube"):
        key = f"youtube:{video_id}"
    elif extractor.startswith("twitch"):
        key = f"twitch-video:{video_id}"
    else:
        return None
    if not _VIDEO_KEY.fullmatch(key):
        return None
    return SourceVideo(
        key=key,
        channel_key=channel.key,
        title=str(info.get("title") or video_id),
        url=str(info.get("webpage_url") or info.get("original_url") or ""),
    )


_TWITCH_LOGIN = re.compile(r"^https://(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{3,25})(?:/videos)?/?$")


def resolve_channel(url: str, *, extract: Callable | None = None) -> SourceChannel:
    """Resolve a channel page or one of its videos to a channel identity."""
    url = str(url or "").strip()
    match = _TWITCH_LOGIN.fullmatch(url)
    if match and match.group(1).lower() not in {"videos", "directory", "settings"}:
        login = match.group(1).lower()
        return SourceChannel(f"twitch:{login}", login, f"https://www.twitch.tv/{login}")
    if not url.lower().startswith("https://"):
        raise ValueError("チャンネルか動画のURL（https://）を入力してください。")
    if extract is None:
        from yt_dlp import YoutubeDL

        def extract(target):
            with YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True,
                            "extract_flat": "in_playlist", "playlist_items": "1",
                            "js_runtimes": {"deno": {}, "node": {}}}) as ydl:
                return ydl.extract_info(target, download=False)
    info = extract(url) or {}
    channel = channel_from_info(info)
    if channel is None:
        for entry in info.get("entries") or []:
            channel = channel_from_info({"extractor_key": info.get("extractor_key"), **(entry or {})})
            if channel:
                break
    if channel is None:
        raise ValueError("このURLからYouTube/Twitchのチャンネルを特定できませんでした。")
    return channel


def resolve_video(url: str, *, extract: Callable | None = None) -> SourceVideo:
    """Resolve a supplied *single-video* URL without broadening it to its channel."""
    url = str(url or "").strip()
    if not url.lower().startswith("https://"):
        raise ValueError("動画のURL（https://）を入力してください。")
    if extract is None:
        from yt_dlp import YoutubeDL

        def extract(target):
            with YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True,
                            "js_runtimes": {"deno": {}, "node": {}}}) as ydl:
                return ydl.extract_info(target, download=False)
    info = extract(url) or {}
    video = video_from_info(info)
    if video is None:
        raise ValueError("このURLからYouTube/Twitchの動画を特定できませんでした。動画ページのURLを入力してください。")
    # Preserve a canonical URL when yt-dlp does not provide one, but never use
    # it as identity: the extractor's video id is the policy key.
    return SourceVideo(video.key, video.channel_key, video.title, video.url or url)


def list_channels() -> list[dict]:
    value = _read(_channels_path(), [])
    return value if isinstance(value, list) else []


def add_channel(channel: SourceChannel, permission_note: str) -> dict:
    note = str(permission_note or "").strip()
    if not note:
        raise ValueError("許可の根拠（配信者の許可・ガイドラインなど）を入力してください。")
    if not _KEY.fullmatch(channel.key):
        raise ValueError("チャンネルを特定できませんでした。")
    channels = [item for item in list_channels() if item.get("key") != channel.key]
    entry = {
        "key": channel.key, "name": channel.name, "url": channel.url,
        "permission_note": note[:500],
        "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    channels.append(entry)
    _write(_channels_path(), channels)
    return entry


def remove_channel(key: str) -> bool:
    channels = list_channels()
    kept = [item for item in channels if item.get("key") != key]
    _write(_channels_path(), kept)
    return len(kept) != len(channels)


def list_videos() -> list[dict]:
    value = _read(_videos_path(), [])
    return value if isinstance(value, list) else []


def add_video(video: SourceVideo, permission_note: str) -> dict:
    note = str(permission_note or "").strip()
    if not note:
        raise ValueError("許可の根拠（配信者の許可・ガイドラインなど）を入力してください。")
    if not _VIDEO_KEY.fullmatch(video.key) or not _KEY.fullmatch(video.channel_key):
        raise ValueError("動画または元チャンネルを特定できませんでした。")
    videos = [item for item in list_videos() if item.get("key") != video.key]
    entry = {
        "key": video.key, "channel_key": video.channel_key, "title": video.title,
        "url": video.url, "permission_note": note[:500],
        "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    videos.append(entry)
    _write(_videos_path(), videos)
    return entry


def remove_video(key: str) -> bool:
    videos = list_videos()
    kept = [item for item in videos if item.get("key") != key]
    _write(_videos_path(), kept)
    return len(kept) != len(videos)


def is_allowed(channel: SourceChannel | None, video: SourceVideo | None = None) -> bool:
    """Return true for a legacy channel permission or an exact video+channel pair."""
    if channel is None:
        return False
    if any(item.get("key") == channel.key for item in list_channels()):
        return True
    return video is not None and any(
        item.get("key") == video.key and item.get("channel_key") == channel.key
        for item in list_videos()
    )


def settings() -> dict:
    value = _read(_settings_path(), {})
    value = value if isinstance(value, dict) else {}
    return {"daily_limit": int(value.get("daily_limit", DEFAULT_DAILY_LIMIT))}


def save_settings(daily_limit: int) -> dict:
    daily_limit = int(daily_limit)
    if not 0 <= daily_limit <= 20:
        raise ValueError("1日の公開上限は0〜20本で指定してください。")
    value = {"daily_limit": daily_limit}
    _write(_settings_path(), value)
    return value


def published_in_last_day(now: float | None = None) -> int:
    now = time.time() if now is None else now
    log = _read(_log_path(), [])
    return sum(1 for item in log if isinstance(item, dict) and now - float(item.get("at", 0)) < 86400)


def record_publication(video_id: str, channel_key: str, now: float | None = None) -> None:
    log = [item for item in _read(_log_path(), []) if isinstance(item, dict)]
    now = time.time() if now is None else now
    log.append({"youtube_video_id": video_id, "source_channel": channel_key, "at": now})
    _write(_log_path(), [item for item in log if now - float(item.get("at", 0)) < 30 * 86400])


def can_publish(channel_key: str | None, video_key: str | None = None) -> tuple[bool, str]:
    """Whether the GUI may publish this source; video permissions are exact."""
    if not channel_key:
        return False, "元動画のチャンネルが不明なため公開できません（非公開のまま）"
    channel_allowed = any(item.get("key") == channel_key for item in list_channels())
    video_allowed = bool(video_key) and any(
        item.get("key") == video_key and item.get("channel_key") == channel_key
        for item in list_videos()
    )
    if not channel_allowed and not video_allowed:
        return False, "許可済みチャンネルまたはこの動画の許可がないため公開できません（非公開のまま）"
    if published_in_last_day() >= settings()["daily_limit"]:
        return False, "1日の公開上限に達しています"
    return True, "公開できます"
