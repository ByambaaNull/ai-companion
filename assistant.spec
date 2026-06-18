# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Assistant AI Companion (onedir).

Why onedir (not --onefile): this app embeds QtWebEngine (a full Chromium) plus a
heavy ML stack (torch, faster-whisper/ctranslate2, chromadb, sentence-transformers,
piper, rembg). One-file builds of QtWebEngine apps frequently fail at runtime
(QtWebEngineProcess can't locate its resources after self-extraction). onedir
unpacks once beside the exe and is far more reliable.

Build:   venv\\Scripts\\pyinstaller.exe assistant.spec --noconfirm
Run:     dist\\Assistant\\Assistant.exe
Note:    On first launch the app downloads its models (Whisper, Piper, rembg
         u2net) next to the exe — internet required once, then it runs offline.
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

# ui.html is read at runtime via sys._MEIPASS; ship it at the bundle root.
datas = [("ui.html", ".")]
binaries = []
# action_router imports the action modules lazily (inside functions); make sure
# every one is bundled even if static analysis misses a branch.
hiddenimports = collect_submodules("actions")

# Packages that ship data files / dynamic libs PyInstaller's static graph misses.
for pkg in [
    "faster_whisper", "ctranslate2", "chromadb", "mem0", "onnxruntime",
    "piper", "rembg", "pymatting", "skimage", "sounddevice", "soundfile",
    "tokenizers", "sentence_transformers", "huggingface_hub", "yt_dlp",
    # Toolbox Wave 2: pymupdf/qrcode ship data files; pyzbar loads libzbar-64.dll
    # + libiconv.dll via ctypes (NOT a Python import) so its DLLs must be collected
    # explicitly or QR scanning fails at runtime in the frozen app.
    "pymupdf", "qrcode", "pyzbar",
]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass  # optional package not present — skip it

hiddenimports += [
    "win32timezone", "win32gui", "win32con", "win32file", "pywintypes",
    "sklearn.utils._typedefs", "sklearn.neighbors._partition_nodes",
    # In-app updater: imported lazily inside bridge slots, so name it explicitly.
    # (updater.py is not bundled — it runs from the source checkout at update time.)
    "update_manager",
    # In-app video player's localhost media server (lazy-imported in a slot).
    "media_server",
    # Speaker chooser + subtitle conversion (lazy-imported in slots).
    "audio_devices",
    "subtitles",
]

a = Analysis(
    ["assistant_gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Assistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed GUI app (no console window)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Assistant",
)
