"""Downloading mpv and ffmpeg, checking them, and taking the files we want out.

The rule this file exists to enforce: nothing is unpacked until its SHA-256
matches the number pinned in tool_downloads.py. Not the TLS certificate, not the
domain, not "it came from GitHub" — the hash. A mirror, a proxy, a captive
portal and a corporate TLS appliance are all the same thing to this code: a
source of bytes that either hash right or get deleted.

The second rule: no filename ever comes out of an archive. Every file written to
runtime\\ is named by the `wanted` dict in tool_downloads.py, so the classic zip
trick of an entry called "..\\..\\Startup\\evil.exe" has nothing to write to.

Downloads land in the install folder's own update\\ folder, never %TEMP%.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from .setup_common import (CREATE_NO_WINDOW, Cancelled, InstallError,
                            archive_cache_dirs, human_size)
from .tool_downloads import ToolDownload

# 8 MB at a time. Measured on this machine, GitHub gave 38 MB/s and SourceForge
# 12.8 MB/s, so a chunk is a tenth of a second at worst — small enough that
# Cancel feels instant, big enough that the progress callback is not the work.
CHUNK = 8 << 20

USER_AGENT = "Mistery-Setup/1.0 (+https://github.com/DeroXP/Mistery)"

# Windows' own bsdtar, which reads 7-Zip archives. It has shipped in
# %WINDIR%\System32 since Windows 10 1803, and mpv is only published as a .7z,
# so this is how mpv gets unpacked without the installer carrying an LZMA
# decoder. Measured: 1.3 s to pull mpv.exe out of the 33.8 MB archive.
TAR_EXE = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "tar.exe"

Progress = Callable[[str, int, int], None]      # (what, done bytes, total bytes)


def _check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


# --- getting the bytes ------------------------------------------------------


def _cached_copy(tool: ToolDownload) -> Path | None:
    """An archive sitting next to MisterySetup.exe, for a PC with no internet.

    Download the two archives by hand somewhere else, drop them beside the
    installer, carry them over. The hash is checked exactly the same way, so a
    file found here is trusted no further than one off the wire.
    """
    for folder in archive_cache_dirs():
        candidate = folder / tool.archive_name
        try:
            if candidate.is_file() and candidate.stat().st_size == tool.size:
                return candidate
        except OSError:
            continue
    return None


def _download(url: str, target: Path, expected_size: int, report: Progress,
              label: str, cancel: threading.Event | None) -> None:
    """One URL to one file, with progress. Raises OSError/URLError to the caller.

    Writes to <target>.part and renames at the end, so an interrupted download
    can never be mistaken for a finished one by the next run, and never writes
    more than the pinned size — see the check in the loop.
    """
    partial = target.with_name(target.name + ".part")
    partial.unlink(missing_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) != expected_size:
                # Not fatal on its own — the hash is what decides — but it means
                # the far end is offering something other than what was pinned,
                # and saying so now is friendlier than 67 MB and then "hash
                # mismatch".
                raise InstallError(
                    f"{label}: the server offered {human_size(int(declared))}, "
                    f"but Mistery expects exactly {human_size(expected_size)}. "
                    "The download was refused.")
            done = 0
            with open(partial, "wb") as handle:
                while True:
                    _check(cancel)
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    done += len(chunk)
                    # Counted before the write, and checked every chunk, because
                    # a chunked response has no Content-Length for the check
                    # above to read. Measured with that check as the only one:
                    # a source told to send 60 MB against a 1 MB pin wrote all
                    # 60 MB into update\ before anyone compared the numbers.
                    # Over a real connection that is minutes of a filling disk
                    # inside the user's own install folder, for bytes that were
                    # never going to be used. updater/download.py already does
                    # it this way.
                    if done > expected_size:
                        raise InstallError(
                            f"{label}: the server kept sending past the "
                            f"{human_size(expected_size)} Mistery expects. The "
                            "download was stopped and thrown away.")
                    handle.write(chunk)
                    report(label, done, expected_size)
        if done != expected_size:
            raise InstallError(
                f"{label}: the download stopped at {human_size(done)} of "
                f"{human_size(expected_size)}. Check the connection and try again.")
    except BaseException:
        # Including Cancel: half a pinned archive is no use to the next run,
        # which starts again from zero anyway, and leaving it there is 33.8 MB
        # of update\ that nothing will ever clear up. Swallowing the unlink's
        # own error on the way out: whatever brought us here is the thing worth
        # reading, not "could not delete the scrap".
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.replace(partial, target)


def _sha256(path: Path, report: Progress, label: str,
            cancel: threading.Event | None) -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with open(path, "rb") as handle:
        while True:
            _check(cancel)
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            report(label, done, total)
    return digest.hexdigest()


def obtain(tool: ToolDownload, into: Path, report: Progress,
           cancel: threading.Event | None = None) -> Path:
    """Get one archive into `into`, verified. Returns the path to it.

    Tries a copy beside the installer first, then each pinned URL in turn. A URL
    that 404s moves on to the next one — that is what the SourceForge mirror is
    for, because shinchiro keeps only 30 mpv releases and GitHub will eventually
    stop having this one. When every source has failed, the message names the
    file and the project page, because downloading it by hand and dropping it
    next to MisterySetup.exe is a real way out.
    """
    into.mkdir(parents=True, exist_ok=True)
    target = into / tool.archive_name

    cached = _cached_copy(tool)
    if cached is not None and cached != target:
        report(f"Copying {tool.archive_name}", 0, tool.size)
        shutil.copyfile(cached, target)
    elif target.is_file() and target.stat().st_size == tool.size:
        pass                        # left by an earlier run; the hash still decides
    else:
        failures: list[str] = []
        for url in tool.urls:
            _check(cancel)
            try:
                _download(url, target, tool.size,
                          report, f"Downloading {tool.name}", cancel)
                break
            except Cancelled:
                raise
            except (urllib.error.URLError, OSError, InstallError) as error:
                failures.append(f"  {url}\n    {error}")
        else:
            raise InstallError(
                f"Could not download {tool.name}.\n\n"
                + "\n".join(failures)
                + f"\n\nYou can fetch {tool.archive_name} yourself from\n"
                f"  {tool.homepage}\n"
                "and put it in the same folder as MisterySetup.exe — the "
                "installer checks it the same way and will use it.")

    actual = _sha256(target, report, f"Checking {tool.name}", cancel)
    if actual != tool.sha256:
        target.unlink(missing_ok=True)
        raise InstallError(
            f"{tool.archive_name} is not the file Mistery expects.\n\n"
            f"  expected  {tool.sha256}\n"
            f"  got       {actual}\n\n"
            "The download has been deleted. This is what a corrupted download "
            "looks like, and also what a tampered one looks like; either way "
            "nothing from it was unpacked.")
    return target


# --- taking the files out ---------------------------------------------------


def _unpack_zip(archive: Path, tool: ToolDownload, runtime: Path,
                report: Progress, cancel: threading.Event | None) -> list[Path]:
    """Pull the wanted members out of a .zip straight into runtime\\.

    Members are matched by the path in `wanted` and written under the *key*, so
    the name on disk is one of nine constants in tool_downloads.py. zipfile's
    own extract() would use the archive's path; this never asks it to.
    """
    written: list[Path] = []
    wanted = sorted(tool.wanted.items())         # (name on disk, path in archive)
    with zipfile.ZipFile(archive) as bundle:
        # One pass over the archive's index, keyed by the path we asked for.
        # BtbN's zip wraps everything in a versioned top folder whose name moves
        # with every build, so the pins say "bin/ffmpeg.exe" and the match is on
        # the tail of the entry.
        by_tail: dict[str, zipfile.ZipInfo] = {}
        for info in bundle.infolist():
            if not info.is_dir():
                by_tail.setdefault(
                    info.filename.replace("\\", "/").lower(), info)
                tail = info.filename.replace("\\", "/").lower()
                for name, inside in wanted:
                    if tail.endswith("/" + inside.lower()):
                        by_tail.setdefault(inside.lower(), info)
        for index, (name, inside) in enumerate(wanted):
            _check(cancel)
            member = by_tail.get(inside.lower())
            if member is None:
                raise InstallError(
                    f"{tool.archive_name} does not contain {inside}. The pinned "
                    "download changed shape; Mistery will not guess at it.")
            report(f"Unpacking {tool.name}", index + 1, len(wanted))
            destination = runtime / name         # our name, never the archive's
            with bundle.open(member) as source, open(destination, "wb") as handle:
                shutil.copyfileobj(source, handle, CHUNK)
            written.append(destination)
    return written


def _unpack_7z(archive: Path, tool: ToolDownload, runtime: Path, scratch: Path,
               report: Progress, cancel: threading.Event | None) -> list[Path]:
    """Pull the wanted members out of a .7z with Windows' own tar.exe.

    tar is told exactly which members to extract, into an empty scratch folder,
    and then only the files named in `wanted` are moved across into runtime\\.
    Anything else the archive might have unpacked stays in the scratch folder
    and is deleted with it.
    """
    if not TAR_EXE.is_file():
        raise InstallError(
            f"{TAR_EXE} is missing. Windows has shipped it since Windows 10 "
            "1803 and Mistery uses it to unpack mpv; without it the installer "
            "cannot continue.")
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    members = sorted(tool.wanted.values())
    report(f"Unpacking {tool.name}", 0, len(members))
    result = subprocess.run(
        [str(TAR_EXE), "-xf", str(archive), "-C", str(scratch), *members],
        capture_output=True, text=True, timeout=600,
        creationflags=CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise InstallError(
            f"Could not unpack {tool.archive_name}:\n"
            f"{(result.stderr or result.stdout or '').strip()[:400]}")

    written: list[Path] = []
    for index, (name, inside) in enumerate(sorted(tool.wanted.items())):
        _check(cancel)
        source = scratch / inside
        if not source.is_file():
            raise InstallError(
                f"{tool.archive_name} does not contain {inside}. The pinned "
                "download changed shape; Mistery will not guess at it.")
        report(f"Unpacking {tool.name}", index + 1, len(members))
        destination = runtime / name
        destination.unlink(missing_ok=True)
        shutil.move(str(source), str(destination))
        written.append(destination)
    shutil.rmtree(scratch, ignore_errors=True)
    return written


def install_tool(tool: ToolDownload, runtime: Path, staging: Path,
                 report: Progress, cancel: threading.Event | None = None,
                 keep_archive: bool = False) -> list[Path]:
    """Download, verify and unpack one tool into runtime\\. Returns what landed."""
    archive = obtain(tool, staging, report, cancel)
    runtime.mkdir(parents=True, exist_ok=True)
    if archive.suffix.lower() == ".zip":
        written = _unpack_zip(archive, tool, runtime, report, cancel)
    else:
        written = _unpack_7z(archive, tool, runtime,
                             staging / f"unpack-{tool.name}", report, cancel)
    if not keep_archive:
        # 101 MB of archive that has done its job. Keeping it would double what
        # the install costs on disk for the length of one reinstall.
        archive.unlink(missing_ok=True)
    return written


def measure(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def _selftest() -> int:
    """Fetch both tools into a scratch folder and report what it cost.

        python -m packaging.installer.fetch --selftest <folder>
    """
    import sys

    from .tool_downloads import TOOLS

    folder = Path(sys.argv[2] if len(sys.argv) > 2 else ".").resolve()
    runtime, staging = folder / "runtime", folder / "update"
    last = [0.0]

    def report(what: str, done: int, total: int) -> None:
        now = time.monotonic()
        if now - last[0] > 0.5 or done >= total:
            last[0] = now
            print(f"  {what}: {done}/{total}", flush=True)

    grand = 0
    for tool in TOOLS:
        started = time.monotonic()
        written = install_tool(tool, runtime, staging, report)
        size = measure(written)
        grand += size
        print(f"{tool.name}: {len(written)} files, {human_size(size)}, "
              f"{time.monotonic() - started:.1f} s")
    print(f"runtime total {human_size(grand)}")
    return 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(__doc__)
