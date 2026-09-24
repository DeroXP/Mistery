r"""The updater and the Mistery that serves friends with no window.

    python packaging\test_updater_sharing.py

Mistery.exe --share (app/share/background.py) runs from sign-in and never quits
by itself, and the updater will not replace files under a running Mistery.exe.
Without the three things this proves, that one process would stop every
update, for good:

  - it is told apart from the app by share.lock, so it alone does not count as
    "Mistery is running" (liveness._image_says_running);
  - asked to step aside (its stop event), it leaves, and the updater waits for
    it (liveness.stop_sharer);
  - it is started again afterwards (liveness.start_sharer).

The "friends server" here is the app's real code (background.claim, from the
app folder beside this repository's) in a child process with a throwaway data
folder, and the "Mistery.exe" the process list shows is a copy of Windows'
own ping.exe under that name, in a throwaway folder. Nothing touches the
real data folder, the real install, or a real Mistery.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
APP = Path(os.environ.get("MISTERY_APP_SOURCE", ROOT))       # where app/share/background.py is
DATA = Path(tempfile.mkdtemp(prefix="mistery-updater-sharing-"))
os.environ["MISTERY_DATA_DIR"] = str(DATA)
sys.path.insert(0, str(ROOT))

from updater import liveness, paths  # noqa: E402

results: list[bool] = []

CHILD = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
from app.share import background
claim = background.claim()
if claim is None:
    print("taken", flush=True)
    raise SystemExit(3)
print("serving", os.getpid(), flush=True)
while not claim.stop_requested(0.2):
    pass
claim.release()
print("left", flush=True)
"""


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append(bool(ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  - {detail}" if detail else ""), flush=True)
    return bool(ok)


def friends_server() -> subprocess.Popen:
    child = subprocess.Popen([sys.executable, "-c", CHILD, str(APP)], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             env=dict(os.environ, MISTERY_NO_AUDIO="1"))
    first = child.stdout.readline().split()
    if first[:1] != ["serving"]:
        raise SystemExit(f"the stand-in friends server did not start: {first!r} "
                         f"{child.stdout.read()[:500]}")
    # The pid that holds the claim, which is not always child.pid: a virtual
    # environment's python.exe on Windows is a launcher that runs the real one
    # as its own child (the build environment's is, and share.lock named that).
    child.serving_pid = int(first[1])
    return child


def fake_mistery(folder: Path) -> subprocess.Popen:
    """A process the list shows as Mistery.exe: Windows' ping.exe, renamed,
    pinging this PC for a while (timeout.exe refuses to run without a console)."""
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / paths.APP_EXE_NAME
    shutil.copy(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "PING.EXE", exe)
    return subprocess.Popen([str(exe), "-n", "60", "127.0.0.1"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)


def main() -> int:
    if not (APP / "app" / "share" / "background.py").is_file():
        raise SystemExit(f"no app/share/background.py under {APP}: set MISTERY_APP_SOURCE")
    before = liveness._image_says_running()
    if before:
        raise SystemExit(f"a Mistery.exe is already running ({before}): this test needs none")

    print("\nnothing serving friends")
    check(liveness.sharer_pid() is None, "no share.lock: nobody is serving")
    check(liveness.stop_sharer(1.0) is None, "and there is nobody to ask to step aside")

    print("\nthe friends server running")
    child = friends_server()
    try:
        check(liveness.sharer_pid() == child.serving_pid, "share.lock names it, and it is alive",
              f"{liveness.sharer_pid()} vs {child.serving_pid}")
        fake = fake_mistery(DATA / "install")
        try:
            time.sleep(0.3)
            check(liveness._image_says_running() is not None,
                  "CONTROL: a Mistery.exe that is not the friends server still counts as running",
                  str(liveness._image_says_running()))
            (DATA / liveness.SHARE_LOCK).write_text(f"{fake.pid},{time.time()}", encoding="utf-8")
            check(liveness._image_says_running() is None,
                  "the one share.lock names does not count: updates are not blocked by it")
            (DATA / liveness.SHARE_LOCK).write_text(f"{fake.pid},{time.time() - 3600}", encoding="utf-8")
            check(liveness._image_says_running() is not None,
                  "but a share.lock older than the process it names (a reused pid) is ignored")
        finally:
            fake.kill()
            fake.wait(5)
        (DATA / liveness.SHARE_LOCK).write_text(f"{child.serving_pid},{time.time()}", encoding="utf-8")

        started = time.monotonic()
        stopped = liveness.stop_sharer(10.0)
        check(stopped is True, "asked to step aside, it leaves, and the updater waits for it",
              f"{stopped} after {time.monotonic() - started:.2f} s")
        child.wait(5)
        check(child.returncode == 0 and "left" in child.stdout.read(),
              "it left the way it should: its own loop saw the request")
        check(liveness.sharer_pid() is None, "and share.lock no longer names anyone")
    finally:
        if child.poll() is None:
            child.kill()

    print("\na friend's movie night on this PC (night.lock, the app's app/share/nights.py)")
    lock = DATA / liveness.NIGHT_LOCK
    check(liveness._night_says_running() is None, "no night.lock: nothing to wait for")
    lock.write_text(f"{os.getpid()},{time.time()}", encoding="utf-8")
    said = liveness._night_says_running()
    check(said is not None and "night.lock" in said, "one naming a live process: friends are watching", str(said))
    others = (liveness._mutex_says_running, liveness._lock_says_running, liveness._image_says_running)
    liveness._mutex_says_running = liveness._lock_says_running = liveness._image_says_running = lambda: None
    try:
        reason = liveness.running_reason()
    finally:
        liveness._mutex_says_running, liveness._lock_says_running, liveness._image_says_running = others
    check(reason == said, "and the update waits for it, as for an open Mistery (every other check quiet)",
          str(reason))
    lock.write_text(f"{os.getpid()},{time.time() - 7 * 86400}", encoding="utf-8")
    check(liveness._night_says_running() is None,
          "but not for one older than the process it names (a pid handed on)")
    lock.write_text("999999,0", encoding="utf-8")
    check(liveness._night_says_running() is None, "nor for one whose process has gone")
    lock.unlink()

    print("\nstarting it again")
    install = DATA / "install2"
    install.mkdir()
    shutil.copy(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "PING.EXE",
                install / paths.APP_EXE_NAME)
    check(liveness.start_sharer(install), "the updater starts Mistery.exe --share from the install")
    time.sleep(0.5)
    check(liveness.start_sharer(DATA / "nowhere") is False,
          "CONTROL: with no Mistery.exe there, it says it could not")

    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    shutil.rmtree(DATA, ignore_errors=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
