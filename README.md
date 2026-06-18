# AI Companion

A local-first desktop AI companion: a chat assistant with voice (wake-word + speech-to-text + text-to-speech), screen awareness, music and video downloading, and a built-in **Tools** bay (media/image/PDF/text utilities, QR codes, document chat, image upscaling). The interface is a single HTML page (`ui.html`) rendered in a PyQt5 + QtWebEngine window; most AI work runs on your own machine, and external API keys are optional.

> **Heads-up:** the heavy assets — the RVC voice model, Whisper/Piper models, the Python venv and the built `.exe` — are **not** in this repo (they're gitignored). You install dependencies and download models locally with the steps below.

## Platform support

| Platform | Status | Notes |
|----------|--------|-------|
| **Windows 10/11** | ✅ Primary | Full feature set. Can also be packaged into a standalone `.exe` (`build_exe.bat`). |
| **macOS / Linux** | ⚠️ Run from source | The GUI, chat, Tools bay, music/video and most features work. Windows-only bits are skipped gracefully: global hotkey wake-word, mpv named-pipe IPC, touch injection, and the `.exe` build. On Apple Silicon you may need Rosetta for PyQt5 (see Troubleshooting). |

## Prerequisites

- **Python 3.11+**
- **ffmpeg** and **mpv** on your `PATH` (used by music, video downloader, and the media tools):
  - Windows: `winget install Gyan.FFmpeg` and `winget install mpv` (or use `scoop`/`choco`)
  - macOS: `brew install ffmpeg mpv`
  - Linux: `sudo apt install ffmpeg mpv` (or your distro's equivalent)

## Setup

```bash
# 1. Clone
git clone <your-repo-url> ai-companion
cd ai-companion

# 2. Create and activate a virtual environment
python -m venv venv
#   Windows:        venv\Scripts\activate
#   macOS / Linux:  source venv/bin/activate

# 3. Install PyTorch FIRST (its build differs by platform)
#   Windows + NVIDIA GPU (CUDA 12.1):
pip install torch==2.3.1+cu121 torchaudio==2.3.1+cu121 --extra-index-url https://download.pytorch.org/whl/cu121
#   macOS / Linux / CPU-only:
#     pip install "torch<2.4" "torchaudio<2.4"

# 4. Install everything else
pip install -r requirements.txt
playwright install chromium          # for the social-messaging browser automation

# 5. Configure secrets (all optional)
cp .env.example .env                 # Windows: copy .env.example .env
#   then edit .env and add any API keys you have

# 6. (Optional) Pre-download voice/model files
python bootstrap.py                  # mainly for Windows RVC/Piper setup;
                                     # otherwise the app auto-downloads models on first use
```

## Running

```bash
# Windows
run_assistant.bat
#   or:  venv\Scripts\python.exe assistant_gui.py

# macOS / Linux
./run.sh                             # first time: chmod +x run.sh
#   or:  python assistant_gui.py
```

The app reads `ui.html` directly, so UI edits show on the next launch.

**📖 For the packaged `.exe`, the Desktop shortcut, and every launch option, see
the dedicated [Launch Guide → LAUNCH.md](LAUNCH.md).**

## Building a standalone Windows app (optional)

```bat
build_exe.bat
```

This runs PyInstaller against `assistant.spec` (onedir build with QtWebEngine + the ML stack), then `fix_bundle.ps1` aligns the VC++ runtime and seeds models. Output: `dist\Assistant\Assistant.exe`. On first launch it downloads its models once, then runs offline. (Building is Windows-only.)

## API keys

All keys live in `.env` (gitignored) and are **optional** — the app runs without them, with the matching feature disabled:

- `GITHUB_TOKEN`, `GEMINI_API_KEY`, `GROQ_API_KEY` — LLM providers; any one is enough.
- `DISCORD_USER_TOKEN` — Discord auto-reply when away.

See `.env.example` for where to get each.

## Project layout

```
assistant_gui.py     GUI entry point (PyQt5 + QtWebEngine window that loads ui.html)
ui.html              The entire front-end (markup, styling, and bridge JS)
main.py              Voice-assistant core loop
action_router.py     Routes intents to the action modules
actions/             Feature modules (music, video, browser, tools, productivity, …)
config.py            Paths, model locations, provider config (creates data/ on import)
bootstrap.py         First-run model/binary downloader
build_exe.bat        Windows packaging (PyInstaller via assistant.spec)
data/                Runtime state, caches, downloads (gitignored)
```

## Troubleshooting

- **`ModuleNotFoundError: PyQt5` / QtWebEngine** — re-run `pip install -r requirements.txt` inside the activated venv.
- **`pip install` fails on `pywin32` (macOS/Linux)** — it's already marked Windows-only; make sure you're on the current `requirements.txt`.
- **`Failed to initialize NumPy: _ARRAY_API not found`** — numpy 2.x slipped in. Run `pip install "numpy>=1.26,<2"` (torch 2.3.1 needs numpy 1.x).
- **No sound / music won't play** — confirm `ffmpeg` and `mpv` are installed and on your `PATH`.
- **PyQt5 won't install on Apple Silicon** — install under Rosetta, or use a Homebrew/conda Python that ships Qt5 wheels. (The code targets PyQt5; PyQt6 would need code changes.)
