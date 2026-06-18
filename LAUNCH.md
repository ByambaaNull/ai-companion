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

This runs PyInstaller, fixes the bundle runtime, **bundles `ffmpeg` (with
`ffprobe`/`ffplay`) and `mpv` into `dist\Assistant\bin\`**, and **creates an
"AI Companion" shortcut on your Desktop**.

> **For the most complete bundle, install both tools before building** so they
> get copied in:
> `winget install Gyan.FFmpeg` and `winget install mpv`.
> If `mpv` is missing, music falls back to the bundled `ffplay` (still works);
> if `ffmpeg` is missing, the media tools/video downloader won't work. The build
> prints a WARNING for whichever tool it couldn't find.

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

## Distributing a ready-to-run build (for non-developers)

To hand someone a build they just **extract and double-click** — no Python, no
`pip`, no setup:

1. **Build it** on a Windows machine that has `ffmpeg` + `mpv` installed:
   ```bat
   winget install Gyan.FFmpeg
   winget install mpv
   build_exe.bat
   ```
   The result, `dist\Assistant\`, is self-contained: Python, the ML stack,
   `ffmpeg`/`mpv`, and seeded models all live inside it.

2. **Zip it.** The folder is several GB, so a single GitHub Release file (2 GB cap)
   won't fit — split it. With 7-Zip (recommended):
   ```bat
   7z a -v1900m AICompanion.7z dist\Assistant\
   ```
   That produces `AICompanion.7z.001`, `.002`, … each under 2 GB.
   (No size limit on Google Drive / Dropbox — there you can upload one plain
   `.zip` made with `Compress-Archive -Path dist\Assistant\* -DestinationPath AICompanion.zip`.)

3. **Upload** all the `.001/.002/…` parts to a single GitHub Release (or the one
   `.zip` to a file host) and share the link.

4. **The end user**: download every part into one folder → extract (7-Zip opens
   `.001` and rejoins them) → run `Assistant.exe`. On first launch they:
   - paste an API key (GitHub / Gemini / Groq) into **Settings**, and
   - wait once while any remaining models download.

   After that it runs offline.

> **Windows only.** A Windows `.exe` can't run on macOS/Linux — those users run
> from source (see above). Want a smaller download? Build with ffmpeg's
> *essentials* package instead of *full* (the `bin\` ffmpeg trio is ~650 MB on the
> full build).

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Assistant.exe not found` when making the shortcut | You haven't built yet — run `build_exe.bat` first. |
| `run_assistant.bat` flashes and closes | Run it from a terminal to see the error, or check `data\companion.log`. |
| `./run.sh: bad interpreter` | Line-ending issue — run `sed -i 's/\r$//' run.sh`, then `chmod +x run.sh`. |
| GUI opens but chat does nothing | A dependency failed to load — check `data\companion.log`; re-run `pip install -r requirements.txt`. |
