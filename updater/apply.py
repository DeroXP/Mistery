r"""Unpacking a verified zip, and putting the files where the app is.

Two jobs, and they are kept apart on purpose. Everything is unpacked into
update\unpack first and checked as it goes; only when the whole archive is out
and nothing objected does a single file move into the install folder. A zip
that turns out to be malformed halfway through then costs a deleted folder, not
a half-replaced app.

**Unpacking defensively.** The zip is signed, so by the time it gets here it
came from the release key — but "signed" is a statement about who built it, not
about what is in it, and a release built from a poisoned dependency would be
signed too. So: no absolute paths, no drive letters, no `..`, no backslashes,
no symlinks, no Windows device names, a ceiling on each file, a ceiling on the
total, and every destination checked to be inside update\unpack after the path
is resolved. A zip that breaks any of those is deleted and nothing is applied.

**Moving files in.** Windows will not let you write over a file that is open,
but it will happily *rename* one — including a running .exe, because a process
holds its image by handle and not by name. So each target is renamed out of the
way to <name>.old and the new file is moved into the name it just left. That is
what makes an update work with no reboot and no "close Mistery first" dialog.
The .old files are swept at the start of the next run, an hour later, by which
time whatever was holding them has long since exited.

os.replace *is* MoveFileExW(..., MOVEFILE_REPLACE_EXISTING) on Windows —
CPython calls it directly in Modules/posixmodule.c — so it is used here rather
than a hand-rolled ctypes call that would do the same thing less readably.

MisteryUpdate.exe is the exception: this program cannot rename itself out from
under its own running image and then move a new one in — it can, actually, but
the half-second where the scheduled task's target does not exist is not worth
the cleverness. It is staged as MisteryUpdate.exe.new and Mistery.exe swaps it
in the next time it starts, before it does anything else.
"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

from .manifest import Refused
from . import paths

# The frozen app is 174 MB in 570 files here, measured. These are ceilings, not
# expectations: they exist so a malformed or hostile archive cannot fill a disk
# before anyone notices.
MAX_UNPACKED_BYTES = 2_000_000_000
MAX_FILE_BYTES = 500_000_000
MAX_ENTRIES = 20_000

# Compressed Qt DLLs do about 2.5:1. Anything claiming to expand by more than
# a hundred times is a zip bomb, not a build.
MAX_EXPANSION = 100

_RESERVED = {"con", "prn", "aux", "nul", "clock$"} | {
    f"{stem}{n}" for stem in ("com", "lpt") for n in range(1, 10)}

_SYMLINK_MODE = 0xA000          # S_IFLNK in the high half of external_attr


def sweep(install: Path) -> int:
    """Delete last run's leftovers. Returns how many files went.

    Called at the start of every run, before anything else, which is the only
    moment they are certain to be closed: a .old is the image some process was
    running an hour ago. One that is still locked is left for the next run
    rather than fought over.
    """
    gone = 0
    for path in list(install.rglob("*.old")) + list(install.rglob("*.part")):
        try:
            if path.is_file():
                path.unlink()
                gone += 1
        except OSError:
            pass                # still in use; next hour
    stale = paths.unpack_dir()
    if stale.is_dir():
        shutil.rmtree(stale, ignore_errors=True)
    return gone


# --- unpacking ---------------------------------------------------------------


def _check_name(name: str) -> tuple[str, ...]:
    """The entry's path as components, or Refused.

    Zip stores forward slashes. A backslash in a name is either a broken writer
    or someone hoping the reader treats it as a separator on Windows — which
    Python's own os.path does. Refusing is simpler than normalising.
    """
    if not name or len(name) > 260:
        raise Refused(f"zip entry name is empty or absurdly long: {name[:80]!r}")
    if "\\" in name:
        raise Refused(f"zip entry {name!r} contains a backslash")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise Refused(f"zip entry {name!r} is an absolute path")
    if "\x00" in name or any(ord(c) < 32 for c in name):
        raise Refused(f"zip entry {name!r} contains a control character")

    parts = tuple(p for p in name.split("/") if p != "")
    if not parts:
        raise Refused(f"zip entry {name!r} has no file name")
    for part in parts:
        if part in (".", ".."):
            raise Refused(f"zip entry {name!r} walks out of the folder with {part!r}")
        if ":" in part:
            raise Refused(f"zip entry {name!r} names an alternate data stream")
        if part != part.strip() or part.endswith("."):
            # Windows silently drops trailing dots and spaces, so "evil.exe ."
            # and "evil.exe" would be the same file with different names.
            raise Refused(f"zip entry {name!r} has a trailing dot or space")
        if part.split(".")[0].lower() in _RESERVED:
            raise Refused(f"zip entry {name!r} is a Windows device name")
    return parts


def unpack(archive: Path, into: Path) -> tuple[int, int]:
    """Unpack a verified zip into an empty folder. Returns (files, bytes).

    Raises Refused for anything that fails a check, having deleted whatever had
    already been written — a half-unpacked folder is not something a later run
    should be able to mistake for a good one.
    """
    if into.exists():
        shutil.rmtree(into, ignore_errors=True)
    into.mkdir(parents=True, exist_ok=True)
    root = into.resolve()
    compressed = archive.stat().st_size

    files = written = 0
    try:
        with zipfile.ZipFile(archive) as zf:
            entries = zf.infolist()
            if len(entries) > MAX_ENTRIES:
                raise Refused(f"the zip holds {len(entries)} entries; the ceiling is "
                              f"{MAX_ENTRIES}")

            # Everything the central directory claims, before a byte is written.
            declared = 0
            for info in entries:
                parts = _check_name(info.filename)
                mode = (info.external_attr >> 16) & 0xF000
                if mode == _SYMLINK_MODE:
                    raise Refused(f"zip entry {info.filename!r} is a symlink")
                if info.is_dir():
                    continue
                if info.file_size > MAX_FILE_BYTES:
                    raise Refused(f"zip entry {info.filename!r} says it is "
                                  f"{info.file_size / 1e6:.0f} MB")
                declared += info.file_size
                target = (root / Path(*parts)).resolve()
                if target != root and root not in target.parents:
                    raise Refused(f"zip entry {info.filename!r} lands outside "
                                  f"the unpack folder")
            if declared > MAX_UNPACKED_BYTES:
                raise Refused(f"the zip unpacks to {declared / 1e6:.0f} MB; the "
                              f"ceiling is {MAX_UNPACKED_BYTES / 1e6:.0f} MB")
            if compressed and declared > compressed * MAX_EXPANSION:
                raise Refused(f"{compressed / 1e6:.1f} MB of zip claims to unpack "
                              f"to {declared / 1e6:.0f} MB; that is a zip bomb")

            # Now write, still counting: the central directory is a claim, and
            # the only number that matters is what actually comes out.
            for info in entries:
                parts = _check_name(info.filename)
                target = root / Path(*parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as source, open(target, "wb") as out:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > MAX_UNPACKED_BYTES:
                            raise Refused("the zip keeps unpacking past "
                                          f"{MAX_UNPACKED_BYTES / 1e6:.0f} MB")
                        out.write(chunk)
                files += 1
    except zipfile.BadZipFile as exc:
        shutil.rmtree(into, ignore_errors=True)
        raise Refused(f"the download is not a readable zip: {exc}") from None
    except Refused:
        shutil.rmtree(into, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(into, ignore_errors=True)
        raise Refused(f"could not unpack the download: {exc}") from None

    if files == 0:
        shutil.rmtree(into, ignore_errors=True)
        raise Refused("the zip is empty")
    return files, written


# --- moving into place -------------------------------------------------------


class Applied:
    """What a successful apply did, for the log and for last-check.json."""

    def __init__(self) -> None:
        self.replaced = 0
        self.added = 0
        self.staged_updater = False


def move_into_place(unpacked: Path, install: Path) -> Applied:
    """Move every unpacked file into the install folder, or put it all back.

    Files the new build no longer has are left alone rather than deleted. A
    stale DLL in _internal\\ costs disk space; deleting a file the new build
    turns out to need costs an app that does not start, and the updater is the
    program nobody is watching when it runs.
    """
    result = Applied()
    done: list[tuple[Path, Path | None]] = []       # (target, backup or None)
    try:
        for source in sorted(unpacked.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(unpacked)
            target = install / relative

            if relative.as_posix().lower() == paths.UPDATER_NAME.lower():
                # This program. It cannot replace itself while it is the thing
                # running, so it is left beside itself for Mistery.exe to swap
                # in at its next start (see swap_in_staged_updater).
                staged = install / (paths.UPDATER_NAME + ".new")
                staged.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, staged)
                result.staged_updater = True
                # Recorded so that a rollback deletes it: a new updater left
                # behind by an update that was put back would be swapped in by
                # Mistery at its next start, next to an app it does not match.
                done.append((staged, None))
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            backup: Path | None = None
            if target.exists():
                backup = target.with_name(target.name + ".old")
                _unlink(backup)
                os.replace(target, backup)          # MoveFileExW, running exe or not
                result.replaced += 1
            else:
                result.added += 1
            os.replace(source, target)
            done.append((target, backup))
        return result
    except OSError as exc:
        _roll_back(done)
        raise Refused(
            f"could not put {exc.filename or 'a file'} in place ({exc.strerror}); "
            f"{len(done)} file(s) put back as they were") from None


def _roll_back(done: list[tuple[Path, Path | None]]) -> None:
    """Undo the moves that succeeded, newest first.

    Best effort by definition — if a move failed because the disk filled or the
    antivirus took an interest, the move back can fail too. It is still worth
    trying: the common failure is one file late in the list, and putting the
    rest back leaves a Mistery that starts.
    """
    for target, backup in reversed(done):
        try:
            if backup is not None and backup.exists():
                os.replace(backup, target)
            else:
                _unlink(target)
        except OSError:
            pass


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def swap_in_staged_updater(install: Path) -> bool:
    """Put MisteryUpdate.exe.new in place. True if there was one.

    Mistery.exe is what calls this, at startup, before anything else — main.py
    carries its own copy of these three moves so the app needs no import from
    this package. The updater never calls it on itself: renaming your own
    running image works on Windows, but the scheduled task points at this exact
    path, and a moment where that path holds a half-moved file is not worth the
    cleverness. It lives here so the moves have one tested implementation.
    """
    staged = install / (paths.UPDATER_NAME + ".new")
    if not staged.is_file():
        return False
    target = install / paths.UPDATER_NAME
    old = install / (paths.UPDATER_NAME + ".old")
    try:
        _unlink(old)
        if target.exists():
            os.replace(target, old)
        os.replace(staged, target)
        return True
    except OSError:
        return False
