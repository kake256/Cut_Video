from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

from faster_whisper import WhisperModel

from . import config


@dataclass
class Word:
    word: str
    start: float
    end: float


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: List[Word] = field(default_factory=list)


def transcribe(
    video_path: Union[str, Path],
    model_size: Optional[str] = None,
    device: Optional[str] = None,
    compute_type: Optional[str] = None,
    language: str = "ja",
):
    """faster-whisperで動画/音声を文字起こしし、単語タイムスタンプ付きセグメントを返す。"""
    model_size = model_size or config.ASR_MODEL_SIZE
    device = device or config.ASR_DEVICE
    compute_type = compute_type or config.ASR_COMPUTE_TYPE

    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    segments_iter, info = model.transcribe(
        str(video_path),
        language=language,
        word_timestamps=True,
        vad_filter=True,
    )

    segments: List[Segment] = []
    for seg in segments_iter:
        words = [Word(w.word, w.start, w.end) for w in (seg.words or [])]
        text = (seg.text or "").strip()
        if not text:
            continue
        segments.append(Segment(start=seg.start, end=seg.end, text=text, words=words))

    return segments, info
