# Creates a Desktop shortcut to the built AI Companion app.
# Run AFTER build_exe.bat (which produces dist\Assistant\Assistant.exe).
# build_exe.bat calls this automatically, or run it yourself:
#     powershell -ExecutionPolicy Bypass -File create_desktop_shortcut.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe  = Join-Path $root "dist\Assistant\Assistant.exe"

if (-not (Test-Path $exe)) {
    Write-Host "Assistant.exe not found at:" -ForegroundColor Yellow
    Write-Host "  $exe"
    Write-Host "Build it first by running build_exe.bat, then run this script again."
    exit 1
}

$desktop  = [Environment]::GetFolderPath("Desktop")
$lnk      = Join-Path $desktop "AI Companion.lnk"

$shell    = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnk)
$shortcut.TargetPath       = $exe
$shortcut.WorkingDirectory = Split-Path -Parent $exe
$shortcut.IconLocation     = "$exe,0"
$shortcut.Description       = "AI Companion"
$shortcut.Save()

Write-Host "Desktop shortcut created:" -ForegroundColor Green
Write-Host "  $lnk  ->  $exe"
