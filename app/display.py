"""The size of the whole interface: text, buttons and pictures together.

Mistery's text is sized for a monitor at arm's length. On a television across
the room, or for eyes that want it bigger, that is small, and Windows' own
scaling is one setting for every program. So the app has one of its own
(Settings, Display), in steps, on top of whatever Windows asks for.

Everything grows by the same amount, not the text alone: the pages are full of
fixed measures (a button 52 px high, room for exactly three lines of a story)
that bigger letters by themselves would be cut off in. Qt does that scaling
when it is told to before the application object exists, which is why a new
size needs a restart and why this module must not import Qt.

A size is only offered if the window still fits the screen at it: the window is
never smaller than MIN_WINDOW of Qt's units, and a scale under which that is
more than the screen has would put its bottom edge off the screen.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys

from .config import settings

_log = logging.getLogger("startup")

STEPS = (100, 110, 125, 150, 175, 200)      # per cent
MIN_WINDOW = (960, 620)     # MainWindow's minimum size
_TITLE_BAR = 32             # a window's own title bar, about, where the screen is 96 dpi
# Set beside QT_SCALE_FACTOR when it is this module that set it. A Mistery
# started by this one (the restart for a new size, or after a library repair)
# inherits both, and this is how it knows the first is not somebody's wish.
_OURS = "MISTERY_UI_SCALE"

# What this run was started at: 100 unless start() said otherwise. Settings
# compares it with what is chosen to know whether a restart is still owed.
applied = 100
# QT_SCALE_FACTOR was set by whoever started Mistery, and has been left alone.
overridden = False


def _room() -> tuple[float, float] | None:
    """The primary screen's work area as Qt will measure it before any scaling
    of ours: in pixels at 96 dpi. None when it cannot be asked for.

    Asked of Windows directly, since Qt cannot be yet. The thread is made
    aware of the screen's real density for the length of the question only:
    an unaware one is told the screen is smaller than it is, and Qt has still
    to choose the process's own awareness for itself.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes.wintypes as wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.SetThreadDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
        previous = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))    # per monitor, v2
        try:
            area = wintypes.RECT()
            if not user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0):     # SPI_GETWORKAREA
                return None
            dpi = user32.GetDpiForSystem() or 96
        finally:
            if previous:
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))
    except (AttributeError, OSError):
        return None             # a Windows from before any of this
    width, height = area.right - area.left, area.bottom - area.top
    if width <= 0 or height <= 0:
        return None
    return width * 96.0 / dpi, height * 96.0 / dpi


def fitting(room: tuple[float, float] | None = None) -> tuple[int, ...]:
    """The steps this screen has room for. Always 100, whatever the screen."""
    room = room or _room()
    if room is None:
        return STEPS
    most = 100.0 * min(room[0] / MIN_WINDOW[0], (room[1] - _TITLE_BAR) / MIN_WINDOW[1])
    return tuple(step for step in STEPS if step == 100 or step <= most)


def chosen() -> int:
    """The size asked for in Settings: one of STEPS. settings.json is a file a
    person can edit, so anything else is brought to the nearest step."""
    try:
        value = float(settings.get("ui_scale", 100) or 100)
    except (TypeError, ValueError):
        return 100
    return min(STEPS, key=lambda step: abs(step - value))


def start() -> int:
    """Tell Qt the size, before the application exists. main() calls this once.

    The size chosen, or the largest the screen has room for when that is less.
    A QT_SCALE_FACTOR set by whoever started Mistery is left as it is: that is
    someone who knows what they want.
    """
    global applied, overridden
    if "QT_SCALE_FACTOR" in os.environ and _OURS not in os.environ:
        overridden = True
        return applied
    wanted, room = chosen(), fitting()
    applied = wanted if wanted in room else max(room)
    # One line a start: "the text is tiny" and "the window does not fit" both
    # begin with which size this was.
    _log.info("interface size %d %%%s", applied,
              "" if applied == wanted else f" (chosen {wanted} %: this screen has room for {applied} %)")
    if applied != 100:
        os.environ["QT_SCALE_FACTOR"] = f"{applied / 100:.2f}"
        os.environ[_OURS] = str(applied)
    else:
        os.environ.pop("QT_SCALE_FACTOR", None)
        os.environ.pop(_OURS, None)
    return applied
