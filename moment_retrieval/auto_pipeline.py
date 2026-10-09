"""Paste a link or file -> transcribe -> AI picks clips -> Shorts -> YouTube.

One background worker runs jobs in order.  Every step reuses an existing,
separately tested path: ``download_video``, ``index_video.py`` (own process),
an AI agent through CUT's MCP (``agent_runner``), ``export_shorts.py`` (own
process) and ``youtube_upload``.  Uploads are always private; the GUI offers
a one-click "publish" per clip.
"""
from __future__ import annotations

import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import agent_runner, channel_policy, config, db

REPO_ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"done", "failed", "cancelled"}
# YouTube Shorts accept vertical videos up to 3 minutes.
SHORTS_MAX_SEC = 180
STEPS = ("取り込み", "文字起こし", "候補選び", "書き出し", "投稿")


class Cancelled(RuntimeError):
    pass


class PipelineError(RuntimeError):
    pass


@dataclass
class AutoJob:
    job_id: str
    source: str
    agent: str
    clip_count: int = 3
    min_duration_sec: float = 20.0
    max_duration_sec: float = SHORTS_MAX_SEC
    layout: str = "blur"
    upload: bool = True
    # Only fetch the source and attach it to its (shared) transcript; no clipping.
    link_only: bool = False
    focus: str = ""  # optional: what kind of scene to look for
    finish: bool = False  # hook title, word-timed captions and sound effects before upload
    model: str = ""
    effort: str = ""
    state: str = "queued"
    step: str = ""
    log: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    video_id: str = ""
    source_channel: str = ""
    highlight_run_id: str = ""
    outputs: list[str] = field(default_factory=list)
    uploads: list[dict] = field(default_factory=list)
    source_channel_key: str = ""
    source_video_key: str = ""


def _jobs_dir() -> Path:
    return config.CACHE_ROOT / "auto_jobs"


LIBRARY_PREFIX = "library:"


def library_videos() -> list[dict]:
    """Transcribed library videos (also those made in the editor app) usable for a job.

    A video qualifies when its source file is on this PC, or when a shared
    index carries its original URL (the job then downloads and relinks it).
    """
    from . import source_origin

    conn = db.get_conn()
    try:
        db.init_db(conn)
        result = []
        for video in db.list_public_videos(conn):
            public_id = video["public_video_id"]
            if db.get_active_transcript_revision(conn, public_id) is None:
                continue
            raw = db.get_video(conn, public_id) or {}
            available = video["source_state"] == "available" and Path(str(video["path"])).is_file()
            origin = source_origin.origin_url_for_video(conn, raw)
            if not available and not origin:
                continue
            result.append({
                "video_id": public_id,
                "name": video["display_name"],
                "duration_sec": float(video.get("duration") or 0),
                "source_available": available,
                "origin_url": origin,
            })
        return result
    finally:
        conn.close()


def library_source(video_id: str) -> str:
    """Job source for a library video: itself when local, else its original URL."""
    video = next((item for item in library_videos() if item["video_id"] == video_id), None)
    if video is None:
        raise PipelineError("選んだ動画は文字起こし済みでないか、元動画が見つかりません。")
    return LIBRARY_PREFIX + video_id if video["source_available"] else video["origin_url"]


MAX_FOCUS_CHARS = 200


def clean_focus(text: str) -> str:
    """One-line user request for the AI; quotes and newlines are flattened."""
    value = " ".join(str(text or "").replace("「", "").replace("」", "").replace("'", "").split())
    return value[:MAX_FOCUS_CHARS]


def validate_source(source: str) -> str:
    """Accept an http(s) URL, a library video, or a file inside the video folders."""
    value = str(source or "").strip().strip('"')
    if not value:
        raise PipelineError("動画のURLかファイルのパスを入力してください。")
    if value.startswith(LIBRARY_PREFIX):
        return library_source(value[len(LIBRARY_PREFIX):])
    if value.lower().startswith(("http://", "https://")):
        return value
    path = Path(value).expanduser()
    if not path.is_file():
        raise PipelineError("指定したファイルが見つかりません。")
    resolved = path.resolve()
    roots = [Path(root).resolve() for root in config.SOURCE_ROOTS]
    if not any(resolved.is_relative_to(root) for root in roots):
        raise PipelineError("ファイルは動画フォルダ（" + ", ".join(str(r) for r in roots) + "）に置いてください。")
    return str(resolved)


class AutoPipeline:
    """In-process queue with one worker thread; jobs persist as JSON files."""

    def __init__(self, *, index_lock: threading.Lock | None = None, steps: dict | None = None):
        self.index_lock = index_lock or threading.Lock()
        self.jobs: dict[str, AutoJob] = {}
        self.queue: "queue.Queue[str]" = queue.Queue()
        self.cancelled: set[str] = set()
        self.processes: dict[str, object] = {}
        self.lock = threading.RLock()
        self.worker: threading.Thread | None = None
        self.steps = {
            "metadata": self._metadata, "download": self._download, "index": self._index,
            "select": self._select, "export": self._export, "finish": self._finish, "upload": self._upload,
            **(steps or {}),
        }
        self._load()
        try:
            from .used_ranges import backfill_from_jobs

            # Clips uploaded before posted ranges were tracked still count as used.
            backfill_from_jobs(list(self.jobs.values()))
        except Exception:  # never block startup on bookkeeping
            pass

    # ---------- persistence / bookkeeping ----------
    def _load(self) -> None:
        directory = _jobs_dir()
        if not directory.is_dir():
            return
        for path in sorted(directory.glob("auto_*.json")):
            try:
                job = AutoJob(**json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError):
                continue
            if job.state not in TERMINAL:
                # The app restarted mid-job; resuming half-done work is unsafe.
                job.state = "failed"
                job.log.append("アプリの再起動で中断されました。もう一度実行してください。")
            self.jobs[job.job_id] = job

    def _save(self, job: AutoJob) -> None:
        directory = _jobs_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{job.job_id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(job), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def _log(self, job: AutoJob, message: str, *, replace_progress: bool = False) -> None:
        with self.lock:
            if replace_progress and job.log and job.log[-1].startswith("  ") and message.startswith("  "):
                job.log[-1] = message
            else:
                job.log.append(message)
            self._save(job)

    def _check_cancel(self, job: AutoJob) -> None:
        if job.job_id in self.cancelled:
            raise Cancelled()

    def submit(self, source: str, agent: str, *, clip_count: int = 3, min_duration_sec: float = 20.0,
               max_duration_sec: float = SHORTS_MAX_SEC, layout: str = "blur", upload: bool = True,
               model: str = "", effort: str = "", link_only: bool = False, focus: str = "",
               finish: bool = False) -> AutoJob:
        if agent not in agent_runner.AGENTS:
            raise PipelineError("呼び出すAIを選択してください。")
        try:
            model, effort = agent_runner.validate_model(agent, model, effort)
        except agent_runner.AgentError as exc:
            raise PipelineError(str(exc)) from exc
        if not 1 <= int(clip_count) <= 10:
            raise PipelineError("切り抜き本数は1〜10本で指定してください。")
        if not 5 <= float(min_duration_sec) <= float(max_duration_sec) <= SHORTS_MAX_SEC:
            raise PipelineError(f"長さは 5秒 <= 最短 <= 最長 <= {SHORTS_MAX_SEC}秒（ショートの上限）で指定してください。")
        if layout not in {"blur", "crop"}:
            raise PipelineError("レイアウトを選択してください。")
        job = AutoJob(
            job_id="auto_" + secrets.token_hex(6), source=validate_source(source), agent=agent,
            clip_count=int(clip_count), min_duration_sec=float(min_duration_sec),
            max_duration_sec=float(max_duration_sec), layout=layout, upload=bool(upload),
            model=model, effort=effort, link_only=bool(link_only), focus=clean_focus(focus), finish=bool(finish),
        )
        with self.lock:
            self.jobs[job.job_id] = job
            self._save(job)
        self.queue.put(job.job_id)
        self._ensure_worker()
        return job

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job.state in TERMINAL:
                return False
            self.cancelled.add(job_id)
            process = self.processes.get(job_id)
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
        return True

    def list_jobs(self) -> list[AutoJob]:
        with self.lock:
            return sorted(self.jobs.values(), key=lambda job: job.created_at, reverse=True)

    def _ensure_worker(self) -> None:
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._work, name="auto-pipeline", daemon=True)
            self.worker.start()

    def _work(self) -> None:
        while True:
            try:
                job_id = self.queue.get(timeout=5)
            except queue.Empty:
                return
            job = self.jobs.get(job_id)
            if job is not None and job.state == "queued":
                self.run(job)

    # ---------- the pipeline ----------
    def run(self, job: AutoJob) -> None:
        job.state = "running"
        try:
            self._check_cancel(job)
            job.step = STEPS[0]
            info, channel, local_path = self.steps["metadata"](job)
            if channel is not None:
                job.source_channel = f"{channel.name}（{channel.key}）"
                job.source_channel_key = channel.key
            video = channel_policy.video_from_info(info or {}) if info else None
            if video is not None:
                job.source_video_key = video.key
            if local_path is None:
                local_path = self.steps["download"](job)
            self._check_cancel(job)
            job.step = STEPS[1]
            job.video_id = self.steps["index"](job, Path(local_path))
            if job.link_only:
                job.state = "done"
                self._log(job, "ダウンロードと文字起こしが完了しました（切り抜きはしていません）。"
                               "「文字起こし済みの動画から選ぶ」から切り抜けます。")
                return
            self._check_cancel(job)
            job.step = STEPS[2]
            job.highlight_run_id = self.steps["select"](job)
            self._check_cancel(job)
            job.step = STEPS[3]
            job.outputs = self.steps["export"](job)
            self._check_cancel(job)
            if job.finish:
                self.steps["finish"](job)
                self._check_cancel(job)
            job.step = STEPS[4]
            if job.upload:
                self.steps["upload"](job)
            else:
                self._log(job, "投稿しない設定のため、書き出しまでで終了しました。")
            job.state = "done"
            self._log(job, "完了しました。")
        except Cancelled:
            job.state = "cancelled"
            self._log(job, "停止しました。")
        except Exception as exc:  # every failure ends the job with a readable reason
            job.state = "failed"
            self._log(job, f"失敗（{job.step}）: {exc}")
        finally:
            with self.lock:
                self.processes.pop(job.job_id, None)
                self.cancelled.discard(job.job_id)
                self._save(job)

    def _metadata(self, job: AutoJob):
        if job.source.startswith(LIBRARY_PREFIX):
            conn = db.get_conn()
            try:
                db.init_db(conn)
                video = db.get_public_video(conn, job.source[len(LIBRARY_PREFIX):])
            finally:
                conn.close()
            if not video or not Path(str(video["path"])).is_file():
                raise PipelineError("ライブラリの元動画が見つかりません。")
            self._log(job, f"文字起こし済みの動画を使います: {video['display_name']}")
            return None, None, str(Path(video["path"]).resolve())
        if not job.source.lower().startswith(("http://", "https://")):
            self._log(job, f"ローカルファイルを使います: {Path(job.source).name}（投稿は非公開）")
            return None, None, job.source
        from yt_dlp import YoutubeDL

        self._log(job, "動画情報を取得しています。")
        with YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True,
                        "js_runtimes": {"deno": {}, "node": {}}}) as ydl:
            info = ydl.extract_info(job.source, download=False)
        channel = channel_policy.channel_from_info(info or {})
        self._log(job, f"動画: {info.get('title', '')} / チャンネル: {channel.name if channel else '不明'}")
        return info, channel, None

    def _download(self, job: AutoJob) -> str:
        from .downloader import download_video
        from . import source_origin

        save_dir = Path(config.SOURCE_ROOTS[0]) if config.SOURCE_ROOTS else Path("video")
        path = None
        for message, result in download_video(job.source, save_dir=save_dir):
            self._log(job, message if not message.startswith("  ") else message, replace_progress=True)
            self._check_cancel(job)
            if result is not None:
                path = Path(result)
        if path is None:
            raise PipelineError("ダウンロードしたファイルが見つかりません。")
        conn = db.get_conn()
        try:
            db.init_db(conn)
            source_origin.record_download(conn, path, job.source)
        finally:
            conn.close()
        return str(path.resolve())

    def _indexed_video_id(self, path: Path) -> str | None:
        conn = db.get_conn()
        try:
            db.init_db(conn)
            video = db.find_video_by_path(conn, str(path.resolve()))
            if video and db.get_active_transcript_revision(conn, video["video_id"]):
                return str(video.get("public_video_id") or video["video_id"])
            return None
        finally:
            conn.close()

    def _relink_shared(self, job: AutoJob, path: Path) -> str | None:
        """Attach a downloaded file to an imported shared index with the same URL."""
        if not job.source.lower().startswith(("http://", "https://")):
            return None
        from . import source_origin
        from .share import ShareError, relink_video

        conn = db.get_conn()
        try:
            db.init_db(conn)
            waiting = source_origin.unlinked_videos_for_origin(conn, job.source)
        finally:
            conn.close()
        for public_id in waiting:
            try:
                relink_video(public_id, path)
            except ShareError as exc:
                self._log(job, f"共有インデックスとの関連付けを見送りました: {exc}")
                continue
            self._log(job, "共有インデックスに関連付けました（文字起こしは共有されたものを使います）。")
            return public_id
        return None

    def _index(self, job: AutoJob, path: Path) -> str:
        existing = self._indexed_video_id(path)
        if existing:
            self._log(job, "文字起こし済みのため再利用します。")
            return existing
        shared = self._relink_shared(job, path)
        if shared:
            return shared
        if not self.index_lock.acquire(timeout=1):
            self._log(job, "別の文字起こしが終わるのを待っています。")
            while not self.index_lock.acquire(timeout=5):
                self._check_cancel(job)
        try:
            self._log(job, "文字起こしを開始します（長い動画では時間がかかります）。")
            command = [sys.executable, "index_video.py", "--video", str(path),
                       "--asr-model", config.ASR_MODEL_SIZE]
            code = self._run_logged(job, command)
        finally:
            self.index_lock.release()
        if code != 0:
            raise PipelineError(f"文字起こしが異常終了しました（exit {code}）。")
        video_id = self._indexed_video_id(path)
        if not video_id:
            raise PipelineError("文字起こし後の動画が見つかりません。")
        return video_id

    def _run_logged(self, job: AutoJob, command: list[str]) -> int:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        process = subprocess.Popen(
            command, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env,
        )
        with self.lock:
            self.processes[job.job_id] = process
        for line in iter(process.stdout.readline, ""):
            line = line.rstrip()
            if line and "it/s]" not in line and "Warning" not in line:
                self._log(job, line, replace_progress=line.startswith("  文字起こし中"))
        code = process.wait()
        self._check_cancel(job)
        return code

    def _ready_run_ids(self, video_id: str) -> list[str]:
        """Ready highlight runs of the active transcript, oldest first."""
        conn = db.get_conn()
        try:
            db.init_db(conn)
            revision = db.get_active_transcript_revision(conn, video_id)
            runs = db.list_highlight_runs(conn, video_id, revision) if revision else []
            return [run["highlight_run_id"] for run in reversed(runs) if run and run["status"] == "ready"]
        finally:
            conn.close()

    def _collect_runs(self, job: AutoJob, before: set[str]) -> str | None:
        """The run to export: an AI may save its picks over several calls (e.g. adding a
        replacement after one was rejected); combine them so no pick is dropped."""
        new = [run_id for run_id in self._ready_run_ids(job.video_id) if run_id not in before]
        if len(new) <= 1:
            return new[0] if new else None
        from .mcp_library import LibraryToolError, LibraryTools

        conn = db.get_conn()
        try:
            revision = db.get_highlight_run(conn, new[-1])["transcript_revision"]
            picks, seen = [], set()
            for run_id in new:
                for candidate in db.get_highlight_candidates(conn, run_id):
                    key = (candidate["start_segment_id"], candidate["end_segment_id"])
                    if key not in seen:
                        seen.add(key)
                        picks.append({"start_segment_id": int(candidate["start_segment_id"]),
                                      "end_segment_id": int(candidate["end_segment_id"]),
                                      "title": str(candidate["title"]), "reason": str(candidate["reason"] or "-"),
                                      "summary": str(candidate.get("summary") or "")})
        finally:
            conn.close()
        try:
            merged = LibraryTools().propose_clips(
                job.video_id, revision, picks[:max(job.clip_count, 1)],
                min_duration_sec=job.min_duration_sec, max_duration_sec=job.max_duration_sec,
                note="自動投稿パイプライン（AIの複数回の提案をまとめたもの）",
            )
        except LibraryToolError as exc:
            self._log(job, f"分けて保存された候補をまとめられなかったため、最後の提案を使います: {exc}")
            return new[-1]
        self._log(job, f"AIが候補を{len(new)}回に分けて保存したため、まとめて{len(merged['saved_candidates'])}件にしました。")
        return merged["highlight_run_id"]

    def _signals(self, job: AutoJob) -> list:
        """Excitement cues for the AI: audio profile (built once per video) and stream chat."""
        from . import signals

        conn = db.get_conn()
        try:
            db.init_db(conn)
            video = db.get_video(conn, job.video_id)
        finally:
            conn.close()
        if not video or not Path(str(video["path"])).is_file():
            return []
        source, storage_id = Path(str(video["path"])), str(video["video_id"])
        try:
            signals.ensure_audio(storage_id, source, log=lambda message: self._log(job, message))
        except Exception as exc:  # selection still works from the transcript alone
            self._log(job, f"音声の波形を解析できなかったため、文字起こしだけで選びます（{type(exc).__name__}）")
        self._check_cancel(job)
        if signals.load_chat(storage_id) is None:
            self._fetch_chat(job, video, storage_id)
            self._check_cancel(job)
        hints = signals.hints_for(storage_id, source)
        loud = sum(1 for hint in hints if hint.kind == "loud")
        chat = sum(1 for hint in hints if hint.kind == "chat")
        if hints:
            self._log(job, f"盛り上がりの手がかり: 音量の急上昇{loud}か所" + (f"、コメントの急増{chat}か所" if chat else "")
                      + "をAIに伝えます。")
        return hints

    def _fetch_chat(self, job: AutoJob, video: dict, storage_id: str) -> None:
        """Store the original stream's chat once (an empty record when it has none)."""
        from . import chat_history, signals, source_origin

        url = job.source if job.source.lower().startswith(("http://", "https://")) else None
        if url is None:
            conn = db.get_conn()
            try:
                db.init_db(conn)
                url = source_origin.origin_url_for_video(conn, video)
            finally:
                conn.close()
        if not url:
            return
        try:
            chat = chat_history.fetch(url, log=lambda message: self._log(job, message, replace_progress=True))
        except chat_history.ChatUnavailable as exc:
            self._log(job, f"コメント履歴は使いません: {exc}")
            signals.save_chat(storage_id, {"url": url, "messages": [], "unavailable": str(exc)})
            return
        except Exception as exc:  # network trouble: try again next time
            self._log(job, f"コメント履歴を取得できなかったため、今回は使いません（{type(exc).__name__}）")
            return
        signals.save_chat(storage_id, chat)
        self._log(job, f"コメント履歴を{len(chat['messages'])}件取得しました。")

    def _select(self, job: AutoJob) -> str:
        before = set(self._ready_run_ids(job.video_id))
        from .signals import prompt_section

        hints = self._signals(job)
        label = agent_runner.AGENT_LABELS[job.agent]
        from .used_ranges import for_video

        used = for_video(job.video_id)
        if used:
            self._log(job, f"投稿済みの{len(used)}か所と重ならない場面を選ぶよう指示します。")
        detail = " / ".join(item for item in (job.model, job.effort) if item)
        self._log(job, f"{label}{f'（{detail}）' if detail else ''} に文字起こしを渡して候補を選んでもらいます。")
        if job.focus:
            self._log(job, f"探す場面: {job.focus}")

        if job.agent == "local":
            from .local_selector import LocalSelectionError, select_clips

            try:
                select_clips(
                    job.video_id, clip_count=job.clip_count, min_duration_sec=job.min_duration_sec,
                    max_duration_sec=job.max_duration_sec, model=job.model,
                    log=lambda message: self._log(job, message, replace_progress=True), used=used,
                    focus=job.focus, hints=hints,
                )
            except LocalSelectionError as exc:
                raise PipelineError(str(exc)) from exc
            self._check_cancel(job)
            after = self._collect_runs(job, before)
            if not after:
                raise PipelineError(
                f"「{job.focus}」に当てはまる場面が見つかりませんでした。" if job.focus
                else f"{label} が候補を保存しませんでした。"
            )
            return after

        def register(process):
            with self.lock:
                self.processes[job.job_id] = process

        output = agent_runner.run_agent(
            job.agent,
            agent_runner.ClipRequest(job.video_id, job.clip_count, job.min_duration_sec, job.max_duration_sec,
                                     used_ranges=tuple(used), focus=job.focus, hints=prompt_section(hints)),
            register_process=register,
            model=job.model,
            effort=job.effort,
        )
        self._check_cancel(job)
        after = self._collect_runs(job, before)
        if not after:
            raise PipelineError(
                f"「{job.focus}」に当てはまる場面が見つかりませんでした。" if job.focus
                else f"{label} が候補を保存しませんでした。"
            )
        # Keep the agent's closing report; drop CLI chatter such as token counts.
        noise = {"codex", "tokens used", "user", "assistant"}
        summary = [
            line for line in output.splitlines()
            if line.strip() and line.strip().lower() not in noise
            and not line.strip().replace(",", "").isdigit() and not line.startswith("mcp:")
        ]
        summary = list(dict.fromkeys(summary))[-4:]
        for line in summary:
            self._log(job, f"  {label}: {line}")
        return after

    def _export(self, job: AutoJob) -> list[str]:
        status_file = _jobs_dir() / f"{job.job_id}.export.json"
        command = [sys.executable, "export_shorts.py", "--video-id", job.video_id,
                   "--highlight-run-id", job.highlight_run_id, "--layout", job.layout,
                   "--status-file", str(status_file)]
        self._log(job, "9:16ショートとして書き出しています。")
        code = self._run_logged(job, command)
        status = json.loads(status_file.read_text(encoding="utf-8")) if status_file.is_file() else {}
        status_file.unlink(missing_ok=True)
        if code != 0 or status.get("state") != "done":
            raise PipelineError(str(status.get("log") or f"書き出しに失敗しました（exit {code}）。")[-300:])
        outputs = [str(item) for item in status.get("outputs") or []]
        self._log(job, f"{len(outputs)}本を書き出しました。")
        return outputs

    def _finish(self, job: AutoJob) -> None:
        """Give each exported Short a hook title, word-timed captions and sound effects."""
        from . import finishing

        self._log(job, "仕上げ（引きのタイトル・字幕のタイミング調整・効果音）を行います。")
        for index, output in enumerate(job.outputs, start=1):
            self._check_cancel(job)
            try:
                plan = finishing.finish_exported_clip(
                    Path(output), agent=job.agent, model=job.model, focus=job.focus,
                    log=lambda message: self._log(job, message),
                )
            except Exception as exc:  # an unfinished clip is still a valid Short
                self._log(job, f"  {index}本目の仕上げを省略しました（{type(exc).__name__}）")
                continue
            self._log(job, f"  {index}本目: 引き「{plan.hook_text}」・効果音{len(plan.sound_effects)}か所")

    def _upload(self, job: AutoJob) -> None:
        from . import youtube_upload

        problems = youtube_upload.setup_problems()
        if problems or not youtube_upload.token_path().is_file():
            self._log(job, "YouTubeアカウントが未連携のため、書き出しまでで終了しました。")
            return
        for output in job.outputs:
            self._check_cancel(job)
            receipt = None
            for message, result in youtube_upload.upload_private(Path(output)):
                self._log(job, message, replace_progress=message.startswith("  アップロード中"))
                receipt = result or receipt
            if receipt:
                job.uploads.append({**receipt, "file": Path(output).name})
                from .used_ranges import record_from_clip

                record_from_clip(Path(output), receipt.get("title", ""))
        if job.uploads:
            self._log(job, "非公開でアップロードしました。ジョブ一覧の「公開する」かYouTube Studioで公開できます。")

    def publish(self, job_id: str, youtube_video_id: str) -> dict:
        """One-click publish from the GUI.

        CUT is meant for footage the user has permission to clip, so there is no
        allow-list; publishing stays an explicit click per video.
        """
        from . import youtube_upload

        with self.lock:
            job = self.jobs.get(job_id)
            upload = next((item for item in (job.uploads if job else [])
                           if item.get("video_id") == youtube_video_id), None)
        if job is None or upload is None:
            raise PipelineError("公開する動画が見つかりません。")
        if upload.get("privacy_status") == "public":
            raise PipelineError("この動画は公開済みです。")
        result = youtube_upload.publish(youtube_video_id)
        with self.lock:
            upload["privacy_status"] = "public"
            upload["watch_url"] = result["watch_url"]
            self._log(job, f"公開しました: {result['watch_url']}")
        return result
