"""The Mistery that serves friends with no window, and the two registry entries
the app writes for itself, as Setup and Uninstall meet them.

`Mistery.exe --share` (the app's app/share/background.py) runs from sign-in
while sharing is on, and never quits by itself. Replacing or deleting
Mistery.exe under it fails exactly as it does under an open Mistery, and the
message for that ("close it, check the system tray") would send the person
looking for a window that is not there. So both ask it to step aside first,
the way the updater does (updater/liveness.py stop_sharer): share.lock in the
library folder names its pid, and its stop event asks it to leave. Only one
running out of the folder being replaced or removed: a Mistery serving friends
from some other copy is that copy's business. Setup starts it again afterwards;
Uninstall does not.

The app also writes two entries in HKEY_CURRENT_USER that no installer made,
so no mistery-install.json records them: the Run value that starts it at
sign-in, and Software\\Classes\\mistery, which makes mistery:// links open it
(main.py _register_links). Uninstall removes each only when it points into the
folder being removed; one aimed at another copy of Mistery belongs to that copy.

Every name here is built exactly as the app builds it. A test install
(MISTERY_SETUP_TEST_ROOT) keeps its two entries under Software\\Mistery\\SetupTest,
never in the real Run key or Classes.
"""

from __future__ import annotations

import ctypes
import getpass
import hashlib
import os
import re
import subprocess
from pathlib import Path

from .setup_common import APP_EXE_NAME, data_dir, test_root

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "Mistery sharing"
LINK_KEY = r"Software\Classes\mistery"
SHARE_LOCK = "share.lock"
_TEST_KEY = r"Software\Mistery\SetupTest"

_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_EVENT_MODIFY_STATE = 0x0002
_STILL_ACTIVE = 259


def run_key() -> str:
    return _TEST_KEY + r"\Run" if test_root() else RUN_KEY


def link_key() -> str:
    return _TEST_KEY + r"\Classes\mistery" if test_root() else LINK_KEY


def _tag() -> str:
    """This Windows user's part of the names, as app/share/background.py _tag
    builds it, MISTERY_DATA_DIR and all."""
    try:
        user = getpass.getuser() or "user"
    except Exception:  # noqa: BLE001
        user = "user"
    tag = re.sub(r"[^A-Za-z0-9_-]", "_", user)
    elsewhere = os.environ.get("MISTERY_DATA_DIR")
    if elsewhere:
        digest = hashlib.sha256(str(Path(elsewhere).resolve()).lower().encode())
        tag += "-" + digest.hexdigest()[:8]
    return tag


def stop_event_name() -> str:
    return "Local\\Mistery-sharing-stop-" + _tag()


# --- the one serving friends -------------------------------------------------


def _kernel32():
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                                    wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return kernel32


def _created_at(kernel32, handle) -> float | None:
    from ctypes import wintypes

    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        return None
    ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    return ticks / 10_000_000 - 11_644_473_600


def _image(kernel32, handle) -> Path | None:
    from ctypes import wintypes

    size = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return None
    return Path(buffer.value)


def _same_folder(exe: Path, folder: Path) -> bool:
    try:
        return exe.resolve().parent == Path(folder).resolve()
    except OSError:
        return False


def sharer_pid(install_dir: Path) -> int | None:
    """The pid of the Mistery serving friends out of install_dir, or None.

    share.lock is checked as the updater checks it: a pid Windows has since
    handed to a newer process is not it, and neither is one whose program
    lives in another folder.
    """
    if os.name != "nt":
        return None
    try:
        parts = (data_dir() / SHARE_LOCK).read_text(encoding="utf-8").strip().split(",")
        pid = int(parts[0])
        started = float(parts[1]) if len(parts) > 1 else None
    except (OSError, ValueError, IndexError):
        return None
    if pid <= 0:
        return None
    from ctypes import wintypes

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return None
        created = _created_at(kernel32, handle)
        if started is not None and created is not None and created > started + 2:
            return None
        image = _image(kernel32, handle)
        if image is None or not _same_folder(image, install_dir):
            return None
        return pid
    finally:
        kernel32.CloseHandle(handle)


def stop_sharer(install_dir: Path, timeout: float = 15.0) -> bool | None:
    """Ask the Mistery serving friends out of install_dir to leave, and wait.

    None when there is none; True once it has gone; False when it is still
    there after `timeout`, and then the folder must not be touched.
    """
    pid = sharer_pid(install_dir)
    if pid is None:
        return None
    kernel32 = _kernel32()
    process = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not process:
        return True                     # gone between the two looks
    try:
        event = kernel32.OpenEventW(_EVENT_MODIFY_STATE, False, stop_event_name())
        if not event:
            return False                # running, with no way to ask it
        try:
            kernel32.SetEvent(event)
        finally:
            kernel32.CloseHandle(event)
        return kernel32.WaitForSingleObject(process, int(timeout * 1000)) == 0
    finally:
        kernel32.CloseHandle(process)


def start_sharer(install_dir: Path) -> bool:
    """Start it again from the files just installed, as the updater does."""
    exe = Path(install_dir) / APP_EXE_NAME
    if os.name != "nt" or not exe.is_file():
        return False
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
             | subprocess.CREATE_NO_WINDOW)
    try:
        subprocess.Popen([str(exe), "--share"], cwd=str(install_dir), creationflags=flags,
                         close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


STOP_FAILED = ("Mistery is sharing your library with friends in the background, and it did "
               "not stop when asked. End Mistery.exe in Task Manager's Details tab, then "
               "try again.")


# --- the two registry entries the app writes for itself -----------------------


def _read_value(key: str, name: str) -> str | None:
    if os.name != "nt":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            value, _kind = winreg.QueryValueEx(handle, name)
    except OSError:
        return None
    return value if isinstance(value, str) else None


def _command_exe(command: str) -> Path | None:
    """The program a Run value or a shell\\open\\command line starts: quoted,
    as the app writes a path with a space in it, or not."""
    text = (command or "").strip()
    if text.startswith('"'):
        end = text.find('"', 1)
        exe = text[1:end] if end > 0 else ""
    else:
        end = text.lower().find(".exe")
        exe = text[:end + 4] if end >= 0 else text.split(" ", 1)[0]
    return Path(exe) if exe else None


def _points_into(command: str | None, install_dir: Path) -> bool:
    exe = _command_exe(command or "")
    return exe is not None and _same_folder(exe, install_dir)


def run_entry(install_dir: Path) -> bool:
    """Whether the sign-in entry that starts the sharer starts this install's."""
    return _points_into(_read_value(run_key(), RUN_VALUE), install_dir)


def link_handler(install_dir: Path) -> bool:
    """Whether mistery:// links open this install's Mistery."""
    return _points_into(_read_value(link_key() + r"\shell\open\command", ""), install_dir)


def remove_run_entry(install_dir: Path) -> bool | None:
    """Delete the sign-in entry if it starts this install's Mistery.

    None when there is none of this install's; True once it is gone; False
    when it could not be deleted.
    """
    if not run_entry(install_dir):
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key(), 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def remove_link_handler(install_dir: Path) -> bool | None:
    """Delete Software\\Classes\\mistery if it opens this install's Mistery.
    None, True or False, as remove_run_entry."""
    if not link_handler(install_dir):
        return None
    return _delete_tree(link_key())


def _delete_tree(path: str) -> bool:
    """A key and everything under it (winreg.DeleteKey takes only an empty one)."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            children = []
            while True:
                try:
                    children.append(winreg.EnumKey(key, len(children)))
                except OSError:
                    break
    except FileNotFoundError:
        return True
    except OSError:
        return False
    for child in children:
        if not _delete_tree(path + "\\" + child):
            return False
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


__all__ = ["RUN_VALUE", "STOP_FAILED", "run_key", "link_key", "stop_event_name", "sharer_pid",
           "stop_sharer", "start_sharer", "run_entry", "link_handler", "remove_run_entry",
           "remove_link_handler"]
