# セットアップスクリプト (Windows / PowerShell)
#   powershell -ExecutionPolicy Bypass -File setup.ps1          # GPUの有無を自動判定
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Cpu     # CPUのみ
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Gpu     # CUDA版を強制
param(
    [switch]$Cpu,
    [switch]$Gpu
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "=== 動画シーン検索 セットアップ ===" -ForegroundColor Cyan

function Test-Winget { [bool](Get-Command winget -ErrorAction SilentlyContinue) }

# Python 3.10-3.12 を探す (pyarrow 16.1 は 3.13 以降に未対応のため)
function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in "3.12", "3.11", "3.10") {
            & py "-$v" -c "import sys" 2>$null
            if ($LASTEXITCODE -eq 0) { return @("py", "-$v") }
        }
    }
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe")
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return @($c) } }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($ver -in @("3.10", "3.11", "3.12")) { return @("python") }
    }
    return $null
}

if (-not (Test-Path "venv\Scripts\python.exe")) {
    $py = Find-Python
    if (-not $py) {
        if (-not (Test-Winget)) {
            throw "Python 3.12 が見つかりません。https://www.python.org/downloads/ から 3.12 を入れてください。"
        }
        Write-Host "[0/4] Python 3.12 を winget でインストール中..."
        winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
        $py = Find-Python
        if (-not $py) { throw "Python 3.12 を入れましたが見つかりません。ウィンドウを閉じてもう一度実行してください。" }
    }
    Write-Host "[1/4] 仮想環境を作成中... ($($py -join ' '))"
    $exe = $py[0]; $rest = @($py | Select-Object -Skip 1)
    & $exe @rest -m venv venv
    if ($LASTEXITCODE -ne 0) { throw "仮想環境の作成に失敗しました。" }
} else {
    Write-Host "[1/4] 既存の仮想環境を使用します"
}

$python = ".\venv\Scripts\python.exe"
& $python -m pip install --upgrade pip --quiet

# GPUの有無 (NVIDIAドライバの nvidia-smi で判定)
if (-not $Cpu -and -not $Gpu) {
    $Cpu = -not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)
    if ($Cpu) { Write-Host "NVIDIA GPU が見つからないため CPU 版で入れます (文字起こしは遅くなります)。" -ForegroundColor Yellow }
}

if ($Cpu) {
    Write-Host "[2/4] PyTorch (CPU版) をインストール中..."
    & $python -m pip install "torch>=2.6" --index-url https://download.pytorch.org/whl/cpu
} else {
    Write-Host "[2/4] PyTorch (CUDA 12.4版) をインストール中... (約2.5GB)"
    & $python -m pip install "torch==2.6.0" --index-url https://download.pytorch.org/whl/cu124
}
if ($LASTEXITCODE -ne 0) { throw "PyTorch のインストールに失敗しました。" }

Write-Host "[3/4] 依存パッケージをインストール中..."
& $python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "依存パッケージのインストールに失敗しました。" }

# 外部ツール: ffmpeg は必須、Node.js は YouTube 取得 (yt-dlp) に必要
Write-Host "[4/4] 外部ツールを確認中..."
$needRestart = $false
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -and -not (Test-Path "tools\ffmpeg\bin\ffmpeg.exe")) {
    if (Test-Winget) {
        Write-Host "  ffmpeg を winget でインストール中..."
        winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
        $needRestart = $true
    } else {
        Write-Host "  警告: ffmpeg がありません。https://ffmpeg.org/ から入れて PATH に追加してください。" -ForegroundColor Yellow
    }
}
if (-not (Get-Command node -ErrorAction SilentlyContinue) -and -not (Get-Command deno -ErrorAction SilentlyContinue)) {
    if (Test-Winget) {
        Write-Host "  Node.js (LTS) を winget でインストール中... (YouTube の取得に使います)"
        winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements
        $needRestart = $true
    } else {
        Write-Host "  警告: Node.js がありません。YouTube の取得に失敗する場合は https://nodejs.org/ から入れてください。" -ForegroundColor Yellow
    }
}

Write-Host ""
& $python -c "import torch; print('torch:', torch.__version__, '/ CUDA:', torch.cuda.is_available())"
Write-Host ""
Write-Host "セットアップ完了。" -ForegroundColor Green
if ($needRestart) {
    Write-Host "新しく入れたツールを使うため、このウィンドウを閉じてから起動し直してください。" -ForegroundColor Yellow
}
Write-Host "  検索・切り抜き: start.bat"
Write-Host "  自動投稿:       CUT_auto_publish.vbs (デスクトップに置くなら make_shortcut.bat)"
Write-Host "  AI (Claude Code / Codex) とYouTube連携は、自動投稿アプリの「設定」タブから行えます。"
