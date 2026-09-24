"""Removing Mistery, and the one question it has to ask first.

    %APPDATA%\\Mistery is not ours to delete.

It holds library.db — every file the person pointed Mistery at, what they have
watched, where they stopped — settings.json, which is the only copy of their
TMDB key, and the artwork cache, which was 237 MB on the machine this was
measured on and took hours of scanning to build. Reinstalling Mistery
afterwards, or moving it to another folder, is supposed to find all of that
still there. So the uninstaller asks, and the answer it suggests is "keep".

What it does remove: the install folder, the two shortcuts, the scheduled task
and the Add/Remove Programs entry — and it removes them by reading
mistery-install.json, which the installer wrote with the exact names it used.
Guessing at those names is how an uninstaller deletes somebody else's shortcut.
Two more entries are the app's own, not the installer's (sharing.py): the one
that starts it at sign-in to serve friends, and the one that makes mistery://
links open it. They go too, when they point into the folder being removed.

mistery-install.json is read, never obeyed. It is an ordinary file in the
install folder, so every path out of it is checked against what it claims to
be before anything is deleted: the install folder has to hold the marker or
Mistery.exe, a shortcut has to point back into that folder, and the library
folder has to look like a library folder — see looks_like_data_dir.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import arp, sharing, task
from .setup_common import (APP_EXE_NAME, APP_NAME, CREATE_NO_WINDOW, MARKER_NAME,
                           UNINSTALLER_NAME, InstallError, arp_key, data_dir,
                           desktop_dir, exe_in_use, folder_size, human_size,
                           is_frozen, start_menu_dir, task_name)
from .shortcut import read_shortcut

Report = Callable[[str, float], None]

CREATE_NEW_PROCESS_GROUP = 0x00000200


@dataclass
class Plan:
    """What is about to be removed, so the window can say so before it happens."""

    install_dir: Path
    version: str = ""
    install_bytes: int = 0
    data_dir: Path | None = None
    data_bytes: int = 0
    shortcuts: list[Path] = field(default_factory=list)
    update_task: str | None = None
    arp_key: str = ""
    sharing_entry: bool = False       # starts this install's Mistery at sign-in to serve friends
    link_handler: bool = False        # mistery:// links open this install's Mistery


@dataclass
class Removed:
    install_dir: bool = False
    shortcuts: list[Path] = field(default_factory=list)
    update_task: bool = False
    arp_entry: bool = False
    sharer_stopped: bool = False
    sharing_entry: bool = False
    link_handler: bool = False
    data_dir: bool = False
    left_behind: list[str] = field(default_factory=list)
    seconds: float = 0.0


def find_install_dir(explicit: Path | None = None) -> Path:
    """The folder to uninstall: the one given, or the one this exe sits in."""
    if explicit is not None:
        return Path(explicit).resolve()
    if is_frozen():
        return Path(sys.executable).resolve().parent
    raise InstallError("Nothing to uninstall: say which folder with --dir.")


def read_plan(install_dir: Path) -> Plan:
    """Read mistery-install.json and work out what removing it would cost."""
    import json

    install_dir = Path(install_dir)
    try:
        marker = json.loads((install_dir / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        marker = {}
    if marker.get("app") != APP_NAME and not (install_dir / APP_EXE_NAME).is_file():
        raise InstallError(
            f"{install_dir} does not look like a Mistery install — there is no "
            f"{MARKER_NAME} and no {APP_EXE_NAME} in it. Nothing was removed.")

    shortcuts = [Path(p) for p in (marker.get("start_menu_shortcut"),
                                   marker.get("desktop_shortcut")) if p]
    # Also sweep the two usual places, for a link an older installer made and
    # did not record. Only ones that point into the folder being removed: a
    # Mistery.lnk aimed at some other copy belongs to that copy.
    for folder in (start_menu_dir(), desktop_dir()):
        candidate = folder / f"{APP_NAME}.lnk"
        if candidate.is_file() and candidate not in shortcuts:
            try:
                target = Path(read_shortcut(candidate).get("target", "."))
                if target.parent.resolve() == install_dir.resolve():
                    shortcuts.append(candidate)
            except (OSError, InstallError):
                pass
    data = Path(marker["data_dir"]) if marker.get("data_dir") else data_dir()
    return Plan(
        install_dir=install_dir,
        version=str(marker.get("version") or ""),
        install_bytes=folder_size(install_dir),
        data_dir=data if data.is_dir() else None,
        data_bytes=folder_size(data),
        shortcuts=[p for p in shortcuts if p.exists()],
        update_task=marker.get("update_task") or (
            task_name() if task.exists(task_name()) else None),
        arp_key=str(marker.get("arp_key") or arp_key()),
        sharing_entry=sharing.run_entry(install_dir),
        link_handler=sharing.link_handler(install_dir),
    )


def looks_like_data_dir(folder: Path) -> bool:
    """True if this folder is Mistery's library folder rather than some other one.

    The path being checked came out of mistery-install.json, which is an
    ordinary file in the install folder that anything running as this user can
    open in Notepad. Unticking "keep my library" is the only unbounded
    recursive delete in this whole chain, and until this check existed, editing
    that one value to say C:\\Users\\<name>\\Documents turned it into a delete
    of Documents — measured, with the file in it gone and nothing said.

    So the folder has to be either the one data_dir() works out for itself, or
    one holding a file only Mistery writes. library.db or settings.json:
    settings.json as well as library.db because a person who has installed
    Mistery, opened it once and never pointed it at anything has a settings.json
    and no library.db yet, and their folder is still theirs to remove.

    read_plan is already careful this way about the install folder (it refuses
    one with no marker and no Mistery.exe) and about shortcuts (it only claims
    the ones pointing into the folder being removed). This is the same rule for
    the one path that was still taken on trust.
    """
    try:
        if folder.resolve() == data_dir().resolve():
            return True
    except OSError:
        pass
    return (folder / "library.db").is_file() or (folder / "settings.json").is_file()


def run(plan: Plan, keep_data: bool, report: Report) -> Removed:
    """Remove everything in the plan. Never raises for a thing already gone."""
    started = time.monotonic()
    out = Removed()

    # The Mistery serving friends with no window is this folder's Mistery.exe
    # too, and it never quits by itself: ask it to leave first, or the check
    # below would send the person looking for a window that is not there.
    if sharing.sharer_pid(plan.install_dir) is not None:
        report("Stopping sharing with friends", 0.02)
        stopped = sharing.stop_sharer(plan.install_dir)
        if stopped is False:
            raise InstallError(sharing.STOP_FAILED)
        out.sharer_stopped = bool(stopped)

    if exe_in_use(plan.install_dir / APP_EXE_NAME):
        raise InstallError(
            f"Mistery is running from {plan.install_dir}. Close it (check the "
            "system tray) and try again — Windows will not delete a folder "
            "something is running out of.")

    # The task first: an hourly updater that wakes up halfway through an
    # uninstall and starts putting files back is a bad afternoon.
    if plan.update_task:
        report(f"Removing the {plan.update_task} task", 0.05)
        out.update_task = task.unregister(plan.update_task)
        if not out.update_task:
            out.left_behind.append(f"the scheduled task {plan.update_task}")

    # Before the files: an entry left behind would start a Mistery.exe that is
    # not there at the next sign-in, or when a friend's link is clicked.
    report("Removing sharing at sign-in and mistery:// links", 0.1)
    entry = sharing.remove_run_entry(plan.install_dir)
    out.sharing_entry = bool(entry)
    if entry is False:
        out.left_behind.append(f"HKCU\\{sharing.run_key()} ({sharing.RUN_VALUE})")
    handler = sharing.remove_link_handler(plan.install_dir)
    out.link_handler = bool(handler)
    if handler is False:
        out.left_behind.append(f"HKCU\\{sharing.link_key()}")

    report("Removing the Start Menu entry", 0.15)
    for link in plan.shortcuts:
        try:
            link.unlink(missing_ok=True)
            out.shortcuts.append(link)
        except OSError:
            out.left_behind.append(str(link))

    report("Removing the Add/Remove Programs entry", 0.25)
    out.arp_entry = arp.remove(plan.arp_key)
    arp.prune_empty_parents(plan.arp_key)
    if not out.arp_entry:
        out.left_behind.append(f"HKCU\\{plan.arp_key}")

    report(f"Removing {plan.install_dir}", 0.35)
    out.install_dir = _remove_tree(plan.install_dir, report, 0.35, 0.9)
    if not out.install_dir:
        out.left_behind.append(str(plan.install_dir))

    if not keep_data and plan.data_dir is not None:
        if not looks_like_data_dir(plan.data_dir):
            out.left_behind.append(
                f"{plan.data_dir} (no library.db or settings.json in it, so "
                "it is not Mistery's library folder)")
        else:
            report(f"Removing {human_size(plan.data_bytes)} of library data", 0.92)
            out.data_dir = _remove_tree(plan.data_dir, report, 0.92, 0.99)
            if not out.data_dir:
                out.left_behind.append(str(plan.data_dir))

    report("Done", 1.0)
    out.seconds = time.monotonic() - started
    return out


def _remove_tree(folder: Path, report: Report, start: float, end: float) -> bool:
    """Delete a folder, keeping the one file that cannot be deleted yet.

    When Uninstall.exe is running from inside the folder it is deleting, Windows
    holds that one file open. Everything else goes now; the exe itself is handed
    to the short cmd script in `finish_after_exit`.
    """
    if not folder.is_dir():
        return True
    self_exe = Path(sys.executable).resolve() if is_frozen() else None
    entries = sorted(folder.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    total = len(entries) or 1
    stubborn = False
    for index, entry in enumerate(entries):
        if index % 64 == 0:
            report(f"Removing {folder.name}",
                   start + (end - start) * (index / total))
        try:
            if entry.is_dir():
                entry.rmdir()
            else:
                if self_exe is not None and entry.resolve() == self_exe:
                    stubborn = True
                    continue
                entry.unlink()
        except OSError:
            stubborn = True
    if not stubborn:
        try:
            folder.rmdir()
            return True
        except OSError:
            return False
    return False


def finish_after_exit(folder: Path) -> bool:
    """Delete a folder we are still running out of, once we are not.

    `rd /s /q` cannot remove a folder a running program is in, and the reliable
    trick for that on Windows needs an administrator (MoveFileEx with
    MOVEFILE_DELAY_UNTIL_REBOOT writes to HKLM). So a small batch file waits and
    then removes it. If Windows is still holding a file after that, the folder
    stays — visible and deletable by hand rather than silently half-gone.
    """
    if os.name != "nt" or not folder.is_dir():
        return False
    return _run_later([f'rd /s /q "{folder}"',
                       f'if exist "{folder}" (ping -n 5 127.0.0.1 >nul',
                       f' rd /s /q "{folder}")'], f"remove-{folder.name}")


def _run_later(commands: list[str], label: str) -> bool:
    """Write a .cmd that waits three seconds, runs those lines, and deletes itself.

    A batch file rather than `cmd /c "one long string"`. cmd's rules for quotes
    after /c are their own subject, and the one long string here has to contain
    a quoted path: measured, that combination silently did nothing and left an
    11.2 MB copy of the uninstaller in %TEMP% after every run. A file has no
    quoting rules at all.

    `ping` and not `timeout`: timeout.exe wants a console and fails without one,
    which is exactly the situation a detached script is in.

    Two flags that look alike and are not: CREATE_NO_WINDOW is the one to use.
    DETACHED_PROCESS cannot be combined with it — CreateProcess answers
    "invalid parameter" — and a child outlives its parent on Windows anyway.
    """
    temp = Path(os.environ.get("TEMP") or Path.home() / "AppData" / "Local" / "Temp")
    script = temp / f"{APP_NAME}-{label}-{os.getpid()}.cmd"
    try:
        script.write_text(
            "@echo off\r\n"
            "ping -n 4 127.0.0.1 >nul\r\n"
            + "\r\n".join(commands)
            + "\r\n"
            # How a batch file deletes itself. A plain `del "%~f0"` on the last
            # line does not work — cmd still has the file open, measured here,
            # and the script survives. `(goto) 2>nul` ends the batch context
            # first, and then the del on the same line runs with the file
            # already closed.
            '(goto) 2>nul & del /f /q "%~f0"\r\n',
            encoding="utf-8")
        subprocess.Popen(["cmd", "/c", str(script)], close_fds=True,
                         cwd=str(temp),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
    except OSError:
        return False
    return True


def _sweep_old_copies(temp: Path) -> None:
    """Clear out copies a previous uninstall could not clean up after itself.

    cleanup_self only runs when the uninstaller reaches the end; a crash, or
    somebody closing it from Task Manager, leaves 11 MB behind. One copy is
    nothing, but this is the one place in the program that is guaranteed to run
    just before another one is made, so it is the place to notice.

    Nothing that is still running is touched: unlink on a running image raises,
    and that is the only check needed.
    """
    for old in temp.glob(f"{APP_NAME}Uninstall-*.exe"):
        try:
            old.unlink()
        except OSError:
            pass                    # in use, or not ours to delete


def relaunch_from_temp(arguments: list[str]) -> bool:
    """Copy this exe somewhere else and run it there, so it can delete its folder.

    Only for the frozen Uninstall.exe, and only when it is sitting inside the
    install folder. The copy goes to %LOCALAPPDATA%\\Temp, which on Windows is
    the user's own folder and not a shared one — the thing the rest of this
    project avoids about /tmp does not apply to it.
    """
    if not is_frozen():
        return False
    exe = Path(sys.executable).resolve()
    temp = Path(os.environ.get("TEMP") or Path.home() / "AppData" / "Local" / "Temp")
    try:
        temp.mkdir(parents=True, exist_ok=True)
        _sweep_old_copies(temp)
        # The name matters: setup_main.runs_as_uninstaller() reads it, so this
        # copy uninstalls even though nothing on its command line says to.
        copy = temp / f"{APP_NAME}Uninstall-{os.getpid()}.exe"
        shutil.copyfile(exe, copy)
        subprocess.Popen([str(copy), *arguments], cwd=str(temp), close_fds=True)
    except OSError:
        return False
    return True


def cleanup_self(copy: Path) -> None:
    """Delete the %TEMP% copy of the uninstaller after it has finished.

    Two goes, a few seconds apart. A PyInstaller onefile exe is a parent process
    that waits for a child and then clears its own unpacked folder, so the file
    is still open for a moment after the program has, as far as the person is
    concerned, finished. If both goes fail it is 11 MB in the temp folder, which
    Windows clears out on its own; it is not worth a third mechanism.
    """
    _run_later([f'del /f /q "{copy}"',
                f'if exist "{copy}" (ping -n 5 127.0.0.1 >nul',
                f' del /f /q "{copy}")'], "cleanup")


__all__ = ["Plan", "Removed", "read_plan", "run", "find_install_dir",
           "looks_like_data_dir", "finish_after_exit", "relaunch_from_temp",
           "cleanup_self", "InstallError", "UNINSTALLER_NAME"]
