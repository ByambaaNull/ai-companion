# Launch Guide

How to start the AI Companion. Pick the path that matches your setup. For first-time
dependency install, see [README.md](README.md#setup) first.

---

## Windows — packaged app (`.exe`)

The `.exe` is **not** in the repo (it's large and machine-specific), so you build it
once. After building, it lives at:

```
dist\Assistant\Assistant.exe
```

### Build it (one time)

```bat
build_exe.bat
```

This runs PyInstaller, fixes the bundle runtime, **and automatically creates an
"AI Companion" shortcut on your Desktop** pointing at the exe.

### Put the exe on the Desktop

The build does this for you. To (re)create the Desktop shortcut at any time after a build:

```bat
powershell -ExecutionPolicy Bypass -File create_desktop_shortcut.ps1
```

That places **`AI Companion`** on your Desktop → double-click to launch. On first
run the app downloads its models once, then runs offline.

> Prefer a Start-menu / taskbar entry? Right-click the Desktop shortcut →
> *Pin to Start* / *Pin to taskbar*.

---

## Windows — run from source (no build)

```bat
run_assistant.bat
```

or directly:

```bat
venv\Scripts\python.exe assistant_gui.py
```

---

## macOS / Linux — run from source

```bash
chmod +x run.sh      # first time only
./run.sh
```

or directly:

```bash
python assistant_gui.py
```

(The packaged `.exe` is Windows-only; on macOS/Linux you run from source. Windows-only
features — global hotkey wake-word, mpv pipe IPC — are skipped automatically.)

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Assistant.exe not found` when making the shortcut | You haven't built yet — run `build_exe.bat` first. |
| `run_assistant.bat` flashes and closes | Run it from a terminal to see the error, or check `data\companion.log`. |
| `./run.sh: bad interpreter` | Line-ending issue — run `sed -i 's/\r$//' run.sh`, then `chmod +x run.sh`. |
| GUI opens but chat does nothing | A dependency failed to load — check `data\companion.log`; re-run `pip install -r requirements.txt`. |
