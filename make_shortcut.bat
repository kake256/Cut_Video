@echo off
rem Make a desktop shortcut that opens CUT auto publish in its own window.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1"
pause
