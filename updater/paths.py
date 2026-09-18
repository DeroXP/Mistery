r"""Where the updater's world is, and the log that records what it did.

Two folders matter and they are not the same folder:

  the install folder   %LOCALAPPDATA%\Programs\Mistery — the exe, version.txt,
                       _internal\, runtime\. This is the only place the updater
                       writes. It is per-user, so replacing files there needs no
                       administrator prompt, which is what lets an update happen
                       at 4am with nobody there to click one.

  the data folder      %APPDATA%\Mistery — library.db, settings.json (the only
                       copy of the user's TMDB key), 237 MB of artwork on the
                       machine this was measured on. The updater reads two small
                       files out of it — app.lock and last-run.json — and writes
                       nothing, ever, and does not create it if it is missing.

The updater's own settings live in updater.json in the *install* folder rather
than in the app's settings.json, on purpose. The updater has to work on a PC
where Mistery has never been opened and there is no data folder at all, and it
must never be the thing that creates one: app/config.py builds a Settings object
on import and that writes settings.json, which is also why nothing here imports
app.config and why the two small readers below are copies rather than calls.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .keys import DEFAULT_MANIFEST_URL

APP_NAME = "Mistery"

# The three names the installer, the updater and the app all have to agree on.
# packaging/installer/setup_common.py has the same three; they are repeated
# rather than shared because the installer is a build-time program and the
# updater ships without it.
APP_EXE_NAME = "Mistery.exe"
UPDATER_NAME = "MisteryUpdate.exe"
VERSION_FILE_NAME = "version.txt"

# Keep update.log under 64 KB with one old copy beside it. A scheduled run that
# finds nothing to do writes one line of about 70 bytes; at one run an hour that
# is roughly two months per file, and the pair can never cost more than 128 KB.
LOG_LIMIT = 64 * 1024


def install_dir() -> Path:
    """The folder the updater is sitting in — the one it will rewrite.

    Frozen, that is the folder MisteryUpdate.exe is in, not sys._MEIPASS: the
    files being replaced sit beside the exe. From source it is the repository
    root. MISTERY_INSTALL_DIR overrides both, which is how packaging's tests
    point a real frozen updater at a throwaway install under the scratchpad
    instead of at the one the person running the tests uses.
    """
    override = os.environ.get("MISTERY_INSTALL_DIR", "").strip()
    if override:
        return Path(override)
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path | None:
    """Mistery's data folder, or None when there is not one.

    Copied from app/config.py data_dir() with the mkdir taken out, and copied
    rather than imported because importing app.config constructs Settings(),
    which writes settings.json when it is absent. An updater that quietly
    created %APPDATA%\\Mistery on a PC where Mistery has never run would leave
    behind a folder the app afterwards treats as an existing install.
    """
    override = os.environ.get("MISTERY_DATA_DIR", "").strip()
    if override:
        root = Path(override)
    else:
        base = os.environ.get("APPDATA")
        root = Path(base) / APP_NAME if base else Path.home() / f".{APP_NAME.lower()}"
    return root if root.is_dir() else None


def update_dir() -> Path:
    """The staging folder, inside the install folder — never %TEMP%.

    %TEMP% is writable by everything running as this user, so a download parked
    there can be swapped between the moment its hash is checked and the moment
    it is unpacked. Here, the only thing that can reach the file is something
    that could already rewrite the app itself.
    """
    return install_dir() / "update"


def unpack_dir() -> Path:
    """Where a verified zip is unpacked before any of it is moved into place."""
    return update_dir() / "unpack"


def version_file() -> Path:
    return install_dir() / VERSION_FILE_NAME


def installed_version() -> str | None:
    """What is installed, from version.txt: one line, no Python import needed."""
    try:
        lines = version_file().read_text(encoding="utf-8").strip().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    return lines[0].strip() if lines else None


def settings_file() -> Path:
    return install_dir() / "updater.json"


def settings() -> dict:
    """updater.json, or an empty dict. Never a reason to fail a run."""
    try:
        loaded = json.loads(settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def auto_update_enabled() -> bool:
    """The off switch — the only one. app/config.py DEFAULTS has no copy of it.

    A missing file or a missing key means on, and only an explicit false is off,
    so a truncated or half-written updater.json cannot silently stop updates.
    app/updates.py reads this same file the same way to draw the checkbox.
    """
    return settings().get("auto_update", True) is not False


def set_auto_update(enabled: bool) -> None:
    """Turn updates on or off, keeping whatever else is in the file.

    This is what the app's Settings screen reaches, through
    `MisteryUpdate.exe --enable` / `--disable` in app/updates.py, rather than
    the app writing auto_update into its own settings.json where the updater
    would have to go looking for it — see the module docstring for why the
    updater stays out of the data folder entirely.
    """
    values = settings()
    values["auto_update"] = bool(enabled)
    write_settings(values)


def write_settings(values: dict) -> None:
    """Replace updater.json through a temporary file, so a crash mid-write
    cannot leave half a JSON document behind (that is how settings.json used to
    die, and an unreadable updater.json would disable the off switch)."""
    target = settings_file()
    temporary = target.with_name(target.name + ".new")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(values, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def manifest_url() -> str:
    """Where to ask about updates: updater.json first, then the built-in URL.

    Not a security decision — whatever comes back has to carry a signature made
    by the key baked into this exe — so the installer is free to point it at the
    website, and someone testing is free to point it somewhere else.
    """
    configured = settings().get("manifest_url")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return DEFAULT_MANIFEST_URL


# --- the log ---------------------------------------------------------------


def log_path() -> Path:
    return install_dir() / "update.log"


def log(message: str) -> None:
    """One line, with the time, into update.log — and onto stderr when there is
    one. In the frozen windowed build there is not: no console, no stderr, which
    is why the log file is the only account of what happened at 4am."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n"
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size + len(line) > LOG_LIMIT:
            os.replace(path, path.with_name(path.name + ".1"))
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass                 # a full disk is not a reason to stop checking
    stream = getattr(sys, "stderr", None)
    if stream is not None:
        try:
            stream.write(line)
            stream.flush()
        except (OSError, ValueError):
            pass


def write_result(result: dict) -> None:
    """The last check, as JSON, for the app to read and show.

    update\\last-check.json is how a "check for updates" button in Mistery gets
    its answer: the frozen updater is built windowed so that the hourly task
    never flashes a console at anyone, which also means it has no stdout for the
    app to read. It starts the updater and reads this file.
    """
    result = dict(result)
    result.setdefault("checked", time.time())
    try:
        folder = update_dir()
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / "last-check.json.new"
        temporary.write_text(json.dumps(result, indent=2), encoding="utf-8")
        os.replace(temporary, folder / "last-check.json")
    except OSError:
        pass
