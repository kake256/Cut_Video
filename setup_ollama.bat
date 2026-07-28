@echo off
setlocal
cd /d "%~dp0"
title Cut_Video - Optional local Ollama setup

REM This is opt-in because qwen3:8b is a multi-GiB download and is only used
REM by the experimental local LLM transcript analysis feature.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_ollama.ps1" %*
set "SETUP_EXIT=%ERRORLEVEL%"
if not "%SETUP_EXIT%"=="0" (
    echo.
    echo [WARNING] Ollama setup did not complete. Cut_Video itself is unchanged and can still be started with start.bat.
) else (
    echo.
    echo [INFO] Ollama setup completed. You can close this window and start Cut_Video normally.
)
pause
exit /b %SETUP_EXIT%
