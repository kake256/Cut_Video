"""テストパッケージ。"""

import os

# Rendering tests assert libx264 arguments; keep them independent of the GPU driver.
os.environ.setdefault("CUT_VIDEO_ENCODER", "cpu")
