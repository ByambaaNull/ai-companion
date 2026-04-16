"""
slice_sprites.py — Slices gojo_2.png into individual animation strips.

Background detection is colour-based (checkerboard baked into pixels, no true alpha).
Outputs to assets/sprites/:
    idle.png    — breathing idle (4 frames, row 0 right half)
    walk.png    — walk cycle (6 frames, row 1)
    talk.png    — talk (4 frames, row 2)
    react.png   — reacting surprised (6 frames, row 3)
    point.png   — point (4 frames, row 4)
    sleep.png   — sleep (3 frames, row 5)
"""

from __future__ import annotations

from pathlib import Path
from PIL import Image
import numpy as np
import sys

SPRITES_DIR = Path(__file__).parent / "assets" / "sprites"
SOURCE = SPRITES_DIR / "gojo_3_PhotoGrid.png"

# Animation row order top→bottom, and frame counts
# Row 1 has TWO animations side by side: IDLE (4) and BREATHING IDLE (4)
# We want BREATHING IDLE for idle.png (more subtle movement)
ANIMATIONS = [
    # (output_name, which_sub_anim_in_row, frame_count)
    # row_index is 0-based order of detected content rows
    ("idle",   "right", 4),   # row 0 — right half = breathing idle
    ("walk",   "full",  6),   # row 1
    ("talk",   "full",  4),   # row 2
    ("react",  "full",  6),   # row 3
    ("point",  "full",  4),   # row 4
    ("sleep",  "full",  6),   # row 5 — 6 frames total
]

MIN_ROW_HEIGHT  = 40     # ignore content rows thinner than this (noise/labels)
MIN_COL_WIDTH   = 20     # ignore frame columns thinner than this
ROW_GAP         = 1      # rows within this many pixels are merged (gaps are 3-4px wide here)
ALPHA_THRESHOLD = 30     # pixels with alpha > this are "content"
CONTENT_FRAC    = 0.01   # at least this fraction of columns must be content to count as a row
LABEL_X_SKIP    = 220    # skip left N pixels (text labels like "IDLE", "WALK CYCLE" etc.)
ROW_SCAN_X_MAX  = 700    # right edge of sprite columns used for row detection (avoids tall right-side sprites)


def find_content_rows(img: Image.Image) -> list[tuple[int, int]]:
    """Return list of (y_start, y_end) for each horizontal band with content."""
    import numpy as np
    # Use a narrow horizontal band to avoid tall right-side sprites bridging row gaps
    alpha = np.array(img)[:, LABEL_X_SKIP:ROW_SCAN_X_MAX, 3]
    w = alpha.shape[1]
    row_has_content = (alpha > ALPHA_THRESHOLD).sum(axis=1) / w > CONTENT_FRAC

    rows = []
    in_band = False
    y_start = 0
    for y, has in enumerate(row_has_content):
        if has and not in_band:
            in_band = True
            y_start = y
        elif not has and in_band:
            in_band = False
            h = y - y_start
            if h >= MIN_ROW_HEIGHT:
                rows.append((y_start, y))
    if in_band:
        h = len(row_has_content) - y_start
        if h >= MIN_ROW_HEIGHT:
            rows.append((y_start, len(row_has_content)))

    # Merge rows that are very close (split by a thin gap)
    merged = []
    for r in rows:
        if merged and r[0] - merged[-1][1] <= ROW_GAP:
            merged[-1] = (merged[-1][0], r[1])
        else:
            merged.append(list(r))
    return [tuple(r) for r in merged]


def find_frame_columns(img: Image.Image, y_start: int, y_end: int) -> list[tuple[int, int]]:
    """Return list of (x_start, x_end) for each frame column in a row band.

    Scans the full row width. Text label columns are filtered out by the
    60% vertical-coverage threshold (labels are shorter than character sprites).
    """
    import numpy as np
    band_h = y_end - y_start
    alpha = np.array(img)[y_start:y_end, :, 3]   # full width — 60% filter removes text labels
    col_has_content = alpha.max(axis=0) > ALPHA_THRESHOLD

    raw_cols = []
    in_col = False
    x_start = 0
    for x, has in enumerate(col_has_content):
        if has and not in_col:
            in_col = True
            x_start = x
        elif not has and in_col:
            in_col = False
            if x - x_start >= MIN_COL_WIDTH:
                raw_cols.append((x_start, x))
    if in_col and img.width - x_start >= MIN_COL_WIDTH:
        raw_cols.append((x_start, img.width))

    # Filter out text-label columns: require content to span >= 60% of band height
    arr = np.array(img)
    min_span = max(10, int(band_h * 0.60))
    cols = []
    for x0, x1 in raw_cols:
        col_alpha = arr[y_start:y_end, x0:x1, 3]
        rows_with_content = (col_alpha.max(axis=1) > ALPHA_THRESHOLD).sum()
        if rows_with_content >= min_span:
            cols.append((x0, x1))

    return cols


def extract_strip(
    img: Image.Image,
    y_start: int, y_end: int,
    cols: list[tuple[int, int]],
    n_frames: int,
    side: str,
) -> Image.Image:
    """
    Extract n_frames from cols list within the row band.

    side="full"  — take the first n_frames columns
    side="right" — take the last n_frames columns (breathing idle)
    """
    if side == "right":
        frame_cols = cols[-n_frames:]
    else:
        frame_cols = cols[:n_frames]

    if len(frame_cols) < n_frames:
        print(f"  WARNING: expected {n_frames} frames, only found {len(frame_cols)} columns — using all")
        frame_cols = cols

    # Normalize all frames to same width/height (use max dimensions)
    frame_imgs = []
    for x0, x1 in frame_cols:
        frame = img.crop((x0, y_start, x1, y_end))
        frame_imgs.append(frame)

    if not frame_imgs:
        raise ValueError("No frames extracted")

    fw = max(f.width for f in frame_imgs)
    fh = max(f.height for f in frame_imgs)

    # Paste each frame onto uniform-size canvas
    strip = Image.new("RGBA", (fw * len(frame_imgs), fh), (0, 0, 0, 0))
    for i, frame in enumerate(frame_imgs):
        # Centre smaller frames on the canvas
        ox = (fw - frame.width) // 2
        oy = (fh - frame.height) // 2
        strip.paste(frame, (i * fw + ox, oy), frame)

    return strip


def main():
    if not SOURCE.exists():
        print(f"ERROR: {SOURCE} not found")
        sys.exit(1)

    print(f"Loading {SOURCE.name} ({SOURCE.stat().st_size // 1024} KB)…")
    img = Image.open(SOURCE).convert("RGBA")
    print(f"  Size: {img.width} × {img.height}")

    print("Detecting content rows…")
    rows = find_content_rows(img)
    print(f"  Found {len(rows)} rows:")
    for i, (y0, y1) in enumerate(rows):
        print(f"    Row {i}: y={y0}→{y1} (height={y1-y0})")

    if len(rows) < len(ANIMATIONS):
        print(f"WARNING: expected ≥{len(ANIMATIONS)} rows, found {len(rows)} — check MIN_ROW_HEIGHT")

    print("\nSlicing animations…")
    for anim_idx, (name, side, n_frames) in enumerate(ANIMATIONS):
        if anim_idx >= len(rows):
            print(f"  SKIP {name} — no row {anim_idx}")
            continue

        y0, y1 = rows[anim_idx]
        cols = find_frame_columns(img, y0, y1)
        print(f"  {name}: row {anim_idx} y={y0}–{y1}, {len(cols)} columns detected, taking {n_frames} ({side})")

        try:
            strip = extract_strip(img, y0, y1, cols, n_frames, side)
            out_path = SPRITES_DIR / f"{name}.png"
            strip.save(out_path, "PNG")
            print(f"    → Saved {out_path.name} ({strip.width}×{strip.height})")
        except Exception as exc:
            print(f"    ERROR: {exc}")

    print("\nDone. Files in assets/sprites/:")
    for f in sorted(SPRITES_DIR.glob("*.png")):
        if f.name != "gojo.png":
            print(f"  {f.name}")


if __name__ == "__main__":
    main()
