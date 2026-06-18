#!/usr/bin/env bash
# Launch the AI Companion on macOS / Linux (the Windows equivalent is
# run_assistant.bat). Activates ./venv if present, then starts the GUI.
set -e
cd "$(dirname "$0")"

export NUMBA_DISABLE_JIT=1
export KMP_DUPLICATE_LIB_OK=TRUE

if [ -d "venv" ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

exec python assistant_gui.py
