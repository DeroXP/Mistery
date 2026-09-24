"""Game controllers, read the way Windows offers them, with nothing to install.

Xbox controllers (and the many that act like one) through XInput, from
xinput1_4.dll. PlayStation controllers (DualShock 4, DualSense) through the
old joystick API in winmm.dll, which sees them as the game controllers they
are; a pad XInput already has (Microsoft's vendor id) is left to XInput there,
so nothing is read twice.

Both come out as the same button names (a b x y lb rb lt rt start back ls rs
up down left right), in the Xbox places: PlayStation's Cross is "a", Circle
"b", Square "x", Triangle "y". What is printed on the buttons is the page's
business (the hints use `kind`).

Read only while Mistery's window is in front (Gamepad.set_active): a game in
another window is played with the same controller.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QTimer, Signal

_log = logging.getLogger("gamepad")

BUTTONS = ("a", "b", "x", "y", "lb", "rb", "lt", "rt", "start", "back", "ls", "rs",
           "up", "down", "left", "right")
DIRECTIONS = ("up", "down", "left", "right")

POLL_MS = 16                # 60 a second, while a controller is there and the window in front
SCAN_MS = 2000              # looking for one: XInput is slow to answer for an empty slot
REPEAT_DELAY_MS = 380       # a held direction starts repeating after this
REPEAT_MS = 110             # and then every this
STICK_DEAD = 0.55           # a stick past this far counts as that direction
TRIGGER_DOWN = 0.55         # a trigger past this far counts as pressed


@dataclass
class PadState:
    """One controller, as read: which buttons are down, and the sticks."""

    kind: str = "xbox"                          # xbox | playstation | generic
    name: str = "Controller"
    pressed: set[str] = field(default_factory=set)
    left: tuple[float, float] = (0.0, 0.0)      # -1..1 each, up is negative y
    right: tuple[float, float] = (0.0, 0.0)


def _stick_directions(x: float, y: float) -> set[str]:
    out = set()
    if x <= -STICK_DEAD:
        out.add("left")
    elif x >= STICK_DEAD:
        out.add("right")
    if y <= -STICK_DEAD:
        out.add("up")
    elif y >= STICK_DEAD:
        out.add("down")
    return out


# --- XInput ----------------------------------------------------------------------------------

class _XInputGamepad(ctypes.Structure):
    _fields_ = [("wButtons", wintypes.WORD), ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class _XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", _XInputGamepad)]


_XINPUT_BITS = {
    0x0001: "up", 0x0002: "down", 0x0004: "left", 0x0008: "right",
    0x0010: "start", 0x0020: "back", 0x0040: "ls", 0x0080: "rs",
    0x0100: "lb", 0x0200: "rb", 0x1000: "a", 0x2000: "b", 0x4000: "x", 0x8000: "y",
}


def xinput_state(buttons: int, left_trigger: int, right_trigger: int,
                 lx: int, ly: int, rx: int, ry: int) -> PadState:
    """An XINPUT_GAMEPAD's numbers as a PadState (separate, so it can be tried)."""
    pressed = {name for bit, name in _XINPUT_BITS.items() if buttons & bit}
    if left_trigger / 255 >= TRIGGER_DOWN:
        pressed.add("lt")
    if right_trigger / 255 >= TRIGGER_DOWN:
        pressed.add("rt")
    left = (lx / 32768.0, -ly / 32768.0)            # XInput's up is positive
    pressed |= _stick_directions(*left)
    return PadState("xbox", "Xbox controller", pressed, left, (rx / 32768.0, -ry / 32768.0))


class XInputBackend:
    def __init__(self) -> None:
        self._get = None
        for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self._get = getattr(ctypes.WinDLL(name), "XInputGetState")
                break
            except (OSError, AttributeError):
                continue

    @property
    def available(self) -> bool:
        return self._get is not None

    def read(self, slot: int) -> PadState | None:
        if self._get is None:
            return None
        state = _XInputState()
        if self._get(slot, ctypes.byref(state)) != 0:           # ERROR_DEVICE_NOT_CONNECTED
            return None
        pad = state.Gamepad
        return xinput_state(pad.wButtons, pad.bLeftTrigger, pad.bRightTrigger,
                            pad.sThumbLX, pad.sThumbLY, pad.sThumbRX, pad.sThumbRY)


# --- the joystick API (PlayStation and others) ------------------------------------------------

class _JoyInfoEx(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("dwXpos", wintypes.DWORD),
                ("dwYpos", wintypes.DWORD), ("dwZpos", wintypes.DWORD), ("dwRpos", wintypes.DWORD),
                ("dwUpos", wintypes.DWORD), ("dwVpos", wintypes.DWORD), ("dwButtons", wintypes.DWORD),
                ("dwButtonNumber", wintypes.DWORD), ("dwPOV", wintypes.DWORD),
                ("dwReserved1", wintypes.DWORD), ("dwReserved2", wintypes.DWORD)]


class _JoyCaps(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("szPname", wintypes.WCHAR * 32),
                ("wXmin", wintypes.UINT), ("wXmax", wintypes.UINT), ("wYmin", wintypes.UINT),
                ("wYmax", wintypes.UINT), ("wZmin", wintypes.UINT), ("wZmax", wintypes.UINT),
                ("wNumButtons", wintypes.UINT), ("wPeriodMin", wintypes.UINT), ("wPeriodMax", wintypes.UINT),
                ("wRmin", wintypes.UINT), ("wRmax", wintypes.UINT), ("wUmin", wintypes.UINT),
                ("wUmax", wintypes.UINT), ("wVmin", wintypes.UINT), ("wVmax", wintypes.UINT),
                ("wCaps", wintypes.UINT), ("wMaxAxes", wintypes.UINT), ("wNumAxes", wintypes.UINT),
                ("wMaxButtons", wintypes.UINT), ("szRegKey", wintypes.WCHAR * 32),
                ("szOEMVxD", wintypes.WCHAR * 260)]


_JOY_RETURNALL = 0x000000FF
_SONY = 0x054C
_MICROSOFT = 0x045E
# DirectInput's numbering for DualShock 4 and DualSense (bit n is button n+1):
# Square, Cross, Circle, Triangle, L1, R1, L2, R2, Share/Create, Options, L3, R3.
_SONY_BITS = {0: "x", 1: "a", 2: "b", 3: "y", 4: "lb", 5: "rb", 6: "lt", 7: "rt",
              8: "back", 9: "start", 10: "ls", 11: "rs"}
# A generic pad's first buttons, in the order most of them come in.
_GENERIC_BITS = {0: "a", 1: "b", 2: "x", 3: "y", 4: "lb", 5: "rb", 6: "back", 7: "start", 8: "ls", 9: "rs"}


def _axis(value: int) -> float:
    return max(-1.0, min(1.0, (value - 32767.5) / 32767.5))


def joystick_state(vendor: int, name: str, buttons: int, x: int, y: int, z: int, r: int,
                   pov: int) -> PadState:
    """A joyGetPosEx reading as a PadState (separate, so it can be tried)."""
    sony = vendor == _SONY
    bits = _SONY_BITS if sony else _GENERIC_BITS
    pressed = {label for bit, label in bits.items() if buttons & (1 << bit)}
    if pov != 0xFFFF and pov <= 36000:                  # the d-pad, in hundredths of a degree
        angle = pov / 100.0
        if angle >= 315 or angle <= 45:
            pressed.add("up")
        if 45 <= angle <= 135:
            pressed.add("right")
        if 135 <= angle <= 225:
            pressed.add("down")
        if 225 <= angle <= 315:
            pressed.add("left")
    left = (_axis(x), _axis(y))
    pressed |= _stick_directions(*left)
    return PadState("playstation" if sony else "generic",
                    name or ("PlayStation controller" if sony else "Controller"),
                    pressed, left, (_axis(z), _axis(r)))


class JoystickBackend:
    def __init__(self) -> None:
        try:
            self._winmm = ctypes.WinDLL("winmm")
        except OSError:
            self._winmm = None
        self._caps: dict[int, _JoyCaps] = {}

    @property
    def available(self) -> bool:
        return self._winmm is not None

    def slots(self) -> int:
        return int(self._winmm.joyGetNumDevs()) if self._winmm is not None else 0

    def read(self, slot: int) -> PadState | None:
        if self._winmm is None:
            return None
        caps = self._caps.get(slot)
        if caps is None:
            caps = _JoyCaps()
            if self._winmm.joyGetDevCapsW(slot, ctypes.byref(caps), ctypes.sizeof(caps)) != 0:
                return None
            self._caps[slot] = caps
        if caps.wMid == _MICROSOFT:
            return None                     # an Xbox pad: XInput reads it, and better
        info = _JoyInfoEx()
        info.dwSize = ctypes.sizeof(info)
        info.dwFlags = _JOY_RETURNALL
        if self._winmm.joyGetPosEx(slot, ctypes.byref(info)) != 0:
            self._caps.pop(slot, None)      # gone: ask its caps again when it is back
            return None
        return joystick_state(caps.wMid, caps.szPname, info.dwButtons, info.dwXpos, info.dwYpos,
                              info.dwZpos, info.dwRpos, info.dwPOV)


# --- the one the window listens to ------------------------------------------------------------

class Gamepad(QObject):
    """The first controller found, as button presses.

    `pressed(name)` once per press, and again while a direction is held (after
    REPEAT_DELAY_MS, every REPEAT_MS). `connected(kind, name)` and
    `disconnected()` when one comes and goes.
    """

    pressed = Signal(str)
    released = Signal(str)
    connected = Signal(str, str)
    disconnected = Signal()

    def __init__(self, parent=None, backends=None) -> None:
        super().__init__(parent)
        if backends is None:
            backends = [XInputBackend(), JoystickBackend()] if sys.platform == "win32" else []
        self._backends = [backend for backend in backends if backend.available]
        self._source: tuple[object, int] | None = None
        self._state = PadState()
        self._held: dict[str, float] = {}           # direction -> when it next repeats
        self._active = True
        self._enabled = True
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._restart()

    # --- state -----------------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._source is not None

    @property
    def kind(self) -> str:
        return self._state.kind if self._source is not None else ""

    @property
    def name(self) -> str:
        return self._state.name if self._source is not None else ""

    def set_active(self, active: bool) -> None:
        """Read only while the window is in front; otherwise, just look now and then."""
        self._active = bool(active)
        if not self._active:
            self._release_all()
        self._restart()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not self._enabled:
            self._release_all()
        self._restart()

    def _restart(self) -> None:
        if not self._enabled or not self._backends:
            self._timer.stop()
            return
        fast = self._active and self._source is not None
        self._timer.start(POLL_MS if fast else SCAN_MS)

    # --- reading ---------------------------------------------------------------------------

    def _find(self) -> tuple[object, int, PadState] | None:
        for backend in self._backends:
            count = 4 if isinstance(backend, XInputBackend) else getattr(backend, "slots", lambda: 4)()
            for slot in range(min(count, 16)):
                try:
                    state = backend.read(slot)
                except Exception:                   # noqa: BLE001 - a driver's odd answer
                    state = None
                if state is not None:
                    return backend, slot, state
        return None

    def _tick(self) -> None:
        if self._source is None:
            found = self._find()
            if found is None:
                return
            backend, slot, state = found
            self._source = (backend, slot)
            self._state = PadState(state.kind, state.name)
            _log.info("controller connected: %s (%s)", state.name, state.kind)
            self.connected.emit(state.kind, state.name)
            self._restart()
            if not self._active:
                return
        backend, slot = self._source
        try:
            state = backend.read(slot)
        except Exception:                           # noqa: BLE001 - read as gone
            state = None
        if state is None:
            _log.info("controller disconnected")
            self._release_all()
            self._source = None
            self.disconnected.emit()
            self._restart()
            return
        if self._active:
            self.feed(state)

    def feed(self, state: PadState) -> None:
        """One reading: presses for what went down, repeats for held directions.
        Public for tests, which feed readings instead of a controller."""
        now = time.monotonic()
        before = self._state.pressed
        self._state = state
        for name in [n for n in BUTTONS if n in state.pressed and n not in before]:
            if name in DIRECTIONS:
                self._held[name] = now + REPEAT_DELAY_MS / 1000.0
            self.pressed.emit(name)
        for name in [n for n in BUTTONS if n in before and n not in state.pressed]:
            self._held.pop(name, None)
            self.released.emit(name)
        for name, when in list(self._held.items()):
            if name in state.pressed and now >= when:
                self._held[name] = now + REPEAT_MS / 1000.0
                self.pressed.emit(name)

    def _release_all(self) -> None:
        for name in sorted(self._state.pressed):
            self.released.emit(name)
        self._state = PadState(self._state.kind, self._state.name)
        self._held.clear()
