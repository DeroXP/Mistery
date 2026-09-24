"""What installing Mistery actually does, in order, with no window attached.

The window in setup_main.py runs this on a worker thread and draws whatever it
reports. Keeping the two apart is not tidiness for its own sake: it is the only
way the installer gets tested end to end, because a test can call run() and read
what came back, and cannot click a button.

The order matters in one place. The app is copied before mpv and ffmpeg are
fetched, so that a download that fails leaves a folder containing a Mistery that
starts and says it cannot find mpv — which is a thing a person can fix — rather
than an empty folder and an apology.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import arp, fetch, payload, sharing, task
from .setup_common import (APP_EXE_NAME, APP_ID, APP_NAME, CREATE_NO_WINDOW,
                           MARKER_NAME, PUBLISHER, RUNTIME_DIR_NAME,
                           UNINSTALLER_NAME, UPDATER_NAME, UPDATE_DIR_NAME,
                           Cancelled, InstallError, app_is_running, arp_key,
                           data_dir, desktop_dir, exe_in_use, folder_size,
                           free_space, human_size, is_frozen, start_menu_dir,
                           task_name)
from .shortcut import read_shortcut, write_shortcut
from .tool_downloads import DOWNLOAD_BYTES, RUNTIME_BYTES, TOOLS

ABOUT_URL = "https://github.com/DeroXP/Mistery"

# Where each stage sits on the one progress bar, as (start, end) percentages.
# Weighted by bytes moved, measured: 174 MB of app, a 34 MB download unpacking
# to 120 MB of mpv, a 67 MB download unpacking to 134 MB of ffmpeg.
STAGES = {
    "check": (0.0, 0.02),
    "app": (0.02, 0.34),
    "mpv": (0.34, 0.62),
    "ffmpeg": (0.62, 0.95),
    "finish": (0.95, 1.0),
}

# Folders nobody meant to type into the box. Installing into one of these would
# be survivable; uninstalling out of one would not, because the uninstaller
# deletes the folder it was pointed at.
def _forbidden_targets() -> list[Path]:
    names = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData",
             "LOCALAPPDATA", "APPDATA", "USERPROFILE", "PUBLIC", "TEMP")
    out = [data_dir()]
    for name in names:
        value = os.environ.get(name)
        if value:
            out.append(Path(value))
    return out


Report = Callable[[str, float], None]           # (what is happening, 0.0 .. 1.0)


@dataclass
class Options:
    install_dir: Path
    start_menu_shortcut: bool = True
    desktop_shortcut: bool = False
    register_update_task: bool = True
    launch_when_done: bool = False
    keep_archives: bool = False                 # for testing an offline install
    refetch_tools: bool = False


@dataclass
class Result:
    install_dir: Path
    version: str = ""
    app_bytes: int = 0
    runtime_bytes: int = 0
    start_menu: Path | None = None
    desktop: Path | None = None
    update_task: str | None = None
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return self.app_bytes + self.runtime_bytes


# --- before anything is written ---------------------------------------------


def read_marker(install_dir: Path) -> dict:
    try:
        loaded = json.loads((install_dir / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def check_target(folder: Path) -> str:
    """Raise InstallError if this folder must not be installed into.

    Returns a one-line note about what is there now: "" for a new folder,
    otherwise what version is being replaced.
    """
    folder = Path(folder)
    if not folder.is_absolute():
        raise InstallError("Choose a full path, starting with a drive letter.")
    resolved = folder.resolve() if folder.exists() else folder
    if resolved.parent == resolved:
        raise InstallError(
            "Mistery will not install into the root of a drive. Pick a folder "
            "inside it — the uninstaller deletes the folder it is given.")
    for bad in _forbidden_targets():
        try:
            if resolved == bad.resolve():
                raise InstallError(
                    f"{bad} is a Windows folder, not a place to install into. "
                    "Pick a folder of its own, such as "
                    f"{bad / 'Programs' / APP_NAME}.")
        except OSError:
            continue

    if not folder.exists():
        return ""
    if not folder.is_dir():
        raise InstallError(f"{folder} is a file, not a folder.")
    entries = [p for p in folder.iterdir() if p.name.lower() != "desktop.ini"]
    if not entries:
        return ""

    marker = read_marker(folder)
    if marker.get("app") == APP_NAME:
        was = marker.get("version") or "an unknown version"
        return f"Replacing the Mistery {was} already in this folder."
    if (folder / APP_EXE_NAME).is_file():
        return "Replacing the Mistery already in this folder."

    shown = ", ".join(sorted(p.name for p in entries)[:4])
    raise InstallError(
        f"{folder} already has something else in it ({shown}"
        f"{', …' if len(entries) > 4 else ''}).\n\n"
        "Mistery only installs into an empty folder or over an older Mistery, "
        "because uninstalling deletes the whole folder. Choose another one.")


def space_needed(install_dir: Path, reuse_tools: bool = False) -> tuple[int, int | None]:
    """(bytes needed, bytes free) for the window to show before it starts."""
    needed = payload.unpacked_size()
    if not reuse_tools:
        needed += RUNTIME_BYTES + DOWNLOAD_BYTES       # the archive is deleted after
    return needed, free_space(install_dir)


# --- doing it ---------------------------------------------------------------


def _staged(report: Report, stage: str) -> fetch.Progress:
    """Turn a stage's (done, total) into a place on the one progress bar."""
    start, end = STAGES[stage]

    def inner(what: str, done: int, total: int) -> None:
        fraction = (done / total) if total else 0.0
        report(what, start + (end - start) * min(1.0, max(0.0, fraction)))

    return inner


def _clear_old_install(folder: Path, report: Report) -> None:
    """Remove the previous version's files, keeping the expensive ones.

    runtime\\ stays because it is 254 MB that a reinstall has no reason to
    download again, and updater.json stays because turning updates off is the
    person's decision, not the installer's. Everything else goes: an upgrade
    that only overwrites leaves the old version's DLLs behind, and PyInstaller
    bundles are exactly the kind of thing that keeps working while quietly
    loading a file from two versions ago.
    """
    keep = {RUNTIME_DIR_NAME.lower(), UPDATE_DIR_NAME.lower(), "updater.json"}
    for entry in folder.iterdir():
        if entry.name.lower() in keep:
            continue
        report(f"Removing the old {entry.name}", STAGES["app"][0])
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as error:
            raise InstallError(
                f"Could not remove {entry.name} from the install folder: "
                f"{error}\n\nIf Mistery is open, close it and run the "
                "installer again.") from error


def _shortcut(link: Path, app_exe: Path, folder: Path, result: Result) -> Path | None:
    """Write one .lnk, and turn a refusal into a note instead of a dead install.

    The shell writes the link, and it can say no: it reads the target as a PE
    while saving, so a Mistery.exe that is not a valid executable comes back as
    a bare E_FAIL. Everything else about the install is already correct at this
    point, and a missing Start Menu entry is a thing a person can see and fix.
    """
    try:
        return write_shortcut(link, app_exe, working_dir=folder, icon=app_exe,
                              description="Mistery — movies, shows and music")
    except (InstallError, OSError) as error:
        result.notes.append(f"could not write {link}: {error}")
        return None


def _remove_our_shortcut(link: Path, folder: Path, result: Result) -> None:
    """Take away a shortcut the person has just said they do not want.

    Installing again with the Desktop box unticked used to leave the shortcut
    from last time sitting there, pointing at this install, and the uninstaller
    then had no record of it and left it behind for good. An install finishes
    with the machine in the state the boxes describe, including the boxes that
    are empty. Only ever a link that points into this install folder: a
    Mistery.lnk somebody made themselves, to something else, is theirs.
    """
    if not link.is_file():
        return
    try:
        target = read_shortcut(link).get("target", "")
    except (InstallError, OSError):
        return
    try:
        if not target or Path(target).parent.resolve() != folder.resolve():
            return
    except OSError:
        return
    try:
        link.unlink()
        result.notes.append(f"removed the shortcut at {link}")
    except OSError:
        pass


def _tools_already_there(folder: Path, refetch: bool) -> dict[str, str]:
    """Which pinned tools are already unpacked in runtime\\, by archive hash.

    The marker records the SHA-256 of the archive each tool came out of, so a
    reinstall of the same build skips 101 MB of download, and a build that
    changed the pin does not.
    """
    if refetch:
        return {}
    installed = read_marker(folder).get("tools")
    if not isinstance(installed, dict):
        return {}
    runtime = folder / RUNTIME_DIR_NAME
    out = {}
    for tool in TOOLS:
        if installed.get(tool.name) != tool.sha256:
            continue
        if all((runtime / name).is_file() for name in tool.wanted):
            out[tool.name] = tool.sha256
    return out


def run(options: Options, report: Report, cancel=None) -> Result:
    """Install Mistery. Raises InstallError with something readable, or Cancelled.

    Cancelling in the middle takes the folder back out again when the installer
    is the one that made it. A folder that already held an older Mistery is left
    alone: half an upgrade is still something the person can reinstall over,
    while deleting it would take their working copy with it.
    """
    existed_before = Path(options.install_dir).exists()
    paused: list[bool] = []
    try:
        return _run(options, report, cancel, paused)
    except Cancelled:
        if not existed_before:
            shutil.rmtree(options.install_dir, ignore_errors=True)
        raise
    finally:
        # Whatever happened after it stepped aside, friends are served again:
        # from the new files, or from the old ones a failed upgrade left.
        if paused:
            sharing.start_sharer(Path(options.install_dir))


def _run(options: Options, report: Report, cancel=None, paused: list[bool] | None = None) -> Result:
    started = time.monotonic()
    folder = Path(options.install_dir)
    result = Result(install_dir=folder)

    report("Checking the folder", 0.0)
    existing = check_target(folder)
    if existing:
        result.notes.append(existing)
        # The Mistery serving friends with no window runs this folder's
        # Mistery.exe and never quits by itself. It steps aside for the files
        # to be replaced, as it does for the updater, and run() starts it again.
        if sharing.sharer_pid(folder) is not None:
            report("Pausing sharing with friends", 0.0)
            stopped = sharing.stop_sharer(folder)
            if stopped is False:
                raise InstallError(sharing.STOP_FAILED)
            if stopped and paused is not None:
                paused.append(True)
                result.notes.append("sharing with friends paused for the install, "
                                    "and started again after it")
        # Only when there is something here to overwrite. A Mistery running out
        # of some other folder is not this install's business, and refusing over
        # it would make a second copy impossible to install.
        if exe_in_use(folder / APP_EXE_NAME):
            raise InstallError(
                f"Mistery is running from {folder}. Close it (check the system "
                "tray) and start the installer again — Windows will not let a "
                "running program be replaced.")
        if app_is_running():
            result.notes.append(
                "a Mistery is open somewhere else; this install did not touch it")

    version_text = payload.version() or "0.0.0"
    result.version = version_text
    reuse = _tools_already_there(folder, options.refetch_tools)
    needed, free = space_needed(folder, reuse_tools=len(reuse) == len(TOOLS))
    if free is not None and free < needed:
        raise InstallError(
            f"Not enough room. Mistery needs about {human_size(needed)} and "
            f"{folder.anchor or 'the drive'} has {human_size(free)} free.")

    folder.mkdir(parents=True, exist_ok=True)
    if existing:
        _clear_old_install(folder, report)

    staging = folder / UPDATE_DIR_NAME
    runtime = folder / RUNTIME_DIR_NAME
    staging.mkdir(exist_ok=True)
    runtime.mkdir(exist_ok=True)

    report("Copying Mistery", STAGES["app"][0])
    result.app_bytes = payload.extract(folder, _staged(report, "app"), cancel)

    tool_hashes = dict(reuse)
    for tool in TOOLS:
        if tool.name in reuse:
            result.notes.append(f"{tool.name} was already installed — not downloaded again.")
            continue
        report(f"Downloading {tool.name}", STAGES[tool.name][0])
        written = fetch.install_tool(tool, runtime, staging, _staged(report, tool.name),
                                     cancel, keep_archive=options.keep_archives)
        tool_hashes[tool.name] = tool.sha256
        result.notes.append(
            f"{tool.name}: {len(written)} files, {human_size(fetch.measure(written))}")
    result.runtime_bytes = folder_size(runtime)

    report("Making the shortcuts", STAGES["finish"][0])
    app_exe = folder / APP_EXE_NAME
    if not app_exe.is_file():
        result.notes.append(
            f"{APP_EXE_NAME} is not in the payload, so no shortcut was made.")
    else:
        start_link = start_menu_dir() / f"{APP_NAME}.lnk"
        desktop_link = desktop_dir() / f"{APP_NAME}.lnk"
        if options.start_menu_shortcut:
            result.start_menu = _shortcut(start_link, app_exe, folder, result)
        else:
            _remove_our_shortcut(start_link, folder, result)
        if options.desktop_shortcut:
            result.desktop = _shortcut(desktop_link, app_exe, folder, result)
        else:
            _remove_our_shortcut(desktop_link, folder, result)

    uninstaller = folder / UNINSTALLER_NAME
    if is_frozen():
        # The uninstaller is this same exe under another name, minus the copy of
        # Mistery it carries: one program that has to agree with itself rather
        # than two that have to agree with each other.
        payload.write_uninstaller(uninstaller)
    else:
        result.notes.append(
            "running unfrozen, so no Uninstall.exe was written "
            "(uninstall_steps.run removes the install directly)")

    if options.register_update_task:
        report("Registering the hourly update check", STAGES["finish"][0] + 0.02)
        updater = folder / UPDATER_NAME
        if not updater.is_file():
            result.notes.append(
                f"{UPDATER_NAME} is not in the payload, so the hourly update "
                "check was not registered.")
        else:
            start = time.strftime("%Y-%m-%dT%H:%M:%S",
                                  time.localtime(time.time() + 3600))
            task.register(task_name(), updater, start, staging / "task.xml")
            result.update_task = task_name()

    report("Writing the Add/Remove Programs entry", STAGES["finish"][0] + 0.04)
    total = folder_size(folder)
    arp.write(arp_key(), display_name=APP_NAME, version=version_text,
              publisher=PUBLISHER, install_dir=folder,
              uninstaller=uninstaller, icon=app_exe,
              estimated_bytes=total, about_url=ABOUT_URL)

    (folder / MARKER_NAME).write_text(json.dumps({
        "app": APP_NAME,
        "version": version_text,
        "installed": time.time(),
        "install_dir": str(folder),
        "data_dir": str(data_dir()),
        "start_menu_shortcut": str(result.start_menu) if result.start_menu else None,
        "desktop_shortcut": str(result.desktop) if result.desktop else None,
        "update_task": result.update_task,
        "arp_key": arp_key(),
        "app_id": APP_ID,
        "tools": tool_hashes,
    }, indent=2), encoding="utf-8")

    # The updater needs its manifest URL without having to guess, and it must
    # work on a PC where Mistery has never been opened and %APPDATA%\Mistery
    # does not exist — so it lives here, in the install folder, not in the app's
    # settings.json. Written only if the updater did not bring its own.
    updater_settings = folder / "updater.json"
    if not updater_settings.exists():
        updater_settings.write_text(json.dumps({"auto_update": True}, indent=2),
                                    encoding="utf-8")

    # 101 MB of verified archive has done its job; update\ goes back to being
    # the empty staging folder the updater expects.
    for leftover in staging.glob("*"):
        if leftover.name != "last-check.json" and not options.keep_archives:
            try:
                shutil.rmtree(leftover) if leftover.is_dir() else leftover.unlink()
            except OSError:
                pass

    report("Done", 1.0)
    result.seconds = time.monotonic() - started
    return result


def launch(install_dir: Path) -> bool:
    """Start the installed Mistery and let go of it."""
    exe = Path(install_dir) / APP_EXE_NAME
    if not exe.is_file():
        return False
    try:
        subprocess.Popen([str(exe)], cwd=str(install_dir), close_fds=True,
                         creationflags=CREATE_NO_WINDOW)
    except OSError:
        return False
    return True


__all__ = ["Options", "Result", "run", "check_target", "space_needed",
           "read_marker", "launch", "Cancelled", "InstallError"]
