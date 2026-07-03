import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Union

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


def detect_batch_size(device: str) -> int:
    """空きVRAMから文字起こしのバッチサイズを自動決定する。

    large-v3 (fp16) はモデル本体+作業領域で約6GBを使うため、残りを
    1バッチあたり約700MBとして割り当てる。GPUが読めない場合は1(逐次)。
    """
    if device != "cuda":
        return 1
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        free_mb = int(out.stdout.strip().splitlines()[0])
    except Exception:
        return 1
    # 6000MB=モデル+作業領域。さらに1500MBは検索(BGE-M3)用に空けておく
    # (インデックス処理中に検索してもVRAMが衝突しないように)
    batch = (free_mb - 7500) // 700
    return max(1, min(int(batch), 16))


def transcribe_stream(
    audio_path: Union[str, Path],
    model_size: Optional[str] = None,
    device: Optional[str] = None,
    compute_type: Optional[str] = None,
    language: str = "ja",
    start_offset: float = 0.0,
    batch_size: Optional[int] = None,
) -> Tuple[Iterator[Segment], object, int]:
    """文字起こしをストリーミングで行う。

    セグメントを認識でき次第yieldするイテレータを返す(呼び出し側で
    進捗表示や途中保存ができる)。start_offsetを指定すると、全タイム
    スタンプにそのオフセットが加算される(音声の途中から再開する場合用)。

    batch_sizeがNoneなら空きVRAMから自動決定し、2以上なら
    BatchedInferencePipelineで並列バッチ推論する(2〜4倍高速)。
    戻り値: (セグメントイテレータ, info, 実際に使ったbatch_size)
    """
    model_size = model_size or config.ASR_MODEL_SIZE
    device = device or config.ASR_DEVICE
    compute_type = compute_type or config.ASR_COMPUTE_TYPE

    if batch_size is None:
        batch_size = detect_batch_size(device)

    model = WhisperModel(model_size, device=device, compute_type=compute_type)

    kwargs = dict(language=language, word_timestamps=True, vad_filter=True)
    if batch_size > 1:
        from faster_whisper import BatchedInferencePipeline

        pipeline = BatchedInferencePipeline(model=model)
        seg_iter, info = pipeline.transcribe(
            str(audio_path), batch_size=batch_size, **kwargs
        )
    else:
        seg_iter, info = model.transcribe(str(audio_path), **kwargs)

    def _gen() -> Iterator[Segment]:
        for seg in seg_iter:
            text = (seg.text or "").strip()
            if not text:
                continue
            words = [
                Word(w.word, w.start + start_offset, w.end + start_offset)
                for w in (seg.words or [])
            ]
            segment = Segment(
                start=seg.start + start_offset,
                end=seg.end + start_offset,
                text=text,
                words=words,
            )
            yield from _split_long_segment(segment)

    return _gen(), info, batch_size


# バッチ推論はVADチャンク単位(30秒超)の粗いセグメントを返すため、
# 単語タイムスタンプを使って文末付近で分割し、検索粒度を保つ
SPLIT_TARGET_SEC = 12.0
SPLIT_MAX_SEC = 18.0
_SENTENCE_ENDINGS = ("。", "?", "？", "!", "！", ".", "、")


def _split_long_segment(seg: Segment) -> Iterator[Segment]:
    if seg.end - seg.start <= SPLIT_MAX_SEC or not seg.words:
        yield seg
        return

    cur: List[Word] = []
    cur_start = None
    for w in seg.words:
        if cur_start is None:
            cur_start = w.start
        cur.append(w)
        dur = w.end - cur_start
        is_sentence_end = w.word.strip().endswith(_SENTENCE_ENDINGS)
        if (dur >= SPLIT_TARGET_SEC and is_sentence_end) or dur >= SPLIT_MAX_SEC:
            text = "".join(x.word for x in cur).strip()
            if text:
                yield Segment(start=cur_start, end=w.end, text=text, words=cur)
            cur = []
            cur_start = None
    if cur:
        text = "".join(x.word for x in cur).strip()
        if text:
            yield Segment(start=cur_start, end=cur[-1].end, text=text, words=cur)
