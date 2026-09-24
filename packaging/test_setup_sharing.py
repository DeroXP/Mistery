r"""Setup, Uninstall and the Mistery that serves friends with no window.

    set MISTERY_APP_SOURCE=<a folder with app\share\background.py>
    python packaging\test_setup_sharing.py

Mistery.exe --share (app/share/background.py) runs out of the install folder
from sign-in and never quits by itself. Before installer/sharing.py, Uninstall
refused to run while it did ("Mistery is running ... check the system tray",
with no window or tray icon anywhere), and so did Setup over the same folder.
This proves that both now ask it to step aside and wait for it, only when it
runs out of their folder; that Setup starts it again; and that Uninstall takes
away the two entries the app writes for itself (the sign-in entry and the
mistery:// link handler) when they point into the folder, and no others.

The stand-in "Mistery.exe" is this PC's python.exe copied into a throwaway
install folder, running the app's real background.claim() with a throwaway
data folder, so the process Windows sees really is <folder>\Mistery.exe. The
two registry entries are written under HKCU\Software\Mistery\SetupTest (where
MISTERY_SETUP_TEST_ROOT sends them), never into the real Run key or Classes,
which are only read, to show they were not touched.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import winreg
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = Path(os.environ.get("MISTERY_APP_SOURCE", HERE.parent))
ROOT = Path(tempfile.mkdtemp(prefix="mistery-setup-sharing-"))
DATA = ROOT / "Roaming" / "Mistery"
os.environ["MISTERY_SETUP_TEST_ROOT"] = str(ROOT)
os.environ["MISTERY_DATA_DIR"] = str(DATA)       # the sharer's names, as the app builds them
sys.path.insert(0, str(HERE))

from installer import install_steps, sharing, uninstall_steps  # noqa: E402
from installer.setup_common import InstallError, data_dir, exe_in_use  # noqa: E402

results: list[bool] = []

CHILD = r"""
import sys
sys.path.insert(0, sys.argv[1])
from app.share import background
claim = background.claim()
if claim is None:
    print("taken", flush=True)
    raise SystemExit(3)
print("serving", flush=True)
stubborn = len(sys.argv) > 2
while stubborn or not claim.stop_requested(0.2):
    if stubborn:
        import time
        time.sleep(0.2)
claim.release()
print("left", flush=True)
"""


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append(bool(ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  - {detail}" if detail else ""), flush=True)
    return bool(ok)


def fake_install(folder: Path) -> Path:
    """An install folder whose Mistery.exe is python.exe: enough to run as one."""
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    base = Path(sys.base_prefix)
    shutil.copy(base / "python.exe", folder / "Mistery.exe")
    for dll in glob.glob(str(base / "python*.dll")) + glob.glob(str(base / "vcruntime*.dll")):
        shutil.copy(dll, folder)
    (folder / "mistery-install.json").write_text('{"app": "Mistery", "version": "1.0.0"}',
                                                 encoding="utf-8")
    return folder


def sharer(folder: Path, stubborn: bool = False) -> subprocess.Popen:
    """<folder>\\Mistery.exe serving friends, as far as anyone can tell."""
    arguments = [str(folder / "Mistery.exe"), "-c", CHILD, str(APP)] + (["stubborn"] if stubborn else [])
    child = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             env=dict(os.environ, PYTHONHOME=sys.base_prefix, MISTERY_NO_AUDIO="1"),
                             creationflags=subprocess.CREATE_NO_WINDOW)
    first = child.stdout.readline().strip()
    if first != "serving":
        raise SystemExit(f"the stand-in did not start serving: {first!r} {child.stdout.read()[:600]}")
    return child


def set_entries(folder: Path) -> None:
    """The two entries, as the app writes them, but under the test key."""
    exe = folder / "Mistery.exe"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, sharing.run_key()) as key:
        winreg.SetValueEx(key, sharing.RUN_VALUE, 0, winreg.REG_SZ,
                          subprocess.list2cmdline([str(exe), "--share"]))
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, sharing.link_key()) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:Mistery")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, sharing.link_key() + r"\DefaultIcon") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f"{exe},0")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, sharing.link_key() + r"\shell\open\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, f'"{exe}" "%1"')


def read(key: str, name: str) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            return winreg.QueryValueEx(handle, name)[0]
    except OSError:
        return None


def key_exists(key: str) -> bool:
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, key))
        return True
    except OSError:
        return False


def the_real_ones() -> tuple:
    return (read(sharing.RUN_KEY, sharing.RUN_VALUE),
            read(sharing.LINK_KEY + r"\shell\open\command", ""))


def main() -> int:
    if not (APP / "app" / "share" / "background.py").is_file():
        raise SystemExit(f"no app/share/background.py under {APP}: set MISTERY_APP_SOURCE")
    DATA.mkdir(parents=True)
    real_before = the_real_ones()
    check(data_dir() == DATA and sharing.run_key().startswith(r"Software\Mistery\SetupTest")
          and sharing.link_key().startswith(r"Software\Mistery\SetupTest"),
          "the test's data folder and registry entries are its own")
    children: list[subprocess.Popen] = []
    try:
        print("\nuninstalling while it serves friends from the install folder")
        install = fake_install(ROOT / "Programs" / "Mistery")
        child = sharer(install)
        children.append(child)
        set_entries(install)
        check(sharing.sharer_pid(install) == child.pid, "share.lock names it, running out of the folder",
              f"{sharing.sharer_pid(install)} vs {child.pid}")
        check(sharing.sharer_pid(ROOT / "elsewhere") is None,
              "CONTROL: asked about another folder, it is not that folder's")
        check(exe_in_use(install / "Mistery.exe"),
              "its Mistery.exe is in use: what used to stop the uninstall")
        plan = uninstall_steps.read_plan(install)
        check(plan.sharing_entry and plan.link_handler,
              "the plan finds the sign-in entry and the link handler, both this install's")
        steps: list[str] = []
        removed = uninstall_steps.run(plan, keep_data=True, report=lambda what, _f: steps.append(what))
        child.wait(10)
        check(child.returncode == 0 and "left" in child.stdout.read(),
              "asked to step aside, it left by its own loop, and the uninstall went on")
        check(removed.sharer_stopped and "Stopping sharing with friends" in steps,
              "the uninstall says it stopped sharing", str(steps[:3]))
        check(removed.install_dir and not install.exists(), "the install folder is gone")
        check(removed.sharing_entry and read(sharing.run_key(), sharing.RUN_VALUE) is None,
              "the sign-in entry is gone")
        check(removed.link_handler and not key_exists(sharing.link_key()),
              "the mistery:// link handler is gone, all of it")
        check(DATA.is_dir(), "the library folder is kept, as asked")
        check(not removed.left_behind, "nothing left behind", str(removed.left_behind))

        print("\nentries and a sharer that belong to another copy")
        install = fake_install(ROOT / "Programs" / "Mistery")
        other = fake_install(ROOT / "Other" / "Mistery")
        child = sharer(other)
        children.append(child)
        set_entries(other)
        plan = uninstall_steps.read_plan(install)
        check(not plan.sharing_entry and not plan.link_handler,
              "the plan claims neither entry: they start the other copy")
        removed = uninstall_steps.run(plan, keep_data=True, report=lambda *_: None)
        check(removed.install_dir and not removed.sharer_stopped and child.poll() is None,
              "this copy is removed, and the other copy's sharer is left serving")
        check(read(sharing.run_key(), sharing.RUN_VALUE) is not None and key_exists(sharing.link_key()),
              "and its two entries are left alone")
        background_ok = sharing.stop_sharer(other, timeout=10.0)
        child.wait(10)
        check(background_ok is True and child.returncode == 0, "(and asked, it leaves too)")

        print("\ninstalling over the folder it serves friends from")
        install = fake_install(ROOT / "Programs" / "Mistery")
        payload = ROOT / "payload"
        (payload / "_internal").mkdir(parents=True)
        (payload / "Mistery.exe").write_bytes(os.urandom(64_000))
        (payload / "version.txt").write_text("1.3.0\n", encoding="utf-8")
        os.environ["MISTERY_SETUP_PAYLOAD"] = str(payload)
        child = sharer(install)
        children.append(child)
        started_again: list[Path] = []
        real_start = sharing.start_sharer
        sharing.start_sharer = lambda folder: started_again.append(Path(folder)) or True
        cancel = threading.Event()
        cancel.set()                    # stop at the first file copied: the pause is what is tested
        try:
            install_steps.run(install_steps.Options(install_dir=install, start_menu_shortcut=False,
                                                    register_update_task=False),
                              lambda *_: None, cancel)
            outcome = "finished"
        except install_steps.Cancelled:
            outcome = "cancelled"
        except InstallError as error:
            outcome = f"refused: {error}"
        finally:
            sharing.start_sharer = real_start
        child.wait(10)
        check(child.returncode == 0 and "left" in child.stdout.read(),
              "it stepped aside for the install")
        check(outcome == "cancelled" and not (install / "python312.dll").exists(),
              "and the old files could be cleared (the copy was then cancelled on purpose)", outcome)
        check(started_again == [install], "and Setup starts it again afterwards, from the folder",
              str(started_again))

        print("\none that does not stop when asked")
        install = fake_install(ROOT / "Programs" / "Mistery")
        child = sharer(install, stubborn=True)
        children.append(child)
        started = time.monotonic()
        check(sharing.stop_sharer(install, timeout=1.0) is False,
              "stop_sharer says it is still there", f"{time.monotonic() - started:.1f} s")
        try:
            uninstall_steps.run(uninstall_steps.read_plan(install), keep_data=True, report=lambda *_: None)
            said = ""
        except InstallError as error:
            said = str(error)
        check("Task Manager" in said and install.exists(),
              "the uninstall says what to do, and removes nothing", said[:90])
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(5)
        for key in (sharing.link_key(), sharing.run_key()):
            sharing._delete_tree(key)
        check(the_real_ones() == real_before,
              "CONTROL: the real sign-in entry and link handler are exactly as they were")

    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed")
    shutil.rmtree(ROOT, ignore_errors=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
