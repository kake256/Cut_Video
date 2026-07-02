#!/usr/bin/env bash
# セットアップスクリプト (Linux / macOS)
#   ./setup.sh          # GPU (CUDA) 版
#   ./setup.sh --cpu    # CPUのみ
set -e

echo "=== 動画シーン検索 セットアップ ==="

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "警告: ffmpeg が見つかりません。パッケージマネージャ等でインストールしてください。"
fi

if [ ! -d venv ]; then
    echo "[1/3] 仮想環境を作成中..."
    python3 -m venv venv
else
    echo "[1/3] 既存の仮想環境を使用します"
fi

./venv/bin/python -m pip install --upgrade pip --quiet

if [ "$1" = "--cpu" ]; then
    echo "[2/3] PyTorch (CPU版) をインストール中..."
    ./venv/bin/pip install "torch>=2.6"
else
    echo "[2/3] PyTorch (CUDA 12.4版) をインストール中... (約2.5GB)"
    ./venv/bin/pip install "torch==2.6.0" --index-url https://download.pytorch.org/whl/cu124
fi

echo "[3/3] 依存パッケージをインストール中..."
./venv/bin/pip install -r requirements.txt

echo ""
./venv/bin/python -c "import torch; print('torch:', torch.__version__, '/ CUDA:', torch.cuda.is_available())"
echo ""
echo "セットアップ完了。以下で起動できます:"
echo "  ./venv/bin/python app.py"
