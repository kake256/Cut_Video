"""yt-dlpによる動画ダウンロード (y.pyのロジックを移植)。

インデックス化パイプライン(index_video.run_indexing)と同じパターンで、
進捗メッセージをyieldするジェネレータとして実装する。

yt-dlpのダウンロードはブロッキング呼び出しのため、別スレッドで実行し、
メインのジェネレータがキューをポーリングして進捗をリアルタイムにyieldする。
"""
import queue
import threading
import time
from pathlib import Path
from typing import Iterator, Optional, Tuple


class DownloadError(Exception):
    """ダウンロード失敗(GUI側で表示するためのメッセージ付き)。"""


# 進捗メッセージをyieldする最短間隔 (秒)。フックは毎秒何回も発火するため間引く。
PROGRESS_INTERVAL_SEC = 2.0
# HLS/DASHの断片を同時に取得する数 (yt-dlpの -N)。
CONCURRENT_FRAGMENTS = 8


def _progress_message(d: dict) -> Optional[str]:
    if d.get("status") == "downloading":
        pct = d.get("_percent_str", "").strip()
        speed = d.get("_speed_str", "").strip()
        eta = d.get("_eta_str", "").strip()
        if pct:
            parts = [f"  ダウンロード中... {pct}"]
            if speed:
                parts.append(speed)
            if eta:
                parts.append(f"残り {eta}")
            return " / ".join(parts)
    elif d.get("status") == "finished":
        return "  ダウンロード完了、結合処理中... (大きい動画では数分かかります)"
    return None


def _sanitize(name: str) -> str:
    """ファイル名に使えない文字を除去する。"""
    for ch in '/\\:*?"<>|#':
        name = name.replace(ch, "_")
    return name[:120]


def _build_basename(info: dict) -> str:
    """投稿日時ベースの分かりやすいファイル名 (YYYYMMDD_HHMMSS_<ID>) を作る。

    投稿時刻(timestamp)があれば「日付_時刻_ID」、日付だけなら「日付_ID」、
    どちらも無ければIDのみにフォールバックする。IDを末尾に残すのは
    同一動画の再ダウンロード検出(スキップ)と元動画の特定のため。
    """
    from datetime import datetime

    vid = str(info.get("id", "video"))
    ts = info.get("timestamp") or info.get("release_timestamp")
    if ts:
        try:
            prefix = datetime.fromtimestamp(ts).strftime("%Y%m%d_%H%M%S")
            return _sanitize(f"{prefix}_{vid}")
        except (ValueError, OSError, OverflowError):
            pass
    date = info.get("upload_date") or info.get("release_date")
    if date and str(date).isdigit() and len(str(date)) == 8:
        return _sanitize(f"{date}_{vid}")
    return _sanitize(vid)


AUDIO_SUFFIX = ".audio"  # <basename>.audio.<ext>: the audio-only stand-in for <basename>.mp4


class DownloadCancelled(DownloadError):
    """cancel_event が立ったため中断した。"""


def audio_proxy_for(video_path: Path) -> list[Path]:
    """``<basename>.mp4`` に対応する音声だけのファイル (完成したものだけ)。"""
    video_path = Path(video_path)
    return sorted(path for path in video_path.parent.glob(f"{video_path.stem}{AUDIO_SUFFIX}.*")
                  if not path.name.endswith((".part", ".ytdl")) and ".part-Frag" not in path.name)


def video_path_for_proxy(audio_path: Path) -> Optional[Path]:
    """音声だけのファイルに対応する ``<basename>.mp4`` (音声ファイルでなければ None)。"""
    audio_path = Path(audio_path)
    stem = audio_path.stem
    if not stem.endswith(AUDIO_SUFFIX):
        return None
    return audio_path.with_name(stem[: -len(AUDIO_SUFFIX)] + ".mp4")


def download_video(url: str, save_dir: Path = Path("video"), *, audio_only: bool = False,
                   cancel_event: Optional[threading.Event] = None) -> Iterator[Tuple[str, Optional[Path]]]:
    """URLから動画をダウンロードする。

    進捗メッセージを (message, None) としてリアルタイムにyieldし、
    完了時に最後の要素として (message, 完了ファイルパス) をyieldする。
    既に同じファイルが存在する場合はダウンロードをスキップしてそのパスを返す。
    失敗時はDownloadErrorを送出する。

    audio_only=True では音声だけ (Twitchの Audio_Only、YouTubeの bestaudio) を
    ``<basename>.audio.<ext>`` に保存する。文字起こしを先に始めるための軽い取得で、
    音声だけの形式が無い動画では DownloadError になる。
    cancel_event が立つと、次の進捗通知で中断して DownloadCancelled を送出する。
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError as e:
        raise DownloadError(f"yt-dlpがインストールされていません: {e}")

    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    yield f"URLから動画情報を取得中... ({url})", None

    msg_queue: "queue.Queue[str]" = queue.Queue()

    def _hook(d):
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("ダウンロードを中断しました")
        msg = _progress_message(d)
        if msg:
            msg_queue.put(msg)

    # YouTube's EJS challenges need a JS runtime. yt-dlp enables only Deno
    # by default; also allow a supported Node.js already installed on PATH.
    # Runtime availability/version checks remain owned by yt-dlp.
    runtime_options = {"js_runtimes": {"deno": {}, "node": {}}}
    try:
        with YoutubeDL({"noplaylist": True, "quiet": True, **runtime_options}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise DownloadError(f"動画情報の取得に失敗しました: {e}")

    # 投稿日時ベースの分かりやすいファイル名にする
    basename = _build_basename(info)
    out_path = save_dir / f"{basename}.mp4"
    if out_path.exists():
        yield f"既にダウンロード済みです: {out_path}", out_path
        return
    if audio_only:
        existing = audio_proxy_for(out_path)
        if existing:
            yield f"音声は取得済みです: {existing[0]}", existing[0]
            return
        if not any(str(item.get("vcodec") or "") in ("none", "audio only") or item.get("format_id") == "Audio_Only"
                   for item in info.get("formats") or []):
            raise DownloadError("この動画には音声だけの形式がありません")

    ydl_opts = {
        **runtime_options,
        **({"format": "bestaudio/Audio_Only"} if audio_only
           else {"format": "bestvideo+bestaudio/best", "merge_output_format": "mp4"}),
        "outtmpl": str(save_dir / (f"{basename}{AUDIO_SUFFIX}.%(ext)s" if audio_only else f"{basename}.%(ext)s")),
        "noplaylist": True,
        "retries": 21,
        # Twitch VODs and many streams are HLS fragments; fetching several at
        # once is much faster than one by one.
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
        "progress_hooks": [_hook],
    }

    title = info.get("title", basename)
    duration = info.get("duration")
    dur_note = f" (長さ {duration // 3600}時間{(duration % 3600) // 60}分)" if duration else ""
    what = "音声だけ" if audio_only else "動画"
    yield f"{what}のダウンロードを開始します: {title}{dur_note}", None

    # ダウンロード本体は別スレッドで実行し、進捗をリアルタイムに流す
    result: dict = {}

    def _worker():
        try:
            with YoutubeDL(ydl_opts) as ydl:
                result["info"] = ydl.extract_info(url, download=True)
        except Exception as e:
            result["error"] = e

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    last_yield = 0.0
    while thread.is_alive() or not msg_queue.empty():
        latest = None
        try:
            while True:
                latest = msg_queue.get_nowait()  # 最新のメッセージだけ残す
        except queue.Empty:
            pass

        now = time.monotonic()
        if latest is not None and (now - last_yield) >= PROGRESS_INTERVAL_SEC:
            yield latest, None
            last_yield = now

        if thread.is_alive():
            time.sleep(0.5)

    thread.join()

    if "error" in result:
        error = result["error"]
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("ダウンロードを中断しました") from error
        raise DownloadError(f"ダウンロードに失敗しました: {error}")

    if audio_only:
        found = audio_proxy_for(out_path)
        if not found:
            raise DownloadError(f"ダウンロード後に音声ファイルが見つかりません: {basename}{AUDIO_SUFFIX}.*")
        yield f"音声のダウンロード完了: {found[0]}", found[0]
        return

    dl_info = result.get("info") or {}
    out_path = save_dir / f"{basename}.mp4"
    if not out_path.exists():
        # merge_output_format='mp4' 以外で保存された場合のフォールバック
        ext = dl_info.get("ext", "mp4")
        alt = save_dir / f"{basename}.{ext}"
        if alt.exists():
            out_path = alt
        else:
            raise DownloadError(f"ダウンロード後にファイルが見つかりません: {out_path}")

    yield f"ダウンロード完了: {out_path}", out_path
