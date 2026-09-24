r"""MisteryUpdate.exe — the program a scheduled task runs every hour.

Almost every run does nothing and says so in one line. That is the design: the
task fires hourly so that an update is never more than an hour late, and the
run is cheap enough (a 700-byte fetch, or not even that when Mistery is open)
that hourly costs nothing worth measuring.

A full run, in order:

    sweep       delete last run's .old files, now that nothing holds them
    off switch  updater.json in the install folder, not the app's settings.
                It stops --check-now as well: off means this program does not
                talk to the update server, button or no button.
    liveness    Mistery closed, and closed for half an hour (updater/liveness)
    manifest    fetch over https, verify the Ed25519 signature, then read it
    version     strictly newer than version.txt, by number
    download    into install\update\, size checked as it arrives, then SHA-256
    unpack      into install\update\unpack\, defensively (updater/apply)
    move        rename-then-replace, with everything put back if one fails

Nothing but the last step changes the installed app, and it only starts once
every check above has passed.

Exit codes, because a scheduled task and a test both read them:

     0  nothing to do — already up to date
     1  could not check — no network, the server is down. Nothing changed.
     2  refused — a signature, size, hash, version or zip check failed.
        Nothing changed, and the download has been deleted.
     3  the update verified but could not be put in place. The files that had
        moved were put back.
    10  an update was applied.
    20  skipped — Mistery is running, quit less than half an hour ago, or
        updates are switched off.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import sys
import time
from pathlib import Path

from . import apply as apply_files
from . import download, keys, liveness, manifest, paths
from .manifest import Refused, Unreachable

OK = 0
UNREACHABLE = 1
REFUSED = 2
NOT_APPLIED = 3
UPDATED = 10
SKIPPED = 20


_held_mutex: int | None = None


def _single_instance() -> bool:
    """Only one updater at a time. False if another one holds the name.

    The hourly task and a "check for updates" the person just clicked can land
    together, and two runs unpacking into the same folder would race. Windows
    releases a mutex however the process ends, so a crashed run does not lock
    the next one out.
    """
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    name = "Local\\MisteryUpdate-" + liveness.user_tag()
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True                     # no mutex to be had; carry on regardless
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return False
    global _held_mutex
    _held_mutex = handle                # held until this process exits
    return True


def check() -> tuple[dict, str]:
    """Fetch and verify the manifest: (body, url). Raises Refused or Unreachable."""
    url = paths.manifest_url()
    raw = manifest.fetch(url, manifest.MAX_MANIFEST_BYTES)
    return manifest.verify(raw), url


def run(check_only: bool = False) -> int:
    install = paths.install_dir()
    installed = paths.installed_version()

    swept = apply_files.sweep(install)
    if swept:
        paths.log(f"swept {swept} leftover file(s) from the last update")

    if not keys.trusted_keys():
        # Checked here as well as in manifest.verify(), because a build with no
        # key can never accept anything and there is no reason to make a
        # network request to find that out once an hour, for ever.
        paths.log("REFUSED: this build carries no public key, so it can verify "
                  "nothing. It was built before packaging/make_key.py was run.")
        return REFUSED

    if not paths.auto_update_enabled():
        # The off switch stops --check-now too. Somebody who turned updates off
        # said they did not want this program talking to the update server on
        # their behalf, and "except when you press the button" is not what an
        # off switch means. The answer is written down instead, so Mistery's
        # Settings screen can say "updates are switched off" rather than sit
        # there waiting for a check that is never going to come back.
        paths.log("auto_update is off in updater.json; nothing to do")
        if check_only:
            paths.write_result({"installed": installed, "disabled": True})
        return SKIPPED

    if not check_only:
        waiting = liveness.quiet_reason()
        if waiting:
            paths.log(f"not now: {waiting}")
            return SKIPPED

    try:
        body, url = check()
    except Unreachable as exc:
        paths.log(f"could not check: {exc}")
        paths.write_result({"error": str(exc), "installed": installed,
                            "reachable": False})
        return UNREACHABLE
    except Refused as exc:
        paths.log(f"REFUSED: {exc}")
        paths.write_result({"error": str(exc), "installed": installed,
                            "refused": True})
        return REFUSED

    offered = body["version"]
    if not manifest.is_newer(offered, installed):
        paths.log(f"up to date: {installed} installed, {offered} offered "
                  f"(key {body['_key_id']})")
        paths.write_result({"installed": installed, "latest": offered,
                            "available": False, "notes": body.get("notes", "")})
        return OK

    package = body["package"]
    paths.log(f"{offered} is available ({package['size'] / 1e6:.0f} MB), "
              f"{installed} installed, manifest from {url} signed by key "
              f"{body['_key_id']}")
    paths.write_result({"installed": installed, "latest": offered,
                        "available": True, "notes": body.get("notes", ""),
                        "size": package["size"]})
    if check_only:
        return OK

    # From here on files can change. Everything above this line is reading.
    try:
        archive, seconds = download.fetch_package(package, paths.update_dir())
    except Unreachable as exc:
        paths.log(f"download did not finish: {exc}")
        return UNREACHABLE
    except Refused as exc:
        paths.log(f"REFUSED: {exc}")
        return REFUSED
    speed = package["size"] / 1e6 / max(seconds, 0.001)
    paths.log(f"downloaded {archive.name} in {seconds:.0f} s ({speed:.0f} MB/s), "
              f"size and SHA-256 both match")

    try:
        files, written = apply_files.unpack(archive, paths.unpack_dir())
    except Refused as exc:
        paths.log(f"REFUSED: {exc}")
        _delete(archive)
        return REFUSED
    paths.log(f"unpacked {files} files, {written / 1e6:.0f} MB")

    # Mistery can have been started while the download ran — 170 MB over a slow
    # connection is minutes, and the person who opened the app in that time is
    # about to have their window rebuilt under them. Asked again here because
    # this is the last moment it is still free to stop.
    waiting = liveness.running_reason()
    if waiting:
        paths.log(f"not applying: {waiting}. The download is kept for the next run.")
        return SKIPPED

    # The Mistery serving friends with no window holds Mistery.exe open too.
    # It steps aside for the few seconds the files take, and is started again
    # from the new ones (or the old, if they could not go in) straight after.
    sharing = liveness.stop_sharer()
    if sharing is False:
        paths.log("not applying: the Mistery serving friends did not step aside. "
                  "The download is kept for the next run.")
        return SKIPPED
    if sharing:
        paths.log("the Mistery serving friends stepped aside for the update")
    try:
        result = apply_files.move_into_place(paths.unpack_dir(), install)
    except Refused as exc:
        paths.log(f"NOT APPLIED: {exc}")
        return NOT_APPLIED
    finally:
        if sharing:
            started = liveness.start_sharer(install)
            paths.log("started serving friends again" if started else
                      "could not start serving friends again; it starts at the next sign-in")

    # version.txt normally comes out of the zip; write it if the build forgot,
    # so that the next run does not offer the same update again forever.
    if paths.installed_version() != offered:
        try:
            paths.version_file().write_text(offered + "\n", encoding="utf-8")
        except OSError as exc:
            paths.log(f"could not write version.txt: {exc}")

    _delete(archive)
    _delete(paths.unpack_dir())
    staged = " (the new updater is staged; Mistery swaps it in at its next start)" \
        if result.staged_updater else ""
    paths.log(f"updated {installed} -> {offered}: {result.replaced} file(s) "
              f"replaced, {result.added} added{staged}")
    paths.write_result({"installed": offered, "latest": offered, "available": False,
                        "applied": offered, "notes": body.get("notes", "")})
    return UPDATED


def _delete(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _attach_console() -> None:
    """Print into the console of whoever started us, if there is one.

    The exe is built windowed so the hourly task never flashes a window, and a
    windowed build has no stdout at all — print() raises. AttachConsole(-1)
    borrows the console of the process that started this one, so `--status`
    typed in a terminal answers there; started by the task, there is no console
    to attach to, this does nothing, and the lines go to update.log instead.
    """
    if sys.platform != "win32" or sys.stdout is not None:
        return
    ATTACH_PARENT_PROCESS = -1
    try:
        if not ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            return
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
    except (OSError, AttributeError):
        pass


def _say(line: str) -> None:
    """A line for a person, wherever a person might see it."""
    if sys.stdout is not None:
        try:
            print(line)
            return
        except (OSError, ValueError):
            pass
    paths.log(line)


def status() -> int:
    """What the updater can see. Asks nothing of the network, changes nothing."""
    install = paths.install_dir()
    data = paths.data_dir()
    trusted = keys.trusted_keys()
    _say(f"install folder   {install}")
    _say(f"version.txt      {paths.installed_version()}")
    _say(f"data folder      {data if data else '(none — Mistery has never run)'}")
    _say(f"manifest         {paths.manifest_url()}")
    _say(f"auto_update      {'on' if paths.auto_update_enabled() else 'off'}")
    _say(f"trusted keys     {', '.join(sorted(trusted)) or '(none — this build trusts nothing)'}")
    _say(f"Mistery          {liveness.running_reason() or liveness.describe_idle()}")
    _say(f"may update now   {'yes' if liveness.quiet_reason() is None else 'no'}")
    staged = install / (paths.UPDATER_NAME + ".new")
    if staged.is_file():
        _say(f"staged           {staged.name} is waiting for Mistery to start")
    return OK


class _Parser(argparse.ArgumentParser):
    """argparse, minus its habit of writing to a stderr that may not exist.

    A windowed build has sys.stderr = None until a console is attached, and the
    plain parser's error path would die with an AttributeError instead of
    saying what was wrong. 64 is the usual "you typed it wrong" exit code and
    does not collide with the ones this program uses for its own answers.
    """

    def error(self, message: str) -> None:      # type: ignore[override]
        _say(f"{self.prog}: {message}")
        raise SystemExit(64)


def main(argv: list[str] | None = None) -> int:
    _attach_console()
    parser = _Parser(
        prog=paths.UPDATER_NAME,
        description="Keep Mistery up to date. Run hourly by a scheduled task.")
    parser.add_argument("--check-now", action="store_true",
                        help="ask whether there is an update and write "
                             "update\\last-check.json; download nothing")
    parser.add_argument("--status", action="store_true",
                        help="print what the updater can see and stop")
    parser.add_argument("--sweep", action="store_true",
                        help="delete leftover .old files and stop")
    # The off switch, as two flags rather than a value to parse. This is what
    # the app's Settings toggle calls, so it needs no import from this package
    # and no idea where updater.json lives.
    switch = parser.add_mutually_exclusive_group()
    switch.add_argument("--enable", action="store_true", help="turn updates on")
    switch.add_argument("--disable", action="store_true", help="turn updates off")
    args = parser.parse_args(argv)

    if args.status:
        return status()
    if args.enable or args.disable:
        paths.set_auto_update(bool(args.enable))
        paths.log(f"auto_update set to {bool(args.enable)}")
        return OK
    if args.sweep:
        gone = apply_files.sweep(paths.install_dir())
        paths.log(f"swept {gone} file(s)")
        return OK

    if not _single_instance():
        paths.log("another updater is already running; leaving it to it")
        return OK

    started = time.monotonic()
    try:
        code = run(check_only=args.check_now)
    except Exception as exc:                     # never a traceback into the void
        paths.log(f"unexpected failure: {type(exc).__name__}: {exc}")
        if os.environ.get("MISTERY_UPDATE_DEBUG"):
            raise
        return 1
    took = time.monotonic() - started
    if took > 5:
        paths.log(f"finished in {took:.0f} s")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
