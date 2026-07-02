from pathlib import Path

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "index.db"
TEXT_INDEX_PATH = DATA_DIR / "text.index"

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
