# Make a desktop shortcut that opens CUT auto publish in its own window (no console).
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$name = 'CUT ' + (-join [char[]](0x81EA, 0x52D5, 0x6295, 0x7A3F))  # CUT + jidou toukou
$path = Join-Path ([Environment]::GetFolderPath('Desktop')) ($name + '.lnk')
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
$shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\wscript.exe'
$shortcut.Arguments = '"' + (Join-Path $here 'CUT_auto_publish.vbs') + '"'
$shortcut.WorkingDirectory = $here
$shortcut.IconLocation = 'shell32.dll,115'
$shortcut.Save()
Write-Output "Created: $path"
