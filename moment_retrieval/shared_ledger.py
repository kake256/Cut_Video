"""Shared clip ledger, so several people using CUT do not clip the same videos or scenes.

Each person appends only to their own file ``<folder>/<name>.jsonl`` in a folder that is
synced between them (Google Drive, OneDrive, Dropbox ...).  Nobody writes another
person's file, so simultaneous use never produces a sync conflict.  Entries are keyed by
the canonical public origin URL (YouTube video / Twitch VOD), because local video IDs
(``vid_...``) differ on every PC.  Videos without a known origin URL are not shared.

Entry kinds:
- ``claim``: started a job on the video (the daily batch skips videos others claimed)
- ``made``: exported a clip of a source range (not uploaded yet)
- ``uploaded``: uploaded a clip of a source range

Instead of a synced folder, the ledger can live behind a "send-to URL": a small Google Apps
Script web app (``gas_ledger.js``) that one person deploys; everybody pastes its ``/exec`` URL,
CUT POSTs its entries there and reads the others' entries for one video with a GET.  The
entries then collect in a spreadsheet in the deployer's Drive.  Nobody else needs a Google
sign-in or Google Drive for desktop; the URL itself is the shared secret.

The ledger is off unless a folder (or URL) and a name are set in ``data/shared_ledger.json``
(``{"folder": "G:\\\\マイドライブ\\\\CUT台帳", "name": "taro"}``) or in the environment
variables ``CUT_SHARED_LEDGER_DIR`` / ``CUT_SHARED_LEDGER_NAME``.  When the folder cannot
be reached, entries wait in ``data/shared_ledger_pending.jsonl`` and are written with the
next entry; reading then sees only the files already there.  Ledger problems never stop
a job.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config, source_origin

KINDS = ("claim", "made", "uploaded")
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\s]+')
_MAX_DURATION_SEC = 48 * 3600
_URL_PATTERN = re.compile(r"^https://script\.google\.com/(?:a/[^/\s]+/)?macros/s/[A-Za-z0-9_-]{20,}/exec$")
_TIMEOUT_SEC = 15
_urlopen = urllib.request.urlopen  # replaced in tests


@dataclass(frozen=True)
class Settings:
    folder: Path | None
    name: str
    url: str = ""


def clean_url(url: object) -> str:
    """The /exec URL of the ledger web app, without query or trailing spaces; "" when not one."""
    text = str(url or "").strip().strip('"').split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return text if _URL_PATTERN.match(text) else ""


def settings_path() -> Path:
    return config.CACHE_ROOT / "shared_ledger.json"


def pending_path() -> Path:
    return config.CACHE_ROOT / "shared_ledger_pending.jsonl"


def safe_name(name: object) -> str:
    return _UNSAFE_NAME.sub("_", str(name or "")).strip("._")[:40]


def settings() -> Settings | None:
    url = os.environ.get("CUT_SHARED_LEDGER_URL", "")
    folder = os.environ.get("CUT_SHARED_LEDGER_DIR", "")
    name = os.environ.get("CUT_SHARED_LEDGER_NAME", "")
    if not ((url or folder) and name):
        try:
            data = json.loads(settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            if not (url or folder):
                url = str(data.get("url") or "")
                folder = str(data.get("folder") or "")
            name = name or str(data.get("name") or "")
    name = safe_name(name)
    if not name:
        return None
    if clean_url(url):
        return Settings(None, name, clean_url(url))
    if not folder.strip():
        return None
    return Settings(Path(folder.strip()), name)


def enabled() -> bool:
    return settings() is not None


class LedgerSettingsError(ValueError):
    pass


def saved_settings() -> dict:
    """What the settings file holds (for filling the form), even when incomplete."""
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def default_name() -> str:
    """The Windows user name, as a starting point for the name each person uses."""
    return safe_name(os.environ.get("USERNAME") or os.environ.get("USER") or "")


def google_drive_roots() -> list[Path]:
    """「マイドライブ」/ My Drive of Google Drive for desktop: a drive letter (streaming mode)
    or a folder in the user's home (mirroring mode)."""
    candidates = [Path(f"{letter}:\\") / label for letter in "GHIJKLMNOPQRSTUVWXYZDEF"
                  for label in ("マイドライブ", "My Drive")]
    home = Path.home()
    candidates += [home / "マイドライブ", home / "My Drive", home / "Google Drive" / "My Drive",
                   home / "Google ドライブ" / "マイドライブ"]
    found = []
    for path in candidates:
        try:
            if path.is_dir():
                found.append(path)
        except OSError:
            continue
    return found


def drive_locations() -> list[str]:
    """Places offered in the 「Googleドライブの場所」 box: My Drive first, then shared drives."""
    return list(dict.fromkeys(str(path) for path in [*google_drive_roots(), *_shared_drive_roots()]))


def ledger_folder_name(drive: str) -> str:
    """The ledger folder already in that place (e.g. shared by someone else), else CUT台帳."""
    try:
        for path in sorted(Path(drive).iterdir()):
            if ("台帳" in path.name or "ledger" in path.name.casefold()) and path.is_dir():
                return path.name
    except (OSError, ValueError):
        pass
    return "CUT台帳"


def suggested_folder() -> str:
    """CUT台帳 in My Drive, or the ledger folder already there (e.g. one shared by someone else)."""
    locations = drive_locations()
    if not locations:
        return ""
    return str(Path(locations[0]) / ledger_folder_name(locations[0]))


def _shared_drive_roots() -> list[Path]:
    found = []
    for root in google_drive_roots():
        for label in ("共有ドライブ", "Shared drives"):
            base = root.parent / label
            try:
                if base.is_dir():
                    found.extend(path for path in sorted(base.iterdir()) if path.is_dir())
            except OSError:
                continue
    return found


def save_settings(folder: str, name: str, *, create: bool = True) -> Settings:
    """Save the folder and name. The folder itself is made only when its parent exists
    (so a drive that is not mounted is never replaced by a local folder)."""
    folder_text = str(folder or "").strip().strip('"')
    clean = safe_name(name)
    if not folder_text:
        raise LedgerSettingsError("同期フォルダを入力してください。")
    if not clean:
        raise LedgerSettingsError("名前を入力してください（人ごとに別の名前にします）。")
    path = Path(folder_text)
    if not path.is_absolute():
        raise LedgerSettingsError("同期フォルダは G:\\マイドライブ\\CUT台帳 のような完全な場所で入力してください。")
    if not path.is_dir():
        if not (create and path.parent.is_dir()):
            raise LedgerSettingsError(f"フォルダが見つかりません: {path}（ドライブが同期されているか確認してください）")
        try:
            path.mkdir()
        except OSError as exc:
            raise LedgerSettingsError(f"フォルダを作れませんでした: {exc}") from exc
    target = settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"folder": str(path), "name": clean}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return Settings(path, clean)


def _request(url: str, *, params: dict | None = None, body: dict | None = None) -> dict:
    """GET (params) or POST (body) to the ledger web app; raises OSError on any failure."""
    target = url + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(target, data=data, headers={"Content-Type": "text/plain; charset=utf-8"})
    try:
        # Apps Script answers a POST with a redirect to the result, which urllib follows as a GET.
        with _urlopen(request, timeout=_TIMEOUT_SEC) as response:
            raw = response.read()
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise OSError(f"ledger URL not reachable: {exc}") from exc
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        page = raw.decode("utf-8", "ignore")
        if "doGet" in page or "doPost" in page:
            # The deployed version has no CUT code (saved after deploying, or still myFunction).
            raise OSError("デプロイした版にCUTのコードが入っていません。コードを貼って保存し、"
                          "「デプロイを管理」→ 編集 →「新バージョン」でデプロイし直してください") from exc
        # A login page instead of JSON: deployed without 「アクセスできるユーザー: 全員」.
        raise OSError("台帳の答えが返ってきません。デプロイの「アクセスできるユーザー」が「全員」か確認してください") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise OSError(f"ledger URL refused: {result.get('error') if isinstance(result, dict) else result}")
    return result


def ping(url: str) -> dict:
    return _request(url)


def save_url(url: str, name: str) -> Settings:
    """Save a send-to URL after checking that it answers like the ledger web app."""
    clean = safe_name(name)
    target = clean_url(url)
    if not clean:
        raise LedgerSettingsError("名前を入力してください（人ごとに別の名前にします）。")
    if not target:
        raise LedgerSettingsError(
            "送信用URLは https://script.google.com/macros/s/…/exec の形です"
            "（Apps Scriptの「ウェブアプリのURL」を貼ってください）。")
    try:
        answer = ping(target)
    except OSError as exc:
        raise LedgerSettingsError(
            f"送信用URLにつながりませんでした: {exc}") from exc
    if answer.get("app") != "cut-ledger":
        raise LedgerSettingsError("このURLはCUTの共有台帳ではないようです。")
    target_path = settings_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps({"url": target, "name": clean}, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return Settings(None, clean, target)


def clear_settings() -> bool:
    try:
        settings_path().unlink()
        return True
    except FileNotFoundError:
        return False


def status() -> dict:
    """State shown in the settings tab: on/off, folder reachable, others seen, waiting entries."""
    current = settings()
    try:
        waiting = sum(1 for line in pending_path().read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        waiting = 0
    result = {"enabled": current is not None, "folder": "", "url": "", "name": "", "reachable": False,
              "others": [], "waiting": waiting,
              "from_env": any(os.environ.get(key) for key in (
                  "CUT_SHARED_LEDGER_URL", "CUT_SHARED_LEDGER_DIR", "CUT_SHARED_LEDGER_NAME"))}
    if current is None:
        return result
    result["name"] = current.name
    if current.url:
        result["url"] = current.url
        try:
            people = ping(current.url).get("people") or []
            result["reachable"] = True
            result["others"] = sorted({safe_name(item) for item in people} - {current.name, ""})
        except OSError:
            pass
        return result
    result["folder"] = str(current.folder)
    try:
        if current.folder.is_dir():
            result["reachable"] = True
            result["others"] = sorted({safe_name(p.stem) for p in current.folder.glob("*.jsonl")}
                                      - {current.name, ""})
    except OSError:
        pass
    return result


def _append(current: Settings, entry: dict) -> bool:
    """Write the entry (and any waiting ones) to the own file; keep them pending on failure."""
    line = json.dumps(entry, ensure_ascii=False)
    pending = pending_path()
    try:
        waiting = [item for item in pending.read_text(encoding="utf-8").splitlines() if item.strip()]
    except OSError:
        waiting = []
    try:
        if current.url:
            _request(current.url, body={"by": current.name, "entries": [json.loads(item) for item in [*waiting, line]]})
            if waiting:
                pending.unlink(missing_ok=True)
            return True
        # A missing folder means the synced drive is not mounted; never create it in its place.
        if not current.folder.is_dir():
            raise OSError(f"ledger folder not found: {current.folder}")
        with (current.folder / f"{current.name}.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(f"{item}\n" for item in [*waiting, line]))
    except (OSError, ValueError):
        try:
            pending.parent.mkdir(parents=True, exist_ok=True)
            with pending.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        return False
    if waiting:
        pending.unlink(missing_ok=True)
    return True


def record(kind: str, url: str, start_sec: float | None = None, end_sec: float | None = None,
           title: str = "") -> bool:
    """Add one entry to the own ledger file. Returns False when off, unshareable or pending."""
    current = settings()
    canonical = source_origin.canonical_source_url(url)
    if current is None or canonical is None or kind not in KINDS:
        return False
    entry: dict = {"v": 1, "kind": kind, "source": canonical, "by": current.name,
                   "at": datetime.now().astimezone().isoformat(timespec="seconds")}
    if kind != "claim":
        start, end = round(float(start_sec), 3), round(float(end_sec), 3)
        if not (0 <= start < end <= _MAX_DURATION_SEC):
            return False
        entry.update(start_sec=start, end_sec=end, title=str(title or "")[:120])
    return _append(current, entry)


def _others_entries(canonical: str) -> list[dict]:
    current = settings()
    if current is None:
        return []
    if current.url:
        try:
            entries = _request(current.url, params={"action": "entries", "source": canonical}).get("entries") or []
        except OSError:
            return []
        found = []
        for item in entries:
            if not isinstance(item, dict) or item.get("kind") not in KINDS:
                continue
            person = safe_name(item.get("by"))
            if not person or person == current.name:
                continue
            if source_origin.canonical_source_url(item.get("source")) != canonical:
                continue
            found.append({**item, "by": person})
        return found
    try:
        files = sorted(current.folder.glob("*.jsonl"))
    except OSError:
        return []
    found = []
    for path in files:
        person = safe_name(path.stem)
        if not person or person == current.name:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for raw in lines:
            try:  # the last line may be half-synced
                item = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(item, dict) or item.get("kind") not in KINDS:
                continue
            if source_origin.canonical_source_url(item.get("source")) != canonical:
                continue
            found.append({**item, "by": person})  # the file name, not the field, says who wrote it
    return found


def claimed_by(url: str) -> list[str]:
    """Other people who started or made clips from this video."""
    canonical = source_origin.canonical_source_url(url)
    if canonical is None:
        return []
    return list(dict.fromkeys(item["by"] for item in _others_entries(canonical)))


def ranges(url: str) -> list[dict]:
    """Source ranges other people clipped from this video, as used-range rows."""
    canonical = source_origin.canonical_source_url(url)
    if canonical is None:
        return []
    found: dict[tuple, dict] = {}
    for item in _others_entries(canonical):
        try:
            start, end = float(item["start_sec"]), float(item["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= start < end <= _MAX_DURATION_SEC):
            continue
        key = (item["by"], round(start, 3), round(end, 3))
        found.setdefault(key, {"start_sec": start, "end_sec": end, "title": str(item.get("title") or "")[:120],
                               "origin": "ledger", "by": item["by"]})
    return sorted(found.values(), key=lambda row: row["start_sec"])


def origin_for_video(conn, video_id: str) -> str | None:
    from . import db

    video = db.get_video(conn, video_id)
    return source_origin.origin_url_for_video(conn, video) if video else None
