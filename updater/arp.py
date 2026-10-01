r"""Keeping what the installer wrote down true after an update.

The installer records the version twice, once each, when Mistery is installed:
in the Add/Remove Programs entry (packaging/installer/arp.py), and in
mistery-install.json in the install folder. Until this file nothing ever wrote
either again. Read on the PC this was written on: installed as 1.4.0, updated
twice by this program, Mistery.exe at 1.6.0, and the entry, which is what
Settings > Apps shows, still at 1.4.0. Every file had been replaced, and the one
line a person can read in Windows itself said nothing had happened. The marker
said 1.4.0 as well, and the uninstaller's window takes its title from that:
"Remove Mistery 1.4.0".

So both are made to say what version.txt says: after an update goes in, and at
the start of every run. Every run, because the run that applies an update is
the *old* MisteryUpdate.exe (the new one waits as .new for Mistery to start, see
apply.py), so an install updated by a build from before this file existed is
put right by the first run of a newer one: the hourly run after Mistery was next
started. When everything already agrees, which is nearly every run, that costs
two reads of a small file and two registry values.

**The entry in Windows' list.** The one the installer recorded: the marker
names the registry key ("arp_key"), and it is how the uninstaller finds the
entry too. No marker means the installer never ran here (a copy unzipped by
hand, a source checkout, a test's throwaway folder): there is no entry that is
this folder's, and none is looked for. The entry also has to say it is this
folder's (InstallLocation) before it is touched, so a second Mistery somewhere
else cannot rewrite the first one's line. And the key is only ever opened, never
created: an entry somebody removed stays removed. Two values are written and
nothing else: DisplayVersion, and EstimatedSize, the other number Settings shows
on that line, since an update changes how much disk uninstalling gives back.

**The marker.** Its "version" is what the uninstaller's window is titled from,
and Uninstall.exe is the one program in the folder no update replaces: it is the
setup exe under another name, written when Mistery was installed. A newer
uninstaller reads version.txt instead (setup_common.installed_version), but the
ones already on people's PCs read the marker, so the marker is what has to
change. Only that one value: the uninstaller also reads what to remove out of
this file, so the rest goes back exactly as it was read, through a temporary
file, and a crash halfway cannot leave half a marker.

Its own few lines of winreg and json rather than the installer's modules: the
installer is a build-time program and the updater ships without it (paths.py
says why for the names they share). packaging/test_setup.py installs for real
and then asks this module to find and refresh what that install wrote, so the
two cannot drift apart without a test saying so.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import paths

# packaging/installer/setup_common.py has the same two.
MARKER_NAME = "mistery-install.json"
ARP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Mistery"

# Names an update leaves in the folder for an hour or until Mistery starts, not
# for good: a file renamed out of the way, a download cut short, the new
# updater in waiting (which takes the place of one already counted).
_PASSING = (".old", ".part", ".new")


def _marker(install: Path) -> dict | None:
    """mistery-install.json as the installer wrote it, or None when the
    installer never ran here or the file is not one of its own."""
    try:
        marker = json.loads((install / MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(marker, dict) or marker.get("app") != paths.APP_NAME:
        return None
    return marker


def entry_key(install: Path) -> str | None:
    """The registry key the installer wrote this folder's entry under, or None
    when the installer never ran here.

    A marker with no key in it means Windows' own list, which is what the
    uninstaller takes it to mean as well (uninstall_steps.read_plan).
    """
    marker = _marker(install)
    if marker is None:
        return None
    recorded = marker.get("arp_key")
    return recorded.strip() if isinstance(recorded, str) and recorded.strip() else ARP_KEY


def installed_bytes(install: Path) -> int:
    """How much disk uninstalling would give back: the folder, as the installer
    counts it (setup_common.folder_size), less what is only passing through.

    update\\ is staging and is emptied after every run. The rest are the files
    an update leaves beside the ones it replaced until the next sweep.
    """
    staging = os.path.normcase(str(install / "update"))
    total = 0
    for folder, folders, files in os.walk(install):
        folders[:] = [name for name in folders
                      if os.path.normcase(os.path.join(folder, name)) != staging]
        for name in files:
            if name.lower().endswith(_PASSING):
                continue
            try:
                total += os.stat(os.path.join(folder, name)).st_size
            except OSError:
                pass                # a file that vanished mid-walk is not an error
    return total


def refresh(install: Path, version: str | None) -> str | None:
    """Make this install's entry in Windows' list say `version`. Never raises.

    Returns what the entry said before, when this call changed it ("" if it
    said nothing), and None when there was nothing to do or nothing that could
    be done: no entry, not this folder's, already right, or Windows said no.
    The update that led here has happened either way, and a stale line in
    Settings is not a reason to call it a failure.
    """
    if sys.platform != "win32" or not version:
        return None
    key_path = entry_key(install)
    if key_path is None:
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            if not _same_folder(_value(key, "InstallLocation"), install):
                return None
            shown = _value(key, "DisplayVersion")
            if shown == version:
                return None
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, version)
            size = installed_bytes(install)
            if size:
                # In kilobytes, and a DWORD: the installer's arithmetic.
                winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD,
                                  min(0xFFFFFFFF, max(1, size // 1024)))
            return shown if isinstance(shown, str) else ""
    except (OSError, ValueError, OverflowError):
        return None


def refresh_marker(install: Path, version: str | None) -> str | None:
    """Make mistery-install.json say `version` as well. Never raises.

    Returns what it said before, when this call changed it ("" if it said
    nothing), and None when there was nothing to do or the file could not be
    replaced: no marker, not the installer's, already right, or Windows said
    no. The file is replaced whole or not at all.
    """
    if not version:
        return None
    marker = _marker(install)
    if marker is None:
        return None
    was = marker.get("version")
    if was == version:
        return None
    marker["version"] = version
    target = install / MARKER_NAME
    temporary = target.with_name(target.name + ".new")
    try:
        # indent=2 and nothing after it: the installer's own writing of it.
        temporary.write_text(json.dumps(marker, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    except (OSError, ValueError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return was if isinstance(was, str) else ""


def _value(key, name: str) -> object:
    """One value under an open key, or None if it is not there."""
    import winreg

    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _same_folder(recorded: object, install: Path) -> bool:
    """Whether the folder an entry names is this one.

    By what the two paths lead to, not by how they are spelt: the installer
    wrote the path as it was typed or defaulted, and install_dir() is resolved,
    so case, a short 8.3 name or a junction on the way can each make two
    spellings of one folder. A folder that cannot be looked at is not this one.
    """
    if not isinstance(recorded, str) or not recorded.strip():
        return False
    try:
        return os.path.samefile(recorded, install)
    except (OSError, ValueError):
        return False
