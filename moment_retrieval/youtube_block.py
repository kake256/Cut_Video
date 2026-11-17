"""Stop asking YouTube for a while once it answers "Sign in to confirm you're not a bot".

Retrying a refused request only adds to what YouTube sees, so the first bot check
is recorded in ``youtube_block.json`` next to the library database and every
YouTube request made through CUT (metadata, downloads, sections, chat, titles,
captions) fails at once, without contacting YouTube, until the cooldown ends.
The first request after the cooldown is a single probe: success clears the
record, another bot check starts a new cooldown.  Twitch and other sites are
not affected.  Deleting the file ends the cooldown early.

The cooldown is kept per address family: when this PC has both IPv6 and IPv4,
a bot check on one moves the following requests to the other, and YouTube is
left alone only while both are refused.

Before that point is reached, requests are kept light (YouTube only):
``pace`` waits a random 5-10 s between the start of two requests,
``polite_options`` limits retries and parallel connections and waits longer
after each failure, and ``remember_info`` / ``cached_info`` let one job reuse
the video information (downloads, sections and chat included) instead of
asking for it again at every step.

``client_options`` is what every yt-dlp call passes: the JS runtimes yt-dlp
may use (Deno first, then Node.js; yt-dlp enables only Deno by default) and,
when the bgutil PO token plugin is installed, the ``mweb`` player client next
to yt-dlp's defaults.  ``scripts/check_ytdlp_setup.py`` shows what is in effect.
"""
from __future__ import annotations

import functools
import importlib.util
import json
import os
import random
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from .downloader import DownloadError

COOLDOWN_SEC = float(os.environ.get("CUT_VIDEO_YOUTUBE_COOLDOWN_HOURS", "24")) * 3600
# The marker the daily batch (cut_batch/daily_clip_batch.py) looks for in a failed job's log.
BOT_MARKER = "not a bot"
_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
# Time between the start of two YouTube requests made by this process: random in [MIN, MAX].
MIN_GAP_SEC = float(os.environ.get("CUT_VIDEO_YOUTUBE_MIN_GAP_SEC", "5"))
MAX_GAP_SEC = float(os.environ.get("CUT_VIDEO_YOUTUBE_MAX_GAP_SEC", "10"))
# How long fetched video information is reused.  Format URLs carry their own
# expiry (about 6 h), which ``cached_info(min_valid_sec=...)`` also checks.
INFO_CACHE_SEC = float(os.environ.get("CUT_VIDEO_YOUTUBE_INFO_CACHE_MIN", "180")) * 60
# A download reuses cached formats only while their URLs stay valid at least this long.
DOWNLOAD_MIN_VALID_SEC = 3600.0
# JS runtimes yt-dlp may use for YouTube's challenges, in order of preference.
JS_RUNTIMES = ("deno", "node")
# Player clients when the bgutil PO token provider is installed ("default" = yt-dlp's own set).
# Override with CUT_VIDEO_YOUTUBE_PLAYER_CLIENTS (comma separated; empty = yt-dlp's defaults only).
POT_PLAYER_CLIENTS = ("default", "mweb")
_BGUTIL_MODULE = "yt_dlp_plugins.extractor.getpot_bgutil"
_lock = threading.Lock()
_pace_lock = threading.Lock()
_last_request = 0.0
_info_cache: dict[str, tuple[float, dict]] = {}


class YouTubeBlocked(DownloadError):
    """YouTube refused this PC (bot check); no request was or will be sent until the cooldown ends."""


def store_path() -> Path:
    from . import config

    return Path(config.LIBRARY_ROOT) / "youtube_block.json"


def is_youtube(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    return any(host == item or host.endswith("." + item) for item in _YOUTUBE_HOSTS)


def is_bot_check(error: BaseException | str) -> bool:
    return BOT_MARKER in str(error).lower()


def load() -> dict:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---- IPv4 / IPv6 ----
# A bot check can stick to one address family only (2026-11-17: IPv6 refused, IPv4 fine on the
# same line), so each family has its own cooldown and requests move to the other one meanwhile.
# YouTube is asked over IPv6 first when this PC has it (Windows' own preference), unless
# CUT_VIDEO_YOUTUBE_FORCE_IPV4=1 puts IPv4 first.
FAMILIES = ("ipv6", "ipv4")
_FAMILY_LABEL = {"ipv4": "IPv4", "ipv6": "IPv6"}


@functools.lru_cache(maxsize=1)
def available_families() -> tuple[str, ...]:
    """Address families this PC can reach the internet with (no packet is sent)."""
    import socket

    found = []
    for family, name, probe in ((socket.AF_INET6, "ipv6", "2001:4860:4860::8888"),
                                (socket.AF_INET, "ipv4", "8.8.8.8")):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 53))  # UDP connect only picks a route
            found.append(name)
        except OSError:
            pass
    return tuple(found) or ("ipv4",)


def force_ipv4() -> bool:
    """CUT_VIDEO_YOUTUBE_FORCE_IPV4=1: ask YouTube over IPv4 first (IPv6 only while IPv4 is refused)."""
    return os.environ.get("CUT_VIDEO_YOUTUBE_FORCE_IPV4", "").strip().lower() in ("1", "true", "yes", "on")


def family_order() -> list[str]:
    order = ["ipv4", "ipv6"] if force_ipv4() else ["ipv6", "ipv4"]
    return [family for family in order if family in available_families()]


def _blocks(data: dict, now: float) -> dict[str, float]:
    """Family -> end of its running cooldown.  An old single record blocks every family."""
    if data.get("until") is not None and "families" not in data:
        until = float(data.get("until") or 0)
        return {family: until for family in FAMILIES} if until > now else {}
    found = {}
    for family, entry in (data.get("families") or {}).items():
        until = float((entry or {}).get("until") or 0)
        if until > now:
            found[family] = until
    return found


def current_family(now: float | None = None) -> str | None:
    """The address family the next YouTube request uses (None while every one is refused)."""
    blocks = _blocks(load(), time.time() if now is None else now)
    return next((family for family in family_order() if family not in blocks), None)


def blocked_until(now: float | None = None) -> float:
    """End of the current cooldown (0 when YouTube may be asked over some address family)."""
    now = time.time() if now is None else now
    blocks = _blocks(load(), now)
    usable = family_order()
    if any(family not in blocks for family in usable):
        return 0.0
    return min(blocks[family] for family in usable)


def message(until: float) -> str:
    return (f"YouTubeにボット判定（{BOT_MARKER}）されたため、{datetime.fromtimestamp(until):%m/%d %H:%M}まで"
            "YouTubeへの問い合わせを止めています（再試行すると判定が長引くため）。Twitchやローカルファイルは使えます。"
            f"早めに試すときは {store_path()} を削除してください。")


def switch_message(refused: str, next_family: str) -> str:
    return (f"YouTubeに{_FAMILY_LABEL.get(refused, refused)}でボット判定（{BOT_MARKER}）されたため、"
            f"次からは{_FAMILY_LABEL.get(next_family, next_family)}で問い合わせます。")


def _write(data: dict) -> None:
    with _lock:
        path = store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, path)


def record(error: BaseException | str, *, now: float | None = None, family: str | None = None) -> float:
    """Start a cooldown for ``family`` (default: the one requests are using now); returns its end."""
    now = time.time() if now is None else now
    family = family or current_family(now) or (family_order() or ["ipv4"])[0]
    until = now + COOLDOWN_SEC
    data = load()
    families = {name: entry for name, entry in (data.get("families") or {}).items()
                if name in _blocks(data, now)}
    if data.get("until") is not None and "families" not in data:  # old single record: keep it per family
        families = {name: {k: data.get(k) for k in ("since", "until", "error")} for name in _blocks(data, now)}
    families[family] = {"since": now, "until": until, "error": " ".join(str(error).split())[:300]}
    _write({"families": families})
    return until


def clear() -> None:
    with _lock:
        try:
            store_path().unlink()
        except OSError:
            pass


def check(url: str) -> None:
    """Raise YouTubeBlocked for a YouTube URL during the cooldown; do nothing otherwise."""
    if not is_youtube(url):
        return
    until = blocked_until()
    if until:
        raise YouTubeBlocked(message(until))


def note_failure(url: str, error: BaseException | str) -> YouTubeBlocked | None:
    """After a failed YouTube request: start the cooldown on a bot check and return the error to raise.

    When another address family is still usable, the error says so: the request
    may be made again at once (the daily batch retries the video once).
    """
    if not is_youtube(url) or not is_bot_check(error):
        return None
    refused = current_family()
    until = record(error, family=refused)
    following = current_family()
    if following is not None:
        return YouTubeBlocked(switch_message(refused or "", following))
    return YouTubeBlocked(message(blocked_until() or until))


def note_success(url: str) -> None:
    """A YouTube request went through: forget cooldowns that have already ended (probe succeeded)."""
    if not is_youtube(url) or not store_path().exists():
        return
    data = load()
    blocks = _blocks(data, time.time())
    if not blocks:
        clear()
    elif "families" in data and set(data["families"]) - set(blocks):
        _write({"families": {name: entry for name, entry in data["families"].items() if name in blocks}})


def video_id(url: str) -> str:
    """The 11-character video ID of a YouTube watch/live/shorts/youtu.be URL ('' otherwise)."""
    if not is_youtube(url):
        return ""
    parts = urllib.parse.urlsplit(str(url).strip())
    candidate = (urllib.parse.parse_qs(parts.query).get("v") or [""])[0]
    if not candidate:
        segments = [item for item in parts.path.split("/") if item]
        if (parts.hostname or "").lower().endswith("youtu.be") and segments:
            candidate = segments[0]
        elif len(segments) >= 2 and segments[0] in ("live", "shorts", "embed", "v"):
            candidate = segments[1]
    return candidate if len(candidate) == 11 and all(c.isalnum() or c in "-_" for c in candidate) else ""


def pace(url: str) -> None:
    """Wait so that YouTube requests from this process start a random MIN_GAP_SEC..MAX_GAP_SEC apart."""
    global _last_request
    gap = random.uniform(MIN_GAP_SEC, max(MIN_GAP_SEC, MAX_GAP_SEC))
    if not is_youtube(url) or gap <= 0:
        return
    with _pace_lock:
        wait = _last_request + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


def _backoff(n: int) -> float:
    # yt-dlp calls retry sleep functions with the keyword ``n`` (attempts so far).
    return min(5.0 * (2 ** max(0, n)), 120.0)  # 5, 10, 20, 40, 80, 120 s


def polite_options(url: str) -> dict:
    """yt-dlp options that keep YouTube traffic small; empty for other sites."""
    if not is_youtube(url):
        return {}
    return {
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 1,
        "retry_sleep_functions": {"http": _backoff, "fragment": _backoff, "extractor": _backoff},
        "concurrent_fragment_downloads": 1,
        "sleep_interval_requests": 1,
    }


@functools.lru_cache(maxsize=1)
def pot_provider_installed() -> bool:
    """The bgutil PO token plugin (bgutil-ytdlp-pot-provider) is importable by yt-dlp."""
    try:
        return importlib.util.find_spec(_BGUTIL_MODULE) is not None
    except (ImportError, ValueError):
        return False


def player_clients() -> list[str]:
    """YouTube player clients to ask for ([] = leave yt-dlp's defaults alone)."""
    configured = os.environ.get("CUT_VIDEO_YOUTUBE_PLAYER_CLIENTS")
    if configured is not None:
        return [item.strip() for item in configured.split(",") if item.strip()]
    # mweb needs a PO token for its formats; without the provider it would only add a request.
    return list(POT_PLAYER_CLIENTS) if pot_provider_installed() else []


def client_options(url: str) -> dict:
    """yt-dlp options every CUT call passes: JS runtimes, and for YouTube the player clients / PO token setup."""
    options: dict = {"js_runtimes": {name: {} for name in JS_RUNTIMES}}
    if not is_youtube(url):
        return options
    family = current_family()
    if family is not None and len(available_families()) > 1:
        # Bind to the family in use, so a bot check is recorded against the right one.
        options["source_address"] = "0.0.0.0" if family == "ipv4" else "::"  # = --force-ipv4 / --force-ipv6
        # The PO token script (Node.js) makes its own requests; keep those on the same family.
        flags = [item for item in os.environ.get("NODE_OPTIONS", "").split()
                 if not item.startswith("--dns-result-order")]
        flags.append(f"--dns-result-order={family}first")
        os.environ["NODE_OPTIONS"] = " ".join(flags)
    extractor_args: dict = {}
    clients = player_clients()
    if clients:
        extractor_args["youtube"] = {"player_client": clients}
    server_home = os.environ.get("CUT_VIDEO_BGUTIL_SERVER_HOME", "").strip()
    if server_home:  # otherwise the plugin looks in %USERPROFILE%\bgutil-ytdlp-pot-provider\server
        extractor_args["youtubepot-bgutilscript"] = {"server_home": [server_home]}
    if extractor_args:
        options["extractor_args"] = extractor_args
    return options


def request_options(url: str) -> dict:
    """``client_options`` plus ``polite_options``: what a CUT request to a site should carry."""
    return {**client_options(url), **polite_options(url)}


def _cache_key(url: str) -> str:
    return video_id(url) or str(url or "").strip()


def _format_url_expiry(info: dict) -> float | None:
    """When the format URLs of this information stop working (YouTube's ``expire`` parameter)."""
    for item in info.get("formats") or []:
        for key in ("url", "manifest_url"):
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(item.get(key) or "")).query)
            except ValueError:
                continue
            expire = (query.get("expire") or [""])[0]
            if expire.isdigit():
                return float(expire)
    return None


def remember_info(url: str, info: dict | None) -> None:
    """Keep the video information of a YouTube URL so the next step need not ask again."""
    if is_youtube(url) and isinstance(info, dict) and info.get("id"):
        with _lock:
            _info_cache[_cache_key(url)] = (time.monotonic(), info)


def cached_info(url: str, *, min_valid_sec: float = 0.0) -> dict | None:
    """Information fetched for this YouTube URL within INFO_CACHE_SEC, else None.

    ``min_valid_sec`` > 0: also None when its format URLs expire sooner than that
    (a download started from them would be refused part way).
    """
    if not is_youtube(url):
        return None
    with _lock:
        entry = _info_cache.get(_cache_key(url))
    if entry is None or time.monotonic() - entry[0] > INFO_CACHE_SEC:
        return None
    if min_valid_sec > 0:
        expiry = _format_url_expiry(entry[1])
        if expiry is not None and expiry - time.time() < min_valid_sec:
            return None
    return entry[1]


def forget_info() -> None:
    with _lock:
        _info_cache.clear()
