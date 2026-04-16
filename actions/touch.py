"""
actions/touch.py — Touch screen gestures via Windows Pointer/Touch API + pyautogui.

Actions:
  swipe(param)        Swipe in a direction or between two coordinates.
                       e.g. "up", "down", "left", "right"
                            "x1,y1,x2,y2"
  scroll(param)       Scroll at current mouse position.
                       e.g. "up", "down", "up 3", "down 5"
  double_tap(param)   Double-tap at coordinates or screen centre.
                       e.g. "960,540" or "" (centre)
  pinch_zoom(param)   Two-finger pinch/zoom at screen centre via SendInput.
                       e.g. "in" or "out"

All coordinates are in screen pixels (absolute).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import time

import pyautogui

log = logging.getLogger(__name__)

# ─── screen helpers ───────────────────────────────────────────────────────────

def _screen_size() -> tuple[int, int]:
    return pyautogui.size()


def _centre() -> tuple[int, int]:
    w, h = _screen_size()
    return w // 2, h // 2


# ─── swipe ────────────────────────────────────────────────────────────────────

_SWIPE_DISTANCE_FRACTION = 0.35   # fraction of screen dimension per directional swipe
_SWIPE_DURATION = 0.35            # seconds


def swipe(param: str) -> str:
    """
    Swipe gesture.

    param examples:
        "up"              swipe up from centre
        "down 500"        swipe down 500 px from centre
        "left"
        "right"
        "100,400,800,400" swipe from (100,400) to (800,400)
    """
    param = param.strip().lower()
    w, h = _screen_size()
    cx, cy = _centre()

    parts = param.split()
    first = parts[0] if parts else ""

    if first in ("up", "down", "left", "right"):
        dist_str = parts[1] if len(parts) > 1 else None
        if first in ("up", "down"):
            default_dist = int(h * _SWIPE_DISTANCE_FRACTION)
        else:
            default_dist = int(w * _SWIPE_DISTANCE_FRACTION)

        dist = int(dist_str) if dist_str and dist_str.isdigit() else default_dist

        if first == "up":
            x1, y1, x2, y2 = cx, cy + dist // 2, cx, cy - dist // 2
        elif first == "down":
            x1, y1, x2, y2 = cx, cy - dist // 2, cx, cy + dist // 2
        elif first == "left":
            x1, y1, x2, y2 = cx + dist // 2, cy, cx - dist // 2, cy
        else:  # right
            x1, y1, x2, y2 = cx - dist // 2, cy, cx + dist // 2, cy
    else:
        # Coordinate form: "x1,y1,x2,y2"
        coords = [p.strip() for p in param.replace(" ", ",").split(",") if p.strip()]
        if len(coords) != 4:
            return f"Swipe: expected 'up/down/left/right' or 'x1,y1,x2,y2', got '{param}'."
        try:
            x1, y1, x2, y2 = int(coords[0]), int(coords[1]), int(coords[2]), int(coords[3])
        except ValueError:
            return f"Swipe: invalid coordinates '{param}'."

    pyautogui.moveTo(x1, y1, duration=0.05)
    pyautogui.dragTo(x2, y2, duration=_SWIPE_DURATION, button="left")
    return f"Swiped from ({x1},{y1}) to ({x2},{y2})."


# ─── scroll ───────────────────────────────────────────────────────────────────

def scroll(param: str) -> str:
    """
    Scroll at the current mouse position.

    param: "up [clicks]" | "down [clicks]"
    Default clicks = 3.
    """
    parts = param.strip().lower().split()
    direction = parts[0] if parts else "down"
    try:
        clicks = int(parts[1]) if len(parts) > 1 else 3
    except ValueError:
        clicks = 3

    amount = clicks if direction == "up" else -clicks
    pyautogui.scroll(amount)
    return f"Scrolled {direction} {clicks} clicks."


# ─── double_tap ───────────────────────────────────────────────────────────────

def double_tap(param: str) -> str:
    """
    Double-tap at given coordinates.

    param: "x,y"  or  "" (taps screen centre)
    """
    param = param.strip()
    if param:
        try:
            parts = param.split(",")
            x, y = int(parts[0].strip()), int(parts[1].strip())
        except (ValueError, IndexError):
            return f"double_tap: invalid coordinates '{param}'. Use 'x,y'."
    else:
        x, y = _centre()

    pyautogui.doubleClick(x, y)
    return f"Double-tapped at ({x},{y})."


# ─── pinch_zoom ───────────────────────────────────────────────────────────────
# Uses the Windows SendInput API with POINTER_TOUCH_INFO to inject two-finger
# touch events — the only reliable way to trigger pinch-to-zoom on Windows.

_POINTER_FLAG_INRANGE     = 0x0002
_POINTER_FLAG_INCONTACT   = 0x0004
_POINTER_FLAG_DOWN        = 0x0040
_POINTER_FLAG_UPDATE      = 0x0100
_POINTER_FLAG_UP          = 0x0200
_TOUCH_FLAG_NONE          = 0x0000
_TOUCH_MASK_CONTACTAREA   = 0x0004
_PT_TOUCH                 = 0x00000002


class _POINTER_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerType",         ctypes.wintypes.DWORD),
        ("pointerId",           ctypes.wintypes.UINT),
        ("frameId",             ctypes.wintypes.UINT),
        ("pointerFlags",        ctypes.wintypes.DWORD),
        ("sourceDevice",        ctypes.wintypes.HANDLE),
        ("hwndTarget",          ctypes.wintypes.HWND),
        ("ptPixelLocation",     ctypes.wintypes.POINT),
        ("ptHimetricLocation",  ctypes.wintypes.POINT),
        ("ptPixelLocationRaw",  ctypes.wintypes.POINT),
        ("ptHimetricLocationRaw", ctypes.wintypes.POINT),
        ("dwTime",              ctypes.wintypes.DWORD),
        ("historyCount",        ctypes.wintypes.UINT),
        ("inputData",           ctypes.c_int32),
        ("dwKeyStates",         ctypes.wintypes.DWORD),
        ("PerformanceCount",    ctypes.c_uint64),
        ("ButtonChangeType",    ctypes.wintypes.DWORD),
    ]


class _POINTER_TOUCH_INFO(ctypes.Structure):
    _fields_ = [
        ("pointerInfo",    _POINTER_INFO),
        ("touchFlags",     ctypes.wintypes.DWORD),
        ("touchMask",      ctypes.wintypes.DWORD),
        ("rcContact",      ctypes.wintypes.RECT),
        ("rcContactRaw",   ctypes.wintypes.RECT),
        ("orientation",    ctypes.wintypes.UINT),
        ("pressure",       ctypes.wintypes.UINT),
    ]


def _make_touch(pointer_id: int, x: int, y: int, flags: int) -> _POINTER_TOUCH_INFO:
    pti = _POINTER_TOUCH_INFO()
    pti.pointerInfo.pointerType = _PT_TOUCH
    pti.pointerInfo.pointerId = pointer_id
    pti.pointerInfo.ptPixelLocation.x = x
    pti.pointerInfo.ptPixelLocation.y = y
    pti.pointerInfo.pointerFlags = flags
    pti.touchFlags = _TOUCH_FLAG_NONE
    pti.touchMask = _TOUCH_MASK_CONTACTAREA
    r = 10
    pti.rcContact.left   = x - r
    pti.rcContact.right  = x + r
    pti.rcContact.top    = y - r
    pti.rcContact.bottom = y + r
    pti.orientation = 90
    pti.pressure = 32000
    return pti


def _inject(*contacts: _POINTER_TOUCH_INFO) -> bool:
    arr = (_POINTER_TOUCH_INFO * len(contacts))(*contacts)
    try:
        ok = ctypes.windll.user32.InjectTouchInput(len(contacts), arr)
        return bool(ok)
    except Exception:
        return False


_PINCH_STEPS = 20
_PINCH_STEP_MS = 0.018


def pinch_zoom(param: str) -> str:
    """
    Two-finger pinch (zoom out) or spread (zoom in) at screen centre.

    param: "in" or "out"
    """
    direction = param.strip().lower()
    if direction not in ("in", "out"):
        direction = "in"

    w, h = _screen_size()
    cx, cy = w // 2, h // 2
    spread = int(min(w, h) * 0.20)   # half-distance between fingers at max spread

    try:
        # InitializeTouchInjection — max 2 contacts, hover mode
        ctypes.windll.user32.InitializeTouchInjection(2, 1)  # TOUCH_FEEDBACK_DEFAULT=1
    except Exception as exc:
        return f"Touch injection unavailable: {exc}"

    if direction == "in":   # zoom in = fingers start close, move apart
        start_spread, end_spread = spread // 4, spread
    else:                    # zoom out = fingers start apart, move close
        start_spread, end_spread = spread, spread // 4

    for step in range(_PINCH_STEPS + 1):
        t = step / _PINCH_STEPS
        cur = int(start_spread + (end_spread - start_spread) * t)

        if step == 0:
            f0 = _POINTER_FLAG_DOWN | _POINTER_FLAG_INRANGE | _POINTER_FLAG_INCONTACT
            f1 = _POINTER_FLAG_DOWN | _POINTER_FLAG_INRANGE | _POINTER_FLAG_INCONTACT
        elif step == _PINCH_STEPS:
            f0 = _POINTER_FLAG_UP
            f1 = _POINTER_FLAG_UP
        else:
            f0 = _POINTER_FLAG_UPDATE | _POINTER_FLAG_INRANGE | _POINTER_FLAG_INCONTACT
            f1 = _POINTER_FLAG_UPDATE | _POINTER_FLAG_INRANGE | _POINTER_FLAG_INCONTACT

        c0 = _make_touch(0, cx - cur, cy, f0)
        c1 = _make_touch(1, cx + cur, cy, f1)
        _inject(c0, c1)
        time.sleep(_PINCH_STEP_MS)

    return f"Pinch zoom {direction} at screen centre."
