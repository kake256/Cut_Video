@echo off
setlocal
cd /d "%~dp0"
set CUT_VIDEO_UI_LAYOUT=studio
echo [INFO] Studio layout selected. Close any running CUT instance before switching layouts.
call "%~dp0start.bat" %*
endlocal
