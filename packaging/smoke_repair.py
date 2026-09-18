"""Damage a throwaway library, let the frozen Mistery repair it, and watch it
restart itself.

    python packaging/build_app.py
    python packaging/smoke_repair.py

This is the one path that freezing really could break, in two places, and both
of them are silent until the day somebody's library is damaged:

    main._repair_and_restart imports tools.repair_db. It used to load the file
    from tools\\repair_db.py by path, and a frozen app has no tools\\ folder.

    It then starts Mistery again. It used to do that with sys.executable plus
    __file__ plus a cwd of the source folder; frozen, sys.executable is
    Mistery.exe and the rest is nonsense.

So: a real corrupt library.db, the real frozen exe, the real dialogs (answered
with Enter, which is the default button in both), and then a check that a second
Mistery.exe — the one the first started — is running from the same install.

It writes nothing outside the folder given to --root, and MISTERY_DATA_DIR keeps
the damaged library in that folder too. The only processes it closes are the one
it started and the one that one started.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_frozen import close_window, read_log, stub, visible_windows   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUNDLE = ROOT / "packaging" / "out" / "dist" / "Mistery"

WM_KEYDOWN, WM_KEYUP, VK_RETURN = 0x0100, 0x0101, 0x0D
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def press_enter(hwnd: int) -> None:
    """Enter, to the dialog and nothing else.

    Posted straight at the window rather than played into the keyboard with
    SendInput: this runs on somebody's desktop, and synthetic keystrokes go to
    whatever has focus, which may not be us. Both dialogs in this path make the
    button we want the default one, so Enter is the whole answer.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM)
    user32.PostMessageW(hwnd, WM_KEYDOWN, VK_RETURN, 0)
    user32.PostMessageW(hwnd, WM_KEYUP, VK_RETURN, 0)


def exe_of(pid: int) -> str:
    """The full path of a running process, so this can tell its own Mistery from
    the one the person may have open — both windows are called "Mistery"."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def wait_for_window(match, timeout: float, pid: int) -> tuple[int, str] | None:
    """A visible window of that process whose title `match` likes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for hwnd, title in visible_windows(pid):
            if match(title):
                return hwnd, title
        time.sleep(0.1)
    return None


def mistery_pids(exe: Path) -> list[int]:
    """Every running Mistery.exe that is *this* install, by full path."""
    listing = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Mistery.exe", "/FO", "CSV",
                              "/NH"], capture_output=True, text=True).stdout
    pids = []
    for line in listing.splitlines():
        parts = [part.strip('" ') for part in line.split('","')]
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        pid = int(parts[1])
        if Path(exe_of(pid) or "x").resolve() == exe.resolve():
            pids.append(pid)
    return pids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", default=str(DEFAULT_BUNDLE))
    parser.add_argument("--root", default="",
                        help="where to make the throwaway install "
                             "(default packaging/out/testroot-repair-<pid>)")
    parser.add_argument("--keep", action="store_true",
                        help="keep the throwaway install and its library")
    args = parser.parse_args(argv)

    bundle = Path(args.bundle).resolve()
    if not (bundle / "Mistery.exe").is_file():
        raise SystemExit(f"no {bundle / 'Mistery.exe'} — run packaging/build_app.py first")
    root = (Path(args.root).resolve() if args.root
            else bundle.parent.parent / f"testroot-repair-{os.getpid()}")
    if root.exists():
        raise SystemExit(f"{root} already exists — give --root a folder that does not")

    install = root / "Programs" / "Mistery"
    install_exe = install / "Mistery.exe"
    data = root / "data"
    shutil.copytree(bundle, install)
    for tool in ("mpv", "ffmpeg", "ffprobe"):
        stub(install / "runtime" / f"{tool}.exe")
    data.mkdir(parents=True)
    (data / "settings.json").write_text(json.dumps({"library_folders": []}),
                                        encoding="utf-8")
    # Not a database at all, which sqlite reports as SQLITE_NOTADB — one of the
    # two errors main._is_damage treats as damage worth offering to repair. A
    # half-written page would do as well and is harder to write down exactly.
    (data / "library.db").write_bytes(b"Mistery smoke test, not a database. " * 512)

    environment = dict(os.environ)
    environment["MISTERY_DATA_DIR"] = str(data)
    environment["PATH"] = ""

    problems: list[str] = []
    started = time.monotonic()
    first = subprocess.Popen([str(install_exe)], cwd=str(install), env=environment)
    print(f"started {install_exe} (pid {first.pid}) on a damaged library")

    damaged = wait_for_window(lambda title: "damaged" in title.lower(), 60, first.pid)
    if not damaged:
        problems.append("no 'library is damaged' dialog appeared")
        return _finish(problems, first, install_exe, root, args.keep)
    print(f"  {damaged[1]!r} after {time.monotonic() - started:.1f} s — pressing Repair")
    press_enter(damaged[0])

    done = wait_for_window(lambda title: title.lower().startswith("library repaired"),
                           180, first.pid)
    if not done:
        problems.append("the repair never reported a result")
        return _finish(problems, first, install_exe, root, args.keep)
    print(f"  {done[1]!r} — pressing OK, which is where it starts itself again")
    press_enter(done[0])

    # The first process leaves; the Mistery it started takes its place.
    successor = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and successor is None:
        successor = next((pid for pid in mistery_pids(install_exe) if pid != first.pid),
                         None)
        time.sleep(0.2)
    if successor is None:
        problems.append("no second Mistery.exe appeared — the restart did not work")
    else:
        print(f"  it started itself again as pid {successor}")
        window = wait_for_window(lambda title: title == "Mistery", 60, successor)
        if window:
            print(f"  the new one has a window {time.monotonic() - started:.1f} s "
                  f"after the first one started")
            close_window(window[0])
            for _ in range(150):
                if successor not in mistery_pids(install_exe):
                    break
                time.sleep(0.2)
            else:
                problems.append("the restarted Mistery did not quit when closed")
        else:
            problems.append("the restarted Mistery never showed a window")

    log = read_log(data)
    print("\nwhat the log says")
    for line in log:
        if any(word in line for word in ("repair", "damage", "moved to", "salvag",
                                         "logging started")):
            print(f"    {line}")
    starts = [line for line in log if "logging started" in line]
    if len(starts) < 2:
        problems.append(f"the log shows {len(starts)} start(s), not the two a "
                        f"repair-and-restart writes")
    if not any("moved to" in line for line in log):
        problems.append("the log does not show the damaged library being set aside")

    fresh = data / "library.db"
    if not fresh.is_file() or fresh.read_bytes()[:15] != b"SQLite format 3":
        problems.append("library.db is not a fresh SQLite file afterwards")
    else:
        print(f"\n  library.db      {fresh.stat().st_size / 1024:.0f} KB, "
              f"a real SQLite file again")
    kept = [p for p in data.glob("*.db") if p.name != "library.db"]
    if not kept:
        problems.append("the damaged library was not kept beside the new one")
    for backup in kept:
        print(f"  kept beside it  {backup.name} "
              f"({backup.stat().st_size / 1024:.0f} KB)")

    return _finish(problems, first, install_exe, root, args.keep)


def _finish(problems: list[str], first: subprocess.Popen, install_exe: Path,
            root: Path, keep: bool) -> int:
    """Close anything this test left running — and only what it started."""
    for pid in ([first.pid] if first.poll() is None else []) + mistery_pids(install_exe):
        for hwnd, _ in visible_windows(pid):
            close_window(hwnd)
    time.sleep(2)
    if first.poll() is None:
        first.kill()
    if keep:
        print(f"\nkept {root}")
    else:
        shutil.rmtree(root, ignore_errors=True)

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nthe frozen app repaired its library and started itself again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
