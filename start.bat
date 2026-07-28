@echo off
cd /d "%~dp0"
title Auto_Cut - video scene search

REM Optional only: qwen3:8b is a multi-GiB download used solely by the
REM experimental local LLM transcript analysis feature. Normal startup never
REM downloads or starts Ollama. Double-click setup_ollama.bat, or run:
REM   start.bat --setup-ollama
if /I "%~1"=="--setup-ollama" (
    call "%~dp0setup_ollama.bat"
    exit /b %ERRORLEVEL%
)

REM ---- check Python (try winget install if missing) ----
where python >nul 2>nul
if errorlevel 1 (
    echo [INFO] Python not found. Trying to install via winget...
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo [ERROR] Auto-install failed. Install Python 3.10+ manually from
        echo         https://www.python.org/downloads/
        echo         and check "Add python.exe to PATH" during install.
        pause
        exit /b 1
    )
    echo [INFO] Python installed. Close this window and run start.bat again
    echo        so that PATH changes take effect.
    pause
    exit /b 0
)

REM ---- check ffmpeg (try winget install if missing) ----
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [INFO] ffmpeg not found. Trying to install via winget...
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo [ERROR] Auto-install failed. Install ffmpeg manually from
        echo         https://ffmpeg.org/ and add it to PATH.
        pause
        exit /b 1
    )
    echo [INFO] ffmpeg installed. Close this window and run start.bat again
    echo        so that PATH changes take effect.
    pause
    exit /b 0
)

REM ---- first run: create venv and install dependencies ----
if not exist "venv\Scripts\python.exe" (
    echo [INFO] First-time setup: downloading PyTorch, about 2.5GB...
    powershell -ExecutionPolicy Bypass -File setup.ps1
    if errorlevel 1 (
        echo [ERROR] Setup failed. See errors above.
        pause
        exit /b 1
    )
)

REM ---- launch app ----
echo [INFO] Starting app. Browser will open at http://127.0.0.1:7860
echo        Close this window to stop the app.
set PYTHONIOENCODING=utf-8
venv\Scripts\python.exe app.py
pause
