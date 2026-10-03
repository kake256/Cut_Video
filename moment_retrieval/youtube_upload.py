"""Upload saved clips to the user's own YouTube channel as *private* videos.

Publishing stays a human step in YouTube Studio: this module never sets any
privacy status other than ``private``.  Credentials live under the private
library directory (git-ignored) and are created by the user's own OAuth flow.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from . import config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
PRIVACY_STATUS = "private"
CATEGORY_PEOPLE_AND_BLOGS = "22"
MAX_TITLE = 100
MAX_DESCRIPTION = 5000
MAX_TAGS_CHARS = 450
CHUNK_BYTES = 8 * 1024 * 1024


class UploadError(RuntimeError):
    pass


def client_secret_path() -> Path:
    return config.LIBRARY_ROOT / "youtube_client_secret.json"


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
    if not client_secret_path().is_file():
        problems.append(
            f"OAuthクライアントのJSONを {client_secret_path()} に置いてください"
            "（手順は docs/YOUTUBE_UPLOAD.md）。"
        )
    return problems


@dataclass(frozen=True)
class UploadMetadata:
    title: str
    description: str
    tags: tuple[str, ...]

    def request_body(self) -> dict:
        return {
            "snippet": {
                "title": self.title,
                "description": self.description,
                "tags": list(self.tags),
                "categoryId": CATEGORY_PEOPLE_AND_BLOGS,
            },
            "status": {
                "privacyStatus": PRIVACY_STATUS,
                "selfDeclaredMadeForKids": False,
            },
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
    if token_path().is_file():
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
        if not client_secret_path().is_file():
            raise UploadError("OAuthクライアントのJSONが見つかりません。")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path()), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True)
    token_path().parent.mkdir(parents=True, exist_ok=True)
    token_path().write_text(creds.to_json(), encoding="utf-8")
    return creds


def upload_private(
    video_path: Path,
    *,
    service_factory: Callable | None = None,
) -> Iterator[tuple[str, dict | None]]:
    """Yield progress messages, then a final receipt for one private upload."""
    video_path = Path(video_path)
    if not video_path.is_file() or video_path.suffix.lower() != ".mp4":
        raise UploadError(f"アップロードできるmp4が見つかりません: {video_path.name}")
    receipt = receipt_path(video_path)
    if receipt.is_file():
        previous = json.loads(receipt.read_text(encoding="utf-8"))
        yield f"アップロード済みのため省略しました: {video_path.name}", previous
        return
    metadata = metadata_for(video_path)
    if service_factory is None:
        from googleapiclient.discovery import build

        creds = load_credentials()
        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    else:
        service = service_factory()
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=CHUNK_BYTES, resumable=True)
    request = service.videos().insert(
        part="snippet,status", body=metadata.request_body(), media_body=media,
    )
    yield f"アップロード開始（非公開）: {metadata.title}", None
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
        "privacy_status": (response.get("status") or {}).get("privacyStatus", PRIVACY_STATUS),
        "title": metadata.title,
        "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
    }
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    yield f"アップロード完了（非公開）: {result['studio_url']}", result
