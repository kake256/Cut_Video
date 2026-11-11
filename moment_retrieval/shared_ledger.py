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

The ledger is off unless a folder and a name are set in ``data/shared_ledger.json``
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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config, source_origin

KINDS = ("claim", "made", "uploaded")
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\s]+')
_MAX_DURATION_SEC = 48 * 3600


@dataclass(frozen=True)
class Settings:
    folder: Path
    name: str


def settings_path() -> Path:
    return config.CACHE_ROOT / "shared_ledger.json"


def pending_path() -> Path:
    return config.CACHE_ROOT / "shared_ledger_pending.jsonl"


def safe_name(name: object) -> str:
    return _UNSAFE_NAME.sub("_", str(name or "")).strip("._")[:40]


def settings() -> Settings | None:
    folder = os.environ.get("CUT_SHARED_LEDGER_DIR", "")
    name = os.environ.get("CUT_SHARED_LEDGER_NAME", "")
    if not (folder and name):
        try:
            data = json.loads(settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            folder = folder or str(data.get("folder") or "")
            name = name or str(data.get("name") or "")
    name = safe_name(name)
    if not (folder.strip() and name):
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
    """「マイドライブ」/ My Drive folders of Google Drive for desktop on any drive letter."""
    found = []
    for letter in "GHIJKLMNOPQRSTUVWXYZDEF":
        for label in ("マイドライブ", "My Drive"):
            path = Path(f"{letter}:\\") / label
            try:
                if path.is_dir():
                    found.append(path)
            except OSError:
                continue
    return found


def suggested_folder() -> str:
    roots = google_drive_roots()
    return str(roots[0] / "CUT台帳") if roots else ""


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
    result = {"enabled": current is not None, "folder": "", "name": "", "reachable": False,
              "others": [], "waiting": waiting,
              "from_env": bool(os.environ.get("CUT_SHARED_LEDGER_DIR") or os.environ.get("CUT_SHARED_LEDGER_NAME"))}
    if current is None:
        return result
    result.update(folder=str(current.folder), name=current.name)
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
        # A missing folder means the synced drive is not mounted; never create it in its place.
        if not current.folder.is_dir():
            raise OSError(f"ledger folder not found: {current.folder}")
        with (current.folder / f"{current.name}.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(f"{item}\n" for item in [*waiting, line]))
    except OSError:
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
