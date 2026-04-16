"""
desktop_character.py — Animated pixel-art character that lives on the desktop.

Architecture:
    - Frameless, transparent, always-on-top tkinter window
    - Sprite sheet animator: slices horizontal strip PNGs into frames, cycles at FPS
    - State machine: idle → walk → talk → react → point → sleep
    - Smooth movement: lerp toward target position, bounce on arrive
    - Speech bubble: canvas text that fades after N seconds
    - Character is controlled externally via thread-safe method calls from the async loop

Sprite format expected (per state):
    assets/sprites/<state>.png  — horizontal strip of 48×48 frames, RGBA transparent bg
    e.g. idle.png = 4 frames wide = 192×48 total

Falls back gracefully if a sprite file is missing (uses idle or a coloured square).

Thread safety:
    All tkinter calls must happen on the tkinter thread.
    External callers use thread-safe wrappers that post via widget.after(0, fn).
"""

from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
from pathlib import Path
from typing import Callable

import config

log = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

SPRITE_DIR = config.ROOT / "assets" / "sprites"

# Target render dimensions — sprites are scaled to this height, width is proportional
DISPLAY_HEIGHT = 120
CANVAS_W = 300
CANVAS_H = DISPLAY_HEIGHT + 65  # 65px bubble area above character

# Exact frame counts matching slice_sprites.py ANIMATIONS list
SPRITE_FRAME_COUNTS: dict[str, int] = {
    "idle":  4,
    "walk":  6,
    "talk":  4,
    "react": 6,
    "point": 4,
    "sleep": 6,
}

BUBBLE_MAX_CHARS = 80      # wrap speech bubble text at this width
BUBBLE_SHOW_SECONDS = 5.0  # how long bubble stays before fading

# State definitions: name → (fps, loop)
# loop=False means play once then return to idle
STATES: dict[str, dict] = {
    "idle":   {"fps": 6,  "loop": True},
    "walk":   {"fps": 10, "loop": True},
    "talk":   {"fps": 8,  "loop": True},
    "react":  {"fps": 12, "loop": False},
    "point":  {"fps": 6,  "loop": False},
    "sleep":  {"fps": 3,  "loop": True},
}

# Idle wander: move every this many seconds (random within range)
WANDER_MIN_S = 20.0
WANDER_MAX_S = 50.0

# Sleep after this many seconds of no interaction
SLEEP_AFTER_S = 120.0


class SpriteAnimator:
    """Loads a sprite sheet and manages frame cycling."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.frames: list = []   # list of PIL.ImageTk.PhotoImage
        self.current_frame = 0
        self.display_w: int = DISPLAY_HEIGHT
        self.display_h: int = DISPLAY_HEIGHT
        self._load()

    def _load(self) -> None:
        path = SPRITE_DIR / f"{self.state}.png"
        if not path.exists():
            log.warning("Sprite missing: %s — using placeholder", path)
            self.frames = []
            return
        try:
            from PIL import Image, ImageTk
            img = Image.open(path).convert("RGBA")
            # Use known frame count; fall back to square-frame inference
            frame_count = SPRITE_FRAME_COUNTS.get(self.state, 0)
            if frame_count < 1:
                frame_count = max(1, img.width // img.height)
            frame_w = img.width // frame_count
            frame_h = img.height
            # Scale to DISPLAY_HEIGHT maintaining aspect ratio
            scale = DISPLAY_HEIGHT / frame_h
            self.display_w = max(1, round(frame_w * scale))
            self.display_h = DISPLAY_HEIGHT
            raw_frames = []
            for i in range(frame_count):
                box = (i * frame_w, 0, (i + 1) * frame_w, frame_h)
                frame = img.crop(box).resize(
                    (self.display_w, self.display_h),
                    Image.NEAREST,  # nearest-neighbor = crisp pixel art
                )
                raw_frames.append(frame)
            # Store as PhotoImage — must be kept alive (no gc)
            self.frames = [ImageTk.PhotoImage(f) for f in raw_frames]
            log.debug(
                "Loaded %d frames for state '%s' (frame %dx%d → display %dx%d)",
                len(self.frames), self.state, frame_w, frame_h,
                self.display_w, self.display_h,
            )
        except Exception as exc:
            log.error("Failed to load sprite '%s': %s", self.state, exc)
            self.frames = []

    def has_frames(self) -> bool:
        return len(self.frames) > 0

    def next_frame(self) -> int:
        """Advance and return new frame index."""
        if self.frames:
            self.current_frame = (self.current_frame + 1) % len(self.frames)
        return self.current_frame

    def reset(self) -> None:
        self.current_frame = 0

    @property
    def photo_image(self):
        if not self.frames:
            return None
        return self.frames[self.current_frame]

    @property
    def total_frames(self) -> int:
        return len(self.frames)


class DesktopCharacter:
    """
    The main character window. Runs its own thread with a tkinter mainloop.

    Public thread-safe API (callable from asyncio thread):
        .set_state(state)         — switch animation state
        .say(text)                — show speech bubble + switch to talk state
        .move_toward(x, y)        — start moving toward screen coordinate
        .react(text)              — play react animation + say something
        .point_at(x, y, text)     — point toward a screen area and comment
        .start_listening()        — visual cue: character is listening
        .stop_listening()         — return to idle
        .shutdown()               — close window and exit thread
    """

    def __init__(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="DesktopChar")
        self._ready = threading.Event()
        self._root = None
        self._canvas = None
        self._animators: dict[str, SpriteAnimator] = {}
        self._placeholder_img = None  # fallback colored square

        # Position (top-left of window)
        self._x: float = 100.0
        self._y: float = 100.0
        self._target_x: float = 100.0
        self._target_y: float = 100.0
        self._vel_x: float = 0.0
        self._vel_y: float = 0.0

        # State
        self._state: str = "idle"
        self._facing_right: bool = True
        self._bubble_text: str = ""
        self._bubble_timer: float = 0.0
        self._last_interaction: float = time.time()
        self._wander_timer: float = self._next_wander_time()
        self._one_shot_done: bool = False  # for non-looping animations
        self._frame_tick: int = 0

        # Screen dimensions (updated on start)
        self._screen_w: int = 1920
        self._screen_h: int = 1080

        self._thread.start()
        self._ready.wait(timeout=5.0)

    # ─── Thread-safe public API ───────────────────────────────────────────────

    def set_state(self, state: str) -> None:
        if self._root:
            self._root.after(0, lambda: self._set_state_internal(state))

    def say(self, text: str) -> None:
        if self._root:
            self._root.after(0, lambda: self._say_internal(text))

    def move_toward(self, x: int, y: int) -> None:
        if self._root:
            self._root.after(0, lambda: self._set_target(float(x), float(y)))

    def react(self, text: str = "") -> None:
        if self._root:
            self._root.after(0, lambda: self._react_internal(text))

    def point_at(self, x: int, y: int, text: str = "") -> None:
        if self._root:
            self._root.after(0, lambda: self._point_internal(x, y, text))

    def start_listening(self) -> None:
        if self._root:
            self._root.after(0, lambda: self._set_state_internal("talk"))
            self._root.after(0, lambda: setattr(self, "_last_interaction", time.time()))

    def stop_listening(self) -> None:
        self.set_state("idle")

    def shutdown(self) -> None:
        if self._root:
            self._root.after(0, self._root.destroy)

    # ─── Tk thread internals ─────────────────────────────────────────────────

    def _run(self) -> None:
        """Tkinter main loop — runs on dedicated thread."""
        try:
            import tkinter as tk
            self._root = tk.Tk()
            self._root.title("companion")
            self._root.overrideredirect(True)   # no title bar / borders
            self._root.attributes("-topmost", True)
            self._root.attributes("-transparentcolor", "#010101")  # chroma-key transparency
            self._root.configure(bg="#010101")
            self._root.resizable(False, False)

            self._screen_w = self._root.winfo_screenwidth()
            self._screen_h = self._root.winfo_screenheight()

            # Start near bottom-right by default
            self._x = float(self._screen_w - CANVAS_W - 50)
            self._y = float(self._screen_h - CANVAS_H - 80)
            self._target_x = self._x
            self._target_y = self._y

            self._canvas = tk.Canvas(
                self._root,
                width=CANVAS_W,
                height=CANVAS_H,
                bg="#010101",
                highlightthickness=0,
            )
            self._canvas.pack()

            # Draggable — click-drag to reposition
            self._canvas.bind("<ButtonPress-1>", self._on_drag_start)
            self._canvas.bind("<B1-Motion>", self._on_drag_motion)

            # Load all sprites
            SPRITE_DIR.mkdir(parents=True, exist_ok=True)
            for state in STATES:
                self._animators[state] = SpriteAnimator(state)

            # Update window position
            self._update_window_pos()
            self._ready.set()

            # Start the animation / logic tick loop
            self._tick()
            self._root.mainloop()
        except Exception as exc:
            log.error("DesktopCharacter thread crashed: %s", exc, exc_info=True)
            self._ready.set()  # unblock main thread even on failure

    def _tick(self) -> None:
        """Called every ~33ms (≈30fps) on the tkinter thread."""
        now = time.time()
        dt = 0.033

        self._update_movement(dt, now)
        self._update_state_machine(now)
        self._draw()

        if self._root:
            self._root.after(33, self._tick)

    def _update_movement(self, dt: float, now: float) -> None:
        """Smooth lerp movement toward target."""
        dx = self._target_x - self._x
        dy = self._target_y - self._y
        dist = math.hypot(dx, dy)

        if dist > 2.0:
            speed = min(dist * 4.0, 300.0)  # pixels/sec, max 300
            self._x += (dx / dist) * speed * dt
            self._y += (dy / dist) * speed * dt
            self._facing_right = dx > 0
            if self._state == "idle":
                self._set_state_internal("walk")
        else:
            self._x = self._target_x
            self._y = self._target_y
            if self._state == "walk":
                self._set_state_internal("idle")

        self._update_window_pos()

    def _update_state_machine(self, now: float) -> None:
        """Handle one-shot animation completion, idle wander, sleep."""
        # One-shot animations return to idle when done
        state_cfg = STATES.get(self._state, {})
        if not state_cfg.get("loop", True) and self._one_shot_done:
            self._set_state_internal("idle")
            self._one_shot_done = False

        # Wander timer — only fire when truly at rest to avoid mid-walk direction changes
        if self._state == "idle" and now >= self._wander_timer:
            self._wander()
            self._wander_timer = self._next_wander_time()

        # Sleep after inactivity
        if (
            self._state == "idle"
            and now - self._last_interaction > SLEEP_AFTER_S
        ):
            self._set_state_internal("sleep")

        # Bubble fade
        if self._bubble_text and now >= self._bubble_timer:
            self._bubble_text = ""

    def _draw(self) -> None:
        """Render current frame + speech bubble onto canvas."""
        if not self._canvas:
            return
        self._canvas.delete("all")

        animator = self._animators.get(self._state) or self._animators.get("idle")

        # Draw speech bubble first (behind character)
        if self._bubble_text:
            self._draw_bubble()

        # Draw character sprite
        if animator and animator.has_frames():
            img = animator.photo_image
            if img:
                # Center sprite horizontally on canvas, below bubble area
                sx = max(0, (CANVAS_W - animator.display_w) // 2)
                sy = 65
                self._canvas.create_image(sx, sy, anchor="nw", image=img)
            # Advance frame based on FPS
            fps = STATES.get(self._state, {}).get("fps", 6)
            # We tick at 30fps; advance sprite frame every (30/fps) ticks
            self._frame_tick += 1
            ticks_per_frame = max(1, round(30 / fps))
            if self._frame_tick >= ticks_per_frame:
                self._frame_tick = 0
                old_frame = animator.current_frame
                animator.next_frame()
                # Detect one-shot loop completion
                if (
                    not STATES.get(self._state, {}).get("loop", True)
                    and animator.current_frame == 0
                    and old_frame > 0
                ):
                    self._one_shot_done = True
        else:
            # Fallback placeholder square (so character is visible even without sprites)
            colors = {
                "idle": "#7ecfff", "walk": "#7ecfff", "talk": "#ffe066",
                "react": "#ff6b6b", "point": "#a8ff78", "sleep": "#b0b0b0"
            }
            color = colors.get(self._state, "#7ecfff")
            px = (CANVAS_W - DISPLAY_HEIGHT) // 2
            self._canvas.create_rectangle(
                px, 65, px + DISPLAY_HEIGHT, 65 + DISPLAY_HEIGHT,
                fill=color, outline="white", width=2
            )
            self._canvas.create_text(
                CANVAS_W // 2, 65 + DISPLAY_HEIGHT // 2,
                text=self._state[:4],
                fill="white",
                font=("Consolas", 10, "bold"),
            )

    def _draw_bubble(self) -> None:
        """Draw a speech bubble above the character."""
        import tkinter as tk
        text = self._bubble_text
        # Simple word wrap
        words = text.split()
        lines = []
        current = ""
        for w in words:
            if len(current) + len(w) + 1 <= 28:
                current = (current + " " + w).strip()
            else:
                if current:
                    lines.append(current)
                current = w
        if current:
            lines.append(current)
        wrapped = "\n".join(lines)

        # Measure approximate size
        char_w, char_h = 7, 14
        max_line_len = max((len(l) for l in lines), default=10)
        box_w = max_line_len * char_w + 16
        box_h = len(lines) * char_h + 12

        bx = max(4, min(4, 300 - box_w - 4))
        by = max(4, 56 - box_h - 8)  # above character

        # Bubble background
        self._canvas.create_rectangle(
            bx, by, bx + box_w, by + box_h,
            fill="#ffffcc", outline="#333333", width=1,
        )
        # Tail (little triangle pointing down)
        cx = bx + box_w // 2
        self._canvas.create_polygon(
            cx - 6, by + box_h,
            cx + 6, by + box_h,
            cx, by + box_h + 7,
            fill="#ffffcc", outline="#333333",
        )
        # Text
        self._canvas.create_text(
            bx + 8, by + 6,
            text=wrapped,
            anchor="nw",
            font=("Consolas", 9),
            fill="#222222",
        )

    # ─── Internal state setters ───────────────────────────────────────────────

    def _set_state_internal(self, state: str) -> None:
        if state not in STATES:
            state = "idle"
        if state == self._state:
            return
        self._state = state
        animator = self._animators.get(state)
        if animator:
            animator.reset()
        self._one_shot_done = False
        self._frame_tick = 0
        log.debug("Character state → %s", state)

    def _say_internal(self, text: str) -> None:
        self._bubble_text = text[:BUBBLE_MAX_CHARS]
        self._bubble_timer = time.time() + BUBBLE_SHOW_SECONDS
        self._last_interaction = time.time()
        self._set_state_internal("talk")

    def _react_internal(self, text: str) -> None:
        if text:
            self._say_internal(text)
        self._set_state_internal("react")
        self._last_interaction = time.time()

    def _point_internal(self, tx: int, ty: int, text: str) -> None:
        # Move toward the target area (but stay on screen edge nearest to it)
        edge_x, edge_y = self._nearest_edge_toward(tx, ty)
        self._set_target(edge_x, edge_y)
        self._set_state_internal("point")
        if text:
            self._say_internal(text)
        self._last_interaction = time.time()

    def _set_target(self, x: float, y: float) -> None:
        # Clamp to screen bounds
        x = max(0.0, min(float(self._screen_w - CANVAS_W), x))
        y = max(0.0, min(float(self._screen_h - CANVAS_H), y))
        self._target_x = x
        self._target_y = y

    def _wander(self) -> None:
        """Pick a new random position to wander to."""
        margin = 100
        tx = random.uniform(margin, self._screen_w - CANVAS_W - margin)
        ty = random.uniform(
            self._screen_h * 0.5,  # stay in bottom half of screen
            self._screen_h - CANVAS_H - 80,
        )
        self._set_target(tx, ty)

    def _nearest_edge_toward(self, tx: int, ty: int) -> tuple[float, float]:
        """Return a position on the near side of the screen toward (tx, ty)."""
        cx = self._screen_w / 2
        cy = self._screen_h / 2
        dx = tx - cx
        dy = ty - cy
        # Move character to the quadrant of the screen closest to that target
        px = cx + dx * 0.4
        py = cy + dy * 0.4
        return px, py

    def _update_window_pos(self) -> None:
        if self._root:
            self._root.geometry(
                f"+{int(self._x)}+{int(self._y)}"
            )

    # ─── Drag handlers ───────────────────────────────────────────────────────

    def _on_drag_start(self, event) -> None:
        self._drag_start_x = event.x_root - int(self._x)
        self._drag_start_y = event.y_root - int(self._y)

    def _on_drag_motion(self, event) -> None:
        nx = float(event.x_root - self._drag_start_x)
        ny = float(event.y_root - self._drag_start_y)
        self._x = nx
        self._y = ny
        self._target_x = nx
        self._target_y = ny
        self._update_window_pos()

    # ─── Next wander time ─────────────────────────────────────────────────────

    @staticmethod
    def _next_wander_time() -> float:
        return time.time() + random.uniform(WANDER_MIN_S, WANDER_MAX_S)
