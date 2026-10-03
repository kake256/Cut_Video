"""Identify the source channel/video of a URL from yt-dlp metadata.

CUT assumes the user has permission to clip what they paste, so there is no
allow-list; this only labels jobs with where the footage came from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_VIDEO_KEY = re.compile(r"^(youtube|twitch-video):[A-Za-z0-9_-]{2,100}$")


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
