"""The Add/Remove Programs entry, under HKCU.

HKCU and not HKLM because this is a per-user install: it went into
%LOCALAPPDATA%\\Programs, it needed no administrator, and it must need no
administrator to remove either. Windows reads
HKCU\\...\\CurrentVersion\\Uninstall exactly like the machine-wide one, and
Settings > Apps shows what it finds there.

EstimatedSize is in kilobytes and is the only field Windows will quietly get
wrong if you leave it out: the app folder plus 254 MB of mpv and ffmpeg is what
uninstalling actually gives back, and an entry that claims nothing looks like
something that installed nothing.
"""

from __future__ import annotations

import time
import winreg
from pathlib import Path


def write(key_path: str, *, display_name: str, version: str, publisher: str,
          install_dir: Path, uninstaller: Path, icon: Path,
          estimated_bytes: int, about_url: str = "") -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, version)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, publisher)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(install_dir))
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, f"{icon},0")
        # Quoted: the path has a space in it on any machine whose user name has
        # one, and an unquoted UninstallString is how you uninstall C:\Users\John.
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                          f'"{uninstaller}"')
        winreg.SetValueEx(key, "QuietUninstallString", 0, winreg.REG_SZ,
                          f'"{uninstaller}" --quiet')
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD,
                          max(1, estimated_bytes // 1024))
        winreg.SetValueEx(key, "InstallDate", 0, winreg.REG_SZ,
                          time.strftime("%Y%m%d"))
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        if about_url:
            winreg.SetValueEx(key, "URLInfoAbout", 0, winreg.REG_SZ, about_url)


def read(key_path: str) -> dict[str, object]:
    """Everything under the key, or {} if it is not there."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_READ) as key:
            out: dict[str, object] = {}
            index = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    return out
                out[name] = value
                index += 1
    except FileNotFoundError:
        return {}


def remove(key_path: str) -> bool:
    """Delete the key. True if it is gone afterwards."""
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return not read(key_path)


def prune_empty_parents(key_path: str, stop_at: str = "Software") -> None:
    """Walk back up deleting parents that are now empty.

    Only matters for the test runs, which put their entry under
    Software\\Mistery\\SetupTest\\... rather than Windows' own list: without this
    a test would leave two empty keys behind in the owner's registry.
    """
    parts = key_path.split("\\")
    while len(parts) > 1 and parts[-1].lower() != stop_at.lower():
        parts.pop()
        parent = "\\".join(parts)
        if parent.lower() == stop_at.lower():
            return
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, parent) as key:
                count = winreg.QueryInfoKey(key)[:2]
            if count != (0, 0):
                return
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, parent)
        except OSError:
            return
