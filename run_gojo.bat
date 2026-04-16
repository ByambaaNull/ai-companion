@echo off
cd /d "C:\Users\A_K_A\Desktop\AI_companian"
set NUMBA_DISABLE_JIT=1

:: Ensure Ollama is running (starts in background, no-op if already up)
tasklist /fi "imagename eq ollama.exe" | find /i "ollama.exe" >nul 2>&1
if errorlevel 1 (
    echo Starting Ollama...
    start "" ollama serve
    timeout /t 3 /nobreak >nul
)

venv\Scripts\python.exe main.py
pause
