r"""Is Mistery running, and how long has it been since it was?

Everything in this file exists to answer one question — may I move files out
from under this app right now — and the answer has to be "no" whenever there is
any doubt. Replacing Mistery.exe while it is open fails outright (Windows holds
a running image open), and replacing _internal\PySide6\*.dll under a running
Mistery is worse: it half works, and the app dies the next time it loads a page
whose Qt plugin is no longer the one it started with.

So there are three independent ways to say "it is running", and all three have
to say no:

  app.lock       %APPDATA%\Mistery\app.lock, written as "pid,started" while the
                 app is up (main.py _write_lock) and deleted on a clean quit.
                 A pid on its own is not enough: Windows reuses pids, and a
                 Mistery ended in Task Manager leaves the file behind. So the
                 process's creation time is compared with the time in the file.
                 This is app/config.py app_is_running() line for line, copied
                 rather than imported — importing app.config builds Settings(),
                 which writes settings.json, and the updater must never be the
                 thing that creates a data folder.

  the mutex      Local\Mistery-<user>, taken with CreateMutexW before anything
                 else at startup (main.py _claim_instance) and released by
                 Windows however the process ends, crash included. It is the
                 most reliable of the three and the cheapest. Local\ means
                 per-logon-session, which is why the scheduled task has to run
                 interactively — a task running "whether the user is logged on
                 or not" lives in session 0 and would never see this name.

  the image      any running process called Mistery.exe, from anywhere. Broader
                 than the two above on purpose: a Mistery started from a source
                 checkout, a second copy installed elsewhere, or one whose data
                 folder is somewhere we did not look still holds files open.

Then the quiet period. The app writes last-run.json as it quits, and an update
only goes in once that is half an hour old. Half an hour is not about file
locks — the three checks above cover those — it is about the person: someone
who closed Mistery to restart it, or who is still sitting in front of a paused
film, should not have the app rebuilt underneath them. A crash never writes
last-run.json, so a missing or unreadable file means wait another hour, never
"safe to go".
"""

from __future__ import annotations

import ctypes
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import paths

# The app writes last-run.json as it quits; wait this long after it before
# touching anything.
QUIET_SECONDS = 30 * 60

# Clocks move. A quit time in the future by less than this is treated as "just
# now" (daylight saving, an NTP correction); anything further ahead is treated
# as unreadable, which means waiting rather than updating on a bad clock.
FUTURE_TOLERANCE = 24 * 3600

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_STILL_ACTIVE = 259
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MAX_PATH = 260


def _kernel32():
    """kernel32 with the signatures spelled out.

    restype has to be set for everything that returns a HANDLE: ctypes defaults
    to C int, which truncates a 64-bit handle to 32 bits, and the CloseHandle
    that follows then closes something else or nothing at all.
    """
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.OpenProcess.restype = ctypes.c_void_p
    dll.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong)
    dll.OpenMutexW.restype = ctypes.c_void_p
    dll.OpenMutexW.argtypes = (ctypes.c_ulong, ctypes.c_bool, ctypes.c_wchar_p)
    dll.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    dll.CreateToolhelp32Snapshot.argtypes = (ctypes.c_ulong, ctypes.c_ulong)
    dll.CloseHandle.argtypes = (ctypes.c_void_p,)
    dll.GetProcessTimes.argtypes = (ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    dll.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    dll.Process32FirstW.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    dll.Process32NextW.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    return dll


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_wchar * _MAX_PATH),
    ]


def instance_name() -> str:
    """Local\\Mistery-<user> — the same string main.py _instance_channel builds.

    MISTERY_TEST_MUTEX_NAME replaces it, and only when MISTERY_INSTALL_DIR is
    set as well — that is, only for an updater that has already been pointed at
    a throwaway install folder. The tests need it because the mutex name is per
    Windows user, not per install: on the machine this was written on the real
    Mistery is open right now, and without a seam here every test of the happy
    path would be a test of "Mistery is running".
    """
    override = os.environ.get("MISTERY_TEST_MUTEX_NAME", "").strip()
    if override and os.environ.get("MISTERY_INSTALL_DIR", "").strip():
        return override
    return "Local\\Mistery-" + user_tag()


def user_tag() -> str:
    """The Windows user name with everything awkward taken out, the same way
    main.py _instance_channel does it: a kernel object name cannot contain a
    backslash, and a domain account is DOMAIN\\person."""
    try:
        user = getpass.getuser() or "user"
    except Exception:
        user = "user"
    return re.sub(r"[^A-Za-z0-9_-]", "_", user)


# --- the three checks -------------------------------------------------------


def _lock_says_running() -> str | None:
    """app.lock, checked the way app/config.py app_is_running() checks it."""
    data = paths.data_dir()
    if data is None:
        return None                     # no data folder: Mistery has never run
    lock = data / "app.lock"
    try:
        parts = lock.read_text(encoding="utf-8").strip().split(",")
        pid = int(parts[0])
        started = float(parts[1]) if len(parts) > 1 else None
    except (OSError, ValueError, IndexError):
        return None                     # no lock, or one we cannot read
    if pid <= 0 or os.name != "nt":
        return None

    dll = _kernel32()
    handle = dll.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None                     # nothing with that pid; a stale lock
    try:
        code = ctypes.c_ulong()
        if not dll.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
            return f"app.lock names pid {pid} and it could not be asked about"
        if code.value != _STILL_ACTIVE:
            return None                 # it has exited; the lock is left over
        if started is not None:
            created = _created_at(dll, handle)
            if created is not None and created > started + 2:
                # The pid has been handed to something else since Mistery wrote
                # the lock. Without this, a Mistery ended in Task Manager could
                # block every update for as long as whatever inherited its pid
                # kept running.
                return None
        return f"app.lock: Mistery is running as pid {pid}"
    finally:
        dll.CloseHandle(ctypes.c_void_p(handle))


def _created_at(dll, handle) -> float | None:
    """When a process started, on the time.time() clock; None if unknown."""
    created, exited, kernel, user = (_FILETIME() for _ in range(4))
    if not dll.GetProcessTimes(ctypes.c_void_p(handle), ctypes.byref(created),
                               ctypes.byref(exited), ctypes.byref(kernel),
                               ctypes.byref(user)):
        return None
    ticks = (created.high << 32) | created.low          # 100 ns units since 1601
    return ticks / 10_000_000 - 11_644_473_600


def _mutex_says_running() -> str | None:
    """The single-instance mutex Mistery holds for as long as it is up."""
    if os.name != "nt":
        return None
    dll = _kernel32()
    name = instance_name()
    handle = dll.OpenMutexW(_SYNCHRONIZE, False, name)
    if not handle:
        return None
    dll.CloseHandle(ctypes.c_void_p(handle))
    return f"{name} is held: Mistery is running"


def _image_says_running() -> str | None:
    """Any process called Mistery.exe, wherever it was started from, except
    the one serving friends with no window (sharer_pid): that one steps aside
    when asked (stop_sharer), and blocking every update on it would block
    them for as long as the PC is on."""
    if os.name != "nt":
        return None
    serving = sharer_pid()
    dll = _kernel32()
    snapshot = dll.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        # Cannot see the process list — say nothing rather than guess. The
        # other two checks still apply, and they are the reliable ones.
        return None
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        wanted = paths.APP_EXE_NAME.lower()
        ok = dll.Process32FirstW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == wanted and entry.th32ProcessID != serving:
                return (f"{entry.szExeFile} is running as pid "
                        f"{entry.th32ProcessID}")
            ok = dll.Process32NextW(ctypes.c_void_p(snapshot), ctypes.byref(entry))
        return None
    finally:
        dll.CloseHandle(ctypes.c_void_p(snapshot))


# --- the Mistery serving friends, with no window ------------------------------
#
# Mistery.exe --share (the app's app/share/background.py) serves a person's
# library to their friends while the app itself is closed, from sign-in on. It
# is Mistery.exe, so the image check above would see it, and it never quits by
# itself. Three things let the updater live with it: it writes share.lock (its
# pid and start time, the way the app writes app.lock) so it can be told apart
# from the app; it leaves when its stop event is set; and it is started again
# once the new files are in. Its names are built exactly as that module builds
# them, MISTERY_DATA_DIR and all, so a test's throwaway data folder gets names
# of its own.

SHARE_LOCK = "share.lock"


def _sharing_tag() -> str:
    tag = user_tag()
    elsewhere = os.environ.get("MISTERY_DATA_DIR")
    if elsewhere:
        digest = hashlib.sha256(str(Path(elsewhere).resolve()).lower().encode())
        tag += "-" + digest.hexdigest()[:8]
    return tag


def sharer_stop_event() -> str:
    return "Local\\Mistery-sharing-stop-" + _sharing_tag()


def sharer_pid() -> int | None:
    """The pid of the Mistery serving friends, if share.lock names a live one."""
    return _live_pid(SHARE_LOCK)


# A friend's movie night on this PC's film (the app's app/share/nights.py),
# usually held by the Mistery with no window: night.lock names the process
# holding it for as long as it is on. Replacing the files under it would cut the
# friends off mid-film, so the updater waits for it as it waits for an open
# Mistery, and tries again on a later run.
NIGHT_LOCK = "night.lock"


def _night_says_running() -> str | None:
    pid = _live_pid(NIGHT_LOCK)
    return f"night.lock: friends are watching a film from this PC (pid {pid})" if pid else None


def _live_pid(name: str) -> int | None:
    """The pid a lock file in the data folder names, if it is still that process.

    Checked like app.lock: a pid Windows has since handed to another process
    (one created after the lock was written) is not it.
    """
    data = paths.data_dir()
    if data is None or os.name != "nt":
        return None
    try:
        parts = (data / name).read_text(encoding="utf-8").strip().split(",")
        pid = int(parts[0])
        started = float(parts[1]) if len(parts) > 1 else None
    except (OSError, ValueError, IndexError):
        return None
    if pid <= 0:
        return None
    dll = _kernel32()
    handle = dll.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = ctypes.c_ulong()
        if (not dll.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
                or code.value != _STILL_ACTIVE):
            return None
        created = _created_at(dll, handle)
        if started is not None and created is not None and created > started + 2:
            return None
        return pid
    finally:
        dll.CloseHandle(ctypes.c_void_p(handle))


def stop_sharer(timeout: float = 15.0) -> bool | None:
    """Ask the Mistery serving friends to leave, and wait for it to.

    None when there is none; True once it has gone; False when it is still
    there after `timeout` (the caller must then not touch the files).
    """
    pid = sharer_pid()
    if pid is None:
        return None
    dll = _kernel32()
    dll.OpenEventW.restype = ctypes.c_void_p
    dll.OpenEventW.argtypes = (ctypes.c_ulong, ctypes.c_bool, ctypes.c_wchar_p)
    dll.SetEvent.argtypes = (ctypes.c_void_p,)
    dll.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    dll.WaitForSingleObject.restype = ctypes.c_ulong
    process = dll.OpenProcess(_SYNCHRONIZE, False, pid)
    if not process:
        return True                     # gone between the two looks
    try:
        event = dll.OpenEventW(0x0002, False, sharer_stop_event())     # EVENT_MODIFY_STATE
        if not event:
            return False                # running, but with no way to ask it
        try:
            dll.SetEvent(ctypes.c_void_p(event))
        finally:
            dll.CloseHandle(ctypes.c_void_p(event))
        return dll.WaitForSingleObject(ctypes.c_void_p(process), int(timeout * 1000)) == 0
    finally:
        dll.CloseHandle(ctypes.c_void_p(process))


def start_sharer(install: Path) -> bool:
    """Start the Mistery serving friends again, from the files just put in."""
    exe = Path(install) / paths.APP_EXE_NAME
    if os.name != "nt" or not exe.is_file():
        return False
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
             | subprocess.CREATE_NO_WINDOW)
    try:
        subprocess.Popen([str(exe), "--share"], cwd=str(install), creationflags=flags,
                         close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


def running_reason() -> str | None:
    """One line saying Mistery is running, or None if all four checks say no.

    Order is cheapest first: the mutex is one system call, the locks are small
    reads, the process list is a snapshot of every process on the machine (5 ms
    here, measured on a machine with 320 of them).
    """
    if sys.platform != "win32":
        return None                     # nothing here works anywhere else
    for check in (_mutex_says_running, _lock_says_running, _night_says_running,
                  _image_says_running):
        try:
            reason = check()
        except Exception as exc:        # a check that breaks must not update
            return f"{check.__name__} could not answer ({exc!r}); assuming it is running"
        if reason:
            return reason
    return None


# --- how long has it been quiet ---------------------------------------------


def last_quit() -> tuple[float | None, str]:
    """(when Mistery last quit, one line about where that came from).

    None means we cannot tell, which the caller must treat as "not yet".
    """
    data = paths.data_dir()
    if data is None:
        # No %APPDATA%\Mistery at all: Mistery has never finished starting on
        # this PC, so there is no session to interrupt. This is the one case
        # where a missing last-run.json is not a reason to wait — usually a
        # machine where the installer has just run and nobody has opened the
        # app yet.
        return 0.0, "no data folder: Mistery has never run here"

    record = data / "last-run.json"
    try:
        loaded = json.loads(record.read_text(encoding="utf-8"))
        quit_at = float(loaded["quit"])
    except FileNotFoundError:
        return None, ("last-run.json is not there — Mistery has not quit cleanly "
                      "since it was installed, or it crashed. Waiting.")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return None, f"last-run.json cannot be read ({exc!r}). Waiting."

    now = time.time()
    if quit_at > now + FUTURE_TOLERANCE:
        return None, (f"last-run.json says Mistery quit at {quit_at:.0f}, which is "
                      f"in the future. The clock has moved; waiting.")
    return min(quit_at, now), "last-run.json"


def quiet_reason() -> str | None:
    """None when it is safe to go, or one line saying why it is not.

    Two ways to be told no: something says Mistery is up, or it has not been
    down for long enough. Both are ordinary answers, not errors — on a machine
    someone uses in the evening, most hourly runs end here.
    """
    running = running_reason()
    if running:
        return running

    quit_at, where = last_quit()
    if quit_at is None:
        return where
    idle = time.time() - quit_at
    if idle < QUIET_SECONDS:
        return (f"Mistery quit {idle / 60:.0f} minutes ago "
                f"({where}); waiting for {QUIET_SECONDS // 60}")
    return None


def describe_idle() -> str:
    """For the log line of a run that is allowed to proceed."""
    quit_at, where = last_quit()
    if not quit_at:
        return "Mistery is not running and has never run here"
    return (f"Mistery is not running and quit "
            f"{(time.time() - quit_at) / 60:.0f} minutes ago ({where})")
