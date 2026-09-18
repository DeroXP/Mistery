r"""Talking to MisteryUpdate.exe — the only part of Mistery that knows it exists.

Mistery does not update itself. MisteryUpdate.exe does, started by a scheduled
task once an hour, and it only ever replaces a file when Mistery has been shut
for half an hour. So there are exactly two things the app has any business
doing: flipping the switch, and asking "is there anything new?" when somebody
presses the button.

Both go through the exe rather than through a file this module writes, because
one program owns updater.json and it is not this one. That file lives in the
install folder, not in settings.json, since the updater has to work on a PC
where Mistery has never been opened and %APPDATA%\Mistery does not exist —
updater/paths.py's docstring has the whole argument. Reading it here is fine;
writing it from two programs is how settings end up disagreeing with themselves.

The updater is built windowed, so none of this flashes a console. Run from
source there is no MisteryUpdate.exe beside main.py at all: updater_exe()
returns None and Settings leaves the whole block out rather than showing a
switch that does nothing.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from .config import install_dir, subprocess_flags

UPDATER_NAME = "MisteryUpdate.exe"

# The exit codes MisteryUpdate.exe answers with, repeated rather than imported:
# the updater ships as an exe beside the app, not as a package it can import.
# updater/main.py's docstring is the list in full.
OK = 0
UNREACHABLE = 1
REFUSED = 2
SKIPPED = 20


def updater_exe() -> Path | None:
    """MisteryUpdate.exe beside the app, or None when this copy has no updater."""
    exe = install_dir() / UPDATER_NAME
    return exe if exe.is_file() else None


def auto_update_enabled() -> bool:
    """Whether the hourly check is on, read straight out of updater.json.

    Read rather than asked for: `MisteryUpdate.exe --status` would answer too,
    but starting a process to learn one boolean is silly on a screen that shows
    it on every visit. Missing file or missing key means on and only an explicit
    false is off, exactly as updater/paths.py reads it — a truncated file must
    not silently stop updates.
    """
    try:
        values = json.loads((install_dir() / "updater.json").read_text("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return True
    if not isinstance(values, dict):
        return True
    return values.get("auto_update", True) is not False


def set_auto_update(on: bool) -> bool:
    """Turn updates on or off. False if the updater could not be told.

    Fast — the exe writes one small file and exits — but it is still a process
    start, about 200 ms measured on this machine for the frozen build, so the
    caller should not do it in a loop.
    """
    return _run("--enable" if on else "--disable", timeout=30.0) is not None


def last_check() -> dict | None:
    """What the updater wrote the last time it checked, or None if it never has."""
    try:
        answer = json.loads(
            (install_dir() / "update" / "last-check.json").read_text("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return answer if isinstance(answer, dict) else None


def check_now(timeout: float = 120.0) -> dict:
    """Ask now, and wait for the answer. Blocks — call it on a thread.

    The updater is windowed and so has no stdout to read; it writes what it
    found into update\\last-check.json and this reads that back. Its exit code
    comes back as "code" so the caller can tell "up to date" from "the server
    did not answer", which the file alone does not always say.

    A file left by an earlier check is not an answer to this one, so anything
    written before this call started is thrown away.
    """
    started = time.time()
    code = _run("--check-now", timeout=timeout)
    if code is None:
        return {"code": None}
    answer = last_check() or {}
    try:
        fresh = float(answer.get("checked", 0)) >= started - 2
    except (TypeError, ValueError):
        fresh = False
    answer = dict(answer) if fresh else {}
    answer["code"] = code
    return answer


def describe(answer: dict) -> str:
    """One plain sentence about an answer, for the UI.

    Takes either a fresh answer from check_now() or an old one from
    last_check(): the file says enough on its own, and the exit code is only
    consulted where it says something the file does not.
    """
    code = answer.get("code", OK)
    if code is None:
        return "Mistery could not start its updater."
    if answer.get("disabled") or (code == SKIPPED and not answer.get("latest")):
        return "Updates are switched off."
    if answer.get("available"):
        size = answer.get("size")
        how_big = f" ({size / 1e6:.0f} MB)" if isinstance(size, (int, float)) else ""
        return (f"Mistery {answer.get('latest', '')}{how_big} is ready. It installs "
                "itself once Mistery has been closed for half an hour.")
    if answer.get("refused"):
        # The updater deleted whatever it was handed. Worth saying out loud:
        # this is the case where something tried to hand this PC new code.
        return ("An update was offered but failed its checks and was deleted. "
                "update.log beside Mistery.exe says which check.")
    if code == REFUSED:
        # Refused before there was anything to refuse — an updater built with no
        # public key, which can verify nothing and says so on every run.
        return "The updater would not check. update.log beside Mistery.exe says why."
    if answer.get("reachable") is False or code == UNREACHABLE:
        return "Could not reach the update server. It will try again within the hour."
    if answer.get("applied"):
        return f"Updated to {answer['applied']}."
    if answer.get("latest"):
        return f"Up to date — {answer['latest']} is the newest there is."
    return "Mistery has not checked for updates yet."


def _run(*args: str, timeout: float = 30.0) -> int | None:
    """Run MisteryUpdate.exe and return its exit code. None if it could not run.

    Nothing here raises: a Settings screen that throws because an exe is missing
    is worse than one that says it could not check.
    """
    exe = updater_exe()
    if exe is None:
        return None
    try:
        finished = subprocess.run(
            [str(exe), *args], timeout=timeout, capture_output=True,
            creationflags=subprocess_flags())
    except (OSError, subprocess.SubprocessError):
        return None
    return finished.returncode
