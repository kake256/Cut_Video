"""Fetch the chat of the original stream (YouTube live chat replay / Twitch VOD chat).

Only message times and short texts are kept: ``[[seconds_into_video, text], ...]``.
Both sources are best effort.  YouTube replays come through yt-dlp; Twitch has no
official API for VOD chat, so its public web endpoint is used and may stop working
when Twitch changes it.
"""
from __future__ import annotations

import json
import re
import tempfile
import urllib.request
from pathlib import Path
from typing import Callable

TEXT_LIMIT = 40  # characters kept per message
TWITCH_GQL = "https://gql.twitch.tv/gql"
# Twitch's public web client id (the same one yt-dlp and the twitch.tv site use; not a secret).
TWITCH_WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TWITCH_COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


class ChatUnavailable(RuntimeError):
    pass


def _short(text: str) -> str:
    return " ".join(str(text).split())[:TEXT_LIMIT]


# ---------- YouTube ----------

def parse_youtube_live_chat(lines) -> list[list]:
    """Messages from yt-dlp's ``.live_chat.json`` (one JSON object per line)."""
    messages: list[list] = []
    for line in lines:
        try:
            data = json.loads(line)
        except ValueError:
            continue
        replay = data.get("replayChatItemAction") or {}
        try:
            offset = int(replay.get("videoOffsetTimeMsec")) / 1000
        except (TypeError, ValueError):
            continue
        if offset <= 0:
            continue  # posted in the waiting room before the stream started
        for action in replay.get("actions") or []:
            item = ((action or {}).get("addChatItemAction") or {}).get("item") or {}
            renderer = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
            if not renderer:
                continue
            runs = (renderer.get("message") or {}).get("runs") or []
            text = "".join(run.get("text") or ((run.get("emoji") or {}).get("shortcuts") or [""])[0]
                           for run in runs)
            messages.append([round(offset, 1), _short(text)])
    return messages


def fetch_youtube(url: str, *, log: Callable | None = None) -> list[list]:
    from yt_dlp import YoutubeDL

    with tempfile.TemporaryDirectory(prefix="cut_chat_") as tmp:
        options = {"quiet": True, "no_warnings": True, "noprogress": True, "skip_download": True, "noplaylist": True,
                   "writesubtitles": True, "subtitleslangs": ["live_chat"],
                   "outtmpl": str(Path(tmp, "chat.%(ext)s")), "js_runtimes": {"deno": {}, "node": {}}}
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if "live_chat" not in (info.get("subtitles") or {}):
                raise ChatUnavailable("この動画にはチャットのリプレイがありません")
            if log:
                log("YouTubeのチャットのリプレイを取得しています（長い配信では数分かかります）。")
            ydl.process_info(info)
        files = list(Path(tmp).glob("*.live_chat.json"))
        if not files:
            raise ChatUnavailable("チャットのリプレイを取得できませんでした")
        with files[0].open(encoding="utf-8") as handle:
            return parse_youtube_live_chat(handle)


# ---------- Twitch ----------

def _twitch_page(video_id: str, offset: int, post: Callable) -> dict:
    body = [{"operationName": "VideoCommentsByOffsetOrCursor",
             "variables": {"videoID": video_id, "contentOffsetSeconds": offset},
             "extensions": {"persistedQuery": {"version": 1, "sha256Hash": TWITCH_COMMENTS_HASH}}}]
    reply = post(body)
    item = reply[0] if isinstance(reply, list) and reply else {}
    comments = (((item.get("data") or {}).get("video") or {}).get("comments"))
    if comments is None:
        errors = "; ".join(str(e.get("message")) for e in item.get("errors") or [])
        raise ChatUnavailable(f"Twitchのチャットを取得できませんでした（{errors or '応答が空'}）")
    return comments


def _post_json(body) -> object:
    request = urllib.request.Request(
        TWITCH_GQL, data=json.dumps(body).encode("utf-8"),
        headers={"Client-Id": TWITCH_WEB_CLIENT_ID, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_twitch(video_id: str, *, post: Callable = _post_json, log: Callable | None = None,
                 max_pages: int = 20000) -> list[list]:
    """VOD chat, paged by time offset (cursor paging needs a browser integrity token)."""
    seen: set[str] = set()
    messages: list[list] = []
    offset = 0
    for page in range(max_pages):
        comments = _twitch_page(video_id, offset, post)
        edges = comments.get("edges") or []
        latest = offset
        for edge in edges:
            node = edge.get("node") or {}
            at = int(node.get("contentOffsetSeconds") or 0)
            latest = max(latest, at)
            if node.get("id") in seen:
                continue
            seen.add(node.get("id"))
            text = "".join(str(fragment.get("text") or "")
                           for fragment in (node.get("message") or {}).get("fragments") or [])
            messages.append([at, _short(text)])
        if not (comments.get("pageInfo") or {}).get("hasNextPage") or not edges:
            break
        # A page can sit entirely inside one busy second; move on rather than loop.
        offset = latest if latest > offset else offset + 1
        if log and page % 50 == 49:
            log(f"  Twitchのチャットを取得中... {offset // 60}分まで（{len(messages)}件）")
    messages.sort(key=lambda item: item[0])
    return messages


# ---------- entry point ----------

_TWITCH_VOD = re.compile(r"twitch\.tv/videos/(\d+)")
_YOUTUBE = re.compile(r"(youtube\.com|youtu\.be)/")


def fetch(url: str, *, log: Callable | None = None) -> dict:
    """``{"source", "url", "messages"}`` for a stream URL; raises ChatUnavailable otherwise."""
    twitch = _TWITCH_VOD.search(url or "")
    if twitch:
        return {"source": "twitch", "url": url, "messages": fetch_twitch(twitch.group(1), log=log)}
    if _YOUTUBE.search(url or ""):
        return {"source": "youtube", "url": url, "messages": fetch_youtube(url, log=log)}
    raise ChatUnavailable("YouTubeかTwitchの動画ではないため、コメントは使いません")
