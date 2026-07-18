import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("CUT_VIDEO_DATA_DIR", "data"))
# Phase 1 storage roles.  Defaults preserve the current on-disk layout while
# allowing the frequently accessed library/search/cache roots to live on SSD.
LIBRARY_ROOT = Path(os.environ.get("CUT_VIDEO_LIBRARY_ROOT", str(DATA_DIR)))
SEARCH_ROOT = Path(os.environ.get("CUT_VIDEO_SEARCH_ROOT", str(DATA_DIR)))
CACHE_ROOT = Path(os.environ.get("CUT_VIDEO_CACHE_ROOT", str(DATA_DIR)))
SOURCE_ROOTS = tuple(
    Path(item) for item in os.environ.get("CUT_VIDEO_SOURCE_ROOTS", "video").split(os.pathsep)
    if item
)
ARTIFACT_ROOT = Path(os.environ.get("CUT_VIDEO_ARTIFACT_ROOT", "clips"))
DB_PATH = LIBRARY_ROOT / "index.db"
TEXT_INDEX_PATH = SEARCH_ROOT / "text.index"
SEARCH_GENERATIONS_DIR = SEARCH_ROOT / "generations"

# SQLite / cross-process writer coordination.  WAL keeps readers available
# while an index draft is being prepared; publication itself still uses a
# short BEGIN IMMEDIATE compare-and-swap transaction.
SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("CUT_VIDEO_SQLITE_BUSY_TIMEOUT_MS", "10000"))
SQLITE_JOURNAL_MODE = os.environ.get("CUT_VIDEO_SQLITE_JOURNAL_MODE", "WAL").upper()
SQLITE_SYNCHRONOUS = os.environ.get("CUT_VIDEO_SQLITE_SYNCHRONOUS", "NORMAL").upper()
WRITER_LEASE_TIMEOUT_SEC = float(
    os.environ.get("CUT_VIDEO_WRITER_LEASE_TIMEOUT_SEC", "120")
)
WRITER_HEARTBEAT_SEC = float(
    os.environ.get("CUT_VIDEO_WRITER_HEARTBEAT_SEC", "15")
)


def search_generations_dir() -> Path:
    override = os.environ.get("CUT_VIDEO_SEARCH_GENERATIONS_DIR")
    return Path(override) if override else Path(TEXT_INDEX_PATH).parent / "generations"

# チャンク分割 (検索単位は短め、前後オーバーラップで文脈を確保)
CHUNK_SEC = 15.0
OVERLAP_SEC = 5.0

# 切り出し時に前後へ足すパディング秒数
PAD_SEC = 1.5

# 検索の足切り閾値 (テストでは的中0.59+/無関係0.46以下だった)
MIN_SCORE = 0.55

# 境界拡張: 発話ギャップがこの秒数以上空いたら「話の切れ目」とみなす
BOUNDARY_GAP_SEC = 1.0
# 境界拡張の最大延長秒数 (前方向/後ろ方向)
EXTEND_BACK_MAX_SEC = 10.0
EXTEND_FWD_MAX_SEC = 20.0

# faster-whisper
ASR_MODEL_SIZE = "large-v3"
ASR_DEVICE = "cuda"
ASR_COMPUTE_TYPE = "float16"

# テキスト埋め込み (BGE-M3)
EMBED_MODEL_NAME = "BAAI/bge-m3"
EMBED_VECTOR_DIM = 1024
