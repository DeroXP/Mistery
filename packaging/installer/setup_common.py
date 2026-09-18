"""Names, folders and small helpers shared by the installer and the uninstaller.

Two things here are load-bearing and must never drift:

    APP_ID      "Mistery.Player.1" — the app sets this on itself at startup
                (main.py:37). A shortcut stamped with the same string is the
                same taskbar button as the running window, and a pinned tile
                keeps working across updates. Change it and every pin breaks.

    TASK_NAME   the scheduled task the updater runs under. The uninstaller
                deletes it by this name, so the two have to agree.

The MISTERY_SETUP_* environment variables at the bottom exist so the installer
can be run end to end against a throwaway folder — a real install, a real
shortcut, a real task, a real registry key, none of them on top of anything the
person running the tests cares about. They are read once here and nowhere else.

These are still read inside MisterySetup.exe, and that is deliberate: driving
the frozen exe is the only way to find out whether the payload glued to its end
comes back out, and it has to land somewhere harmless while that is checked.
All they can do is move an install — the folder, the shortcut, the registry
key, the task name — which is what --dir and --log already do in public. What
they cannot do is change *what* gets installed: that is
MISTERY_SETUP_PAYLOAD, and payload.loose_payload_dir refuses to look at it in
a frozen exe for exactly that reason.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "Mistery"
APP_ID = "Mistery.Player.1"
PUBLISHER = "Mistery"
TASK_NAME = "MisteryUpdate"

# Windows' own per-user Add/Remove Programs list. HKCU, never HKLM: this
# installs for one user, needs no admin, and so has no business in a
# machine-wide list it would need admin to clean up again.
ARP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Mistery"

# Written at the top of the install folder. The installer reads it to tell "an
# older Mistery" from "somebody else's folder", and the uninstaller reads it to
# know what it is allowed to remove.
MARKER_NAME = "mistery-install.json"

# Files the installer puts in place beside the app.
UNINSTALLER_NAME = "Uninstall.exe"
UPDATER_NAME = "MisteryUpdate.exe"
APP_EXE_NAME = "Mistery.exe"

# Where the fetched mpv/ffmpeg go. app.config.runtime_dir() looks exactly here
# (install folder\runtime), after PATH, so somebody with their own mpv keeps it.
RUNTIME_DIR_NAME = "runtime"

# The installer's scratch space: downloads and unpacking happen in the install
# folder's own update\, never %TEMP%. %TEMP% is writable by everything on the
# machine, and an installer that verifies a hash and then runs what it unpacked
# from a world-writable folder has verified nothing.
UPDATE_DIR_NAME = "update"

CREATE_NO_WINDOW = 0x08000000


def is_frozen() -> bool:
    """True inside MisterySetup.exe, false running the same code from source.

    Two things hang off it: where the payload comes from (the end of the exe, or
    a build folder named on the command line), and whether there is an exe to
    copy in as Uninstall.exe.
    """
    return bool(getattr(sys, "frozen", False))


class InstallError(Exception):
    """Something the person running the installer needs to read and act on.

    Anything raised as an InstallError is shown in the window as it is written,
    so write it as a sentence, not as a stack trace.
    """


# --- where things go --------------------------------------------------------


def _known_folder(folder_id: str) -> Path | None:
    """A Windows known folder by GUID, e.g. the Start Menu Programs folder.

    Not %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs built by hand: that
    path is right on most machines and wrong on the ones where the profile has
    been redirected to a network share, which is exactly where a silently
    misplaced shortcut is hardest to notice.
    """
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                        ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        guid = GUID()
        if ole32.CLSIDFromString(ctypes.c_wchar_p("{" + folder_id + "}"),
                                 ctypes.byref(guid)) != 0:
            return None
        out = ctypes.c_wchar_p()
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None,
                                        ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value) if out.value else None
        finally:
            ole32.CoTaskMemFree(out)
    except OSError:
        return None


FOLDERID_Programs = "A77F5D77-2E2B-44C3-A6A2-ABA601054A51"   # Start Menu\Programs
FOLDERID_Desktop = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"
FOLDERID_LocalAppData = "F1B32785-6FBA-4FCF-9D55-7B8E7F157091"


def test_root() -> Path | None:
    """The throwaway profile a test install goes into, or None for the real one."""
    value = os.environ.get("MISTERY_SETUP_TEST_ROOT", "").strip()
    return Path(value) if value else None


def default_install_dir() -> Path:
    """%LOCALAPPDATA%\\Programs\\Mistery.

    Per-user on purpose. Program Files would need an administrator both now and
    for every update; this folder is writable by the person who installed it, so
    the hourly updater can replace files without a UAC prompt nobody would be
    there to click.
    """
    root = test_root()
    if root:
        return root / "Programs" / APP_NAME
    local = _known_folder(FOLDERID_LocalAppData) or Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return local / "Programs" / APP_NAME


def start_menu_dir() -> Path:
    root = test_root()
    if root:
        return root / "StartMenu"
    return _known_folder(FOLDERID_Programs) or Path(
        os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")
    ) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def desktop_dir() -> Path:
    root = test_root()
    if root:
        return root / "Desktop"
    return _known_folder(FOLDERID_Desktop) or Path.home() / "Desktop"


def data_dir() -> Path:
    """%APPDATA%\\Mistery — the library, the settings, the artwork cache.

    Never created here, and never written to. The installer only asks how big
    it is; the uninstaller only offers to delete it. On a machine where Mistery
    has never run it does not exist, and it must stay that way until the app
    itself makes it.
    """
    root = test_root()
    if root:
        return root / "Roaming" / APP_NAME
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME


def arp_key() -> str:
    """The Add/Remove Programs key, moved out of the way for test runs."""
    if test_root():
        return r"Software\Mistery\SetupTest\Uninstall\Mistery"
    return ARP_KEY


def task_name() -> str:
    """The update task's name. Tests pass MisteryUpdateTest-<pid> and delete it."""
    return os.environ.get("MISTERY_SETUP_TASK_NAME", "").strip() or TASK_NAME


def archive_cache_dirs() -> list[Path]:
    """Where an already-downloaded mpv/ffmpeg archive may be sitting.

    Beside MisterySetup.exe first, because that is the one place a person can
    put a file without being told how: download the two archives by hand on a
    machine that is allowed to reach GitHub, drop them next to the installer,
    carry both to the machine that is not. The hash is checked either way, so a
    local copy is no more trusted than a downloaded one.
    """
    dirs: list[Path] = []
    cache = os.environ.get("MISTERY_SETUP_ARCHIVE_CACHE", "").strip()
    if cache:
        dirs.append(Path(cache))
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    return dirs


# --- odds and ends ----------------------------------------------------------


def human_size(num_bytes: float) -> str:
    """Sizes the way the rest of the app writes them: 1.2 GB, 340 MB, 12 KB."""
    for unit, step in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if num_bytes >= step:
            return f"{num_bytes / step:.1f} {unit}" if num_bytes < 100 * step \
                else f"{num_bytes / step:.0f} {unit}"
    return f"{int(num_bytes)} B"


def folder_size(folder: Path) -> int:
    """Total bytes under a folder; 0 if it is not there."""
    total = 0
    if not folder.is_dir():
        return 0
    for path in folder.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            pass                    # a file that vanished mid-walk is not an error
    return total


def free_space(folder: Path) -> int | None:
    """Free bytes on the drive a folder is (or would be) on."""
    probe = folder
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return __import__("shutil").disk_usage(str(probe)).free
    except OSError:
        return None


def app_is_running() -> bool:
    """True if a Mistery is up right now, by the two cheap signs of it.

    The mutex is how the app itself settles "am I the only one" (main.py
    _claim_instance), and app.lock in the data folder is what it leaves behind
    while it runs. Neither is read through app.config: importing that builds a
    Settings object, which writes settings.json, which would leave a data folder
    on a machine where Mistery has never started.

    Mistery.exe holding files open is the real reason to care — a copy over a
    running exe fails — so this errs towards saying yes.
    """
    if os.name != "nt":
        return False

    import getpass
    import re
    import time

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    kernel32.OpenMutexW.argtypes = (ctypes.c_ulong, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    SYNCHRONIZE = 0x00100000
    try:
        user = re.sub(r"[^A-Za-z0-9_-]", "_", getpass.getuser() or "user")
    except Exception:
        user = "user"
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, "Local\\Mistery-" + user)
    if handle:
        kernel32.CloseHandle(handle)
        return True

    lock = data_dir() / "app.lock"           # read only; never created here
    try:
        parts = lock.read_text(encoding="utf-8").strip().split(",")
        pid = int(parts[0])
        started = float(parts[1]) if len(parts) > 1 else None
    except (OSError, ValueError, IndexError):
        return False

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        from ctypes import wintypes
        created, exited, kern, user_t = (wintypes.FILETIME() for _ in range(4))
        if started is not None and kernel32.GetProcessTimes(
                ctypes.c_void_p(handle), ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kern), ctypes.byref(user_t)):
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            when = ticks / 10_000_000 - 11_644_473_600
            if when > started + 2:
                return False        # Windows handed that pid to something else
        _ = time
        return True
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def exe_in_use(path: Path) -> bool:
    """True if this exact file is a program that is running right now.

    Windows refuses to hand out a write handle to a running image, and it says
    which kind of no it means: ERROR_SHARING_VIOLATION (32) for "something has
    this open", ERROR_ACCESS_DENIED (5) for "you may not write here at all"
    (checked against a running python.exe and against System32	ar.exe, which
    gives 5 and is not running). Only 32 counts.

    This is asked about one file rather than about Mistery in general on
    purpose. app_is_running() answers "is there a Mistery open anywhere",
    which is the wrong question for an installer: installing a second copy into
    another folder, or removing a test install while the real one is open, are
    both fine. What is not fine is replacing the exe a process is executing.
    """
    if os.name != "nt" or not path.is_file():
        return False
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    GENERIC_READ_WRITE = 0xC0000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    ERROR_SHARING_VIOLATION = 32
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    handle = kernel32.CreateFileW(str(path), GENERIC_READ_WRITE, 0, None,
                                  OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if handle in (INVALID_HANDLE, None, 0):
        return ctypes.get_last_error() == ERROR_SHARING_VIOLATION
    kernel32.CloseHandle(handle)
    return False


def run_quietly(command: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a console tool without flashing a console window at anyone."""
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)


class Cancelled(Exception):
    """The person pressed Cancel.

    Not an error: it is raised out of whatever step was running so the caller
    can stop, and the installer tidies up after itself rather than showing a
    stack trace to somebody who asked it politely to stop.
    """
