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

# 3) FFMPEG + MPV: bundle the external media tools into bin\ so the packaged app
#    is self-contained (config.py prepends this bin\ to PATH at startup). Copied
#    from whatever is on the build machine's PATH. ffmpeg powers the media tools /
#    video downloader / subtitles; mpv is the music player (ffplay, shipped with
#    ffmpeg, is the fallback if mpv is absent).
$bindst = Join-Path $dist "bin"
New-Item -ItemType Directory -Force -Path $bindst | Out-Null

function Copy-Tool([string]$tool, [string[]]$extra) {
  $cmd = Get-Command "$tool.exe" -ErrorAction SilentlyContinue
  if (-not $cmd) { $cmd = Get-Command $tool -ErrorAction SilentlyContinue }
  if (-not $cmd) {
    Write-Host "[fix_bundle] WARNING: $tool not found on PATH - packaged app will need the user to install it. Install it (winget install Gyan.FFmpeg / winget install mpv) and rebuild for a self-contained bundle."
    return
  }
  $src = Get-Item $cmd.Source
  if ($src.Target) { $src = Get-Item ($src.Target | Select-Object -First 1) }   # follow alias/symlink
  $dir = $src.DirectoryName
  if ($dir -like "$env:WINDIR*") {
    Write-Host "[fix_bundle] WARNING: $tool resolves into Windows ($dir); skipping to avoid copying system files. Set ${tool}_EXECUTABLE (uppercased) to a standalone $tool.exe and rebuild."
    return
  }
  if ($src.Length -lt 200KB) {
    Write-Host "[fix_bundle] WARNING: $($src.FullName) looks like a shim (very small) - it likely will not run standalone. Point $($tool.ToUpper())_EXECUTABLE at the real exe and rebuild."
  }
  Copy-Item $src.FullName $bindst -Force
  foreach ($e in $extra) { $p = Join-Path $dir $e; if (Test-Path $p) { Copy-Item $p $bindst -Force } }
  Get-ChildItem -Path $dir -Filter *.dll -File -ErrorAction SilentlyContinue | ForEach-Object { Copy-Item $_.FullName $bindst -Force }
  Write-Host "[fix_bundle] bundled $tool from $dir"
}
Copy-Tool "ffmpeg" @("ffprobe.exe","ffplay.exe")
Copy-Tool "mpv" @()

Write-Host "[fix_bundle] done."
