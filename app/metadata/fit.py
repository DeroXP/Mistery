"""Artwork kept no bigger than is any use, whatever size it arrived at.

TVmaze's "original" still for an episode is the frame as it was broadcast: one
reported from a real art folder was 3840 x 2160 and 1.31 MB, for a card drawn
332 x 187. Every picture from there and from Wikipedia was saved exactly as it
came, so a long series could put hundreds of megabytes in the art folder, and
each of those files was decoded in full every time its card came on screen.
TMDB's pictures were always asked for at a size (w500, w1280); this gives
everything else a ceiling too (WIDE and POSTER, below).

A picture is only ever made smaller, and only when it is a good deal too big:
one already about the right size is kept byte for byte, because encoding a JPEG
again costs detail and saves nothing. It stays the kind of file it was, so its
name stays true. Anything Pillow cannot read as a still picture is left alone.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Callable

from ..config import art_dir

_log = logging.getLogger("artwork")

# Bump to have every library's art folder gone through again (fit_cache), the
# way parser.PARSER_VERSION has titles read again.
FIT_VERSION = 1

# The most that is kept. A wide picture (a backdrop, an episode's still) is
# Full HD across; a poster is four times the tile it is drawn in. On a 4K
# screen at Windows' 150 % a page's backdrop is drawn some 3700 pixels wide and,
# at the largest interface size, a film's poster 618 x 927: nothing is ever
# drawn at more than twice what is kept of it, and a poster never at more than
# it. (The frames this app cuts for itself are 1600 across and its own posters
# 600 x 900, artwork.py: a floor, not a reason to bring real artwork down to it.)
WIDE = 1920
POSTER = (800, 1200)

# A picture this little over the size is left as it is: bringing a 1000 x 1500
# poster down by a fifth would soften it to save a few KB.
_SLACK = 1.25
_SAVE_AS = {
    "JPEG": {"quality": 88, "optimize": True},
    "PNG": {"optimize": True},
    "WEBP": {"quality": 88},
}
_MODES = {"JPEG": ("RGB", "L"), "PNG": ("RGB", "RGBA", "L", "LA", "P"), "WEBP": ("RGB", "RGBA")}
SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
# In the art folder, nothing under this is opened at all: a file this small has
# nothing left to take off it, whatever its size in pixels.
_WORTH_A_LOOK = 48 * 1024


def target_size(width: int, height: int) -> tuple[int, int] | None:
    """The size to bring a picture down to, or None when it is fine as it is.

    A wide picture (a backdrop, an episode's still) is WIDE across at most;
    anything else (a poster) fits in POSTER. Decided by the shape, not by what
    the picture is for, so the art folder can be gone through without knowing
    which row each file belongs to.
    """
    if width <= 0 or height <= 0:
        return None
    if width > height:
        scale = WIDE / width
    else:
        scale = min(POSTER[0] / width, POSTER[1] / height)
    if scale * _SLACK >= 1.0:
        return None
    return max(1, round(width * scale)), max(1, round(height * scale))


def fitted(data: bytes) -> bytes:
    """`data` itself, or the same picture smaller when it is far bigger than it
    is ever shown. Whatever cannot be read as a picture comes back as it was."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            kind = image.format
            size = target_size(image.width, image.height)
            if size is None or kind not in _SAVE_AS or image.mode not in _MODES[kind] \
                    or getattr(image, "is_animated", False):
                return data
            options = dict(_SAVE_AS[kind])
            if image.info.get("icc_profile"):
                options["icc_profile"] = image.info["icc_profile"]
            if kind == "JPEG":
                # Decoded at a half, a quarter or an eighth where that is still
                # big enough, which the JPEG decoder does for nothing.
                image.draft(image.mode, size)
            elif image.mode == "P":
                image = image.convert("RGBA")
            small = image.resize(size, Image.Resampling.LANCZOS)
            out = io.BytesIO()
            small.save(out, kind, **options)
    except Exception:           # noqa: BLE001 - a picture we cannot shrink is still a picture
        return data
    blob = out.getvalue()
    return blob if len(blob) < len(data) else data


def _swap_in(blob: bytes, destination: Path) -> bool:
    """Write through a file of its own, then swap it in, so a reader never
    meets half a picture."""
    try:
        fd, part = tempfile.mkstemp(prefix=destination.name + ".", suffix=".part",
                                    dir=destination.parent)
    except OSError:
        return False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
        for attempt in range(4):
            try:
                os.replace(part, destination)
                return True
            except PermissionError:
                # Open in the window's own loader, or under a virus scan.
                time.sleep(0.05 * (attempt + 1))
        return False
    except OSError:
        return False
    finally:
        try:
            os.unlink(part)
        except OSError:
            pass


def write_fitted(data: bytes, destination: Path) -> bool:
    """Save a picture into the art folder, at the size it is shown at."""
    return _swap_in(fitted(data), destination)


def fit_file(path: Path) -> int | None:
    """Shrink one picture where it lies. The bytes that saved; 0 when it was
    fine as it was; None when it could not be read or swapped just now."""
    try:
        from PIL import Image

        # Opening reads the first few KB, enough for the size: most of an art
        # folder is fine as it is and never read past that.
        with Image.open(path) as image:
            if target_size(image.width, image.height) is None:
                return 0
        data = path.read_bytes()
    except PermissionError:
        return None             # held by the window's loader, or a virus scan
    except Exception:           # noqa: BLE001 - not a picture, or gone since the listing
        return 0
    small = fitted(data)
    if small is data:
        return 0
    if not _swap_in(small, path):
        return None
    return len(data) - len(small)


def fit_cache(cancel: Callable[[], bool] | None = None,
              progress: Callable[[int, int], None] | None = None) -> tuple[int, int, bool]:
    """Bring the pictures already in the art folder down to size.

    (how many were made smaller, the bytes that saved, whether every one was
    seen to). False at the end means it was cut short or a file was held open,
    and the caller should come back another time.
    """
    try:
        with os.scandir(art_dir()) as listing:
            heavy = [Path(entry.path) for entry in listing
                     if entry.name.lower().endswith(SUFFIXES) and entry.is_file()
                     and entry.stat().st_size >= _WORTH_A_LOOK]
    except OSError:
        return 0, 0, False
    shrunk = saved = 0
    complete = True
    for index, path in enumerate(sorted(heavy), start=1):
        if cancel is not None and cancel():
            return shrunk, saved, False
        if progress is not None:
            progress(index, len(heavy))
        gained = fit_file(path)
        if gained is None:
            complete = False
        elif gained:
            shrunk += 1
            saved += gained
    if shrunk:
        _log.info("artwork: %d picture(s) made the size they are shown at, %.1f MB saved",
                  shrunk, saved / 1e6)
    return shrunk, saved, complete
