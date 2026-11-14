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
import shutil
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
# Clips finished side by side.  Each mostly waits for the AI's plan (about 45 s with
# Claude Sonnet); the render and relisten parts share the GPU without trouble at 3.
FINISH_WORKERS = int(os.environ.get("CUT_VIDEO_FINISH_WORKERS", "3"))
JSDRAW_WORKERS = int(os.environ.get("CUT_VIDEO_JSDRAW_WORKERS", "2"))
# Seconds fetched around each clip range when only the ranges are downloaded.
SECTION_MARGIN_SEC = 2.0
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
    # Claude Code / Codex: put the condensed transcript in the first prompt instead of
    # letting the agent page through it with cut_read_transcript (fewer round trips).
    preload_transcript: bool = True
    # Make each clip as a JS-drawn Short (jsdraw_short, Claude API) instead of the normal finish.
    jsdraw: bool = False
    jsdraw_model: str = "fable"
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
    privacy: str = "private"  # upload as "private" or "unlisted"; public only by the publish button
    source_channel_key: str = ""
    source_video_key: str = ""
    # {"step", "started_at", "seconds"} per pipeline step; scripts/report_auto_jobs.py sums them up.
    timings: list[dict] = field(default_factory=list)


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


def kill_process_tree(process) -> None:
    """Stop a child process together with everything it started.

    ``export_shorts.py`` runs ffmpeg and the AI CLIs start their own helpers;
    ``Popen.kill`` would stop only the direct child and leave those running.
    """
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.kill()  # no-op after taskkill; the only step elsewhere
    except OSError:
        pass


class AutoPipeline:
    """In-process queue with one worker thread; jobs persist as JSON files."""

    def __init__(self, *, index_lock: threading.Lock | None = None, steps: dict | None = None):
        self.index_lock = index_lock or threading.Lock()
        self.jobs: dict[str, AutoJob] = {}
        self.queue: "queue.Queue[str]" = queue.Queue()
        self.cancelled: set[str] = set()
        self.processes: dict[str, object] = {}
        self.video_downloads: dict[str, object] = {}  # job_id -> audio_first.VideoDownload
        # job_id -> {"dir": Path, "files": {output: (section file, offset)}} while finishing from sections
        self.clip_sources: dict[str, dict] = {}
        self.background: list[threading.Thread] = []  # full-video downloads that outlive their job
        self.lock = threading.RLock()
        self.worker: threading.Thread | None = None
        self.steps = {
            "metadata": self._metadata, "download": self._download, "index": self._index,
            "select": self._select, "export": self._export, "finish": self._finish, "upload": self._upload,
            "await_video": self._await_video, "sections": self._sections,
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

    def video_download_status(self, job_id: str) -> str:
        """Progress of the job's background video download ("" when there is none)."""
        with self.lock:
            download = self.video_downloads.get(job_id)
        if download is None:
            return ""
        if download.done:
            return "ダウンロード完了" if download.error is None else "失敗しました"
        return " ".join(str(download.message).split())

    def _check_cancel(self, job: AutoJob) -> None:
        if job.job_id in self.cancelled:
            raise Cancelled()

    def submit(self, source: str, agent: str, *, clip_count: int = 3, min_duration_sec: float = 20.0,
               max_duration_sec: float = SHORTS_MAX_SEC, layout: str = "blur", upload: bool = True,
               model: str = "", effort: str = "", link_only: bool = False, focus: str = "",
               finish: bool = False, privacy: str = "private", preload_transcript: bool = True,
               jsdraw: bool = False, jsdraw_model: str = "fable") -> AutoJob:
        if agent not in agent_runner.PIPELINE_AGENTS:
            raise PipelineError("呼び出すAIを選択してください。")
        if agent == agent_runner.API_AGENT:
            model = ""  # the one-call selector always uses its own model
            from .oneshot_selector import NO_KEY_MESSAGE, has_key

            if not link_only and not has_key():
                raise PipelineError(NO_KEY_MESSAGE)
        try:
            model, effort = agent_runner.validate_model(agent, model, effort)
        except agent_runner.AgentError as exc:
            raise PipelineError(str(exc)) from exc
        if jsdraw and not link_only:
            from . import jsdraw_pipeline
            from .jsdraw_short import MODELS as JSDRAW_MODELS

            if jsdraw_model not in JSDRAW_MODELS:
                raise PipelineError("JS描画のモデルを選んでください。")
            if not jsdraw_pipeline.available_for(agent):  # only the API route needs a key
                raise PipelineError(jsdraw_pipeline.NO_KEY_MESSAGE)
            finish = True  # the JS-drawn Short is made in the finishing step, clip by clip
        if not 1 <= int(clip_count) <= 10:
            raise PipelineError("切り抜き本数は1〜10本で指定してください。")
        if not 5 <= float(min_duration_sec) <= float(max_duration_sec) <= SHORTS_MAX_SEC:
            raise PipelineError(f"長さは 5秒 <= 最短 <= 最長 <= {SHORTS_MAX_SEC}秒（ショートの上限）で指定してください。")
        if layout not in {"blur", "crop"}:
            raise PipelineError("レイアウトを選択してください。")
        if privacy not in ("private", "unlisted"):
            raise PipelineError("アップロード時の公開範囲は「非公開」か「限定公開」を選んでください。")
        job = AutoJob(
            job_id="auto_" + secrets.token_hex(6), source=validate_source(source), agent=agent,
            clip_count=int(clip_count), min_duration_sec=float(min_duration_sec),
            max_duration_sec=float(max_duration_sec), layout=layout, upload=bool(upload),
            model=model, effort=effort, link_only=bool(link_only), focus=clean_focus(focus), finish=bool(finish),
            privacy=privacy, preload_transcript=bool(preload_transcript),
            jsdraw=bool(jsdraw) and not link_only, jsdraw_model=jsdraw_model,
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
            download = self.video_downloads.get(job_id)
        if download is not None:
            download.cancel()
        if process is not None:
            kill_process_tree(process)
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
            from .source_origin import canonical_source_url

            # Claim a pasted URL before the long download so others' daily batches skip it.
            claimed = not job.link_only and self._claim_shared(job, canonical_source_url(job.source) or "")
            job.step = STEPS[0]
            info, channel, local_path = self._call(job, "metadata")
            if channel is not None:
                job.source_channel = f"{channel.name}（{channel.key}）"
                job.source_channel_key = channel.key
            video = channel_policy.video_from_info(info or {}) if info else None
            if video is not None:
                job.source_video_key = video.key
            if local_path is None:
                local_path = self._call(job, "download")
            self._check_cancel(job)
            job.step = STEPS[1]
            job.video_id = self._call(job, "index", Path(local_path))
            if job.link_only:
                self._call(job, "await_video")
                job.state = "done"
                self._log(job, "ダウンロードと文字起こしが完了しました（切り抜きはしていません）。"
                               "「文字起こし済みの動画から選ぶ」から切り抜けます。")
                return
            if not claimed:
                self._claim_shared(job)
            self._check_cancel(job)
            job.step = STEPS[2]
            job.highlight_run_id = self._call(job, "select")
            self._check_cancel(job)
            job.step = STEPS[3]
            # Finishing renders each clip itself, so when the full video is still on its
            # way it fetches just the clip ranges instead of waiting for all of it.
            by_sections = job.finish and self._video_still_downloading(job)
            if not by_sections:
                self._call(job, "await_video")
            self._check_cancel(job)
            job.outputs = self._call(job, "export")
            self._check_cancel(job)
            if by_sections and not self._call(job, "sections"):
                self._call(job, "await_video")  # sections failed: fall back to the full video
                self._check_cancel(job)
            if job.finish:
                self._call(job, "finish")
                self._check_cancel(job)
            job.step = STEPS[4]
            if job.upload:
                self._call(job, "upload")
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
                download = self.video_downloads.pop(job.job_id, None)
                sections = self.clip_sources.pop(job.job_id, None)
            if download is not None:
                if job.state == "done" and job.video_id:
                    self._complete_video_later(job, download)  # clips were made from sections
                else:  # stopped or failed before the video was needed
                    download.cancel()
                self._save(job)
            if sections:
                shutil.rmtree(sections["dir"], ignore_errors=True)

    def _claim_shared(self, job: AutoJob, url: str | None = None) -> bool:
        """Put this video in the shared ledger and say who else already clips it.

        ``url`` None looks up the indexed video's origin; "" means there is none yet.
        """
        from . import shared_ledger

        try:
            if not shared_ledger.enabled():
                return False
            if url is None and job.video_id:
                conn = db.get_conn()
                try:
                    db.init_db(conn)
                    url = shared_ledger.origin_for_video(conn, job.video_id)
                finally:
                    conn.close()
            if not url:
                return False
            others = shared_ledger.claimed_by(url)
            if others:
                self._log(job, f"共有台帳: {'・'.join(others)}さんもこの動画を切り抜いています。その場面は避けて選びます。")
            if not shared_ledger.record("claim", url):
                self._log(job, "共有台帳のフォルダに書き込めないため、記録を保留しました（次に書き込めたときに反映します）。")
            return True
        except Exception as exc:  # the ledger is a courtesy to others; never stop the job for it
            self._log(job, f"共有台帳を使えませんでした（{type(exc).__name__}）。")
            return False

    def _call(self, job: AutoJob, name: str, *args):
        """Run one step and record how long it took, so daily jobs can be compared."""
        started = time.time()
        try:
            return self.steps[name](job, *args)
        finally:
            with self.lock:
                job.timings.append({"step": name, "started_at": round(started, 1),
                                    "seconds": round(time.time() - started, 1)})
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

        from . import youtube_block

        youtube_block.check(job.source)  # refused recently: fail now without asking YouTube again
        self._log(job, "動画情報を取得しています。")
        info = youtube_block.cached_info(job.source)
        if info is None:
            youtube_block.pace(job.source)
            try:
                with YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True,
                                "js_runtimes": {"deno": {}, "node": {}},
                                **youtube_block.polite_options(job.source)}) as ydl:
                    info = ydl.extract_info(job.source, download=False)
            except Exception as exc:
                blocked = youtube_block.note_failure(job.source, exc)
                if blocked is not None:
                    raise blocked from exc
                raise
            youtube_block.note_success(job.source)
            youtube_block.remember_info(job.source, info)
        channel = channel_policy.channel_from_info(info or {})
        self._log(job, f"動画: {info.get('title', '')} / チャンネル: {channel.name if channel else '不明'}")
        return info, channel, None

    def _download(self, job: AutoJob) -> str:
        """Fetch the audio first and the video in the background; else the whole video now."""
        from . import audio_first, source_origin

        save_dir = Path(config.SOURCE_ROOTS[0]) if config.SOURCE_ROOTS else Path("video")
        if audio_first.should_use(job.source):
            audio = audio_first.fetch_audio(
                job.source, save_dir, log=lambda message: self._log(job, message, replace_progress=True))
            self._check_cancel(job)
            if audio is not None:
                conn = db.get_conn()
                try:
                    db.init_db(conn)
                    source_origin.record_download(conn, audio, job.source)
                finally:
                    conn.close()
                if audio_first.is_audio_proxy(audio):
                    with self.lock:
                        self.video_downloads[job.job_id] = audio_first.VideoDownload(job.source, save_dir).start()
                    self._log(job, "音声を先に取得しました。文字起こし・候補選びと並行して、動画をダウンロードします。")
                return str(Path(audio).resolve())
        return self._download_video(job, save_dir)

    def _download_video(self, job: AutoJob, save_dir: Path) -> str:
        from .downloader import download_video
        from . import source_origin

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

    def _await_video(self, job: AutoJob) -> None:
        """Make sure the library entry points at the video (not its audio) before clipping."""
        from . import audio_first

        with self.lock:
            download = self.video_downloads.get(job.job_id)
        if not job.video_id:
            return
        conn = db.get_conn()
        try:
            db.init_db(conn)
            video = db.get_video(conn, job.video_id)
            source_origin_url = None
            if video is not None and download is None and audio_first.pending_video_path(video):
                from . import source_origin

                source_origin_url = source_origin.origin_url_for_video(conn, video)
        finally:
            conn.close()
        if video is None or not audio_first.pending_video_path(video):
            return
        if download is None:
            # An earlier run registered the audio but its video download did not finish.
            url = job.source if job.source.lower().startswith(("http://", "https://")) else source_origin_url
            if not url:
                raise PipelineError("動画が音声だけで登録されていて、元のURLが分からないため切り抜けません。")
            self._log(job, "音声だけで登録済みの動画です。動画をダウンロードします。")
            download = audio_first.VideoDownload(url, Path(str(video["path"])).parent).start()
            with self.lock:
                self.video_downloads[job.job_id] = download
        if not download.done:
            self._log(job, "動画のダウンロードが終わるのを待っています（切り抜きには映像が必要です）。")

        def progress(message: str) -> None:
            self._check_cancel(job)
            self._log(job, f"  動画: {message.strip()}", replace_progress=True)

        try:
            path = download.wait(poll=progress)
        except Cancelled:
            download.cancel()
            raise
        except Exception as exc:
            raise PipelineError(
                f"動画のダウンロードに失敗しました（{exc}）。文字起こしと候補は保存済みです。"
                "同じURLでもう一度実行すると、動画だけ取得して続けます。"
            ) from exc
        finally:
            with self.lock:
                self.video_downloads.pop(job.job_id, None)
        audio_first.swap_to_video(job.video_id, path)
        self._log(job, "動画のダウンロードが完了し、音声から動画に差し替えました（文字起こしはそのまま使います）。")

    def _video_still_downloading(self, job: AutoJob) -> bool:
        with self.lock:
            download = self.video_downloads.get(job.job_id)
        return download is not None and not download.done

    def _sections(self, job: AutoJob) -> bool:
        """Download only the exported clip ranges; False when that fails (wait for the video then)."""
        from .downloader import DownloadCancelled, download_sections

        clips = []
        for output in job.outputs:
            try:
                meta = json.loads(Path(output).with_suffix(".metadata.json").read_text(encoding="utf-8"))
                source = meta["source_range"]
                clips.append((output, float(source["start_sec"]), float(source["end_sec"])))
            except (OSError, ValueError, KeyError, TypeError):
                self._log(job, "切り抜く範囲を読めなかったため、動画全体のダウンロードを待ちます。")
                return False
        with self.lock:
            url = getattr(self.video_downloads.get(job.job_id), "url", "")
        if not clips or not url:
            return False
        if job.jsdraw:  # the JS-drawn Short reads a wider stretch around each clip
            from .jsdraw_pipeline import PREPARE_PAD_SEC, range_for_clip

            try:
                clips = [(output, *range_for_clip(Path(output))) for output, _start, _end in clips]
            except Exception as exc:
                self._log(job, f"JS描画の範囲を決められなかったため、動画全体のダウンロードを待ちます（{type(exc).__name__}）")
                return False
            margin = PREPARE_PAD_SEC + SECTION_MARGIN_SEC
        else:
            margin = SECTION_MARGIN_SEC
        ranges = [(max(0.0, start - margin), end + margin) for _output, start, end in clips]
        directory = _jobs_dir() / f"{job.job_id}.sections"
        with self.lock:
            self.clip_sources[job.job_id] = {"dir": directory, "files": {}}
        footage = sum(end - start for start, end in ranges) / 60
        self._log(job, f"動画全体のダウンロードを待たずに、切り抜く{len(ranges)}区間（計{footage:.0f}分）だけを先に取得します。"
                       "全体のダウンロードは裏で続けます。")
        cancel = threading.Event()
        watcher = threading.Thread(target=self._cancel_when_stopped, args=(job, cancel), daemon=True)
        watcher.start()
        try:
            files = download_sections(url, ranges, directory, cancel_event=cancel)
        except DownloadCancelled:
            raise Cancelled()
        except Exception as exc:
            self._log(job, f"区間だけの取得に失敗したため、動画全体のダウンロードを待ちます（{exc}）"[:300])
            shutil.rmtree(directory, ignore_errors=True)
            with self.lock:
                self.clip_sources.pop(job.job_id, None)
            return False
        finally:
            cancel.set()  # also ends the watcher
        with self.lock:
            self.clip_sources[job.job_id]["files"] = {
                output: (path, start) for (output, _start, _end), path, (start, _stop) in zip(clips, files, ranges)}
        self._log(job, "切り抜く区間の取得が完了しました。")
        return True

    def _cancel_when_stopped(self, job: AutoJob, event: threading.Event) -> None:
        while not event.wait(1.0):
            if job.job_id in self.cancelled:
                event.set()

    def _complete_video_later(self, job: AutoJob, download) -> None:
        """Let the full video finish downloading after the job, then point the library at it."""
        from . import audio_first

        def work():
            try:
                path = download.wait()
                for attempt in range(30):  # another job may hold the library for a while
                    try:
                        audio_first.swap_to_video(job.video_id, path)
                        break
                    except audio_first.SwapError as exc:
                        if "実行中" not in str(exc) or attempt == 29:
                            raise
                        time.sleep(20)
                self._log(job, "動画全体のダウンロードが完了し、ライブラリを音声から動画に差し替えました。")
            except Exception as exc:  # the audio entry stays; a later job downloads the video again
                self._log(job, f"動画全体のダウンロードを完了できませんでした（{type(exc).__name__}）。"
                               "次にこの動画を使うときに取得し直します。")

        thread = threading.Thread(target=work, name="video-complete", daemon=True)
        with self.lock:
            self.background = [item for item in self.background if item.is_alive()] + [thread]
        thread.start()

    def wait_for_background_downloads(self, timeout: float | None = None) -> bool:
        """Block until full-video downloads handed off by finished jobs are done; True if none remain."""
        deadline = None if timeout is None else time.time() + timeout
        with self.lock:
            threads = list(self.background)
        for thread in threads:
            thread.join(None if deadline is None else max(0.0, deadline - time.time()))
        return not any(thread.is_alive() for thread in threads)

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

        started = time.time()
        hints = self._signals(job)
        with self.lock:  # part of "select": waveform and chat, before the AI starts
            job.timings.append({"step": "select.signals", "started_at": round(started, 1),
                                "seconds": round(time.time() - started, 1)})
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

        from .channel_performance import prompt_section as performance_section

        # A requested scene wins over the channel's taste, so the hint is only for open choices.
        performance = "" if job.focus else performance_section()
        if performance:
            self._log(job, "過去の投稿でよく見られた切り抜きとそうでないものを参考に選ぶよう指示します。")

        if job.agent == agent_runner.API_AGENT:
            return self._select_api(job, before, prompt_section(hints) + performance, label)

        def register(process):
            with self.lock:
                self.processes[job.job_id] = process

        preloaded = {}
        if job.preload_transcript:
            from .oneshot_selector import OneshotSelectionError, preloaded_transcript

            try:
                transcript, revision = preloaded_transcript(job.video_id)
            except OneshotSelectionError as exc:
                raise PipelineError(str(exc)) from exc
            preloaded = {"transcript": transcript, "transcript_revision": revision}
            self._log(job, f"文字起こし全体（{transcript.count(chr(10)) + 1}行・{len(transcript)}文字）を最初に渡します。")

        output = agent_runner.run_agent(
            job.agent,
            agent_runner.ClipRequest(job.video_id, job.clip_count, job.min_duration_sec, job.max_duration_sec,
                                     used_ranges=tuple(used), focus=job.focus,
                                     hints=prompt_section(hints) + performance,
                                     confirm_captions=not job.finish, want_summary=job.jsdraw, **preloaded),
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

    def _select_api(self, job: AutoJob, before: set[str], hints: str, label: str) -> str:
        """One Claude API call; the checked picks are stored like ``cut_propose_clips`` results."""
        from . import oneshot_selector

        try:
            result = oneshot_selector.select_clips(
                job.video_id, clip_count=job.clip_count, focus=job.focus, hints=hints,
                min_duration_sec=job.min_duration_sec, max_duration_sec=job.max_duration_sec,
                log=lambda message: self._log(job, message, replace_progress=True), want_summary=job.jsdraw,
            )
            self._log(job, f"API料金: 約{result['cost_usd']:.3f}ドル（入力{result['usage'].get('input_tokens', 0)}"
                           f"・出力{result['usage'].get('output_tokens', 0)}トークン、{result['seconds']:g}秒"
                           + ("、1回聞き直し" if result.get("retried") else "") + "）")
            self._check_cancel(job)
            if not result["clips"]:
                raise PipelineError(
                    f"「{job.focus}」に当てはまる場面が見つかりませんでした。" if job.focus
                    else f"{label} が条件に合う候補を返しませんでした。"
                )
            oneshot_selector.save_proposals(job.video_id, result, min_duration_sec=job.min_duration_sec,
                                            max_duration_sec=job.max_duration_sec)
        except oneshot_selector.OneshotSelectionError as exc:
            raise PipelineError(str(exc)) from exc
        except Exception as exc:
            if type(exc).__module__.startswith("anthropic"):  # network / auth / rate limit
                raise PipelineError(f"Claude APIの呼び出しに失敗しました（{type(exc).__name__}）: {exc}"[:300]) from exc
            raise
        after = self._collect_runs(job, before)
        if not after:
            raise PipelineError(f"{label} が候補を保存しませんでした。")
        for clip in result["clips"]:
            self._log(job, f"  {label}: {oneshot_selector.clock(clip['start_sec'])}〜"
                           f"{oneshot_selector.clock(clip['end_sec'])}「{clip['title']}」")
        return after

    def _export(self, job: AutoJob) -> list[str]:
        status_file = _jobs_dir() / f"{job.job_id}.export.json"
        command = [sys.executable, "export_shorts.py", "--video-id", job.video_id,
                   "--highlight-run-id", job.highlight_run_id, "--layout", job.layout,
                   "--status-file", str(status_file)]
        if job.finish:
            # Finishing renders each clip from the source, so record only the ranges here.
            command.append("--defer-render")
        self._log(job, "9:16ショートとして書き出しています。" if not job.finish
                  else "切り抜く範囲を記録しています（動画は仕上げで作成します）。")
        code = self._run_logged(job, command)
        status = json.loads(status_file.read_text(encoding="utf-8")) if status_file.is_file() else {}
        status_file.unlink(missing_ok=True)
        if code != 0 or status.get("state") != "done":
            raise PipelineError(str(status.get("log") or f"書き出しに失敗しました（exit {code}）。")[-300:])
        outputs = [str(item) for item in status.get("outputs") or []]
        self._log(job, f"{len(outputs)}本を書き出しました。")
        from .used_ranges import record_made

        for output in outputs:
            try:  # others avoid these ranges even before they are uploaded
                record_made(Path(output))
            except Exception:
                pass
        return outputs

    def _finish(self, job: AutoJob) -> None:
        """Give each exported Short a hook title, word-timed captions and sound effects."""
        from concurrent.futures import ThreadPoolExecutor

        from . import relisten

        # JS drawing removes the face background and renders in a browser per clip, and on a
        # login CLI three long generations at once eat the plan's limit quickly: two at a time.
        limit = min(FINISH_WORKERS, JSDRAW_WORKERS) if job.jsdraw else FINISH_WORKERS
        workers = max(1, min(limit, len(job.outputs)))
        self._log(job, "仕上げ（2行タイトル・発話単位の字幕・補足字幕・無音カット・音量調整・効果音）を行います"
                       + (f"（{workers}本ずつ並行）。" if workers > 1 else "。"))
        # Most of a clip's finishing is waiting for the AI's plan, so clips run side by side.
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cut_finish") as pool:
                futures = [pool.submit(self._finish_one, job, index, output)
                           for index, output in enumerate(job.outputs, start=1)]
                try:
                    for future in futures:
                        future.result()
                except Cancelled:
                    for future in futures:
                        future.cancel()
                    raise
        finally:
            relisten.release_model()  # give the GPU memory back to the next job's transcription
        missing = [output for output in job.outputs if not Path(output).is_file()]
        if missing:
            job.outputs = [output for output in job.outputs if output not in missing]
            self._log(job, f"  動画を作れなかった{len(missing)}本は投稿しません。")

    def _finish_one(self, job: AutoJob, index: int, output: str) -> None:
        from . import finishing

        self._check_cancel(job)
        with self.lock:
            section = (self.clip_sources.get(job.job_id) or {}).get("files", {}).get(output)
        if job.jsdraw:
            from . import jsdraw_pipeline

            try:
                record = jsdraw_pipeline.make_for_clip(
                    Path(output), model=job.jsdraw_model, highlight_run_id=job.highlight_run_id, section=section,
                    backend=jsdraw_pipeline.backend_for(job.agent), codex_model=job.model if job.agent == "codex" else "",
                    log=lambda message: self._log(job, f"  {index}本目（JS描画）: {message}", replace_progress=True),
                )
                cost = (f"API料金 約{record['usd']}ドル" if record.get("backend", "api") == "api" else
                        "ログインの枠" + (f"・API換算 約{record['usd_equivalent']}ドル"
                                        if record.get("usd_equivalent") is not None else ""))
                self._log(job, f"  {index}本目: JS描画ショートを作りました（{record['seconds']}秒・{cost}）")
                return
            except Cancelled:
                raise
            except Exception as exc:  # keep the clip: make it the normal way instead
                self._log(job, f"  {index}本目はJS描画に失敗したため、通常の仕上げで作ります"
                               f"（{type(exc).__name__}: {exc}）"[:300])
                self._check_cancel(job)
        try:
            plan = finishing.finish_exported_clip(
                Path(output), agent=job.agent, model=job.model, focus=job.focus,
                log=lambda message: self._log(job, f"  {index}本目:{message.strip()}"
                                              if message.startswith("  ") else message),
                section=section,
            )
        except Exception as exc:  # an unfinished clip is still a valid Short
            self._log(job, f"  {index}本目の仕上げを省略しました（{type(exc).__name__}）")
            if not Path(output).is_file():  # export only recorded the range
                try:
                    finishing.render_plain_clip(Path(output), section=section)
                    self._log(job, f"  {index}本目は仕上げなしの動画を作りました。")
                except Exception as fallback_exc:
                    self._log(job, f"  {index}本目の動画を作れませんでした（{type(fallback_exc).__name__}）")
            return
        self._log(job, f"  {index}本目: タイトル「{plan.hook_text}」・補足字幕{len(plan.notes)}件・効果音{len(plan.sound_effects)}か所")

    def _upload(self, job: AutoJob) -> None:
        from . import youtube_upload

        problems = youtube_upload.setup_problems()
        if problems or not youtube_upload.token_path().is_file():
            self._log(job, "YouTubeアカウントが未連携のため、書き出しまでで終了しました。")
            return
        for output in job.outputs:
            self._check_cancel(job)
            receipt = None
            try:
                for message, result in youtube_upload.upload_private(Path(output), privacy=job.privacy,
                                                                     interactive=False):
                    self._log(job, message, replace_progress=message.startswith("  アップロード中"))
                    receipt = result or receipt
            except youtube_upload.UploadRejected as exc:  # this file only; the others still go up
                self._log(job, str(exc))
                continue
            if receipt:
                job.uploads.append({**receipt, "file": Path(output).name})
                from .used_ranges import record_from_clip

                record_from_clip(Path(output), receipt.get("title", ""))
        if job.uploads:
            label = youtube_upload.PRIVACY_LABELS.get(job.privacy, "非公開")
            self._log(job, f"{label}でアップロードしました。ジョブ一覧のリンクから確認し、「公開する」で公開できます。")

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
