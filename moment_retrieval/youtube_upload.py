"""Upload saved clips to the user's own YouTube channel.

Uploads are ``private`` (default) or ``unlisted`` when the user picks it, so a
clip can be checked by link first.  A video becomes public only through
``publish``, which the GUI calls when the user presses "公開する".  Credentials
live under the private library directory (git-ignored) and are created by the
user's own OAuth flow.
"""
from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from . import config

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    # Read-only access shows which channel is linked in the GUI.
    "https://www.googleapis.com/auth/youtube.readonly",
    # Changing a video's privacy (the "publish" button) needs the full YouTube scope;
    # youtube.upload alone only allows inserting new videos.
    "https://www.googleapis.com/auth/youtube",
]
# Asked for when linking, but uploads keep working without them: a token linked
# before these were added stays usable until the user links again.
OPTIONAL_SCOPES = [
    # Retention and swipe-away rates of posted clips (channel_performance).
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def _token_scopes() -> set[str]:
    try:
        return set(json.loads(token_path().read_text(encoding="utf-8")).get("scopes") or [])
    except (OSError, ValueError, AttributeError):
        return set()


def missing_scopes() -> list[str]:
    """Scopes the saved token lacks (e.g. linked before publishing was supported)."""
    if not token_path().is_file():
        return []
    return [scope for scope in SCOPES if scope not in _token_scopes()]


def missing_optional_scopes() -> list[str]:
    if not token_path().is_file():
        return list(OPTIONAL_SCOPES)
    return [scope for scope in OPTIONAL_SCOPES if scope not in _token_scopes()]
PRIVACY_STATUS = "private"
PRIVACY_LABELS = {"private": "非公開", "unlisted": "限定公開", "public": "公開"}
UPLOAD_PRIVACY = ("private", "unlisted")  # choices for new uploads; public only via publish()
CATEGORY_PEOPLE_AND_BLOGS = "22"
MAX_TITLE = 100
MAX_DESCRIPTION = 5000
MAX_TAGS_CHARS = 450
CHUNK_BYTES = 8 * 1024 * 1024
# One 8 MB chunk normally takes seconds; no answer for this long means the connection is gone.
HTTP_TIMEOUT_SEC = 120
UPLOAD_RETRIES = 5  # googleapiclient backs off 1, 2, 4, 8, 16 s (randomised) between tries


class UploadError(RuntimeError):
    pass


BUNDLED_CLIENT_ENV = "CUT_YOUTUBE_CLIENT_FILE"


def client_secret_path() -> Path:
    """The user's own OAuth client (loaded from the GUI); takes precedence."""
    return config.LIBRARY_ROOT / "youtube_client_secret.json"


def bundled_client_path() -> Path:
    """OAuth client shipped with a distributed copy of CUT (never committed to git)."""
    override = os.environ.get(BUNDLED_CLIENT_ENV, "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "youtube_oauth_client.json"


def active_client_path() -> Path | None:
    for path in (client_secret_path(), bundled_client_path()):
        if path.is_file():
            return path
    return None


def client_source() -> str | None:
    """"own" when the user loaded a client, "bundled" when the app ships one."""
    path = active_client_path()
    if path is None:
        return None
    return "own" if path == client_secret_path() else "bundled"


def token_path() -> Path:
    return config.LIBRARY_ROOT / "youtube_token.json"


def receipt_path(video_path: Path) -> Path:
    return Path(video_path).with_suffix(".youtube.json")


def setup_problems() -> list[str]:
    """Return what the user still has to prepare, in plain Japanese."""
    problems = []
    try:
        import googleapiclient  # noqa: F401
        import google_auth_oauthlib  # noqa: F401
    except ImportError:
        problems.append("必要なライブラリが未インストールです（requirements.txt を入れ直してください）。")
    if active_client_path() is None:
        problems.append(
            "OAuthクライアントがありません。配布されたCUTなら同梱ファイル（youtube_oauth_client.json）を"
            "配布元に確認するか、自分で作成したJSONを読み込んでください（手順は docs/YOUTUBE_UPLOAD.md）。"
        )
    return problems


@dataclass(frozen=True)
class UploadMetadata:
    title: str
    description: str
    tags: tuple[str, ...]

    def request_body(self, privacy: str = PRIVACY_STATUS) -> dict:
        if privacy not in UPLOAD_PRIVACY:
            raise UploadError("アップロード時の公開範囲は「非公開」か「限定公開」です。")
        status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
        return {
            "snippet": {
                "title": self.title,
                "description": self.description,
                "tags": list(self.tags),
                "categoryId": CATEGORY_PEOPLE_AND_BLOGS,
            },
            "status": status,
        }


def _youtube_text(value: object, maximum: int) -> str:
    # YouTube rejects angle brackets in titles/descriptions.
    return re.sub(r"[<>]", "", str(value or "")).strip()[:maximum].rstrip()


def metadata_for(video_path: Path) -> UploadMetadata:
    """Use the saved posting metadata JSON when present, else the file name."""
    video_path = Path(video_path)
    sidecar = video_path.with_suffix(".metadata.json")
    data: dict = {}
    if sidecar.is_file():
        try:
            loaded = json.loads(sidecar.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            data = {}
    title = _youtube_text(data.get("title") or video_path.stem, MAX_TITLE) or "切り抜き"
    description = _youtube_text(data.get("description"), MAX_DESCRIPTION)
    tags: list[str] = []
    used = 0
    for tag in data.get("tags") or []:
        tag = _youtube_text(tag, 30)
        if not tag or used + len(tag) > MAX_TAGS_CHARS:
            continue
        tags.append(tag)
        used += len(tag)
    return UploadMetadata(title, description, tuple(tags))


def load_credentials(*, interactive: bool = True, ask_again: bool = False):
    """Load or refresh the token; run the browser consent flow when needed.

    ``ask_again`` runs the consent flow even for a usable token, to add optional scopes.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_path().is_file() and missing_scopes():
        # An older token without the newer scopes: ask once in the browser to add them.
        if not interactive:
            raise UploadError("追加の許可が必要です。「YouTubeアカウントを連携」をやり直してください。")
    elif token_path().is_file() and not ask_again:
        # Keep the optional scopes the token has, or a refresh would narrow it to SCOPES.
        granted = [scope for scope in OPTIONAL_SCOPES if scope in _token_scopes()]
        creds = Credentials.from_authorized_user_file(str(token_path()), SCOPES + granted)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds = _refresh(creds, Request)
    if not creds or not creds.valid:
        if not interactive:
            raise UploadError("YouTubeへのログインが必要です。")
        client_path = active_client_path()
        if client_path is None:
            raise UploadError("OAuthクライアントのJSONが見つかりません。")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES + OPTIONAL_SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
    token_path().parent.mkdir(parents=True, exist_ok=True)
    token_path().write_text(creds.to_json(), encoding="utf-8")
    return creds


def _refresh(creds, request_factory: Callable, *, attempts: int = 3, sleep: Callable = None):
    """Renew the access token; None only when Google says the sign-in itself is gone.

    A network hiccup used to look like an expired sign-in, which made a background
    upload open the browser sign-in and wait there forever.  Transient errors are
    retried and then reported instead.
    """
    import time

    from google.auth.exceptions import RefreshError

    sleep = sleep or time.sleep
    for attempt in range(attempts):
        try:
            creds.refresh(request_factory())
            return creds
        except RefreshError:
            return None  # revoked or expired (invalid_grant): a new sign-in is needed
        except Exception as exc:
            if attempt + 1 == attempts:
                raise UploadError(
                    f"YouTubeに接続できませんでした（{type(exc).__name__}）。ネットワークを確認して再試行してください。"
                ) from exc
            sleep(2 * (attempt + 1))
    return None


def _service(service_factory: Callable | None, *, interactive: bool = True):
    if service_factory is not None:
        return service_factory()
    from googleapiclient.discovery import build

    from google_auth_httplib2 import AuthorizedHttp

    # Without a timeout a connection dropped mid-upload waited for an answer forever.
    http = AuthorizedHttp(load_credentials(interactive=interactive), http=_transport())
    return build("youtube", "v3", http=http, cache_discovery=False)


def _transport():
    """httplib2 connection with a timeout that leaves 308 alone.

    A plain ``httplib2.Http`` treats 308 ("Resume Incomplete", sent after every
    chunk but the last) as a redirect and fails with RedirectMissingLocation;
    googleapiclient's ``build_http`` removes 308 from the redirect codes.
    """
    from googleapiclient.http import build_http

    http = build_http()
    http.timeout = HTTP_TIMEOUT_SEC
    return http


def connected_channel(*, service_factory: Callable | None = None) -> dict | None:
    """Return the linked channel's id/title, or None when not linked yet."""
    if service_factory is None and not token_path().is_file():
        return None
    try:
        service = _service(service_factory, interactive=False)
        response = service.channels().list(part="snippet", mine=True).execute()
    except UploadError:
        return {"id": "", "title": "", "needs_relink": True} if missing_scopes() else None
    items = response.get("items") or []
    if not items:
        return None
    return {"id": items[0].get("id"), "title": (items[0].get("snippet") or {}).get("title")}


def connect_account() -> dict | None:
    """Run the browser consent flow now and report the linked channel."""
    load_credentials(interactive=True, ask_again=bool(missing_optional_scopes()))
    return connected_channel()


def _validated_client(source: Path) -> dict:
    try:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UploadError("JSONとして読み込めませんでした。Google Cloudからダウンロードしたファイルを選んでください。") from exc
    if isinstance(data, dict) and "web" in data:
        raise UploadError("「ウェブアプリケーション」用のクライアントです。種類「デスクトップアプリ」で作り直してください。")
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict) or not installed.get("client_id") or not installed.get("client_secret"):
        raise UploadError("OAuthクライアント（デスクトップアプリ）のJSONではありません。")
    return data


def install_client_secret(source: Path) -> None:
    """Validate a downloaded OAuth client JSON (desktop app) and store it privately."""
    data = _validated_client(source)
    client_secret_path().parent.mkdir(parents=True, exist_ok=True)
    client_secret_path().write_text(json.dumps(data), encoding="utf-8")


def bundle_own_client() -> Path:
    """Ship the user's own client with this copy of CUT so others only need to sign in.

    The file is git-ignored; distribute it with the app folder, never via a public repository.
    """
    if not client_secret_path().is_file():
        raise UploadError("先に自分のOAuthクライアントJSONを読み込んでください。")
    data = _validated_client(client_secret_path())
    target = bundled_client_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data), encoding="utf-8")
    return target


def disconnect_account() -> bool:
    if token_path().is_file():
        token_path().unlink()
        return True
    return False


def session_path(video_path: Path) -> Path:
    """The resumable upload session of an unfinished upload (removed once it completes)."""
    return Path(video_path).with_suffix(".youtube.session.json")


def _lock_path(video_path: Path) -> Path:
    return Path(video_path).with_suffix(".youtube.uploading")


LOCK_STALE_SEC = 2 * 3600  # a lock this old belongs to a process that died


@contextmanager
def _upload_lock(video_path: Path):
    """One upload per clip at a time, also across processes (the app and a batch script)."""
    lock = _lock_path(video_path)
    for _attempt in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue
            if age < LOCK_STALE_SEC:
                raise UploadError(f"この動画は別の処理がアップロード中です: {Path(video_path).name}") from None
            lock.unlink(missing_ok=True)
    else:
        raise UploadError(f"アップロードの準備ができませんでした: {Path(video_path).name}")
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _existing_upload(service, title: str) -> dict | None:
    """A video with this title among the channel's 50 newest uploads, if any.

    Catches a clip that reached YouTube without a receipt (e.g. an upload whose
    session could not be resumed), so it is not posted a second time.
    """
    try:
        channels = service.channels().list(part="contentDetails", mine=True).execute()
    except AttributeError:  # test doubles that only implement videos()
        return None
    except Exception as exc:
        raise UploadError(
            f"二重投稿の確認のためにチャンネルの動画一覧を読めませんでした（{type(exc).__name__}）。"
            "ネットワークを確認して、もう一度実行してください。"
        ) from exc
    items = channels.get("items") or []
    playlist = ((items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads") if items else None
    if not playlist:
        return None
    uploads = service.playlistItems().list(
        part="snippet,contentDetails", playlistId=playlist, maxResults=50).execute().get("items") or []
    match = next((item for item in uploads if (item.get("snippet") or {}).get("title") == title), None)
    if match is None:
        return None
    video_id = str((match.get("contentDetails") or {}).get("videoId") or "")
    found = service.videos().list(part="status", id=video_id).execute().get("items") or []
    status = (found[0].get("status") or {}) if found else {}
    return {"video_id": video_id, "privacy_status": str(status.get("privacyStatus") or ""),
            "upload_status": str(status.get("uploadStatus") or "")}


LEDGER_NAME = "youtube_uploads.jsonl"


def _fingerprint(video_path: Path) -> str:
    """Size plus a hash of the first and last MB: the same clip file, even if renamed."""
    import hashlib

    path = Path(video_path)
    size = path.stat().st_size
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        digest.update(handle.read(1 << 20))
        if size > 1 << 20:
            handle.seek(max(0, size - (1 << 20)))
            digest.update(handle.read(1 << 20))
    return f"{size}:{digest.hexdigest()}"


def _ledger_entry(video_path: Path) -> dict | None:
    """The folder's upload log outlives a lost receipt, and is current at once
    (the channel's upload list lags behind new uploads by minutes)."""
    ledger = Path(video_path).parent / LEDGER_NAME
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    key = _fingerprint(video_path)
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("fingerprint") == key and entry.get("video_id"):
            return entry
    return None


def _record(video_path: Path, result: dict) -> None:
    """Write the receipt and append the upload to the folder's log."""
    receipt_path(video_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    entry = {"fingerprint": _fingerprint(video_path), "file": Path(video_path).name,
             "video_id": result["video_id"], "title": result.get("title", ""), "at": time.time()}
    with (Path(video_path).parent / LEDGER_NAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _result(video_id: str, privacy: str, title: str) -> dict:
    return {
        "video_id": video_id,
        "privacy_status": privacy,
        "title": title,
        "watch_url": f"https://www.youtube.com/watch?v={video_id}",
        "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
    }


def upload_private(
    video_path: Path,
    *,
    privacy: str = PRIVACY_STATUS,
    service_factory: Callable | None = None,
    interactive: bool = True,
) -> Iterator[tuple[str, dict | None]]:
    """Yield progress messages, then a final receipt for one private (or unlisted) upload.

    Background jobs pass ``interactive=False``: a lost sign-in then fails with a
    message instead of opening a browser sign-in nobody is watching.

    YouTube creates the video as soon as an upload starts, so starting over after
    a failure used to leave a stuck copy and post a duplicate.  The upload session
    is saved instead and the next run resumes it; with no session and no receipt,
    a same-titled video on the channel blocks a new upload.
    """
    video_path = Path(video_path)
    if not video_path.is_file() or video_path.suffix.lower() != ".mp4":
        raise UploadError(f"アップロードできるmp4が見つかりません: {video_path.name}")
    receipt = receipt_path(video_path)
    if receipt.is_file():
        previous = json.loads(receipt.read_text(encoding="utf-8"))
        yield f"アップロード済みのため省略しました: {video_path.name}", previous
        return
    with _upload_lock(video_path):
        if receipt.is_file():  # finished by another process meanwhile
            previous = json.loads(receipt.read_text(encoding="utf-8"))
            yield f"アップロード済みのため省略しました: {video_path.name}", previous
            return
        yield from _upload_locked(video_path, privacy, service_factory, interactive)


def _upload_locked(video_path: Path, privacy: str, service_factory, interactive: bool):
    receipt = receipt_path(video_path)
    session = session_path(video_path)
    metadata = metadata_for(video_path)
    label = PRIVACY_LABELS[privacy]
    try:
        saved = json.loads(session.read_text(encoding="utf-8"))
        saved = saved if isinstance(saved, dict) and saved.get("uri") else None
    except (OSError, ValueError):
        saved = None
    if saved is None:
        logged = _ledger_entry(video_path)
        if logged:
            result = _result(str(logged["video_id"]), privacy, metadata.title)
            receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            yield (f"アップロード済みの記録があったため省略しました（二重投稿を防止）: {result['watch_url']}"), result
            return
    service = _service(service_factory, interactive=interactive)
    if saved is None:
        existing = _existing_upload(service, metadata.title)
        if existing and existing["upload_status"] == "processed":
            result = _result(existing["video_id"], existing["privacy_status"] or privacy, metadata.title)
            _record(video_path, result)
            yield (f"同じタイトルの動画がYouTubeにあったため、それを投稿済みとして記録しました（二重投稿を防止）: "
                   f"{result['watch_url']}"), result
            return
        if existing:
            raise UploadError(
                f"同じタイトルの動画がYouTubeに既にあります（https://studio.youtube.com/video/{existing['video_id']}/edit・"
                f"状態: {existing['upload_status'] or '不明'}）。二重投稿を防ぐためアップロードしませんでした。"
                "Studioで確認し、不要なら削除してからもう一度実行してください。"
            )
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=CHUNK_BYTES, resumable=True)
    request = service.videos().insert(
        part="snippet,status", body=metadata.request_body(privacy), media_body=media,
    )
    if saved is not None:
        # Ask YouTube how much of the earlier upload arrived and continue from there.
        request.resumable_uri = saved["uri"]
        request._in_error_state = True
        yield f"前回の続きからアップロードを再開します（{label}）: {metadata.title}", None
    else:
        yield f"アップロード開始（{label}）: {metadata.title}", None
    response = None
    while response is None:
        try:
            # Timeouts, dropped connections and 5xx/429 are retried with backoff, resuming the upload.
            status, response = request.next_chunk(num_retries=UPLOAD_RETRIES)
        except Exception as exc:
            if isinstance(exc, UploadError):
                raise
            code = getattr(getattr(exc, "resp", None), "status", None)
            if saved is not None and code in (404, 410):
                session.unlink(missing_ok=True)
                raise UploadError(
                    "前回のアップロードは再開できませんでした（YouTube側で期限切れか削除済み）。"
                    "もう一度実行すると新しくアップロードします。"
                ) from exc
            raise UploadError(
                f"アップロードが途中で止まりました（{type(exc).__name__}）。もう一度実行すると、"
                "この動画は続きから再開し、アップロード済みの動画は省略します。"
            ) from exc
        finally:
            # Saved as soon as YouTube hands out the session, so a crash or a stop can resume too.
            if response is None and getattr(request, "resumable_uri", None) and saved is None:
                saved = {"uri": request.resumable_uri, "started_at": time.time(), "title": metadata.title}
                session.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        if status is not None:
            yield f"  アップロード中... {int(status.progress() * 100)}%", None
    video_id = str(response.get("id") or "")
    if not video_id:
        raise UploadError("YouTubeから動画IDが返りませんでした。")
    result = _result(video_id, (response.get("status") or {}).get("privacyStatus", privacy), metadata.title)
    _record(video_path, result)
    session.unlink(missing_ok=True)
    yield f"アップロード完了（{PRIVACY_LABELS.get(result['privacy_status'], label)}）: {result['watch_url']}", result


def publish(video_id: str, *, service_factory: Callable | None = None) -> dict:
    """Make one uploaded video public; callers check channel_policy first."""
    service = _service(service_factory)
    try:
        response = service.videos().update(
            part="status",
            body={"id": video_id, "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}},
        ).execute()
    except Exception as exc:
        if "insufficientPermissions" in str(exc) or "insufficient authentication scopes" in str(exc):
            raise UploadError(
                "公開する許可がありません。「設定」タブで「YouTubeアカウントを連携」をやり直し、"
                "「YouTubeアカウントの管理」を許可してください。"
            ) from exc
        raise
    status = (response.get("status") or {}).get("privacyStatus")
    if status != "public":
        raise UploadError(
            "YouTubeが公開を受け付けませんでした。APIプロジェクトが未監査の場合は非公開に固定されます。"
        )
    return {"video_id": video_id, "privacy_status": status,
            "watch_url": f"https://www.youtube.com/watch?v={video_id}"}


PRIVACY_CHOICES = ("private", "unlisted", "public")


def set_privacy(video_id: str, privacy: str, *, service_factory: Callable | None = None) -> str:
    """Change one uploaded video's privacy; returns the status YouTube reports back."""
    if privacy not in PRIVACY_CHOICES:
        raise UploadError("公開設定は「非公開」「限定公開」「公開」から選んでください。")
    service = _service(service_factory)
    try:
        response = service.videos().update(
            part="status",
            body={"id": video_id, "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}},
        ).execute()
    except Exception as exc:
        if "insufficientPermissions" in str(exc) or "insufficient authentication scopes" in str(exc):
            raise UploadError(
                "公開設定を変える許可がありません。「設定」タブで「YouTubeアカウントを連携」をやり直し、"
                "「YouTubeアカウントの管理」を許可してください。"
            ) from exc
        if "videoNotFound" in str(exc) or "HttpError 404" in str(exc):
            raise UploadError("YouTubeに動画が見つかりません（削除された可能性があります）。") from exc
        raise UploadError(f"公開設定を変更できませんでした（{type(exc).__name__}）。") from exc
    status = str((response.get("status") or {}).get("privacyStatus") or "")
    if status != privacy:
        raise UploadError(
            f"YouTubeが「{PRIVACY_LABELS[privacy]}」への変更を受け付けませんでした。"
            "APIプロジェクトが未監査の場合は非公開に固定されます。"
        )
    return status


def video_statuses(video_ids: list[str], *, service_factory: Callable | None = None) -> dict[str, dict]:
    """Privacy and processing state per video; a video missing from the result was deleted."""
    ids = [str(video_id) for video_id in video_ids if video_id]
    if not ids:
        return {}
    service = _service(service_factory, interactive=False)
    result: dict[str, dict] = {}
    for start in range(0, len(ids), 50):  # videos.list accepts up to 50 ids per call
        response = service.videos().list(part="status", id=",".join(ids[start:start + 50])).execute()
        for item in response.get("items") or []:
            status = item.get("status") or {}
            result[str(item.get("id"))] = {
                "privacy_status": str(status.get("privacyStatus") or ""),
                "upload_status": str(status.get("uploadStatus") or ""),
                "rejection_reason": str(status.get("rejectionReason") or status.get("failureReason") or ""),
            }
    return result
