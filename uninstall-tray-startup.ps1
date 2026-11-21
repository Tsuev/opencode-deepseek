$startup = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startup "DeepSeek local bridge.lnk"
if (Test-Path $shortcutPath) {
    Remove-Item $shortcutPath -Force
    Write-Host "Tray autostart removed."
} else {
    Write-Host "Tray autostart was not installed."
}
