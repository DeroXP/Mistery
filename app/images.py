"""Off-thread image loading, cover-cropping and generated placeholder art."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable

_log = logging.getLogger("images")

from PySide6.QtCore import QObject, QRectF, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPixmap,
)

_pool = QThreadPool()
_pool.setMaxThreadCount(4)

_CACHE_LIMIT = 400
_cache: OrderedDict[tuple, QPixmap] = OrderedDict()
# One read per path, shared by every widget that asks meanwhile: path -> (the
# callbacks waiting, when the read started).
_inflight: dict[str, tuple[list[Callable[[QImage], None]], float]] = {}
# A read that has not answered in this long is presumed lost rather than slow,
# and a fresh one is started — otherwise every later request for that file
# (grid, album page, player bar) would queue behind it forever, silently.
_STALE_READ = 15.0
# Paths whose load failed, so the UI can have another go rather than leaving a
# placeholder on screen because a scanner, a backup or the artwork pass held the
# file for a moment. Each read already retries three times over 1.5 s; this is
# for the failures that outlast that. A file that is genuinely broken stops
# being offered after _MAX_RETRIES, so a corrupt cover can't make the interface
# rebuild itself every few seconds forever.
_failed: set[str] = set()
_fail_counts: dict[str, int] = {}
# What each failed file looked like when it last failed: (mtime, size), or None
# when it wasn't there. See forget_failure_counts.
_fail_stamps: dict[str, tuple | None] = {}
_MAX_RETRIES = 4


def failed_paths() -> set[str]:
    return set(_failed)


def clear_failures() -> None:
    """Forget the failures, but not how many times each one has happened."""
    _failed.clear()


def _file_stamp(path: str) -> tuple | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def forget_failure_counts() -> None:
    """Give a file its retries back if it has changed since it last failed — for
    when the library has changed and a file that was missing may have been made.

    Only those. This runs on every music-library change, which is every watcher
    pass while an album downloads, and clearing every count there offered a
    poster that is simply gone again each time: Home and the current page
    rebuilt every 15 s for as long as downloads kept finishing (measured, six
    full rebuilds in 95 s for one deleted file). A stat per failed file, and
    there are rarely more than a few.
    """
    for path in list(_fail_counts):
        if _file_stamp(path) != _fail_stamps.get(path):
            _fail_counts.pop(path, None)
            _fail_stamps.pop(path, None)


class _TaskSignals(QObject):
    done = Signal(str, QImage, object)      # path, image, _file_stamp if it failed


class _LoadTask(QRunnable):
    def __init__(self, path: str, signals: _TaskSignals) -> None:
        super().__init__()
        self._path = path
        self._signals = signals

    def run(self) -> None:
        image = QImage()
        stamp = None
        try:
            image = self._read()
            if image.isNull():
                stamp = _file_stamp(self._path)     # here, not on the UI thread
        finally:
            # Must always fire — whatever happened above, including a logging
            # failure — or every widget waiting on this file keeps a placeholder.
            try:
                self._signals.done.emit(self._path, image, stamp)
            except Exception:
                _log.exception("could not deliver artwork: %s", self._path)

    def _read(self) -> QImage:
        # A single stat() failing is not proof the file is gone — antivirus
        # scans, backup software and indexers briefly lock files (errno 13/32),
        # and treating that as permanent leaves a placeholder on screen forever.
        # A file that simply does not exist is different: waiting 1.5 s for it
        # only ties up a loader thread, so that gets one quick second look.
        delays = (0.0, 0.35, 1.2)
        for attempt, delay in enumerate(delays):
            if delay:
                time.sleep(delay)
            try:
                os.stat(self._path)
            except OSError as exc:
                missing = exc.errno in (errno.ENOENT, errno.ENOTDIR) or getattr(exc, "winerror", 0) in (2, 3)
                if attempt == len(delays) - 1 or (missing and attempt >= 1):
                    _log.warning("artwork unreadable after %d tries (%s): %s",
                                 attempt + 1, exc.strerror or exc, self._path)
                    return QImage()
                continue
            image = QImage()
            if image.load(self._path):
                if attempt:
                    _log.info("artwork loaded on attempt %d: %s", attempt + 1, self._path)
                return image
            if attempt == len(delays) - 1:
                _log.warning("Qt could not decode artwork: %s", self._path)
        return QImage()


_signals = _TaskSignals()


def _on_loaded(path: str, image: QImage, stamp: tuple | None = None) -> None:
    if image.isNull():
        _fail_counts[path] = _fail_counts.get(path, 0) + 1
        _fail_stamps[path] = stamp
        if _fail_counts[path] <= _MAX_RETRIES:
            _failed.add(path)
    else:
        _failed.discard(path)
        _fail_counts.pop(path, None)
        _fail_stamps.pop(path, None)
    waiting, _started = _inflight.pop(path, ([], 0.0))
    for callback in waiting:
        try:
            callback(image)
        except RuntimeError:
            pass          # the widget went away while we were loading
        except Exception:
            # One misbehaving receiver must not rob the rest of their image.
            _log.exception("artwork callback failed: %s", path)


_signals.done.connect(_on_loaded, Qt.ConnectionType.QueuedConnection)


def load_async(path: str | None, callback: Callable[[QImage], None]) -> None:
    """Read an image off the UI thread; `callback` runs on the UI thread.

    Concurrent requests for the same path share one read.
    """
    if not path:
        callback(QImage())
        return
    path = str(path)
    entry = _inflight.get(path)
    if entry is not None:
        waiting, started = entry
        waiting.append(callback)
        if time.monotonic() - started < _STALE_READ:
            return
        # Presumed lost. Whichever read answers first delivers to everyone
        # waiting; a late answer from the old one finds nothing left to do.
        _log.warning("artwork read unanswered for %.0f s (%d waiting, %d loader threads busy); "
                     "reading again: %s", time.monotonic() - started, len(waiting),
                     _pool.activeThreadCount(), path)
        _inflight[path] = (waiting, time.monotonic())
        _pool.start(_LoadTask(path, _signals))
        return
    _inflight[path] = ([callback], time.monotonic())
    _pool.start(_LoadTask(path, _signals))


def _cache_get(key: tuple) -> QPixmap | None:
    pixmap = _cache.get(key)
    if pixmap is not None:
        _cache.move_to_end(key)
    return pixmap


def _cache_put(key: tuple, pixmap: QPixmap) -> None:
    _cache[key] = pixmap
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_LIMIT:
        _cache.popitem(last=False)


def cover_pixmap(
    image: QImage,
    width: int,
    height: int,
    radius: int = 10,
    device_ratio: float = 1.0,
) -> QPixmap:
    """Scale-and-crop to exactly fill width x height, with rounded corners."""
    target = QPixmap(int(width * device_ratio), int(height * device_ratio))
    target.setDevicePixelRatio(device_ratio)
    target.fill(Qt.GlobalColor.transparent)
    if image.isNull():
        return target

    painter = QPainter(target)
    painter.setRenderHints(
        QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
    )
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
    painter.setClipPath(path)

    scale = max(width / image.width(), height / image.height())
    scaled_w, scaled_h = image.width() * scale, image.height() * scale
    painter.drawImage(
        QRectF((width - scaled_w) / 2, (height - scaled_h) / 2, scaled_w, scaled_h),
        image,
    )
    painter.end()
    return target


def framed_pixmap(
    image: QImage,
    width: int,
    height: int,
    radius: int = 10,
    device_ratio: float = 1.0,
) -> QPixmap:
    """A tall picture in a wide frame, whole: over a soft, darkened copy of
    itself that fills the frame. Cropping a poster to 16:9 keeps its middle
    third, which is usually neither the faces nor the title.

    Anything already about as wide as the frame is cropped to fill it, as
    cover_pixmap does.
    """
    if image.isNull() or image.width() / image.height() > 0.8 * width / height:
        return cover_pixmap(image, width, height, radius, device_ratio)
    target = QPixmap(int(width * device_ratio), int(height * device_ratio))
    target.setDevicePixelRatio(device_ratio)
    target.fill(Qt.GlobalColor.transparent)
    painter = QPainter(target)
    painter.setRenderHints(
        QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
    )
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
    painter.setClipPath(path)
    # The blur is a copy shrunk to a few dozen pixels and drawn back up
    # smoothly: soft enough, and costs nothing next to reading the file.
    small = image.scaled(24, 36, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    scale = max(width / small.width(), height / small.height())
    painter.drawImage(QRectF((width - small.width() * scale) / 2,
                             (height - small.height() * scale) / 2,
                             small.width() * scale, small.height() * scale), small)
    painter.fillRect(QRectF(0, 0, width, height), QColor(0, 0, 0, 120))
    fit = height / image.height()
    shown_w = image.width() * fit
    painter.drawImage(QRectF((width - shown_w) / 2, 0, shown_w, height), image)
    painter.end()
    return target


def _hue_for(text: str) -> int:
    digest = hashlib.md5((text or "?").encode("utf-8")).hexdigest()
    return int(digest[:4], 16) % 360


def placeholder_pixmap(
    title: str,
    width: int,
    height: int,
    radius: int = 10,
    device_ratio: float = 1.0,
) -> QPixmap:
    """A deterministic gradient card with the title's initials.

    Used until real artwork exists, so a fresh library still looks intentional.
    """
    key = ("placeholder", title, width, height, radius, device_ratio)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    pixmap = QPixmap(int(width * device_ratio), int(height * device_ratio))
    pixmap.setDevicePixelRatio(device_ratio)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
    painter.setClipPath(path)

    hue = _hue_for(title)
    top = QColor.fromHsl(hue, 90, 58)
    bottom = QColor.fromHsl((hue + 42) % 360, 105, 24)
    gradient = QLinearGradient(0, 0, width * 0.65, height)
    gradient.setColorAt(0.0, top)
    gradient.setColorAt(1.0, bottom)
    painter.fillPath(path, QBrush(gradient))

    painter.setPen(QColor(255, 255, 255, 34))
    for offset in range(-height, width, 26):
        painter.drawLine(offset, height, offset + height, 0)

    initials = "".join(word[0] for word in (title or "?").split()[:2]).upper() or "?"
    font = QFont(painter.font())
    font.setPixelSize(max(20, int(height * 0.26)))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor(255, 255, 255, 225))
    painter.drawText(QRectF(0, 0, width, height), Qt.AlignmentFlag.AlignCenter, initials)
    painter.end()

    _cache_put(key, pixmap)
    return pixmap


def art_pixmap(
    path: str | None,
    title: str,
    width: int,
    height: int,
    radius: int,
    device_ratio: float,
    on_ready: Callable[[QPixmap], None],
    framed: bool = False,
) -> QPixmap:
    """Return artwork immediately if cached, otherwise a placeholder now and
    the real image via `on_ready` once it has been read from disk. `framed`
    shows a tall picture whole in a wide space (framed_pixmap) instead of
    cropping it."""
    if path:
        key = (path, width, height, radius, device_ratio)
        if framed:
            key += ("framed",)          # every other picture's key as it always was
        cached = _cache_get(key)
        if cached is not None:
            return cached

        def _finish(image: QImage) -> None:
            if image.isNull():
                return
            make = framed_pixmap if framed else cover_pixmap
            pixmap = make(image, width, height, radius, device_ratio)
            _cache_put(key, pixmap)
            on_ready(pixmap)

        load_async(path, _finish)

    return placeholder_pixmap(title, width, height, radius, device_ratio)


def clear_cache() -> None:
    _cache.clear()
