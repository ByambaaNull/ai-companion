# actions/__init__.py
# Re-export handler functions so action_router can import them from one place.

from actions.browser import open_browser
from actions.music import MusicPlayer
from actions.screenshot import take_screenshot, screenshot_to_base64

__all__ = [
    "open_browser",
    "MusicPlayer",
    "take_screenshot",
    "screenshot_to_base64",
]
