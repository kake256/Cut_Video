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
PRIVACY_STATUS = "private"
PRIVACY_LABELS = {"private": "非公開", "unlisted": "限定公開", "public": "公開"}
UPLOAD_PRIVACY = ("private", "unlisted")  # choices for new uploads; public only via publish()
CATEGORY_PEOPLE_AND_BLOGS = "22"
MAX_TITLE = 100
MAX_DESCRIPTION = 5000
MAX_TAGS_CHARS = 450
CHUNK_BYTES = 8 * 1024 * 1024


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


def load_credentials(*, interactive: bool = True):
    """Load or refresh the token; run the browser consent flow when needed."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_path().is_file() and missing_scopes():
        # An older token without the newer scopes: ask once in the browser to add them.
        if not interactive:
            raise UploadError("追加の許可が必要です。「YouTubeアカウントを連携」をやり直してください。")
    elif token_path().is_file():
        creds = Credentials.from_authorized_user_file(str(token_path()), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None
    if not creds or not creds.valid:
        if not interactive:
            raise UploadError("YouTubeへのログインが必要です。")
        client_path = active_client_path()
        if client_path is None:
            raise UploadError("OAuthクライアントのJSONが見つかりません。")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
    token_path().parent.mkdir(parents=True, exist_ok=True)
    token_path().write_text(creds.to_json(), encoding="utf-8")
    return creds


def _service(service_factory: Callable | None, *, interactive: bool = True):
    if service_factory is not None:
        return service_factory()
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=load_credentials(interactive=interactive),
                 cache_discovery=False)


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
    load_credentials(interactive=True)
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


def upload_private(
    video_path: Path,
    *,
    privacy: str = PRIVACY_STATUS,
    service_factory: Callable | None = None,
) -> Iterator[tuple[str, dict | None]]:
    """Yield progress messages, then a final receipt for one private (or unlisted) upload."""
    video_path = Path(video_path)
    if not video_path.is_file() or video_path.suffix.lower() != ".mp4":
        raise UploadError(f"アップロードできるmp4が見つかりません: {video_path.name}")
    receipt = receipt_path(video_path)
    if receipt.is_file():
        previous = json.loads(receipt.read_text(encoding="utf-8"))
        yield f"アップロード済みのため省略しました: {video_path.name}", previous
        return
    metadata = metadata_for(video_path)
    service = _service(service_factory)
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=CHUNK_BYTES, resumable=True)
    request = service.videos().insert(
        part="snippet,status", body=metadata.request_body(privacy), media_body=media,
    )
    label = PRIVACY_LABELS[privacy]
    yield f"アップロード開始（{label}）: {metadata.title}", None
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status is not None:
            yield f"  アップロード中... {int(status.progress() * 100)}%", None
    video_id = str(response.get("id") or "")
    if not video_id:
        raise UploadError("YouTubeから動画IDが返りませんでした。")
    result = {
        "video_id": video_id,
        "privacy_status": (response.get("status") or {}).get("privacyStatus", privacy),
        "title": metadata.title,
        "watch_url": f"https://www.youtube.com/watch?v={video_id}",
        "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
    }
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
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
