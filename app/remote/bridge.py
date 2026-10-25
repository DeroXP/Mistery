"""The phone remote's commands that need Mistery's window.

What the PC is playing, and play, pause, skip, seek and volume, and "play this
on my PC", all live in the window: its music player and its video player are
Qt objects, touched only on Qt's thread. api.py answers on worker threads, so
each command is handed over here and waited for (a few seconds at most).

The window registers itself when it opens (install) and goes when it closes.
The Mistery with no window (the one that keeps serving friends) has none, and
the phone is told so; it can still play anything on the phone itself.
"""

from __future__ import annotations

import threading
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal

from .. import db
from ..models import MediaItem


class _Bridge(QObject):
    _call = Signal(object)

    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window
        self._call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, job) -> None:
        job()

    def run(self, work: Callable, timeout: float = 8.0):
        done = threading.Event()
        box: dict = {}

        def job() -> None:
            try:
                box["value"] = work(self.window)
            except Exception as problem:            # noqa: BLE001 - said to the phone
                box["error"] = problem
            finally:
                done.set()

        self._call.emit(job)
        if not done.wait(timeout):
            raise TimeoutError("Mistery on your PC is busy. Try again in a moment.")
        if "error" in box:
            raise box["error"]
        return box.get("value")


_current: _Bridge | None = None


def install(window) -> None:
    global _current
    _current = _Bridge(window)


def uninstall() -> None:
    global _current
    _current = None


def available() -> bool:
    return _current is not None


def run(work: Callable, timeout: float = 8.0):
    bridge = _current
    if bridge is None:
        raise LookupError("Mistery's window isn't open on your PC.")
    return bridge.run(work, timeout)


# --- the commands, run on Qt's thread ---------------------------------------------------------

def now(window) -> dict:
    """What the PC is playing: its music, and the film in its player."""
    music = window.music
    state: dict = {"window": True, "music": None, "video": None}
    track = music.current
    if track:
        state["music"] = {
            "title": track.get("title") or "", "playing": bool(music.is_playing),
            "subtitle": " · ".join(bit for bit in (track.get("artist") or track.get("album_artist") or "",
                                                  track.get("album") or track.get("album_title") or "") if bit),
            "position": float(music.position or 0.0), "duration": float(music.duration or track.get("duration") or 0.0),
            "volume": float(music.volume),
        }
    player = window.player
    item = player.current_item
    if player.is_active and item is not None:
        subtitle = item.code if getattr(item, "is_episode", False) else (str(item.year) if item.year else "")
        state["video"] = {
            "title": item.title or "", "subtitle": subtitle, "playing": not player._paused,
            "position": float(player.position or 0.0), "duration": float(player._duration or item.duration or 0.0),
        }
    return state


def do(window, target: str, action: str, value) -> None:
    if target == "music":
        music = window.music
        if action in ("toggle", "play", "pause"):
            if action == "toggle" or (action == "play") != music.is_playing:
                music.toggle_pause()
        elif action == "next":
            music.next()
        elif action == "previous":
            music.previous()
        elif action == "seek" and isinstance(value, (int, float)):
            music.seek(float(value))
        elif action == "volume" and isinstance(value, (int, float)):
            music.set_volume(int(max(0, min(100, value))))
        else:
            raise ValueError("That isn't something the music player does.")
        return
    player = window.player
    if not player.is_active:
        raise LookupError("No film is playing on your PC.")
    if action in ("toggle", "play", "pause"):
        if action == "toggle" or (action == "play") == player._paused:
            player.toggle_pause()
    elif action == "next":
        player._advance_to_next()
    elif action == "previous":
        player._go_to_previous()
    elif action == "seek" and isinstance(value, (int, float)):
        player._seek_to(float(value))
    else:
        raise ValueError("That isn't something the video player does.")


def play(window, kind: str, number: int, start: int = 0) -> str:
    """Play something of this library on the PC. Returns what to tell the phone."""
    from ..music import library

    if kind in ("album", "track", "playlist-music", "liked"):
        if kind == "album":
            songs = library.album_tracks(number)
            title = (library.album(number) or {"title": "that album"})["title"]
        elif kind == "track":
            row = library.track(number)
            if row is None:
                raise LookupError("That song isn't in your library any more.")
            songs = library.album_tracks(int(row["album_id"])) if row["album_id"] else [row]
            start = next((n for n, song in enumerate(songs) if int(song["id"]) == number), 0)
            title = row["title"]
        elif kind == "liked":
            songs, title = library.liked_tracks(), "your liked songs"
        else:
            songs = [row for row in library.playlist_tracks(number) if not row.get("friend")]
            playlist = db.playlist(number)
            title = playlist["name"] if playlist is not None else "that playlist"
        songs = [dict(song) for song in songs]
        if not songs:
            raise LookupError("There's nothing there to play.")
        context = ({"kind": "album", "title": title, "id": number} if kind == "album" else
                   {"kind": "liked", "title": "Liked Songs", "id": None} if kind == "liked" else
                   {"kind": "queue", "title": title, "id": None})
        window.music.play_tracks(songs, max(0, min(start, len(songs) - 1)), in_order=True, context=context)
        return f"Playing {title} on your PC."
    if kind in ("movie", "episode", "playlist-video"):
        line_up = None
        if kind == "playlist-video":
            rows = db.playlist_media(number)
            if not rows:
                raise LookupError("There's nothing in that playlist to play.")
            line_up = ([int(row["id"]) for row in rows], 0)
            number = int(rows[0]["id"])
        row = db.get_media(number)
        if row is None or row["missing"]:
            raise LookupError("That isn't in your library any more.")
        item = MediaItem.from_row(row)
        if window.isHidden() or window.isMinimized() or getattr(window, "_in_tray", False):
            window.show_from_tray()
        window.play(item, line_up=line_up)
        return f"Playing {item.title} on your PC."
    raise ValueError("That can't be played on the PC.")
