$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$startup = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startup "DeepSeek local bridge.lnk"
$pythonw = (Get-Command pythonw.exe -ErrorAction Stop).Source
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "`"$root\tray.py`""
$shortcut.WorkingDirectory = $root
$shortcut.Description = "DeepSeek local bridge tray controller"
$shortcut.Save()
Write-Host "Tray autostart installed: $shortcutPath"
