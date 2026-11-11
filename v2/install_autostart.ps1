# Optional: install a SEPARATE V2 logon shortcut.
# This does not modify or disable the existing "Pre-Market Pulse" shortcut or
# the existing scheduled task used by the V1 dashboard.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $root "Start Market Pulse V2.bat"
if (-not (Test-Path -LiteralPath $bat)) {
    throw "Launcher not found: $bat"
}
$startup = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$link = Join-Path $startup "Pre-Market Pulse V2.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $bat
$shortcut.Arguments = "--no-open"
$shortcut.WorkingDirectory = $root
$shortcut.WindowStyle = 7
$shortcut.Description = "Start Pre-Market Pulse V2 long-term dashboard at logon"
$shortcut.Save()
Write-Host "Installed: $link"
Write-Host "V1 auto-start was not touched."
