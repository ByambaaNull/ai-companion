@echo off
REM Build the Assistant AI Companion into a Windows app folder (dist\Assistant\).
REM Uses the tuned spec (onedir + QtWebEngine + ML data files), then fix_bundle.ps1
REM aligns the VC++ runtime (fixes a ctranslate2/torch 0xC0000005 crash) and seeds
REM the Whisper/Piper models. Run dist\Assistant\Assistant.exe.
cd /d "%~dp0"
venv\Scripts\python.exe -m pip install "pyinstaller>=6.0"
venv\Scripts\pyinstaller.exe assistant.spec --noconfirm
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_bundle.ps1"
REM Put an "AI Companion" shortcut on the Desktop pointing at the new exe.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_desktop_shortcut.ps1"
echo.
echo Done. Launch from the Desktop "AI Companion" shortcut,
echo   or directly:  dist\Assistant\Assistant.exe
pause
