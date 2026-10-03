@echo off
setlocal
cd /d "%~dp0"
title CUT - auto publish
if not exist "venv\Scripts\python.exe" (
    echo [ERROR] venv not found. Run start.bat once to set up the environment.
    pause
    exit /b 1
)
"venv\Scripts\python.exe" auto_publish_app.py %*
endlocal
