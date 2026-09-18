"""Install Mistery for real, check every part of it, then uninstall it.

    python packaging/test_setup.py
    python packaging/test_setup.py --payload packaging/out/dist/Mistery
    python packaging/test_setup.py --exe packaging/out/setup/MisterySetup.exe

Nothing here is mocked. It makes a throwaway profile under packaging/out and
points the installer's MISTERY_SETUP_TEST_ROOT at it, so %LOCALAPPDATA%, the
Start Menu, the Desktop and %APPDATA%\\Mistery for the duration of the test are
folders inside it. The scheduled task is real, is named MisteryUpdateTest-<pid>
so it can never be mistaken for the live one, and is deleted in a finally block
even when a check fails. The Add/Remove Programs entry is real and goes under
HKCU\\Software\\Mistery\\SetupTest rather than Windows' own list.

With --exe it drives the frozen MisterySetup.exe instead of importing the code,
which is the only way to find out whether the payload glued to the end of the
exe comes back out — the thing that cannot be tested any other way. That run
also has MISTERY_SETUP_PAYLOAD pointing at a decoy folder the whole time, so
every check is also a check that the frozen installer ignores it. --exe and
--payload together are refused, because the frozen exe refuses --payload.

The two things it cannot check by itself are on the screen: that the window
looks right, and that Windows' Settings > Apps shows the entry. Both are one
look away and neither can be automated from here.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))

from installer import arp, task                              # noqa: E402
from installer.setup_common import human_size                # noqa: E402
from installer.shortcut import read_shortcut                 # noqa: E402

PASS, FAIL = "  ok  ", " FAIL "
_failures: list[str] = []


def check(condition: bool, what: str, detail: str = "") -> bool:
    print(f"[{PASS if condition else FAIL}] {what}" + (f"   {detail}" if detail else ""))
    if not condition:
        _failures.append(what)
    return condition


def make_fake_payload(folder: Path) -> Path:
    """A Mistery-shaped folder that is not Mistery, so the test is seconds long.

    The installer does not care what is in the payload — it copies a folder —
    so a real 174 MB build only proves the copy loop can count higher. Use
    --payload for that when the real build is what is being tested.
    """
    shutil.rmtree(folder, ignore_errors=True)
    (folder / "_internal" / "assets").mkdir(parents=True)
    for name in ("Mistery.exe", "MisteryUpdate.exe"):
        # Deliberately not starting with "MZ": the shell reads the target as a
        # PE while saving a shortcut to it, and a file that claims to be an exe
        # and then is not makes IPersistFile::Save return a bare E_FAIL. That is
        # a fact about this fake, not about Mistery, and --payload with the real
        # build is the way to test the real thing.
        (folder / name).write_bytes(os.urandom(64_000))
    (folder / "version.txt").write_text("1.0.0\n", encoding="utf-8")
    (folder / "_internal" / "python312.dll").write_bytes(os.urandom(200_000))
    (folder / "_internal" / "assets" / "icon.ico").write_bytes(os.urandom(4_000))
    return folder


def make_decoy_payload(folder: Path) -> Path:
    """A folder that is not Mistery, for MISTERY_SETUP_PAYLOAD to point at.

    Set for the whole --exe run. The frozen installer must ignore it and unpack
    the copy of Mistery inside itself, so every check in _test_install is also a
    check that none of this got in.
    """
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    (folder / "Mistery.exe").write_text(
        "I am a plain text file, not Mistery.", encoding="utf-8")
    (folder / "MisteryUpdate.exe").write_text("nor am I", encoding="utf-8")
    (folder / "NOT-MISTERY.txt").write_text("planted", encoding="utf-8")
    (folder / "version.txt").write_text("9.9.9\n", encoding="utf-8")
    return folder


def payload_flag(payload: Path | None) -> list[str]:
    """--payload <folder>, or nothing when the exe carries its own."""
    return ["--payload", str(payload)] if payload is not None else []


def run_installer(exe: Path | None, arguments: list[str]) -> subprocess.CompletedProcess:
    """Either the frozen exe or the same code in this interpreter.

    The frozen one is a --windowed exe, which means Windows gives it no console
    and a pipe on stdout is not one either: its progress lines have to come back
    through --log. The log is read afterwards and pasted into .stdout so the
    checks do not have to care which of the two they ran.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "packaging")
    log: Path | None = None
    if exe is not None:
        log = ROOT / "packaging" / "out" / f"setup-log-{os.getpid()}.txt"
        log.unlink(missing_ok=True)
        command = [str(exe), *arguments, "--log", str(log)]
    else:
        command = [sys.executable, "-m", "installer.setup_main", *arguments]
    done = subprocess.run(command, capture_output=True, text=True,
                          timeout=1800, env=environment, cwd=str(ROOT))
    if log is not None and log.is_file():
        done = subprocess.CompletedProcess(
            done.args, done.returncode,
            (done.stdout or "") + log.read_text(encoding="utf-8", errors="replace"),
            done.stderr)
        log.unlink(missing_ok=True)
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--payload", type=Path, default=None,
                        help="the app folder to install (default: a fake one)")
    parser.add_argument("--exe", type=Path, default=None,
                        help="drive this frozen MisterySetup.exe instead of "
                             "importing the installer")
    parser.add_argument("--cache", type=Path,
                        default=ROOT / "packaging" / "tools-cache",
                        help="where already-downloaded mpv/ffmpeg archives are")
    parser.add_argument("--keep", action="store_true",
                        help="leave the test root behind to look at")
    args = parser.parse_args(argv)

    test_root = ROOT / "packaging" / "out" / f"testroot-{os.getpid()}"
    task_name = f"MisteryUpdateTest-{os.getpid()}"
    # --exe on its own is the interesting case: the frozen installer unpacks the
    # copy of Mistery glued to the end of itself, which is the one thing no
    # other test can reach. Anything else needs a folder to install from.
    payload: Path | None = args.payload.resolve() if args.payload else None
    if payload is None and args.exe is None:
        payload = make_fake_payload(
            ROOT / "packaging" / "out" / f"fakepayload-{os.getpid()}")
    if payload is not None and args.exe is not None:
        parser.error("--payload and --exe together test nothing: "
                     "MisterySetup.exe refuses --payload and ignores "
                     "MISTERY_SETUP_PAYLOAD, because a shipped installer "
                     "installs the Mistery inside it and nothing else.")

    os.environ["MISTERY_SETUP_TEST_ROOT"] = str(test_root)
    os.environ["MISTERY_SETUP_TASK_NAME"] = task_name
    if args.cache.is_dir():
        os.environ["MISTERY_SETUP_ARCHIVE_CACHE"] = str(args.cache)

    # For the --exe run only: a decoy in the environment for the whole test. If
    # the frozen installer were still reading MISTERY_SETUP_PAYLOAD it would
    # install this instead, and every check below would notice.
    decoy: Path | None = None
    if args.exe is not None:
        decoy = make_decoy_payload(ROOT / "packaging" / "out"
                                   / f"decoy-{os.getpid()}")
        os.environ["MISTERY_SETUP_PAYLOAD"] = str(decoy)

    install_dir = test_root / "Programs" / "Mistery"
    arp_path = r"Software\Mistery\SetupTest\Uninstall\Mistery"

    print(f"test root   {test_root}")
    print(f"task        {task_name}")
    print(f"payload     {payload or 'the one inside ' + args.exe.name}")
    print(f"installer   {args.exe or '(imported)'}\n")

    try:
        _test_refusals(install_dir, test_root)
        _test_payload_seam(args, test_root)
        _test_task_xml(test_root, task_name)
        _test_oversize_download(test_root)
        _test_data_dir_guard(test_root)
        _test_install(args, payload, install_dir, test_root, task_name, arp_path,
                      decoy)
        _test_reinstall(args, payload, install_dir)
        _test_bad_hash(test_root)
        _test_uninstall(args, install_dir, test_root, task_name, arp_path)
    finally:
        print("\ncleaning up")
        for name in (task_name, f"{task_name}-amp"):
            if task.exists(name):
                print(f"  removing leftover task {name}: "
                      f"{task.unregister(name)}")
        arp.remove(arp_path)
        arp.prune_empty_parents(arp_path)
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\Mistery\SetupTest")
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\Mistery")
        except OSError:
            pass
        if not args.keep:
            shutil.rmtree(test_root, ignore_errors=True)
            if args.payload is None and payload is not None:
                shutil.rmtree(payload, ignore_errors=True)
            if decoy is not None:
                shutil.rmtree(decoy, ignore_errors=True)
        print(f"  test root {'kept' if args.keep else 'removed'}")

    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed:")
        for item in _failures:
            print(f"  - {item}")
        return 1
    print("every check passed")
    return 0


# --- the checks -------------------------------------------------------------


def _test_refusals(install_dir: Path, test_root: Path) -> None:
    print("-- folders the installer must refuse")
    from installer.install_steps import check_target
    from installer.setup_common import InstallError

    occupied = test_root / "SomebodyElse"
    occupied.mkdir(parents=True, exist_ok=True)
    (occupied / "Important.docx").write_text("not mine", encoding="utf-8")
    for folder, why in ((occupied, "a folder with someone else's files in it"),
                        (Path("C:\\"), "the root of a drive"),
                        (Path(os.environ["LOCALAPPDATA"]), "%LOCALAPPDATA% itself"),
                        (Path("Mistery"), "a relative path")):
        try:
            check_target(folder)
            check(False, f"refuses {why}")
        except InstallError as error:
            check(True, f"refuses {why}", str(error).splitlines()[0][:70])
    check(check_target(install_dir) == "", "accepts a folder that is not there yet")


def _test_payload_seam(args, test_root: Path) -> None:
    """MisterySetup.exe installs what is inside it, and nothing else.

    MISTERY_SETUP_PAYLOAD and --payload are how the installer is run from source
    without rebuilding an 82 MB exe every time. In a shipped exe they were a way
    to say "install this folder instead", which walked straight past the SHA-256
    the installer keeps over its own payload — measured before the fix: the real
    MisterySetup.exe installed a 41-byte text file called Mistery.exe and said
    "installed Mistery 9.9.9" in 2.0 s.
    """
    print("\n-- the payload seam is closed in the frozen exe")
    from installer import payload as payload_module

    was_frozen = getattr(sys, "frozen", None)
    was_set = os.environ.get("MISTERY_SETUP_PAYLOAD")
    os.environ["MISTERY_SETUP_PAYLOAD"] = str(test_root / "somewhere-else")
    try:
        sys.frozen = True                      # pretend to be MisterySetup.exe
        check(payload_module.loose_payload_dir() is None,
              "frozen: MISTERY_SETUP_PAYLOAD is ignored",
              str(payload_module.loose_payload_dir()))
        sys.frozen = False
        check(payload_module.loose_payload_dir() is not None,
              "from source: it still works, which is what the tests run on")
    finally:
        if was_frozen is None:
            del sys.frozen
        else:
            sys.frozen = was_frozen
        if was_set is None:
            os.environ.pop("MISTERY_SETUP_PAYLOAD", None)
        else:
            os.environ["MISTERY_SETUP_PAYLOAD"] = was_set

    if args.exe is not None:
        # And the flag, against the real exe. Refused out loud rather than
        # ignored, so a harness passing it cannot think it tested something.
        done = subprocess.run([str(args.exe), "--payload", str(test_root),
                               "--unattended"],
                              capture_output=True, text=True, timeout=120,
                              env=dict(os.environ), cwd=str(ROOT))
        check(done.returncode == 2, "MisterySetup.exe --payload is refused",
              f"exit {done.returncode}")


def _test_task_xml(test_root: Path, task_name: str) -> None:
    """An & in the install path must not break the update task.

    The default install path contains the account name and Windows allows & in
    both account and folder names, so "Tom & Jerry" is a path somebody has. The
    XML used to be built by string formatting with no escaping: measured, Expat
    rejected it at line 47 column 29, schtasks refused the file, and the install
    finished saying Mistery would never update itself.
    """
    print("\n-- an ampersand in the install path")
    import xml.dom.minidom

    start = time.strftime("%Y-%m-%dT%H:%M:%S")
    awkward = test_root / "Tom & Jerry" / "Mistery" / "MisteryUpdate.exe"
    for label, where in (("a plain path", test_root / "Plain" / "MisteryUpdate.exe"),
                         ("Tom & Jerry", awkward)):
        text = task.build_xml(task_name, where, start)
        try:
            xml.dom.minidom.parseString(text)
            check(True, f"the XML for {label} is well-formed")
        except Exception as error:
            check(False, f"the XML for {label} is well-formed", str(error)[:70])

    # And through schtasks, which is the only thing whose opinion counts.
    amp_task = f"{task_name}-amp"
    awkward.parent.mkdir(parents=True, exist_ok=True)
    awkward.write_bytes(b"MZ" + os.urandom(400))     # register() wants a real file
    try:
        task.register(amp_task, awkward, start,
                      test_root / "task-amp.xml")
        check(task.exists(amp_task),
              "Windows registers the task for a path with & in it", amp_task)
        described = task.describe(amp_task)
        check("Tom &amp; Jerry" in described,
              "and reads it back with the & still escaped")
    except Exception as error:
        check(False, "Windows registers the task for a path with & in it",
              str(error).splitlines()[0][:70])
    finally:
        task.unregister(amp_task)


def _test_oversize_download(test_root: Path) -> None:
    """A source with no Content-Length that keeps sending must be cut off.

    The pinned size is only checked up front when the response declares one, and
    a chunked response never does. Measured before the fix: told the pin was
    1,000,000 bytes, a local source sent 60,000,000 and all 60 MB landed in
    update\\ before the sizes were compared. On a real line that is minutes of a
    filling disk inside the user's own install folder, for bytes the SHA-256 was
    always going to throw away.
    """
    print("\n-- a download that will not stop")
    import http.server
    import socketserver
    import threading

    from installer import fetch
    from installer.setup_common import InstallError

    flood = 60_000_000
    pinned = 1_000_000

    class Flood(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            block = b"\x00" * 1_000_000
            try:
                for _ in range(flood // len(block)):
                    self.wfile.write(b"%X\r\n" % len(block) + block + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass                     # we hung up on it, which is the point

        def log_message(self, *a):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Flood)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    into = test_root / "flood"
    into.mkdir(parents=True, exist_ok=True)
    target = into / "pinned.bin"
    part = target.with_name(target.name + ".part")
    peak = {"bytes": 0}
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            try:
                peak["bytes"] = max(peak["bytes"], part.stat().st_size)
            except OSError:
                pass
            time.sleep(0.02)

    threading.Thread(target=watch, daemon=True).start()
    try:
        fetch._download(f"http://127.0.0.1:{port}/pinned.bin", target, pinned,
                        lambda *a: None, "pinned", None)
        check(False, "the download is cut off at the pinned size",
              "it finished instead")
    except InstallError as error:
        check("kept sending past" in str(error),
              "the download is cut off at the pinned size",
              str(error).splitlines()[0][:70])
    finally:
        stop.set()
        server.shutdown()
        server.server_close()

    # The count goes up before the write, so a chunk that would take the file
    # past the pin is never written at all: here the very first 8 MB read is
    # already over a 1 MB pin, and the file is never created. 60 MB on disk is
    # what this used to be.
    check(peak["bytes"] <= pinned,
          "and not one byte past the pin reached the disk",
          f"peak {peak['bytes']:,} B of the {flood:,} B offered")
    check(not part.exists() and not target.exists(),
          "and nothing was left in update\\")
    shutil.rmtree(into, ignore_errors=True)


def _test_data_dir_guard(test_root: Path) -> None:
    """"Delete my library" must delete a library, not whatever the file says.

    data_dir comes out of mistery-install.json, which is a plain file in the
    install folder. Measured before the fix: editing that one value to name a
    Documents folder and unticking "keep my library" deleted Documents.
    """
    print("\n-- the library folder the marker names is checked first")
    from installer import uninstall_steps

    # Its own task name and its own registry key, so this section cannot reach
    # the ones the real install uses whatever order the checks end up running
    # in. Neither ever exists: removing a task or a key that is not there is a
    # no-op both ways round.
    guard_task = f"MisteryUpdateTest-{os.getpid()}-guard"
    guard_arp = r"Software\Mistery\SetupTest\Uninstall\MisteryGuard"
    was_task = os.environ.get("MISTERY_SETUP_TASK_NAME")
    os.environ["MISTERY_SETUP_TASK_NAME"] = guard_task

    install = test_root / "guard" / "Programs" / "Mistery"
    install.mkdir(parents=True, exist_ok=True)
    (install / "Mistery.exe").write_bytes(b"MZ" + os.urandom(400))
    victim = test_root / "guard" / "Documents"
    victim.mkdir(parents=True, exist_ok=True)
    (victim / "Important.docx").write_text("the only copy", encoding="utf-8")
    (install / "mistery-install.json").write_text(json.dumps({
        "app": "Mistery", "version": "1.1.0", "install_dir": str(install),
        "data_dir": str(victim),               # the one edited value
        "arp_key": guard_arp,
    }), encoding="utf-8")

    plan = uninstall_steps.read_plan(install)
    removed = uninstall_steps.run(plan, keep_data=False, report=lambda *a: None)
    check((victim / "Important.docx").is_file(),
          "a folder with no library.db and no settings.json is not deleted")
    check(any("not Mistery's library folder" in item
              for item in removed.left_behind),
          "and the window is told why", "; ".join(removed.left_behind)[:70])

    # The other half: a folder that really is a library still goes.
    real = test_root / "guard" / "Roaming" / "Mistery"
    real.mkdir(parents=True, exist_ok=True)
    (real / "library.db").write_bytes(os.urandom(2_000))
    install.mkdir(parents=True, exist_ok=True)
    (install / "Mistery.exe").write_bytes(b"MZ" + os.urandom(400))
    (install / "mistery-install.json").write_text(json.dumps({
        "app": "Mistery", "version": "1.1.0", "install_dir": str(install),
        "data_dir": str(real),
        "arp_key": guard_arp,
    }), encoding="utf-8")
    plan = uninstall_steps.read_plan(install)
    removed = uninstall_steps.run(plan, keep_data=False, report=lambda *a: None)
    check(not real.exists() and removed.data_dir,
          "a folder that does hold library.db is still deleted when asked")

    if was_task is None:
        os.environ.pop("MISTERY_SETUP_TASK_NAME", None)
    else:
        os.environ["MISTERY_SETUP_TASK_NAME"] = was_task
    arp.prune_empty_parents(guard_arp)
    shutil.rmtree(test_root / "guard", ignore_errors=True)


def _test_install(args, payload: Path, install_dir: Path, test_root: Path,
                  task_name: str, arp_path: str, decoy: Path | None) -> None:
    print("\n-- installing")
    started = time.monotonic()
    done = run_installer(args.exe, ["--unattended", *payload_flag(payload),
                                    "--desktop"])
    took = time.monotonic() - started
    if not check(done.returncode == 0, "the installer finished",
                 f"{took:.1f} s"):
        print(done.stdout[-2000:])
        print(done.stderr[-2000:])
        return

    for name in ("Mistery.exe", "version.txt", "mistery-install.json",
                 "updater.json", "_internal/python312.dll",
                 "runtime/mpv.exe", "runtime/ffmpeg.exe", "runtime/ffprobe.exe",
                 "runtime/avcodec-63.dll", "runtime/avformat-63.dll",
                 "runtime/avutil-61.dll", "runtime/avfilter-12.dll",
                 "runtime/avdevice-63.dll", "runtime/swresample-7.dll",
                 "runtime/swscale-10.dll"):
        path = install_dir / name
        check(path.is_file(), f"{name} is there",
              human_size(path.stat().st_size) if path.is_file() else "missing")

    if decoy is not None:
        # MISTERY_SETUP_PAYLOAD has been pointing at a decoy folder for this
        # whole run. What came out of the exe has to be Mistery, not that.
        check(not (install_dir / "NOT-MISTERY.txt").exists(),
              "nothing from the decoy folder got in")
        head = (install_dir / "Mistery.exe").read_bytes()[:2] \
            if (install_dir / "Mistery.exe").is_file() else b""
        check(head == b"MZ", "the installed Mistery.exe is a real program",
              repr(head))
        installed = (install_dir / "version.txt").read_text("utf-8").strip() \
            if (install_dir / "version.txt").is_file() else ""
        check(installed != "9.9.9",
              "and its version came from the payload, not the decoy", installed)

    check((install_dir / "update").is_dir(), "update\\ exists for the updater")
    leftovers = list((install_dir / "update").glob("*"))
    check(not leftovers, "update\\ is empty — the archives were cleaned up",
          ", ".join(p.name for p in leftovers))

    if args.exe is not None:
        check((install_dir / "Uninstall.exe").is_file(), "Uninstall.exe is there",
              human_size((install_dir / "Uninstall.exe").stat().st_size)
              if (install_dir / "Uninstall.exe").is_file() else "missing")

    total = sum(p.stat().st_size for p in install_dir.rglob("*") if p.is_file())
    print(f"       install is {human_size(total)} in "
          f"{sum(1 for p in install_dir.rglob('*') if p.is_file())} files")

    print("\n-- do the fetched tools actually run")
    # The point of the shared ffmpeg build is that ffmpeg.exe and ffprobe.exe are
    # tiny and every codec lives in the DLLs beside them. Miss one DLL and they
    # do not start at all, with no message, because Windows reports a missing
    # import before main() runs. So each one is started, once.
    for name, flag, expect in (("mpv.exe", "--version", "mpv"),
                               ("ffmpeg.exe", "-version", "ffmpeg version"),
                               ("ffprobe.exe", "-version", "ffprobe version")):
        exe = install_dir / "runtime" / name
        if not exe.is_file():
            check(False, f"{name} runs", "not installed")
            continue
        try:
            out = subprocess.run([str(exe), flag], capture_output=True, text=True,
                                 timeout=60, creationflags=0x08000000)
            text = ((out.stdout or "") + (out.stderr or "")).strip()
            check(out.returncode == 0 and expect in text.lower(),
                  f"{name} starts and reports its version",
                  text.splitlines()[0][:60] if text
                  else f"exit {out.returncode}, no output")
        except OSError as error:
            check(False, f"{name} starts", str(error)[:70])

    print("\n-- the shortcuts")
    for link in (test_root / "StartMenu" / "Mistery.lnk",
                 test_root / "Desktop" / "Mistery.lnk"):
        if not check(link.is_file(), f"{link.parent.name}\\Mistery.lnk exists"):
            continue
        info = read_shortcut(link)
        check(Path(info["target"]) == install_dir / "Mistery.exe",
              f"{link.parent.name} shortcut points at the installed exe",
              info["target"][-60:])
        check(Path(info["target"]).is_file(),
              f"{link.parent.name} shortcut target actually exists")
        check(info.get("app_id") == "Mistery.Player.1",
              f"{link.parent.name} shortcut carries the AppUserModelID",
              info.get("app_id", "(none)"))

    print("\n-- the update task")
    if check(task.exists(task_name), f"{task_name} is registered"):
        xml = task.describe(task_name)
        check("<LogonType>InteractiveToken</LogonType>" in xml,
              "the task runs interactively, not in session 0")
        check("whether user is logged on" not in xml.lower(),
              "the task is not a run-whether-logged-on-or-not task")
        check("<Interval>PT1H</Interval>" in xml, "it repeats every hour")
        check("MisteryUpdate.exe" in xml, "it runs MisteryUpdate.exe")
        check(str(install_dir) in xml, "from the folder just installed into")

    print("\n-- Add/Remove Programs")
    values = arp.read(arp_path)
    check(values.get("DisplayName") == "Mistery", "DisplayName",
          str(values.get("DisplayName")))
    check(bool(values.get("DisplayVersion")), "DisplayVersion",
          str(values.get("DisplayVersion")))
    check(values.get("Publisher") == "Mistery", "Publisher")
    check(values.get("InstallLocation") == str(install_dir), "InstallLocation")
    check(isinstance(values.get("EstimatedSize"), int)
          and values["EstimatedSize"] > 200_000,
          "EstimatedSize is the real size in KB",
          f"{values.get('EstimatedSize')} KB")
    uninstall_string = str(values.get("UninstallString", ""))
    check(uninstall_string.startswith('"') and "Uninstall.exe" in uninstall_string,
          "UninstallString is quoted and points at Uninstall.exe")

    print("\n-- what it wrote about itself")
    marker = json.loads((install_dir / "mistery-install.json").read_text("utf-8"))
    check(marker.get("app") == "Mistery", "the marker says this folder is ours")
    check(marker.get("update_task") == task_name,
          "the marker records the task name the uninstaller must remove")
    check(marker.get("app_id") == "Mistery.Player.1", "the marker records the app id")
    check(len(marker.get("tools", {})) == 2,
          "the marker records which mpv and ffmpeg builds are installed")
    data = Path(marker["data_dir"])
    check(not data.exists(),
          "the installer did not create the library folder",
          str(data))


def _test_reinstall(args, payload: Path, install_dir: Path) -> None:
    print("\n-- installing again over the top")
    stale = install_dir / "_internal" / "from-the-old-version.dll"
    stale.write_bytes(b"stale")
    before = (install_dir / "runtime" / "mpv.exe").stat().st_mtime_ns
    started = time.monotonic()
    done = run_installer(args.exe, ["--unattended", *payload_flag(payload)])
    took = time.monotonic() - started
    if not check(done.returncode == 0, "the second install finished",
                 f"{took:.1f} s"):
        print(done.stdout[-1500:], done.stderr[-1500:])
        return
    check(not stale.exists(),
          "the old version's leftover file is gone")
    check((install_dir / "runtime" / "mpv.exe").stat().st_mtime_ns == before,
          "mpv was not downloaded and unpacked a second time")
    check("not downloaded again" in done.stdout, "and it said so")


def _test_bad_hash(test_root: Path) -> None:
    print("\n-- a download whose hash is wrong")
    from installer import fetch
    from installer.setup_common import InstallError
    from installer.tool_downloads import MPV

    staging = test_root / "badhash"
    staging.mkdir(parents=True, exist_ok=True)
    planted = staging / MPV.archive_name
    planted.write_bytes(b"\x00" * MPV.size)          # right size, wrong bytes
    # Without this the good archive in the cache folder would be copied straight
    # over the planted one and the test would prove nothing.
    cache = os.environ.pop("MISTERY_SETUP_ARCHIVE_CACHE", None)
    try:
        fetch.obtain(MPV, staging, lambda *a: None)
        check(False, "a wrong-hash archive is refused")
    except InstallError as error:
        check("not the file Mistery expects" in str(error),
              "a wrong-hash archive is refused",
              str(error).splitlines()[0][:60])
        check(not planted.exists(), "and the bad download was deleted")
    finally:
        if cache is not None:
            os.environ["MISTERY_SETUP_ARCHIVE_CACHE"] = cache
    shutil.rmtree(staging, ignore_errors=True)


def _test_uninstall(args, install_dir: Path, test_root: Path, task_name: str,
                    arp_path: str) -> None:
    print("\n-- uninstalling, keeping the library")
    # What is already in %TEMP% before this starts, so a copy left there by an
    # earlier run cannot be mistaken for one this run failed to clear up.
    temp_dir = Path(os.environ.get("TEMP", "."))
    before = set(temp_dir.glob("MisteryUninstall-*.exe"))
    data = test_root / "Roaming" / "Mistery"
    data.mkdir(parents=True, exist_ok=True)
    (data / "library.db").write_bytes(os.urandom(50_000))
    (data / "settings.json").write_text('{"tmdb_key": "keep me"}', encoding="utf-8")

    if args.exe is not None:
        # The real thing: Uninstall.exe, from inside the folder it deletes. It
        # copies itself to %TEMP% and that copy finishes the job, so this has to
        # wait for a process it did not start.
        done = subprocess.run([str(install_dir / "Uninstall.exe"), "--quiet",
                               "--log", str(ROOT / "packaging" / "out"
                                            / f"uninstall-log-{os.getpid()}.txt")],
                              capture_output=True, text=True, timeout=600,
                              env=dict(os.environ))
        check(done.returncode == 0, "Uninstall.exe --quiet ran")
        deadline = time.monotonic() + 90
        while install_dir.exists() and time.monotonic() < deadline:
            time.sleep(1.0)
    else:
        done = run_installer(None, ["--uninstall", "--quiet",
                                    "--dir", str(install_dir)])
        if not check(done.returncode == 0, "the uninstaller ran"):
            print(done.stdout[-1500:], done.stderr[-1500:])

    check(not install_dir.exists(), "the install folder is gone",
          "" if not install_dir.exists()
          else ", ".join(p.name for p in install_dir.iterdir()))
    check(not (test_root / "StartMenu" / "Mistery.lnk").exists(),
          "the Start Menu shortcut is gone")
    check(not (test_root / "Desktop" / "Mistery.lnk").exists(),
          "the Desktop shortcut is gone")
    check(not task.exists(task_name), "the scheduled task is gone")
    check(arp.read(arp_path) == {}, "the Add/Remove Programs entry is gone")
    check((data / "library.db").is_file(),
          "the library was kept, which is the default")
    check((data / "settings.json").read_text("utf-8").find("keep me") >= 0,
          "settings.json — the only copy of the TMDB key — was kept")

    if args.exe is not None:
        # The uninstaller copies itself to %TEMP% so it can delete the folder it
        # was running from, and then has to clear that copy up. It takes a few
        # seconds after the process exits, so this waits rather than looking once.
        deadline = time.monotonic() + 40
        leftovers = set(temp_dir.glob("MisteryUninstall-*.exe")) - before
        while leftovers and time.monotonic() < deadline:
            time.sleep(1.0)
            leftovers = set(temp_dir.glob("MisteryUninstall-*.exe")) - before
        scripts = list(temp_dir.glob("Mistery-cleanup-*.cmd"))
        check(not leftovers and not scripts,
              "the uninstaller cleared its own copy out of %TEMP%",
              ", ".join(p.name for p in sorted(leftovers) + scripts))


if __name__ == "__main__":
    raise SystemExit(main())
