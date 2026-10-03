@echo off
setlocal
cd /d "%~dp0"
title Cut_Video - yt-dlp updater

if not exist "%~dp0venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment missing. Run start.bat or setup.ps1 first.
    pause
    exit /b 1
)

echo Updating yt-dlp and its required dependencies...
"%~dp0venv\Scripts\python.exe" -m pip install --upgrade "yt-dlp[default]>=2026.8.19"
set "UPDATE_EXIT=%ERRORLEVEL%"

if not "%UPDATE_EXIT%"=="0" (
    echo.
    echo [ERROR] Update failed. Check the network and the error above.
    pause
    exit /b %UPDATE_EXIT%
)

"%~dp0venv\Scripts\python.exe" -c "import yt_dlp; print('yt-dlp:', yt_dlp.version.__version__)"
set "VERSION_EXIT=%ERRORLEVEL%"
if not "%VERSION_EXIT%"=="0" (
    echo.
    echo [ERROR] Unable to verify yt-dlp after the update.
    pause
    exit /b %VERSION_EXIT%
)

echo.
echo [INFO] Update complete. Restart CUT before downloading a URL.
pause
exit /b 0
