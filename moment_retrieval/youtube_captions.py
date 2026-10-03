"""Safe, metadata-only inspection of public YouTube JSON3 caption tracks."""
from __future__ import annotations

from dataclasses import dataclass
import html
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser


_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_CUES = 20_000
_MAX_CUE_CHARS = 8_000
_MAX_TOTAL_TEXT_CHARS = 1_500_000
_MAX_TITLE_CHARS = 500
_MAX_TIMESTAMP_MS = 31 * 24 * 60 * 60 * 1000
_TIMEOUT_SEC = 15
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}


@dataclass(frozen=True)
class CaptionCue:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class CaptionPreview:
    source_url: str
    title: str
    language: str
    is_automatic: bool
    cues: tuple[CaptionCue, ...]


class CaptionError(RuntimeError):
    """A deliberately non-sensitive error suitable for UI display."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _error(code: str, message: str) -> CaptionError:
    return CaptionError(code, message)


def normalize_youtube_url(url: str) -> str:
    """Accept one public YouTube video locator and return its canonical watch URL."""
    if not isinstance(url, str) or not url.strip():
        raise _error("INVALID_URL", "YouTube URLを入力してください。")
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise _error("INVALID_URL", "YouTube URLが不正です。") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.username or parsed.password or port is not None:
        raise _error("INVALID_URL", "HTTPS の公開YouTube動画URLだけを指定できます。")
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if "list" in query or "index" in query:
        raise _error("INVALID_URL", "再生リストではなく単一動画URLを指定してください。")
    video_id = ""
    if host == "youtu.be":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 1:
            video_id = parts[0]
    elif host in _YOUTUBE_HOSTS:
        path = parsed.path.rstrip("/") or "/"
        if path == "/watch":
            values = query.get("v", [])
            if len(values) == 1:
                video_id = values[0]
        elif path.startswith(("/shorts/", "/live/", "/embed/")):
            parts = [part for part in path.split("/") if part]
            if len(parts) == 2:
                video_id = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise _error("INVALID_URL", "単一の公開YouTube動画URLだけを指定できます。")
    return f"https://www.youtube.com/watch?v={video_id}"


def _video_id(canonical_url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(canonical_url).query)["v"][0]


def _metadata_options() -> dict:
    return {
        "skip_download": True,
        "noplaylist": True,
        "cachedir": False,
        "quiet": True,
        "no_warnings": True,
        "logger": _NoopLogger(),
        "socket_timeout": _TIMEOUT_SEC,
        "retries": 0,
        "extractor_retries": 0,
        "js_runtimes": {"deno": {}, "node": {}},
    }


class _NoopLogger:
    def debug(self, _message): pass
    def warning(self, _message): pass
    def error(self, _message): pass


def _select_track(info: dict, preferred_language: str) -> tuple[str, bool, str]:
    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    if not isinstance(manual, dict) or not isinstance(automatic, dict):
        raise _error("INVALID_METADATA", "動画情報の形式が不正です。")
    preferred = (preferred_language or "ja").strip().lower()
    # Preferred-language automatic captions outrank a different-language manual
    # track, but translated automatic tracks are never selected.
    choices = (
        (preferred, manual, False), (preferred, automatic, True),
        ("en", manual, False), ("en", automatic, True),
    )
    for language, source, automatic_flag in choices:
        track = source.get(language)
        selected = _caption_url(track, automatic_flag)
        if selected:
            return str(language), automatic_flag, selected
    for source, automatic_flag in ((manual, False), (automatic, True)):
        for language, track in sorted(source.items(), key=lambda item: str(item[0])):
            selected = _caption_url(track, automatic_flag)
            if selected:
                return str(language), automatic_flag, selected
    raise _error("CAPTIONS_UNAVAILABLE", "利用可能な字幕が見つかりません。")


def _caption_url(track: object, is_automatic: bool) -> str | None:
    if not isinstance(track, list):
        return None
    for item in track:
        if not isinstance(item, dict) or item.get("ext") != "json3":
            continue
        value = item.get("url")
        if not isinstance(value, str):
            continue
        try:
            parsed = urllib.parse.urlsplit(value)
            valid = (parsed.scheme == "https" and parsed.hostname in {"youtube.com", "www.youtube.com"}
                and parsed.port is None and parsed.path == "/api/timedtext"
                and not parsed.username and not parsed.password
                and "tlang" not in urllib.parse.parse_qs(parsed.query, keep_blank_values=True))
        except (TypeError, ValueError):
            valid = False
        if valid:
            return value
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect blocked", headers, fp)


def _fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "CUT-caption-inspector/1"})
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=_TIMEOUT_SEC) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > _MAX_RESPONSE_BYTES:
                raise _error("CAPTIONS_TOO_LARGE", "字幕データが大きすぎます。")
            payload = response.read(_MAX_RESPONSE_BYTES + 1)
    except CaptionError:
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        raise _error("CAPTION_FETCH_FAILED", "字幕の取得に失敗しました。") from None
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise _error("CAPTIONS_TOO_LARGE", "字幕データが大きすぎます。")
    return payload


class _TextCleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parser = _TextCleaner()
    try:
        parser.feed(html.unescape(value))
        parser.close()
    except Exception:
        return ""
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()


def _parse_cues(payload: bytes) -> tuple[CaptionCue, ...]:
    try:
        document = json.loads(payload.decode("utf-8"))
        events = document["events"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        raise _error("INVALID_CAPTIONS", "字幕データの形式が不正です。") from None
    if not isinstance(events, list):
        raise _error("INVALID_CAPTIONS", "字幕データの形式が不正です。")
    cues: list[CaptionCue] = []
    total_chars = 0
    seen: set[tuple[int, int, str]] = set()
    for event in events:
        if not isinstance(event, dict):
            raise _error("INVALID_CAPTIONS", "字幕データの形式が不正です。")
        segs = event.get("segs")
        if not segs:  # metadata events have no caption text.
            continue
        if not isinstance(segs, list) or any(not isinstance(seg, dict) for seg in segs):
            raise _error("INVALID_CAPTIONS", "字幕データの形式が不正です。")
        if any("utf8" in seg and not isinstance(seg["utf8"], str) for seg in segs):
            raise _error("INVALID_CAPTIONS", "字幕データの形式が不正です。")
        text = _clean_text("".join(seg.get("utf8", "") for seg in segs))
        if not text:
            continue
        start, duration = event.get("tStartMs"), event.get("dDurationMs")
        if (isinstance(start, bool) or isinstance(duration, bool) or not isinstance(start, (int, float))
                or not isinstance(duration, (int, float))
                or start < 0 or duration <= 0 or start > _MAX_TIMESTAMP_MS or duration > _MAX_TIMESTAMP_MS
                or not math.isfinite(start) or not math.isfinite(duration)
                or int(start) != start or int(duration) != duration):
            raise _error("INVALID_TIMING", "字幕時刻が不正です。")
        end = start + duration
        if end > _MAX_TIMESTAMP_MS or (cues and start < cues[-1].start_ms):
            raise _error("INVALID_TIMING", "字幕時刻が不正です。")
        cue = CaptionCue(int(start), int(end), text)
        if len(text) > _MAX_CUE_CHARS:
            raise _error("CAPTION_CUE_TOO_LARGE", "字幕行が長すぎます。")
        total_chars += len(text)
        if total_chars > _MAX_TOTAL_TEXT_CHARS or len(cues) >= _MAX_CUES:
            raise _error("CAPTIONS_TOO_LARGE", "字幕データが大きすぎます。")
        identity = (cue.start_ms, cue.end_ms, cue.text)
        if identity not in seen:
            cues.append(cue)
            seen.add(identity)
    if not cues:
        raise _error("CAPTIONS_UNAVAILABLE", "本文を含む字幕が見つかりません。")
    return tuple(cues)


def fetch_youtube_captions(url: str, preferred_language: str = "ja") -> CaptionPreview:
    canonical = normalize_youtube_url(url)
    try:
        from yt_dlp import YoutubeDL
        with YoutubeDL(_metadata_options()) as ydl:
            info = ydl.extract_info(canonical, download=False)
    except CaptionError:
        raise
    except Exception:
        raise _error("METADATA_FETCH_FAILED", "動画情報の取得に失敗しました。") from None
    if not isinstance(info, dict) or str(info.get("id") or "") != _video_id(canonical):
        raise _error("VIDEO_MISMATCH", "指定した動画を確認できません。")
    if info.get("is_live") or str(info.get("live_status") or "") in {"is_live", "is_upcoming", "post_live"}:
        raise _error("VIDEO_UNAVAILABLE", "ライブ動画はこの字幕確認では利用できません。")
    availability = str(info.get("availability") or "").lower()
    if availability != "public":
        raise _error("VIDEO_UNAVAILABLE", "公開動画として確認できません。")
    language, is_automatic, caption_url = _select_track(info, preferred_language)
    cues = _parse_cues(_fetch_bytes(caption_url))
    title = str(info.get("title") or "(タイトルなし)")
    if len(title) > _MAX_TITLE_CHARS:
        raise _error("TITLE_TOO_LARGE", "動画タイトルが長すぎます。")
    return CaptionPreview(canonical, title, language, is_automatic, cues)
