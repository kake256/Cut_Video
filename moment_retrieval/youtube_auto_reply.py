"""Reply automatically with canned text to new comments on the user's own channel.

Fully automatic (no approval step), but deliberately slow to stay far from
spam behaviour: at most ``daily_limit`` replies per local day (default 3), one
reply per run, at least ``MIN_GAP_MINUTES`` between replies and a random template
per reply.  Past unanswered comments are included oldest first by default; with
``include_past`` off, only comments posted after the first save are answered.

The OAuth token is separate from the upload token (``youtube_upload``) because
``comments.insert`` needs the ``youtube.force-ssl`` scope; linking here never
invalidates the upload login.  Settings, the token and the reply log live under
the private library directory (git-ignored).
"""
from __future__ import annotations

import json
import random
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import config, youtube_upload

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
DEFAULT_DAILY_LIMIT = 3
MAX_DAILY_LIMIT = 20
DEFAULT_INTERVAL_MINUTES = 30
MIN_INTERVAL_MINUTES = 10
MIN_GAP_MINUTES = 10
REPLIES_PER_RUN = 1
MAX_THREAD_PAGES = 2
# With past comments included: 20 pages x 50 threads, 1 quota unit per page.
MAX_THREAD_PAGES_PAST = 20
MAX_REPLY_CHARS = 500


class AutoReplyError(RuntimeError):
    pass


def token_path() -> Path:
    return config.LIBRARY_ROOT / "youtube_reply_token.json"


def settings_path() -> Path:
    return config.LIBRARY_ROOT / "youtube_auto_reply.json"


def db_path() -> Path:
    return config.LIBRARY_ROOT / "youtube_auto_reply.db"


# ----------------------------------------------------------------- settings

@dataclass
class Settings:
    enabled: bool = False
    templates: list[str] = field(default_factory=list)
    daily_limit: int = DEFAULT_DAILY_LIMIT
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    # When off, comments published before ``since`` are never answered.
    since: str = ""
    # Also answer older unanswered comments, oldest first (still within the daily limit).
    include_past: bool = True
    # "pc": this app replies while it is open.  "gas": Google Apps Script replies (PC may be off).
    run_on: str = "pc"


def normalize_templates(items) -> list[str]:
    """Each item is one (possibly multi-line) reply; blanks and duplicates are dropped."""
    templates = []
    for item in items or []:
        text = "\n".join(line.rstrip() for line in str(item).replace("\r\n", "\n").split("\n")).strip()
        text = text[:MAX_REPLY_CHARS].strip()
        if text and text not in templates:
            templates.append(text)
    return templates


def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def load_settings() -> Settings:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    templates = data.get("templates")
    return Settings(
        enabled=data.get("enabled") is True,
        templates=normalize_templates(templates) if isinstance(templates, list) else [],
        daily_limit=_clamp(data.get("daily_limit"), 1, MAX_DAILY_LIMIT, DEFAULT_DAILY_LIMIT),
        interval_minutes=_clamp(data.get("interval_minutes"), MIN_INTERVAL_MINUTES, 24 * 60,
                                DEFAULT_INTERVAL_MINUTES),
        since=str(data.get("since") or ""),
        include_past=data.get("include_past") is not False,
        run_on="gas" if data.get("run_on") == "gas" else "pc",
    )


def save_settings(enabled: bool, templates, daily_limit, interval_minutes,
                  *, include_past: bool | None = None, run_on: str | None = None,
                  now: datetime | None = None) -> Settings:
    templates = normalize_templates(templates)
    if enabled and not templates:
        raise AutoReplyError("有効にするには定型文を1つ以上入力してください。")
    previous = load_settings()
    since = previous.since or (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    settings = Settings(
        enabled=bool(enabled),
        templates=templates,
        daily_limit=_clamp(daily_limit, 1, MAX_DAILY_LIMIT, DEFAULT_DAILY_LIMIT),
        interval_minutes=_clamp(interval_minutes, MIN_INTERVAL_MINUTES, 24 * 60, DEFAULT_INTERVAL_MINUTES),
        since=since,
        include_past=previous.include_past if include_past is None else bool(include_past),
        run_on=previous.run_on if run_on is None else ("gas" if run_on == "gas" else "pc"),
    )
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
    return settings


def add_template(text: str, *, now: datetime | None = None) -> Settings:
    """Append one reply (line breaks are kept) and save, leaving other settings as they are."""
    current = load_settings()
    new = normalize_templates([text])
    if not new:
        raise AutoReplyError("追加する定型文を入力してください。")
    if new[0] in current.templates:
        raise AutoReplyError("同じ定型文が既に登録されています。")
    return save_settings(current.enabled, current.templates + new, current.daily_limit,
                         current.interval_minutes, now=now)


def remove_template(index) -> Settings:
    current = load_settings()
    try:
        position = int(index)
    except (TypeError, ValueError):
        raise AutoReplyError("削除する定型文を選んでください。") from None
    if not 0 <= position < len(current.templates):
        raise AutoReplyError("削除する定型文を選んでください。")
    remaining = current.templates[:position] + current.templates[position + 1:]
    # Removing the last template also switches replies off; nothing would be left to send.
    return save_settings(current.enabled and bool(remaining), remaining, current.daily_limit,
                         current.interval_minutes)


# ----------------------------------------------------------------- reply log

def _connect() -> sqlite3.Connection:
    db_path().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path()))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS replies ("
        " comment_id TEXT PRIMARY KEY, video_id TEXT, author TEXT, comment_text TEXT,"
        " reply_text TEXT, reply_id TEXT, status TEXT NOT NULL, error TEXT,"
        " created_at TEXT NOT NULL, local_day TEXT NOT NULL)"
    )
    return conn


def _record(comment: dict, *, status: str, reply_text: str = "", reply_id: str = "",
            error: str = "", now: datetime) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO replies VALUES (?,?,?,?,?,?,?,?,?,?)",
            (comment["id"], comment.get("video_id", ""), comment.get("author", ""),
             comment.get("text", "")[:1000], reply_text, reply_id, status, error[:500],
             now.astimezone(timezone.utc).isoformat(), now.astimezone().date().isoformat()),
        )


def _handled_ids() -> set[str]:
    with _connect() as conn:
        return {row[0] for row in conn.execute("SELECT comment_id FROM replies")}


def replies_today(now: datetime | None = None) -> int:
    day = (now or datetime.now(timezone.utc)).astimezone().date().isoformat()
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM replies WHERE status='replied' AND local_day=?",
                           (day,)).fetchone()
    return int(row[0])


def _last_reply_at() -> datetime | None:
    with _connect() as conn:
        row = conn.execute("SELECT MAX(created_at) FROM replies WHERE status='replied'").fetchone()
    return parse_time(row[0]) if row and row[0] else None


def _last_template() -> str:
    with _connect() as conn:
        row = conn.execute(
            "SELECT reply_text FROM replies WHERE status='replied' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return row[0] if row else ""


def recent_log(limit: int = 20) -> list[dict]:
    if not db_path().is_file():
        return []
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM replies ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


# ----------------------------------------------------------------- OAuth

def parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def oauth_credentials(token: Path, scopes: list[str], *, interactive: bool, login_message: str):
    """Same flow as the upload login, with a token file and scopes of its own."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token.is_file():
        creds = Credentials.from_authorized_user_file(str(token), scopes)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None
    if not creds or not creds.valid:
        if not interactive:
            raise AutoReplyError(login_message)
        client_path = youtube_upload.active_client_path()
        if client_path is None:
            raise AutoReplyError("OAuthクライアントのJSONがありません。「設定」タブで読み込んでください。")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_path), scopes)
        creds = flow.run_local_server(port=0, open_browser=True)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    return creds


def load_credentials(*, interactive: bool = True):
    return oauth_credentials(
        token_path(), SCOPES, interactive=interactive,
        login_message="コメント返信用のYouTubeログインが必要です。「返信用に連携」を押してください。")


def _service(service_factory: Callable | None, *, interactive: bool):
    if service_factory is not None:
        return service_factory()
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=load_credentials(interactive=interactive),
                 cache_discovery=False)


def connect_account() -> None:
    load_credentials(interactive=True)


def disconnect_account() -> bool:
    if token_path().is_file():
        token_path().unlink()
        return True
    return False


def is_connected() -> bool:
    return token_path().is_file()


# ----------------------------------------------------------------- core

def _own_channel_id(service) -> str:
    items = service.channels().list(part="id", mine=True).execute().get("items") or []
    if not items or not items[0].get("id"):
        raise AutoReplyError("連携したGoogleアカウントにYouTubeチャンネルがありません。")
    return str(items[0]["id"])


def _author_id(comment_resource: dict) -> str:
    return str(((comment_resource.get("snippet") or {}).get("authorChannelId") or {}).get("value") or "")


def _already_replied_by_owner(service, thread: dict, owner_id: str) -> bool:
    included = (thread.get("replies") or {}).get("comments") or []
    if any(_author_id(reply) == owner_id for reply in included):
        return True
    total = int((thread.get("snippet") or {}).get("totalReplyCount") or 0)
    if total <= len(included):
        return False
    # The thread resource carries only a few replies; look at all of them.
    top_id = thread["snippet"]["topLevelComment"]["id"]
    request = service.comments().list(part="snippet", parentId=top_id, maxResults=100, textFormat="plainText")
    while request is not None:
        response = request.execute()
        if any(_author_id(reply) == owner_id for reply in response.get("items") or []):
            return True
        request = service.comments().list_next(request, response)
    return False


def _candidates(service, owner_id: str, since: datetime | None, handled: set[str]):
    """Yield top-level comments by other people that the owner has not answered, oldest
    first.  ``since=None`` includes past comments (within the newest pages fetched)."""
    request = service.commentThreads().list(
        part="snippet,replies", allThreadsRelatedToChannelId=owner_id,
        order="time", maxResults=50, textFormat="plainText",
    )
    pending = []
    reached_cutoff = False
    for _page in range(MAX_THREAD_PAGES if since is not None else MAX_THREAD_PAGES_PAST):
        if request is None or reached_cutoff:
            break
        response = request.execute()
        for thread in response.get("items") or []:
            snippet = thread.get("snippet") or {}
            top = snippet.get("topLevelComment") or {}
            top_snippet = top.get("snippet") or {}
            published = parse_time(top_snippet.get("publishedAt") or "")
            if published is None:
                continue
            if since is not None and published < since:
                reached_cutoff = True  # order=time: everything after this is older
                break
            comment_id = str(top.get("id") or "")
            if not comment_id or comment_id in handled:
                continue
            if _author_id(top) == owner_id or snippet.get("canReply") is False:
                continue
            pending.append((published, thread))
        request = service.commentThreads().list_next(request, response)
    pending.sort(key=lambda item: item[0])
    for _published, thread in pending:
        # Checked lazily: a full reply listing costs quota, and only one reply is sent per run.
        if _already_replied_by_owner(service, thread, owner_id):
            continue
        snippet = thread["snippet"]
        top = snippet["topLevelComment"]
        top_snippet = top.get("snippet") or {}
        yield {
            "id": str(top["id"]),
            "video_id": str(snippet.get("videoId") or top_snippet.get("videoId") or ""),
            "author": str(top_snippet.get("authorDisplayName") or ""),
            "text": str(top_snippet.get("textDisplay") or top_snippet.get("textOriginal") or ""),
        }


def choose_template(templates: list[str], *, last: str = "", rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    pool = [t for t in templates if t != last] or list(templates)
    return rng.choice(pool)


@dataclass
class RunResult:
    replied: int
    message: str


_run_lock = threading.Lock()


def run_once(*, manual: bool = False, service_factory: Callable | None = None,
             now: datetime | None = None, rng: random.Random | None = None) -> RunResult:
    """Reply to at most one unanswered comment, respecting every limit.  Never raises for
    'nothing to do'; raises AutoReplyError for problems the user must fix."""
    if not _run_lock.acquire(blocking=False):
        return RunResult(0, "別の返信処理が実行中です。")
    try:
        return _run_once(manual=manual, service_factory=service_factory,
                         now=now or datetime.now(timezone.utc), rng=rng)
    finally:
        _run_lock.release()


def _run_once(*, manual: bool, service_factory, now: datetime, rng) -> RunResult:
    settings = load_settings()
    if settings.run_on == "gas":
        return RunResult(0, "GAS（Googleのサーバー）で返信しているため、このパソコンからは返信しません。")
    if not settings.enabled and not manual:
        return RunResult(0, "自動返信は無効です。")
    if not settings.templates or not settings.since:
        return RunResult(0, "定型文を入力して設定を保存してください。")
    done_today = replies_today(now)
    if done_today >= settings.daily_limit:
        return RunResult(0, f"今日の上限（{settings.daily_limit}件）に達しています。")
    last = _last_reply_at()
    if last is not None and now - last < timedelta(minutes=MIN_GAP_MINUTES):
        return RunResult(0, f"前回の返信から{MIN_GAP_MINUTES}分たっていないため待機します。")
    since = parse_time(settings.since)
    if since is None:
        return RunResult(0, "設定を保存し直してください。")
    if settings.include_past:
        since = None

    service = _service(service_factory, interactive=False)
    owner_id = _own_channel_id(service)
    budget = min(REPLIES_PER_RUN, settings.daily_limit - done_today)
    replied = 0
    last_text = _last_template()
    for comment in _candidates(service, owner_id, since, _handled_ids()):
        if replied >= budget:
            break
        text = choose_template(settings.templates, last=last_text, rng=rng)
        text = text.replace("{name}", comment["author"].lstrip("@"))[:MAX_REPLY_CHARS]
        try:
            response = service.comments().insert(
                part="snippet", body={"snippet": {"parentId": comment["id"], "textOriginal": text}},
            ).execute()
        except Exception as exc:
            # Recorded so a permanent refusal (e.g. comments turned off) is not retried every run.
            _record(comment, status="error", reply_text=text, error=f"{type(exc).__name__}: {exc}", now=now)
            return RunResult(replied, f"返信に失敗しました（{type(exc).__name__}）。このコメントは今後スキップします。")
        _record(comment, status="replied", reply_text=text, reply_id=str(response.get("id") or ""), now=now)
        replied += 1
        last_text = text
    if replied:
        return RunResult(replied, f"{replied}件に返信しました（今日 {done_today + replied}/{settings.daily_limit}件）。")
    return RunResult(0, "返信する新しいコメントはありません。")


# ----------------------------------------------------------------- background

class BackgroundReplier:
    """Runs ``run_once`` every ``interval_minutes`` while the app is open."""

    def __init__(self, runner: Callable[[], RunResult] = run_once):
        self._runner = runner
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_message = ""

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="youtube-auto-reply", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> None:
        settings = load_settings()
        if not settings.enabled or settings.run_on == "gas" or not is_connected():
            return
        try:
            self.last_message = self._runner().message
        except Exception as exc:  # keep the loop alive; the UI shows the last problem
            self.last_message = f"自動返信でエラー: {type(exc).__name__}: {exc}"[:300]

    def _loop(self) -> None:
        # Short first wait so starting the app does not immediately post.
        while not self._stop.wait(60):
            self.tick()
            if self._stop.wait(max(0, load_settings().interval_minutes * 60 - 60)):
                return
