"""Start the frozen Mistery for real and watch what it does.

    python packaging/build_app.py
    python packaging/smoke_frozen.py

packaging/test_frozen_paths.py fakes sys.frozen and checks the path logic in
process. This does not fake anything: it copies the built folder somewhere else
(which is all the installer does), starts Mistery.exe, waits for its window,
reads its log and closes it again. That is the only way to find the failures
that only exist in a frozen build — a missing hidden import, a Qt plugin that
did not come along, an asset the code can no longer find.

Two runs, because the tool lookup has two halves that matter:

    1. PATH emptied, stub mpv.exe in runtime\\ beside the exe. This is the PC the
       installer just finished on, and the log must show it picked runtime\\mpv.
    2. PATH pointing at a folder with its own mpv.exe. Somebody who already has
       mpv keeps theirs, and the log must show that one.

Nothing here touches the real install or the real library:

  - the app is copied into a folder under packaging\\out\\testroot-<pid>, removed
    at the end unless --keep;
  - MISTERY_DATA_DIR points at a throwaway data folder inside it, with a
    settings.json that lists no library folders, so it scans nothing;
  - the only process it ever closes is the one it started.
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

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUNDLE = ROOT / "packaging" / "out" / "dist" / "Mistery"

WM_CLOSE = 0x0010


# --- looking at windows the way Windows does ---------------------------------

def _user32():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD))
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM)
    return user32


def visible_windows(pid: int) -> list[tuple[int, str]]:
    """Every visible top-level window belonging to a process, with its title.

    Titles come back too because a startup failure also puts a window on screen
    — the message box saying Mistery could not start — and "a window appeared"
    would otherwise read as success.
    """
    user32 = _user32()
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            found.append((hwnd, buffer.value))
        return True

    user32.EnumWindows(each, 0)
    return found


def close_window(hwnd: int) -> None:
    """Ask the window to close, the way its X button does.

    Not TerminateProcess: Mistery saves the playing position and the music
    session on the way out, and a test that never exercises the quit path would
    miss a quit that crashes.
    """
    _user32().PostMessageW(hwnd, WM_CLOSE, 0, 0)


# --- the run ------------------------------------------------------------------

def stub(path: Path) -> Path:
    """An empty file standing in for an executable. Nothing here ever runs mpv;
    the app only asks whether the file is there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def read_log(data_dir: Path) -> list[str]:
    log = data_dir / "mistery.log"
    if not log.is_file():
        return []
    return log.read_text(encoding="utf-8", errors="replace").splitlines()


def run_once(command: list[str], data_dir: Path, path_value: str, label: str,
             cwd: Path | None = None, timeout: float = 90.0) -> dict:
    """Start Mistery, wait for its window, close it. Returns what was measured.

    A command rather than an exe, so the same run can be pointed at
    `python main.py` — the frozen app and the source app have to behave the
    same, and the only way to know is to run both.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    # No library folders, so the first start scans nothing at all. Written
    # rather than left to the app so this can never point at a real library.
    (data_dir / "settings.json").write_text(
        json.dumps({"library_folders": []}), encoding="utf-8")
    for leftover in ("mistery.log", "app.lock", "last-run.json"):
        (data_dir / leftover).unlink(missing_ok=True)

    environment = dict(os.environ)
    environment["MISTERY_DATA_DIR"] = str(data_dir)
    environment["PATH"] = path_value
    # A frozen app must not be reading the build machine's Python out of the
    # environment; on a stranger's PC there is none to read.
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        environment.pop(name, None)

    print(f"\n{label}")
    print(f"  PATH = {path_value!r}")
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=str(cwd) if cwd else None, env=environment)
    result: dict = {"label": label, "pid": process.pid}
    window = None
    while time.monotonic() - started < timeout:
        if process.poll() is not None:
            result["failed"] = (f"it exited with code {process.returncode} after "
                                f"{time.monotonic() - started:.1f} s, with no window")
            result["log"] = read_log(data_dir)
            return result
        windows = visible_windows(process.pid)
        if windows:
            window = windows[0]
            break
        time.sleep(0.05)

    if window is None:
        result["failed"] = f"no window after {timeout:.0f} s"
        process.kill()
        result["log"] = read_log(data_dir)
        return result

    result["seconds"] = time.monotonic() - started
    result["title"] = window[1]
    print(f"  window {window[1]!r} after {result['seconds']:.2f} s")

    close_window(window[0])
    closing = time.monotonic()
    while process.poll() is None and time.monotonic() - closing < 30:
        time.sleep(0.05)
    if process.poll() is None:
        # We started it, so we may end it; say so rather than quietly killing.
        print("  it did not quit within 30 s of the close — ending it")
        process.kill()
        result["quit_clean"] = False
    else:
        result["quit_clean"] = True
        result["quit_seconds"] = time.monotonic() - closing
        print(f"  quit in {result['quit_seconds']:.2f} s (code {process.returncode})")
    result["log"] = read_log(data_dir)
    return result


def line_with(log: list[str], needle: str) -> str | None:
    for line in log:
        if needle in line:
            return line
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", default=str(DEFAULT_BUNDLE),
                        help="the built folder (default packaging/out/dist/Mistery)")
    parser.add_argument("--root", default="",
                        help="where to make the throwaway install "
                             "(default packaging/out/testroot-<pid>)")
    parser.add_argument("--keep", action="store_true",
                        help="keep the throwaway install and data folder")
    args = parser.parse_args(argv)

    bundle = Path(args.bundle).resolve()
    exe = bundle / "Mistery.exe"
    if not exe.is_file():
        raise SystemExit(f"no {exe} — run packaging/build_app.py first")

    if os.name == "nt":
        running = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Mistery.exe", "/NH"],
            capture_output=True, text=True).stdout
        if "Mistery.exe" in running:
            # Not a reason to stop: the one-instance name is per library, and
            # this test runs against a throwaway one (main._instance_channel).
            # Worth saying, because two Mistery windows will be on screen.
            print("note: a Mistery.exe is already running. It is left alone — "
                  "this test uses a library of its own, so they do not collide.")

    # A folder of its own per run: two of these side by side must not share an
    # install, and a run that is interrupted leaves something obviously disposable.
    root = Path(args.root).resolve() if args.root else \
        bundle.parent.parent / f"testroot-{os.getpid()}"
    if root.exists():
        raise SystemExit(f"{root} already exists — give --root a folder that does not")
    install = root / "Programs" / "Mistery"
    print(f"copying the build into {install}")
    copy_started = time.monotonic()
    shutil.copytree(bundle, install)
    copy_seconds = time.monotonic() - copy_started
    size = sum(p.stat().st_size for p in install.rglob("*") if p.is_file())
    print(f"  {size / 1e6:.1f} MB copied in {copy_seconds:.1f} s "
          f"({size / 1e6 / copy_seconds:.0f} MB/s)")

    for tool in ("mpv", "ffmpeg", "ffprobe"):
        stub(install / "runtime" / f"{tool}.exe")
    own_mpv = stub(root / "somebody-elses-mpv" / "mpv.exe").parent

    problems: list[str] = []
    try:
        bundled_data, own_data = root / "data-bundled", root / "data-own-mpv"
        bundled = run_once([str(install / "Mistery.exe")], data_dir=bundled_data,
                           path_value="", label="1. nothing on PATH", cwd=install)
        own = run_once([str(install / "Mistery.exe")], data_dir=own_data,
                       path_value=str(own_mpv), label="2. their own mpv on PATH",
                       cwd=install)

        print("\nwhat the log says")
        for run, expected_mpv, data_dir in (
                (bundled, install / "runtime" / "mpv.exe", bundled_data),
                (own, own_mpv / "mpv.exe", own_data)):
            if run.get("failed"):
                problems.append(f"{run['label']}: {run['failed']}")
                print(f"  FAIL  {run['label']}: {run['failed']}")
                for line in run.get("log", [])[-15:]:
                    print(f"        {line}")
                continue
            if "Mistery" not in (run.get("title") or ""):
                problems.append(f"{run['label']}: the window is {run['title']!r}")

            start_line = line_with(run["log"], "logging started")
            paths_line = line_with(run["log"], "frozen=")
            print(f"  {run['label']}")
            print(f"    {start_line}")
            print(f"    {paths_line}")
            if not paths_line:
                problems.append(f"{run['label']}: no startup path line in the log")
                continue
            if "frozen=True" not in paths_line:
                problems.append(f"{run['label']}: it did not know it was frozen")
            if str(install / "_internal" / "assets") not in paths_line:
                problems.append(f"{run['label']}: assets did not resolve into the bundle")
            if str(expected_mpv).lower() not in paths_line.lower():
                problems.append(f"{run['label']}: expected mpv {expected_mpv}")

            bad = [line for line in run["log"]
                   if " ERROR " in line or " CRITICAL " in line]
            if bad:
                problems.append(f"{run['label']}: {len(bad)} error line(s) in the log")
                for line in bad[:5]:
                    print(f"    {line}")
            if not run.get("quit_clean"):
                problems.append(f"{run['label']}: it did not quit when its window closed")
            # The updater waits for this file before it replaces anything, so a
            # frozen build that stopped writing it would stop updates dead.
            if not (data_dir / "last-run.json").is_file():
                problems.append(f"{run['label']}: it wrote no last-run.json in {data_dir}")

        print("\nmeasured")
        print(f"  bundle            {size / 1e6:.1f} MB")
        print(f"  copy              {copy_seconds:.1f} s")
        if "seconds" in bundled:
            print(f"  cold start        {bundled['seconds']:.2f} s (first run, "
                  f"empty library)")
        if "seconds" in own:
            print(f"  second start      {own['seconds']:.2f} s")
    finally:
        if args.keep:
            print(f"\nkept {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nthe frozen app starts, finds its own files and quits cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
