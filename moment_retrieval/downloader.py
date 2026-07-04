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


def download_video(url: str, save_dir: Path = Path("video")) -> Iterator[Tuple[str, Optional[Path]]]:
    """URLから動画をダウンロードする。

    進捗メッセージを (message, None) としてリアルタイムにyieldし、
    完了時に最後の要素として (message, 完了ファイルパス) をyieldする。
    既に同じファイルが存在する場合はダウンロードをスキップしてそのパスを返す。
    失敗時はDownloadErrorを送出する。
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
        msg = _progress_message(d)
        if msg:
            msg_queue.put(msg)

    try:
        with YoutubeDL({"noplaylist": True, "quiet": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise DownloadError(f"動画情報の取得に失敗しました: {e}")

    # 投稿日時ベースの分かりやすいファイル名にする
    basename = _build_basename(info)
    out_path = save_dir / f"{basename}.mp4"
    if out_path.exists():
        yield f"既にダウンロード済みです: {out_path}", out_path
        return

    ydl_opts = {
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": str(save_dir / f"{basename}.%(ext)s"),
        "noplaylist": True,
        "retries": 21,
        "progress_hooks": [_hook],
    }

    title = info.get("title", basename)
    duration = info.get("duration")
    dur_note = f" (長さ {duration // 3600}時間{(duration % 3600) // 60}分)" if duration else ""
    yield f"ダウンロードを開始します: {title}{dur_note}", None

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
        raise DownloadError(f"ダウンロードに失敗しました: {result['error']}")

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
