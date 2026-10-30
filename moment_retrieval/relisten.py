"""選んだ範囲の音声だけをWhisperで聞き直し、字幕の元になる単語列を返す。

DBの文字起こしはシーン選びには十分だが、字幕には誤認識が残る。範囲を切り出して
高精度設定で認識し直す。initial_promptは静かな音声・BGMで幻覚として出力されるため使わない。
発話区間はVADで見つけるが、vad_filter（無音を詰めて認識し時刻を戻す）は使わない。
区切りをまたぐ単語が数十秒前の位置に置かれることがあるため。区間ごとに
clip_timestamps で認識し、時刻はそのまま元の位置になる。VADを使わずに全体を
認識すると30秒単位で発話を丸ごと落とすことがあるので、それもしない。
幻覚らしいセグメント（低確信度かつ無音らしい、繰り返し、定型の幻覚文）は捨てる。
"""
from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path

from . import config

MAX_RANGE_SEC = 600.0

_model = None
_model_lock = threading.Lock()


def _get_model():
    """WhisperModelをプロセス内で1回だけ読み込む。"""
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            _model = WhisperModel(
                config.ASR_MODEL_SIZE, device=config.ASR_DEVICE, compute_type=config.ASR_COMPUTE_TYPE,
            )
        return _model


def _extract_audio(media_path: Path, start_sec: float, end_sec: float, wav_path: Path) -> None:
    """範囲を16kHzモノラルwavに切り出す。"""
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start_sec:.3f}", "-t", f"{end_sec - start_sec:.3f}",
         "-i", str(media_path), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav_path)],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0 or not wav_path.is_file():
        raise RuntimeError("音声の切り出しに失敗しました。")


_HALLUCINATIONS = ("ご視聴ありがとうございました", "チャンネル登録", "最後までご覧いただき")


def _keep_segment(segment) -> bool:
    """Whisperが無音・BGMから作りがちなセグメントを除く。"""
    text = str(getattr(segment, "text", "") or "")
    if any(phrase in text for phrase in _HALLUCINATIONS):
        return False
    if float(getattr(segment, "compression_ratio", 0.0) or 0.0) > 2.4:
        return False
    no_speech = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
    logprob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
    # no_speech_prob alone is unreliable (0.9 on clear talk); only together with low confidence.
    return not (no_speech > 0.3 and logprob < -1.0)


def _speech_clips(wav_path: Path) -> list[float]:
    """発話区間を [開始, 終了, 開始, 終了, ...]（秒）で返す。叫び声も拾うよう緩めに検出する。"""
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    rate = 16000
    audio = decode_audio(str(wav_path), sampling_rate=rate)
    options = VadOptions(threshold=0.35, min_speech_duration_ms=100, min_silence_duration_ms=500,
                         speech_pad_ms=300)
    regions: list[list[float]] = []
    for item in get_speech_timestamps(audio, options):
        start, end = item["start"] / rate, item["end"] / rate
        if regions and start - regions[-1][1] < 1.0:
            regions[-1][1] = end
        else:
            regions.append([start, end])
    return [value for region in regions for value in region]


def _transcribe_words(wav_path: Path) -> list[dict]:
    """wav全体を認識し、wav先頭からの秒数で単語を返す。"""
    clips = _speech_clips(wav_path)
    if not clips:
        return []
    segments, _info = _get_model().transcribe(
        str(wav_path), language="ja", word_timestamps=True, vad_filter=False, clip_timestamps=clips,
        beam_size=5, condition_on_previous_text=False,
    )
    words: list[dict] = []
    for segment in segments:
        if not _keep_segment(segment):
            continue
        for item in segment.words or []:
            words.append({"start": float(item.start), "end": float(item.end), "word": str(item.word)})
    return words


def retranscribe_range(media_path: Path, start_sec: float, end_sec: float) -> list[dict]:
    """範囲を聞き直し、元動画の絶対秒で {"start","end","word"} を返す。"""
    start_sec, end_sec = float(start_sec), float(end_sec)
    if not end_sec > start_sec or start_sec < 0:
        raise ValueError("範囲の指定が正しくありません。")
    if end_sec - start_sec > MAX_RANGE_SEC:
        raise ValueError(f"聞き直せるのは {MAX_RANGE_SEC:g} 秒までです。")
    with tempfile.TemporaryDirectory(prefix="cut_relisten_") as tmp:
        wav = Path(tmp) / "range.wav"
        _extract_audio(Path(media_path), start_sec, end_sec, wav)
        raw = _transcribe_words(wav)
    length = end_sec - start_sec
    words = []
    for item in raw:
        text = str(item["word"]).strip()
        if not text or item["start"] >= length:
            continue
        words.append({
            "start": round(start_sec + max(0.0, item["start"]), 3),
            "end": round(start_sec + min(length, item["end"]), 3),
            "word": text,
        })
    return words
