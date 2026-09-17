"""Mistery — local movie and TV library with an embedded mpv player.

    python main.py
"""

from __future__ import annotations

import ctypes
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

from app import db
from app.config import find_mpv, icon_path
from app.ui.main_window import MainWindow
from app.ui.theme import C


def _set_windows_app_id() -> None:
    """Give the app its own taskbar entry rather than sharing python.exe's."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Mistery.Player.1")
    except Exception:
        pass


def _dark_palette(app: QApplication) -> None:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, Qt.GlobalColor.black)
    palette.setColor(QPalette.ColorRole.Base, Qt.GlobalColor.black)
    palette.setColor(QPalette.ColorRole.Text, Qt.GlobalColor.white)
    palette.setColor(QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
    app.setPalette(palette)


def _write_lock() -> None:
    """Record that the app is running, so tooling can refuse to interfere."""
    from app.config import data_dir

    import time

    try:
        (data_dir() / "app.lock").write_text(
            f"{os.getpid()},{time.time()}", encoding="utf-8"
        )
    except OSError:
        pass


def _clear_lock() -> None:
    """Remove app.lock if it is still ours. A lock another launch has written
    since belongs to a Mistery that is still running, and deleting it told
    tools/repair_db.py that Mistery was closed."""
    from app.config import data_dir

    lock = data_dir() / "app.lock"
    try:
        if int(lock.read_text(encoding="utf-8").split(",")[0]) == os.getpid():
            lock.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def _instance_channel() -> str:
    """A name only this Windows user's Mistery listens on."""
    import getpass
    import re

    return "Mistery-" + re.sub(r"[^A-Za-z0-9_-]", "_", getpass.getuser() or "user")


_instance_mutex: int | None = None


def _claim_instance() -> bool:
    """Take the name only one Mistery at a time may hold. False if another has it.

    The pipe and app.lock come too late to settle this on their own: the lock
    is written once the library is open and the pipe listens only once the
    window is up. Two launches tens of milliseconds apart found neither, and
    both became full instances, scanning and writing the same library. A named
    mutex is taken in one step, before any of that, and Windows lets go of it
    when the process ends, however it ends.
    """
    global _instance_mutex
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.CreateMutexW(None, False, "Local\\" + _instance_channel())
    if not handle:
        return True             # no mutex to be had; the pipe and the lock still apply
    if ctypes.get_last_error() == 183:          # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _instance_mutex = handle
    return True


def _release_instance() -> None:
    global _instance_mutex
    if _instance_mutex:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle(_instance_mutex)
        _instance_mutex = None


def _wake_running_instance() -> bool | None:
    """Ask an already-running Mistery to show itself.

    True if it answered, False if nothing is listening, None if something took
    the request and never answered.

    Needed since Mistery can live in the tray: the old "already running —
    switch to that window" message is useless when there is no window to
    switch to. Opening the shortcut again should just bring it back.

    A connection alone proves nothing. Windows completes a pipe connection in
    the kernel, so a Mistery whose window had frozen took "show" and the new
    launch exited quietly: no window, no message. So the running instance
    answers, and a launch that hears nothing says so.
    """
    from PySide6.QtNetwork import QLocalSocket

    socket = QLocalSocket()
    socket.connectToServer(_instance_channel())
    if not socket.waitForConnected(700):
        return False
    socket.write(b"show\n")
    socket.flush()
    socket.waitForBytesWritten(700)
    answered = socket.waitForReadyRead(2000) and socket.readLine().data().startswith(b"ok")
    socket.disconnectFromServer()
    return True if answered else None


def _listen_for_other_instances(window) -> None:
    from PySide6.QtNetwork import QLocalServer

    server = QLocalServer(window)
    # A crash can leave the name registered; clear it so listening succeeds.
    QLocalServer.removeServer(_instance_channel())
    if not server.listen(_instance_channel()):
        return

    def on_connection() -> None:
        connection = server.nextPendingConnection()
        if connection is not None:
            connection.disconnected.connect(connection.deleteLater)
            connection.write(b"ok\n")
            connection.flush()
        window.show_from_tray()

    server.newConnection.connect(on_connection)


def _is_damage(exc: BaseException) -> bool:
    """Whether SQLite says the file itself is damaged, as opposed to busy,
    locked, full or out of reach, none of which a repair would help."""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    name = getattr(exc, "sqlite_errorname", "")
    if not name:
        return type(exc) is sqlite3.DatabaseError
    return name.startswith("SQLITE_CORRUPT") or name == "SQLITE_NOTADB"


def _startup_failed(exc: Exception) -> int:
    """Say why Mistery cannot start, and offer a repair when the library is damaged.

    Under pythonw there is no console, so an exception here ended the process
    with no window and no message: the only trace was a line in mistery.log that
    nothing pointed to. A damaged library.db did exactly that on every start, and
    the repair tool that fixes it was only mentioned in the README.
    """
    from app.config import data_dir
    from app.log import log_path

    logging.getLogger("startup").critical("Mistery could not start", exc_info=exc)
    if not _is_damage(exc):
        QMessageBox.critical(
            None, "Mistery could not start",
            f"Mistery could not start:\n\n{exc}\n\nThe details are in {log_path()}",
        )
        return 1

    box = QMessageBox(
        QMessageBox.Icon.Critical, "Mistery's library is damaged",
        f"Mistery could not open its library because the file is damaged ({exc}).\n\n"
        "Repair copies everything still readable into a fresh library and keeps the "
        "damaged file beside it as a backup. Your video and music files are not "
        f"touched.\n\nThe details are in {log_path()}",
    )
    repair = box.addButton("Repair", QMessageBox.ButtonRole.AcceptRole)
    folder = box.addButton("Open data folder", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Close)
    box.setDefaultButton(repair)
    box.exec()
    if box.clickedButton() is folder:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_dir())))
    if box.clickedButton() is not repair:
        return 1
    return _repair_and_restart()


def _repair_and_restart() -> int:
    """Run tools/repair_db.py's repair here, then start a fresh Mistery.

    A fresh process rather than building the window again in this one: a window
    that failed part way through being built may already have its timers and
    background pool going.
    """
    import importlib.util

    from app.log import log_path

    log = logging.getLogger("repair")
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        spec = importlib.util.spec_from_file_location(
            "repair_db", Path(__file__).resolve().parent / "tools" / "repair_db.py")
        repair_db = importlib.util.module_from_spec(spec)
        sys.modules["repair_db"] = repair_db     # its dataclasses look themselves up there
        spec.loader.exec_module(repair_db)
        result = repair_db.repair(say=lambda line: line.strip() and log.info("%s", line))
    except Exception as exc:
        log.exception("repair did not finish")
        QApplication.restoreOverrideCursor()
        QMessageBox.critical(
            None, "Mistery could not repair its library",
            f"The repair did not finish:\n\n{exc}\n\nThe details are in {log_path()}",
        )
        return 1
    QApplication.restoreOverrideCursor()

    if not result.repaired:
        QMessageBox.critical(
            None, "Mistery could not repair its library",
            f"{result.message or 'The repair did not finish.'}\n\n"
            f"The details are in {log_path()}",
        )
        return 1
    if result.complete:
        QMessageBox.information(
            None, "Library repaired",
            "The library was repaired and nothing was lost. Mistery will now start.",
        )
    else:
        QMessageBox.warning(
            None, "Library repaired, with losses",
            "The library was repaired, but this could not be saved:\n\n"
            f"{result.message}\n\n"
            f"The damaged original is kept in the data folder as {result.backup.name}. "
            "Do not delete it: it still holds what was lost.\n\n"
            "Mistery will now start.",
        )

    _release_instance()
    _clear_lock()
    import subprocess

    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
                         cwd=str(Path(__file__).resolve().parent), close_fds=True)
    except OSError:
        log.exception("could not start Mistery again")
        QMessageBox.information(None, "Library repaired", "Start Mistery again.")
    return 0


def _log_art_health() -> None:
    """One-shot record of what the running instance can actually see."""
    from app import db

    log = logging.getLogger("startup")
    try:
        covers = db.query("SELECT title, cover FROM albums WHERE art_state = 'done'")
        missing_covers = [row["title"] for row in covers
                          if not row["cover"] or not os.path.exists(row["cover"])
                          or not os.path.exists(row["cover"].replace(".jpg", "-sm.jpg"))]
        log.info("album covers: %d made, %d missing%s", len(covers), len(missing_covers),
                 f" — {', '.join(missing_covers[:4])} (they will be made again)"
                 if missing_covers else "")
    except Exception:
        log.exception("could not audit album covers")
    try:
        rows = db.query(
            "SELECT title, poster, backdrop FROM media WHERE missing = 0 LIMIT 400"
        )
        referenced = missing = 0
        first_error = None
        for row in rows:
            for column in ("poster", "backdrop"):
                value = row[column]
                if not value:
                    continue
                referenced += 1
                try:
                    os.stat(value)
                except OSError as exc:
                    missing += 1
                    # errno matters: 2 is genuinely absent, 13/32 mean another
                    # program is holding the file (antivirus, backup, indexer).
                    first_error = first_error or (
                        f"{row['title']!r} errno={exc.errno} "
                        f"({exc.strerror}) -> {value}"
                    )
        log.info("artwork: %d referenced, %d unreadable%s",
                 referenced, missing, f" — first: {first_error}" if first_error else "")
    except Exception:
        log.exception("could not audit artwork")


def main() -> int:
    _set_windows_app_id()
    from app.log import setup as setup_logging

    setup_logging(verbose="--verbose" in sys.argv)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("Mistery")
    app.setOrganizationName("Mistery")
    app.setStyle("Fusion")
    # Mistery can keep playing music with no window at all, so the last window
    # closing is not the end of the app — MainWindow quits explicitly instead.
    app.setQuitOnLastWindowClosed(False)
    _dark_palette(app)

    icon = icon_path()
    if icon:
        app.setWindowIcon(QIcon(str(icon)))

    # One instance only: two copies both scanning and writing fight over the
    # library and each keeps reloading in response to the other's writes. Ask
    # over the named pipe first, before the database is even opened: the lock
    # file is invisible across an AppData virtualization boundary (see below),
    # the pipe is not.
    first = _claim_instance()
    woke = _wake_running_instance()
    # Another launch holds the name but may still be starting: it listens
    # only once its window is up, which can take a while right after boot.
    deadline = time.monotonic() + 20
    while not first and woke is False and time.monotonic() < deadline:
        time.sleep(0.25)
        first = _claim_instance()           # it quit before getting that far
        if not first:
            woke = _wake_running_instance()
    if woke:
        return 0
    from app.config import app_is_running, virtualized_appdata

    existing = app_is_running()
    if woke is None or not first or existing is not None:
        process = f" (process {existing})" if existing is not None else ""
        if woke is None:
            text = (f"Mistery is already open{process}, but it did not answer.\n\n"
                    "If its window does not appear, it is not responding: end it in "
                    "Task Manager and start Mistery again.")
        else:
            text = (f"Mistery is already open{process}.\n\n"
                    "Switch to that window instead. If it is not responding, end it in "
                    "Task Manager and start Mistery again.")
        QMessageBox.information(None, "Mistery is already running", text)
        return 0

    private = virtualized_appdata()
    if private is not None:
        logging.getLogger("startup").warning(
            "started inside a packaged app: new files would go to %s", private)
        answer = QMessageBox.warning(
            None, "Mistery was started from inside another app",
            "Mistery was opened by a packaged app whose sandbox redirects new "
            "files. Artwork and thumbnails made in this session would be saved "
            f"to a private copy:\n\n{private}\n\n"
            "The normal Mistery can never see them, but the library would record "
            "them as made, so they would show as blank placeholders.\n\n"
            "Start Mistery from the Start menu or its desktop shortcut instead.",
            QMessageBox.StandardButton.Close | QMessageBox.StandardButton.Ignore,
            QMessageBox.StandardButton.Close,
        )
        if answer != QMessageBox.StandardButton.Ignore:
            return 0

    try:
        db.init()
        _write_lock()
        window = MainWindow()
        window.show()
    except Exception as exc:
        _clear_lock()
        return _startup_failed(exc)

    # Windows signing out or shutting down closes no window: Qt 6 emits
    # commitDataRequest and then quits, so closeEvent never ran, the playing
    # film's position went unsaved and mpv was killed rather than stopped.
    # _shutdown runs once; a quit from the window or the tray has already run it.
    app.aboutToQuit.connect(window._shutdown)
    app.aboutToQuit.connect(_clear_lock)
    _listen_for_other_instances(window)
    # Windows signing out or shutting down must never meet a close that goes to
    # the tray instead — that is what "this app is preventing shutdown" is. Only
    # the flag, not a quit: Windows can still cancel the shutdown after asking.
    app.commitDataRequest.connect(lambda _manager: setattr(window, "_quitting", True))
    _log_art_health()

    if not find_mpv():
        QMessageBox.warning(
            window,
            "mpv not found",
            "Mistery plays video with mpv, which is not installed.\n\n"
            "Install it with:\n    winget install shinchiro.mpv\n\n"
            "The library will still scan, but playback will not start.",
        )

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
