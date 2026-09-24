"""Serving friends while Mistery is closed: the Mistery that has no window.

The owner wanted friends to reach their library whenever this PC is on, not
only while Mistery's window is open. `Mistery.exe --share` is that (sharer.py
run_background does the serving); this module makes sure it runs when it
should, only once, and never in anybody's way:

  - **Started at sign-in** by a per-user Run entry in the registry, the list
    Task Manager's Startup tab shows (where it can be switched off like any
    other). The entry is there while sharing is on with friends to serve and
    "Keep sharing while Mistery is closed" is ticked, and gone otherwise.
  - **Started when Mistery quits** with sharing on, so friends can carry on from
    the moment the window closes rather than from the next sign-in.
  - **One at a time.** It holds a named mutex; a second one started while the
    first is alive sees it taken and leaves.
  - **Out of the updater's way.** MisteryUpdate.exe will not replace files
    under a running Mistery.exe, and this one would be running for ever. So it
    writes share.lock (its pid and start time, as the app writes app.lock) for
    the updater to recognise it by, and leaves when the updater sets its stop
    event; the updater starts it again once the new files are in
    (updater/liveness.py).

Only Windows has any of this; elsewhere every function is a quiet no.
"""

from __future__ import annotations

import ctypes
import getpass
import hashlib
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .. import db
from ..config import data_dir, install_dir, settings

_log = logging.getLogger("share")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Mistery sharing"
LOCK_NAME = "share.lock"
_SYNCHRONIZE = 0x00100000
_EVENT_MODIFY_STATE = 0x0002
_ERROR_ALREADY_EXISTS = 183
_WAIT_OBJECT_0 = 0


def _tag() -> str:
    """This Windows user's part of every name here, as main.py _instance_channel
    builds it: a library pointed elsewhere with MISTERY_DATA_DIR (a test) gets
    names of its own, so it never meets the owner's real one."""
    try:
        user = getpass.getuser() or "user"
    except Exception:                               # noqa: BLE001
        user = "user"
    tag = re.sub(r"[^A-Za-z0-9_-]", "_", user)
    elsewhere = os.environ.get("MISTERY_DATA_DIR")
    if elsewhere:
        digest = hashlib.sha256(str(Path(elsewhere).resolve()).lower().encode())
        tag += "-" + digest.hexdigest()[:8]
    return tag


def mutex_name() -> str:
    """Held by the one without a window for as long as it runs."""
    return "Local\\Mistery-sharing-" + _tag()


def stop_event_name() -> str:
    """Set by the updater (or anyone) to ask it to leave."""
    return "Local\\Mistery-sharing-stop-" + _tag()


def command() -> list[str]:
    """How to start it: the installed Mistery.exe, or from source pythonw (no
    console window) running main.py."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--share"]
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    return [str(windowless if windowless.is_file() else python),
            str(install_dir() / "main.py"), "--share"]


def wanted() -> bool:
    """Whether friends should be served while Mistery is closed."""
    return (bool(settings.get("sharing_background", True))
            and bool(settings.get("sharing_enabled")) and bool(db.friends()))


def _sandboxed() -> bool:
    """A throwaway data folder (MISTERY_DATA_DIR): a test's, or somebody trying
    a copy of Mistery. The owner's sign-in list and processes are not theirs to
    change, so sync() and start_now() do nothing there, unless a test has put
    stand-ins in place of `registry` and `spawn`."""
    return bool(os.environ.get("MISTERY_DATA_DIR"))


# --- the sign-in entry ------------------------------------------------------------------------

class _Registry:
    """HKEY_CURRENT_USER's Run key, the one value in it that is ours. Tests
    replace `registry` with something that keeps it in memory."""

    def get(self) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, RUN_VALUE)
        except OSError:
            return None
        return value if isinstance(value, str) else None

    def set(self, value: str) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, value)

    def delete(self) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE)
        except FileNotFoundError:
            pass


registry = _Registry()


def sync() -> bool:
    """Make the sign-in entry match what is wanted. Returns whether it is there.

    Called whenever sharing, friends or the setting change, and at every start:
    an entry left pointing at an old install's path is written again.
    """
    if sys.platform != "win32" or (_sandboxed() and isinstance(registry, _Registry)):
        return False
    try:
        if wanted():
            line = subprocess.list2cmdline(command())
            if registry.get() != line:
                registry.set(line)
                _log.info("share: Mistery will serve friends from sign-in (%s)", line)
            return True
        if registry.get() is not None:
            registry.delete()
            _log.info("share: no longer starting at sign-in to serve friends")
    except OSError as problem:
        _log.warning("share: could not change the sign-in entry: %s", problem)
    return False


# --- now ---------------------------------------------------------------------------------------

def running() -> bool:
    """Whether the one without a window is up (its mutex is held)."""
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    kernel32.OpenMutexW.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, mutex_name())
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def _spawn(line: list[str]) -> None:
    """Start it, detached, with no window."""
    flags = 0
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.CREATE_NO_WINDOW)
    subprocess.Popen(line, cwd=str(install_dir()), creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


spawn = _spawn          # tests replace this


def start_now() -> bool:
    """Mistery is quitting with sharing on: start the one without a window now,
    rather than at the next sign-in. True when it was started."""
    if (sys.platform != "win32" or (_sandboxed() and spawn is _spawn) or not wanted()
            or running()):
        return False
    try:
        spawn(command())
    except OSError as problem:
        _log.warning("share: could not start serving friends in the background: %s", problem)
        return False
    _log.info("share: serving friends in the background from now")
    return True


# --- inside the one without a window ----------------------------------------------------------

class Claim:
    """Being the one: the mutex, the stop event, and share.lock."""

    def __init__(self, mutex: int, event: int) -> None:
        self._mutex = mutex
        self._event = event
        self.lock = data_dir() / LOCK_NAME
        started = time.time()
        try:
            self.lock.write_text(f"{os.getpid()},{started}", encoding="utf-8")
        except OSError as problem:
            _log.warning("share: could not write %s: %s", LOCK_NAME, problem)

    def stop_requested(self, wait: float = 0.0) -> bool:
        """Whether the updater (or anyone) has asked it to leave, waiting up to
        `wait` seconds to be asked: the background loop's sleep."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        return kernel32.WaitForSingleObject(self._event, int(max(0.0, wait) * 1000)) == _WAIT_OBJECT_0

    def release(self) -> None:
        try:
            if self.lock.is_file() and self.lock.read_text(encoding="utf-8").split(",")[0] == str(os.getpid()):
                self.lock.unlink()
        except (OSError, IndexError):
            pass
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        for handle in (self._event, self._mutex):
            if handle:
                kernel32.CloseHandle(handle)
        self._event = self._mutex = 0


def claim() -> Claim | None:
    """Become the one without a window, or None when another already is."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.ResetEvent.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    mutex = kernel32.CreateMutexW(None, False, mutex_name())
    if not mutex:
        return None
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(mutex)
        return None
    # Manual reset: once asked to leave, every look at it says so.
    event = kernel32.CreateEventW(None, True, False, stop_event_name())
    if not event:
        kernel32.CloseHandle(mutex)
        return None
    kernel32.ResetEvent(event)          # a request left over from one that has gone
    return Claim(mutex, event)


def ask_to_stop() -> bool:
    """Set the stop event: what the updater does, from here for tests and for a
    Mistery that wants the port for itself. True when there was one to ask."""
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.restype = ctypes.c_void_p
    kernel32.OpenEventW.argtypes = (ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.SetEvent.argtypes = (ctypes.c_void_p,)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    event = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, stop_event_name())
    if not event:
        return False
    kernel32.SetEvent(event)
    kernel32.CloseHandle(event)
    return True
