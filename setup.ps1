# セットアップスクリプト (Windows / PowerShell)
#   powershell -ExecutionPolicy Bypass -File setup.ps1          # GPU (CUDA) 版
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Cpu     # CPUのみ
param(
    [switch]$Cpu
)

$ErrorActionPreference = "Stop"

Write-Host "=== 動画シーン検索 セットアップ ===" -ForegroundColor Cyan

# ffmpeg チェック
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "警告: ffmpeg が見つかりません。https://ffmpeg.org/ からインストールしてPATHに追加してください。" -ForegroundColor Yellow
}

# venv 作成
if (-not (Test-Path venv)) {
    Write-Host "[1/3] 仮想環境を作成中..."
    python -m venv venv
} else {
    Write-Host "[1/3] 既存の仮想環境を使用します"
}

$pip = ".\venv\Scripts\pip.exe"
& .\venv\Scripts\python.exe -m pip install --upgrade pip --quiet

# PyTorch (GPU/CPU)
if ($Cpu) {
    Write-Host "[2/3] PyTorch (CPU版) をインストール中..."
    & $pip install "torch>=2.6"
} else {
    Write-Host "[2/3] PyTorch (CUDA 12.4版) をインストール中... (約2.5GB)"
    & $pip install "torch==2.6.0" --index-url https://download.pytorch.org/whl/cu124
}

# 残りの依存関係
Write-Host "[3/3] 依存パッケージをインストール中..."
& $pip install -r requirements.txt

# 動作確認
Write-Host ""
& .\venv\Scripts\python.exe -c "import torch; print('torch:', torch.__version__, '/ CUDA:', torch.cuda.is_available())"
Write-Host ""
Write-Host "セットアップ完了。以下で起動できます:" -ForegroundColor Green
Write-Host "  .\venv\Scripts\python.exe app.py"
