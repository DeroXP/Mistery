"""Friends' artwork, kept on this PC.

A friend's catalogue says which of their things has a picture, with a mark for
it (app/share/catalog.py). The picture itself comes the first time it is shown,
over the same channel as everything else (client.Channel.art), and is kept here
named by that mark: one they replace gets a new mark and is fetched again, one
that has not changed never is. Removing a friend removes their pictures.

    data folder/friends/<friend id>/<kind>-<their id>-<mark>.jpg

Everything written here came from another PC, so nothing is kept that is not
the picture it says it is: a JPEG, PNG or WebP by its first bytes, under
MAX_BYTES, under a name made only of a kind, a number and a hex mark. Qt reads
it later like any other artwork.
"""

from __future__ import annotations

import collections
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Callable

from .. import db
from ..config import data_dir

_log = logging.getLogger("share")

MAX_BYTES = 12 * 1024 * 1024        # the client's own ceiling (client.MAX_ART)
IDLE_CLOSE = 20.0                   # a channel with nothing to fetch closes after this
# A film's or show's wide picture, their backdrop (the server's `which`), kept
# as a kind of its own so it never replaces the poster: movie-wide-<id>-<mark>.
WIDE = {"movie": "movie-wide", "show": "show-wide"}
KINDS = ("movie", "show", "episode", "album", *WIDE.values())
_MARK = re.compile(r"[0-9a-f]{1,64}")
_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def folder(friend_id: int) -> Path:
    return data_dir() / "friends" / str(int(friend_id))


def _stem(kind: str, remote_id: int, mark: str | None) -> str | None:
    """The file name before its extension, or None for anything that isn't
    a kind, a number and a hex mark: a mark is their text, and must never be
    able to name a path."""
    if kind not in KINDS or isinstance(remote_id, bool) or not isinstance(remote_id, int) \
            or remote_id <= 0 or not mark or not _MARK.fullmatch(str(mark)):
        return None
    return f"{kind}-{remote_id}-{mark}"


def cached(friend_id: int, kind: str, remote_id: int, mark: str | None) -> str | None:
    """Our copy of that picture, if it is the one with that mark."""
    stem = _stem(kind, remote_id, mark)
    if stem is None:
        return None
    base = folder(friend_id)
    for extension in _TYPES.values():
        path = base / (stem + extension)
        if path.is_file():
            return str(path)
    return None


def _looks_like(blob: bytes, extension: str) -> bool:
    if extension == ".jpg":
        return blob[:3] == b"\xff\xd8\xff"
    if extension == ".png":
        return blob[:8] == b"\x89PNG\r\n\x1a\n"
    return blob[:4] == b"RIFF" and blob[8:12] == b"WEBP"


def store(friend_id: int, kind: str, remote_id: int, mark: str | None, blob: bytes,
          content_type: str | None) -> str | None:
    """Keep a picture a friend sent. Where it went, or None when it wasn't one."""
    stem = _stem(kind, remote_id, mark)
    extension = _TYPES.get(str(content_type or "").split(";")[0].strip().lower())
    if stem is None or extension is None or not blob or len(blob) > MAX_BYTES \
            or not _looks_like(blob, extension):
        return None
    base = folder(friend_id)
    base.mkdir(parents=True, exist_ok=True)
    target = base / (stem + extension)
    part = base / (stem + extension + ".part")
    part.write_bytes(blob)
    os.replace(part, target)
    # The picture this one replaces: the same thing under an older mark.
    prefix = f"{kind}-{remote_id}-"
    for old in base.glob(prefix + "*"):
        if old != target:
            try:
                old.unlink()
            except OSError:
                pass
    if kind == "album" and extension == ".jpg":
        _small_copy(target)
    return str(target)


SMALL_EDGE = 360            # app/music/art.py's own small covers


def _small_copy(path: Path) -> None:
    """The <cover>-sm.jpg the music screens look for beside every cover
    (app/music/art.py makes one for each of yours): the Now Playing bar and the
    album tiles show that, not the full picture."""
    try:
        from PIL import Image

        with Image.open(path) as opened:
            image = opened.convert("RGB")
        image.thumbnail((SMALL_EDGE, SMALL_EDGE), Image.Resampling.LANCZOS)
        image.save(path.with_name(path.stem + "-sm.jpg"), "JPEG", quality=90, optimize=True)
    except Exception as problem:            # noqa: BLE001 - the full one still shows
        _log.debug("share: no small copy of %s: %s", path.name, problem)


def forget(friend_id: int) -> None:
    """A friend removed: their pictures go with them."""
    shutil.rmtree(folder(friend_id), ignore_errors=True)


def fetch_wide(friend_id: int, kind: str, remote_id: int, mark: str | None) -> None:
    """A film's wide picture (their backdrop), fetched now on a thread of its
    own, unless it is here already: it is being played, so their PC is on, and
    Continue Watching's card shows it from then on."""
    wide = WIDE.get(kind)
    if wide and mark and not cached(friend_id, wide, remote_id, mark):
        Fetcher(friend_id, lambda *_: None).want(wide, remote_id, mark)


class Fetcher:
    """One friend's pictures, fetched one after another on a thread of its own.

    The page asks for what it shows, in the order it shows it; `done` is called
    (on this thread) with (kind, their id, path) for each one that arrives. One
    connection carries them all, opened for the first and closed once nothing
    is left to fetch for IDLE_CLOSE seconds. A PC that does not answer ends the
    round: `offline` says so, and nothing more is tried until `retry`.
    """

    def __init__(self, friend_id: int, done: Callable[[str, int, str], None]) -> None:
        self.friend_id = int(friend_id)
        self.offline = False
        self._done = done
        self._queue: collections.deque = collections.deque()
        self._queued: set[tuple[str, int]] = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def want(self, kind: str, remote_id: int, mark: str | None, *, first: bool = False) -> None:
        """Fetch that picture unless it is here already. `first` puts it at the
        front, for what has just come on screen."""
        if self.offline or self._stopped.is_set() or _stem(kind, remote_id, mark) is None:
            return
        if cached(self.friend_id, kind, remote_id, mark):
            return
        key = (kind, int(remote_id))
        with self._lock:
            if key in self._queued:
                if first:
                    self._queue = collections.deque(
                        [entry for entry in self._queue if entry[:2] != key])
                    self._queue.appendleft((kind, int(remote_id), mark))
                return
            self._queued.add(key)
            (self._queue.appendleft if first else self._queue.append)((kind, int(remote_id), mark))
            # None only once the last one has decided, under this lock, that
            # nothing was left: so a picture asked for as it goes is never stranded.
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="share-art", daemon=True)
                self._thread.start()
        self._wake.set()

    def clear(self) -> None:
        """Forget what is waiting: another tab is showing now."""
        with self._lock:
            self._queue.clear()
            self._queued.clear()

    def retry(self) -> None:
        self.offline = False

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()

    def _next(self, idle_since: float):
        """The next picture; None to wait for one; False to finish, which is
        decided under the lock want() takes, and said by leaving _thread None."""
        with self._lock:
            if self._stopped.is_set():
                self._queue.clear()
                self._queued.clear()
                self._thread = None
                return False
            if self._queue:
                entry = self._queue.popleft()
                self._queued.discard(entry[:2])
                return entry
            if time.monotonic() - idle_since > IDLE_CLOSE:
                self._thread = None
                return False
            return None

    def _run(self) -> None:
        from . import client

        channel = None
        idle_since = time.monotonic()
        try:
            while True:
                entry = self._next(idle_since)
                if entry is False:
                    return
                if entry is None:
                    self._wake.wait(1.0)
                    self._wake.clear()
                    continue
                kind, remote_id, mark = entry
                if cached(self.friend_id, kind, remote_id, mark):
                    continue
                try:
                    if channel is None:
                        friend = db.friend(self.friend_id)
                        if friend is None:
                            self.stop()
                            continue
                        channel = client.Channel(friend).open()
                    wide = kind.endswith("-wide")
                    answer = channel.art(kind[:-len("-wide")] if wide else kind, remote_id,
                                         "backdrop" if wide else "poster")
                except client.Unreachable:
                    self.offline = True
                    with self._lock:
                        self._queue.clear()
                        self._queued.clear()
                        self._thread = None
                    return
                except (client.ShareError, OSError) as problem:
                    _log.debug("share: no picture for %s %d: %s", kind, remote_id, problem)
                    if channel is not None:
                        channel.close()
                        channel = None
                    continue
                idle_since = time.monotonic()
                if answer is None:
                    continue
                path = store(self.friend_id, kind, remote_id, mark, *answer)
                if path:
                    self._done(kind, remote_id, path)
        except Exception:                       # noqa: BLE001 - pictures, not the app
            _log.exception("share: fetching a friend's pictures failed")
            with self._lock:
                self._thread = None
        finally:
            if channel is not None:
                channel.close()
            db.close_thread_connection()
