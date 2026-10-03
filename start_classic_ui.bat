@echo off
setlocal
cd /d "%~dp0"
set CUT_VIDEO_UI_LAYOUT=classic
echo [INFO] Classic layout selected. Close any running CUT instance before switching layouts.
call "%~dp0start.bat" %*
endlocal
