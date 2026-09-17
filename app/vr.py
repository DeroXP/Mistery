"""Hand playback off to a VR-capable player on this PC.

Two kinds of target exist, and they work very differently:

  player  A dedicated VR video app (DeoVR, Skybox, Whirligig...). We launch it
          with the file path and it takes over playback.
  mirror  Desktop streaming (Meta Quest Link, Virtual Desktop, SteamVR). These
          do not open files — they put your monitor inside the headset. For
          those, Mistery just plays the film fullscreen itself.

Nothing here assumes a particular install location: Steam libraries are read
from libraryfolders.vdf, and executables are found by searching the app folder
rather than hard-coding names that change between versions.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import settings, subprocess_flags

PLAYER = "player"
MIRROR = "mirror"


# Automatic-choice ranking, lowest wins. A player whose command line we know
# beats desktop mirroring; a player whose command line we are guessing at loses
# to it, so nothing surprising happens by default.
RANK_KNOWN_PLAYER = 0
RANK_MIRROR = 1
RANK_UNVERIFIED_PLAYER = 2


@dataclass
class VrTarget:
    key: str
    name: str
    kind: str
    exe: Path | None = None
    note: str = ""
    rank: int = RANK_KNOWN_PLAYER

    @property
    def available(self) -> bool:
        return self.exe is not None and self.exe.is_file()


# folder name (as it appears in steamapps/common or Program Files) -> label
_PLAYERS = [
    ("deovr", "DeoVR", ["DeoVR"], "Handles flat, 3D and 360 video."),
    ("skybox", "Skybox VR Player", ["SkyboxVR", "Skybox VR Player"],
     "Good with local libraries and 3D."),
    ("whirligig", "Whirligig", ["Whirligig"], "Very flexible projection controls."),
    ("simplevr", "Simple VR Video Player", ["Simple VR Video Player"], ""),
    ("moonvr", "Moon VR Player", ["Moon VR Player", "MoonVRPlayer"], ""),
    ("heresphere", "HereSphere", ["HereSphere"], ""),
    ("bigscreen", "Bigscreen", ["Bigscreen"], "Cinema environments."),
]

# Exact locations, never fuzzy-matched: picking the wrong executable inside one
# of these installs is worse than not finding it at all. "steam:App|rel/path"
# resolves the app folder across Steam libraries.
_MIRRORS = [
    ("questlink", "Meta Horizon (Quest Link)", [
        # Meta renamed the desktop app's folder from Oculus to Meta Horizon.
        r"%ProgramFiles%\Meta Horizon\Support\oculus-client\OculusClient.exe",
        r"%ProgramFiles(x86)%\Meta Horizon\Support\oculus-client\OculusClient.exe",
        r"%ProgramFiles%\Oculus\Support\oculus-client\OculusClient.exe",
        r"%ProgramFiles(x86)%\Oculus\Support\oculus-client\OculusClient.exe",
    ], "Connect the Quest with Link or Air Link, open the desktop panel, then play here."),
    ("virtualdesktop", "Virtual Desktop", [
        r"%ProgramFiles%\Virtual Desktop Streamer\VirtualDesktop.Streamer.exe",
        r"%ProgramFiles(x86)%\Virtual Desktop Streamer\VirtualDesktop.Streamer.exe",
    ], "Start Virtual Desktop on the Quest, then play here."),
    ("steamvr", "SteamVR", [
        r"steam:SteamVR|bin\win64\vrstartup.exe",
        r"steam:SteamVR|bin\win32\vrstartup.exe",
    ], "Launch SteamVR, then play here and watch on the virtual desktop."),
]

# Ships with SteamVR and does open video files, but its command line is not
# documented, so it is offered rather than chosen automatically.
_EXTRA_PLAYERS = [
    ("steamvr_media", "SteamVR Media Player",
     [r"steam:SteamVR|tools\steamvr_media_player\win64\steamvr_media_player.exe"],
     "Part of SteamVR. Command line unverified — try it and see."),
]


def openxr_runtime() -> str | None:
    """Name of the active OpenXR runtime, i.e. which headset stack is in charge.

    Its presence is the clearest signal that VR is actually set up on this PC.
    """
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Khronos\OpenXR\1") as key:
            raw, _ = winreg.QueryValueEx(key, "ActiveRuntime")
    except (OSError, ImportError, FileNotFoundError):
        return None

    manifest = Path(str(raw))
    if not manifest.is_file():
        return None
    try:
        import json

        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
        name = (data.get("runtime") or {}).get("name")
        if name:
            return str(name)
    except (OSError, ValueError):
        pass
    # Fall back to the install folder, e.g. ...\Meta Horizon\Support\... -> Meta Horizon
    for parent in manifest.parents:
        if parent.parent and parent.parent.name.lower() in ("program files", "program files (x86)"):
            return parent.name
    return manifest.stem


def _steam_root() -> Path | None:
    for key in ("SteamPath", "InstallPath"):
        for hive in ("HKCU\\SOFTWARE\\Valve\\Steam", "HKLM\\SOFTWARE\\WOW6432Node\\Valve\\Steam"):
            try:
                import winreg

                root = winreg.HKEY_CURRENT_USER if hive.startswith("HKCU") else winreg.HKEY_LOCAL_MACHINE
                with winreg.OpenKey(root, hive.split("\\", 1)[1]) as handle:
                    value, _ = winreg.QueryValueEx(handle, key)
                    path = Path(str(value))
                    if path.is_dir():
                        return path
            except (OSError, ImportError, FileNotFoundError):
                continue
    for guess in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if Path(guess).is_dir():
            return Path(guess)
    return None


def steam_libraries() -> list[Path]:
    """Every Steam library folder, not just the default one."""
    root = _steam_root()
    if root is None:
        return []
    libraries = [root]
    manifest = root / "steamapps" / "libraryfolders.vdf"
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return libraries
    for match in re.finditer(r'"path"\s+"(.+?)"', text):
        path = Path(match.group(1).replace("\\\\", "\\"))
        if path.is_dir() and path not in libraries:
            libraries.append(path)
    return libraries


def _find_executable(folder: Path, app_name: str) -> Path | None:
    """Pick the launcher inside an app folder without hard-coding its name."""
    if not folder.is_dir():
        return None
    candidates = [p for p in folder.glob("*.exe") if p.is_file()]
    if not candidates:
        candidates = [p for p in folder.rglob("*.exe") if p.is_file()][:60]
    if not candidates:
        return None

    wanted = re.sub(r"[^a-z0-9]", "", app_name.lower())
    scored: list[tuple[int, int, Path]] = []
    for path in candidates:
        stem = re.sub(r"[^a-z0-9]", "", path.stem.lower())
        score = 0
        if stem == wanted:
            score = 3
        elif wanted in stem or stem in wanted:
            score = 2
        if re.search(r"(crash|report|setup|install|unins|updater|service)", stem):
            score -= 5
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        scored.append((score, size, path))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = scored[0]
    return best[2] if best[0] > -5 else None


def _expand(raw: str) -> Path | None:
    """Resolve a location spec to a real file, or None.

    "steam:App|rel\\path.exe" looks App up across the Steam libraries and takes
    the given executable inside it. Everything else is an env-expandable path.
    """
    if raw.startswith("steam:"):
        spec = raw.split(":", 1)[1]
        name, _, relative = spec.partition("|")
        for library in steam_libraries():
            folder = library / "steamapps" / "common" / name
            if not folder.is_dir():
                continue
            if relative:
                candidate = folder / relative
                if candidate.is_file():
                    return candidate
            else:
                found = _find_executable(folder, name)
                if found:
                    return found
        return None
    path = Path(os.path.expandvars(raw))
    return path if path.is_file() else None


_cache: tuple[float, list[VrTarget]] | None = None
_CACHE_TTL = 45.0


def detect(force: bool = False) -> list[VrTarget]:
    """Everything on this PC that could put a film in front of a headset.

    Cached briefly: scanning Steam libraries touches the disk and the UI asks
    every time a detail page opens.
    """
    global _cache
    import time

    if not force and _cache is not None and (time.monotonic() - _cache[0]) < _CACHE_TTL:
        return _cache[1]
    targets = _detect_uncached()
    _cache = (time.monotonic(), targets)
    return targets


def _detect_uncached() -> list[VrTarget]:
    targets: list[VrTarget] = []

    libraries = steam_libraries()
    for key, name, folders, note in _PLAYERS:
        exe = None
        for library in libraries:
            for folder in folders:
                exe = _find_executable(library / "steamapps" / "common" / folder, name)
                if exe:
                    break
            if exe:
                break
        if exe is None:
            for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
                if not base:
                    continue
                for folder in folders:
                    exe = _find_executable(Path(base) / folder, name)
                    if exe:
                        break
                if exe:
                    break
        if exe is not None:
            targets.append(VrTarget(key, name, PLAYER, exe, note))

    for key, name, locations, note in _MIRRORS:
        for raw in locations:
            exe = _expand(raw)
            if exe is not None:
                targets.append(VrTarget(key, name, MIRROR, exe, note, RANK_MIRROR))
                break

    for key, name, locations, note in _EXTRA_PLAYERS:
        for raw in locations:
            exe = _expand(raw)
            if exe is not None:
                targets.append(
                    VrTarget(key, name, PLAYER, exe, note, RANK_UNVERIFIED_PLAYER)
                )
                break

    custom = str(settings.get("vr_custom_path", "") or "").strip()
    if custom and Path(custom).is_file():
        targets.append(VrTarget(
            "custom", Path(custom).stem, PLAYER, Path(custom), "Custom player",
            RANK_KNOWN_PLAYER,
        ))

    return targets


def preferred_target(targets: list[VrTarget] | None = None) -> VrTarget | None:
    """The target to use, honouring the user's choice when it is available."""
    targets = detect() if targets is None else targets
    if not targets:
        return None
    chosen = str(settings.get("vr_target", "auto") or "auto")
    if chosen != "auto":
        for target in targets:
            if target.key == chosen:
                return target
    return sorted(targets, key=lambda t: t.rank)[0]


def _split_template(template: str) -> list[str]:
    """Split an argument template the way the player's own C runtime would.

    That is CommandLineToArgvW's rules: a quote can start or end anywhere in
    a word and is dropped, and backslashes are literal unless they come before
    a quote. shlex in non-posix mode, used before, kept quotes that sat inside
    a word, so --file="{file}", the usual way to write it on Windows, reached
    the player as a file name starting with a quote. The player couldn't open
    it and often didn't exit either, so the launch still looked fine.
    """
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        # Private handles, so these prototypes don't leak into anyone else's
        # use of ctypes.windll.
        shell32 = ctypes.WinDLL("shell32")
        kernel32 = ctypes.WinDLL("kernel32")
        shell32.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        count = ctypes.c_int()
        # A stand-in program name first: the first word is parsed by different
        # rules (quotes only, no backslash handling) and an empty line returns
        # the path of this process.
        argv = shell32.CommandLineToArgvW(f"player {template}", ctypes.byref(count))
        if argv:
            try:
                return [argv[index] for index in range(1, count.value)]
            finally:
                kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))
    try:
        return shlex.split(template)
    except ValueError:
        return template.split()


def build_arguments(target: VrTarget, media_path: str) -> list[str]:
    """Expand the custom argument template.

    The template is split into arguments first and {file} filled in after, so
    a path with spaces, quotes or ampersands stays one argument whatever the
    template looks like; subprocess quotes it again for the player.
    """
    template = str(settings.get("vr_custom_args", "") or "").strip()
    if target.key != "custom" or not template:
        return [media_path]

    # Single quotes mean nothing to a Windows command line, but '{file}' is how
    # the placeholder gets written by anyone used to a Unix shell, and it sent
    # the path with the quotes still on it.
    tokens = _split_template(template.replace("'{file}'", "{file}"))
    arguments = [token.replace("{file}", media_path) for token in tokens]
    if "{file}" not in template:
        arguments.append(media_path)
    return arguments


def launch(target: VrTarget, media_path: str) -> tuple[bool, str]:
    """Start the VR player. Mirror targets are handled by the caller instead."""
    if target.kind == MIRROR:
        return False, f"{target.name} mirrors your desktop — play here and look in the headset."
    if target.exe is None or not target.exe.is_file():
        return False, f"{target.name} is no longer where it was installed."
    if not Path(media_path).is_file():
        return False, "That file is missing."

    command = [str(target.exe), *build_arguments(target, media_path)]
    try:
        process = subprocess.Popen(
            command, cwd=str(target.exe.parent), creationflags=subprocess_flags()
        )
    except OSError as exc:
        return False, f"Could not start {target.name}: {exc}"

    # A player that dies immediately means bad arguments, which is otherwise
    # completely silent. Anything still alive after this is assumed fine.
    deadline = time.monotonic() + 0.8
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            if code != 0:
                return False, (f"{target.name} exited immediately (code {code}) — "
                               "check the arguments in Settings.")
            break
        time.sleep(0.05)

    return True, f"Opening in {target.name}…"


# Guidance shown when nothing suitable is installed. Quest-specific because a
# Quest can go either route and neither is obvious.
NOTHING_INSTALLED = (
    "No VR player or headset streaming app was found on this PC.\n\n"
    "For a Meta Quest there are two routes:\n\n"
    "1.  Desktop streaming — install Meta Quest Link, Virtual Desktop, or "
    "Steam Link. Your monitor appears inside the headset, so you just press "
    "Play in Mistery and watch on the virtual screen. Easiest, and works with "
    "everything including 4K HDR.\n\n"
    "2.  A dedicated VR video player on the PC — DeoVR, Skybox VR or Whirligig "
    "(all on Steam). These give you a proper cinema environment and real 3D or "
    "360 support. Mistery will hand the file straight to them.\n\n"
    "Install either and this button starts working — no setup needed. You can "
    "also point Mistery at any player manually in Settings."
)
