' CUT auto publish: open the app in its own window without a console window.
' Double-click this file (or the desktop shortcut made by make_shortcut.bat).
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
python = here & "\venv\Scripts\python.exe"
If Not fso.FileExists(python) Then
    MsgBox "venv not found. Run start.bat once to set up the environment.", vbExclamation, "CUT"
    WScript.Quit 1
End If
shell.CurrentDirectory = here
' 0 = hidden window: child processes (ffmpeg, Whisper) share it, so no console flashes.
shell.Run """" & python & """ auto_publish_app.py", 0, False
