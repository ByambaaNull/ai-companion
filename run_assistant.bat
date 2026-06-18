@echo off
cd /d "%~dp0"
set NUMBA_DISABLE_JIT=1
set KMP_DUPLICATE_LIB_OK=TRUE

venv\Scripts\python.exe assistant_gui.py
pause
