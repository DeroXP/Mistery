r"""Fetching the package, and proving it is the one the manifest describes.

The download goes into the install folder's own update\ folder and never into
%TEMP%. %TEMP% is writable by every process running as this user, so a zip
verified there can be swapped for another between the hash check and the
unpack; update\ is inside a folder that only something already able to rewrite
Mistery can reach.

Size first, then hash. The size is checked *while* the bytes arrive, so a
server that keeps sending forever fills nothing but a counter, and a file that
is the wrong length is thrown away before 200 MB of SHA-256 is computed. The
hash is what actually proves it, and both numbers come from a manifest whose
signature has already been verified.
"""

from __future__ import annotations

import hashlib
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import manifest
from .manifest import Refused, Unreachable

# Read in 1 MB pieces: the package is ~170 MB and there is no reason to hold
# any of it in memory.
CHUNK = 1024 * 1024


def free_space(folder: Path) -> int | None:
    probe = folder
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(str(probe)).free
    except OSError:
        return None


def fetch_package(package: dict, into: Path) -> tuple[Path, float]:
    """Download it, check it, and return (the file, seconds it took).

    Anything wrong — a short read, a long read, a hash that does not match —
    deletes the file and raises. There is deliberately no resume and no retry
    within a run: the task runs again in an hour, and half a file kept between
    runs is one more thing that can be tampered with in the meantime.
    """
    size = int(package["size"])
    expected = str(package["sha256"]).lower()
    target = into / str(package["name"])

    into.mkdir(parents=True, exist_ok=True)
    room = free_space(into)
    if room is not None and room < size * 3:
        # Three times: the zip, what comes out of it, and the .old copies the
        # rename-then-replace leaves behind until the next run sweeps them.
        raise Refused(
            f"{room / 1e6:.0f} MB free where the update goes, and the download "
            f"alone is {size / 1e6:.0f} MB. Not starting.")

    partial = target.with_name(target.name + ".part")
    _remove(partial)
    _remove(target)

    digest = hashlib.sha256()
    written = 0
    started = time.monotonic()
    request = urllib.request.Request(
        package["url"], headers={"User-Agent": manifest.USER_AGENT})
    try:
        with manifest.opener().open(request, timeout=60) as response, \
                open(partial, "wb") as out:
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > size:
                    raise Refused(
                        f"the download is longer than the {size} bytes the "
                        f"manifest promised; stopped at {written}")
                digest.update(chunk)
                out.write(chunk)
    except Refused:
        _remove(partial)
        raise
    except urllib.error.HTTPError as exc:
        _remove(partial)
        raise Unreachable(f"{package['url']} answered {exc.code} {exc.reason}") from None
    except (urllib.error.URLError, OSError) as exc:
        _remove(partial)
        raise Unreachable(f"{package['url']}: {exc}") from None

    took = time.monotonic() - started
    if written != size:
        _remove(partial)
        raise Refused(f"the download is {written} bytes; the manifest says {size}")
    got = digest.hexdigest()
    if got != expected:
        _remove(partial)
        raise Refused(
            f"SHA-256 MISMATCH. Expected {expected}, got {got}. The file has "
            f"been deleted and nothing has been changed.")

    partial.replace(target)
    return target, took


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass                    # a locked leftover is swept on the next run
