"""File logging.

Mistery normally runs under pythonw.exe, which has no console: sys.stdout and
sys.stderr are None, so tracebacks, Qt warnings and any print() vanish. Without
this, a failure in a background thread is completely invisible.

Everything lands in %APPDATA%\\Mistery\\mistery.log (rotated, kept small).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from .config import data_dir

_LOG_NAME = "mistery.log"
_MAX_BYTES = 512 * 1024
_BACKUPS = 2

_configured = False


class _StreamToLog:
    """Stand-in for a missing sys.stdout/sys.stderr under pythonw."""

    def __init__(self, level: int) -> None:
        self._level = level
        self._buffer = ""
        self._writing = threading.local()

    def write(self, text: str) -> int:
        # If the log file itself fails, logging reports that on stderr — which is
        # this object, which logs, which fails again, until a RecursionError
        # escapes into whatever was logging (an artwork loader, say) and kills it.
        # A write that arrives while this thread is already writing is dropped.
        if getattr(self._writing, "active", False):
            return len(text)
        self._writing.active = True
        try:
            self._buffer += text
            while "\n" in self._buffer:
                line, _, self._buffer = self._buffer.partition("\n")
                if line.strip():
                    logging.getLogger("stdio").log(self._level, line.rstrip())
        finally:
            self._writing.active = False
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            logging.getLogger("stdio").log(self._level, self._buffer.rstrip())
        self._buffer = ""

    def isatty(self) -> bool:
        return False


class _RotatingLog(logging.handlers.RotatingFileHandler):
    """Rotation that never loses a record or the previous session's log.

    Windows will not rename a file another process has open, and every second
    launch of Mistery has mistery.log open, for as long as its "already running"
    box is up. The stock handler deletes the oldest backup before it tries the
    rename, so each attempt ate a backup, then the rename failed and took the
    record with it, and the same happened to every record after. Measured with
    a second process holding the log: 0 of 50 records written, mistery.log.1
    gone.

    Here the live file is moved first. If that fails, the backups are left
    alone and writing carries on in the same file, trying again a little later.
    """

    _retry_at = 0.0

    def shouldRollover(self, record) -> bool:
        if time.monotonic() < self._retry_at:
            return False
        return bool(super().shouldRollover(record))

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        parked = self.baseFilename + ".rotating"
        try:
            os.replace(self.baseFilename, parked)
        except OSError:
            self._retry_at = time.monotonic() + 30
        else:
            for n in range(self.backupCount - 1, 0, -1):
                older = self.rotation_filename(f"{self.baseFilename}.{n}")
                if os.path.exists(older):
                    try:
                        os.replace(older, self.rotation_filename(f"{self.baseFilename}.{n + 1}"))
                    except OSError:
                        pass
            try:
                os.replace(parked, self.rotation_filename(f"{self.baseFilename}.1"))
            except OSError:
                pass
        if not self.delay:
            self.stream = self._open()


def log_path() -> Path:
    return data_dir() / _LOG_NAME


def setup(verbose: bool = False) -> Path:
    """Install file logging, exception hooks and Qt message capture."""
    global _configured
    if _configured:
        return log_path()

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = _RotatingLog(
        log_path(), maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(handler)

    # pythonw gives us no streams at all; anything written would raise.
    if sys.stdout is None:
        sys.stdout = _StreamToLog(logging.INFO)
    if sys.stderr is None:
        sys.stderr = _StreamToLog(logging.ERROR)

    def _excepthook(kind, value, tb) -> None:
        logging.getLogger("crash").critical(
            "unhandled exception\n%s",
            "".join(traceback.format_exception(kind, value, tb)),
        )

    sys.excepthook = _excepthook

    # Exceptions raised inside Qt virtual overrides (paintEvent, QRunnable.run)
    # are reported through this, not through excepthook.
    if hasattr(sys, "unraisablehook"):
        def _unraisable(unraisable) -> None:
            logging.getLogger("qt").error(
                "unraisable in %r\n%s", unraisable.object,
                "".join(traceback.format_exception(
                    unraisable.exc_type, unraisable.exc_value,
                    unraisable.exc_traceback)),
            )
        sys.unraisablehook = _unraisable

    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        levels = {
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR,
            QtMsgType.QtFatalMsg: logging.CRITICAL,
        }
        qInstallMessageHandler(
            lambda mode, ctx, message:
                logging.getLogger("qt").log(levels.get(mode, logging.INFO), message)
        )
    except ImportError:
        pass

    _configured = True
    logging.getLogger("mistery").info("--- logging started (%s) ---", sys.executable)
    return log_path()
