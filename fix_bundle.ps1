# fix_bundle.ps1 - post-build fixups for the Assistant onedir bundle.
#
# 1) VC++ RUNTIME: PyInstaller bundles PyQt5's Qt5\bin\MSVCP140.dll (an old
#    14.26 build). Qt's bin is searched first, so ctranslate2 / torch bind to the
#    stale runtime and crash on model load with 0xC0000005 (faulting MSVCP140.dll).
#    Overwrite every bundled VC++ runtime copy with the system's current one.
# 2) MODELS: copy the already-downloaded Whisper + Piper (+ upscaler) models next
#    to the exe so the distributed app works offline on first launch.
#
# ASCII-only on purpose: when run via `powershell -File`, Windows PowerShell 5.1
# reads the script in the system ANSI codepage, so a stray non-ASCII char (e.g. an
# em-dash) corrupts a string literal and the whole script fails to parse.
$ErrorActionPreference = "Stop"
$dist = Join-Path $PSScriptRoot "dist\Assistant"
if (-not (Test-Path $dist)) { Write-Error "dist\Assistant not found - run pyinstaller first."; exit 1 }

$sys   = Join-Path $env:WINDIR "System32"
$names = "msvcp140.dll","msvcp140_1.dll","msvcp140_2.dll","vcruntime140.dll","vcruntime140_1.dll","concrt140.dll"
foreach ($dir in @((Join-Path $dist "_internal"), (Join-Path $dist "_internal\PyQt5\Qt5\bin"))) {
  foreach ($n in $names) {
    $existing = Get-ChildItem -Path $dir -Filter $n -File -ErrorAction SilentlyContinue | Select-Object -First 1
    $src = Join-Path $sys $n
    if ($existing -and (Test-Path $src)) { Copy-Item $src $existing.FullName -Force }
  }
}
Write-Host "[fix_bundle] VC++ runtime DLLs aligned to the system version."

$mdst = Join-Path $dist "models"
New-Item -ItemType Directory -Force -Path $mdst | Out-Null
foreach ($m in "whisper","piper","upscale") {
  $msrc = Join-Path $PSScriptRoot "models\$m"
  if (Test-Path $msrc) { Copy-Item $msrc $mdst -Recurse -Force; Write-Host "[fix_bundle] seeded model: $m" }
}
Write-Host "[fix_bundle] done."
