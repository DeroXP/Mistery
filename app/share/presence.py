"""Friends listening to this PC's music, as they say, and the way to join them.

A friend playing your songs (app/share/music.py) tells your PC what they are
on every few seconds, over the channel their songs already come through:

    listening {id, position, playing, next: [ids], stopped?}
              → listening {join?: {code, name}}

`id` is the song of yours they are hearing, `next` what of yours comes after it
in their queue. Nothing here is the truth about their player, only what they
last said, and it is forgotten once they stop saying it (EXPIRE). Only songs
of this library are taken: an id that is not a playable song here is dropped.

Joining them is a listening party on this PC (app/party/listen_session.py)
with them as its DJ: this PC hosts it, because their PC reaching this one is
the one thing already proven to work. Its code goes back to them in the answer
to their next report (invite()), and their Mistery joins it by itself, so the
music they are hearing carries on and this PC falls in step with it.

Nothing here touches Qt. Watchers are called on the share connection's thread.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from .. import db

_log = logging.getLogger("share.presence")

EXPIRE = 25.0               # a friend who has not reported for this long has stopped
INVITE_LIFE = 60.0          # a join not collected by then is forgotten
REPORT_PLAYING = 4.0        # how often a friend's Mistery reports while playing ...
REPORT_PAUSED = 10.0        # ... and while paused
MAX_NEXT = 200


@dataclass(frozen=True)
class Listening:
    """One friend on one of this library's songs."""

    friend_id: int
    name: str
    person_id: str
    song_id: int
    title: str
    artist: str
    position: float
    playing: bool
    upcoming: tuple[int, ...]
    heard_at: float = field(default_factory=time.monotonic)

    def position_now(self, at: float | None = None) -> float:
        """Where their song is now, from what they last said."""
        if not self.playing:
            return self.position
        return self.position + max(0.0, (time.monotonic() if at is None else at) - self.heard_at)


_lock = threading.Lock()
_listening: dict[int, Listening] = {}
_invites: dict[int, tuple[str, str, float]] = {}     # friend id -> (code, host's name, made at)
watchers: list = []                                   # called with no arguments after any change


def _tell() -> None:
    for watcher in list(watchers):
        try:
            watcher()
        except Exception:                   # noqa: BLE001 - a watcher's problem, not the channel's
            _log.exception("a presence watcher failed")


def heard(friend, request: dict) -> dict:
    """A friend's report (on their share channel's thread): kept, and answered
    with a join code if this PC has one waiting for them."""
    from ..music import library

    friend_id = int(friend["id"])
    stopped = request.get("stopped") is True
    song_id = request.get("id")
    row = None
    if not stopped and isinstance(song_id, int) and not isinstance(song_id, bool) and 0 < song_id < 2 ** 53:
        try:
            row = library.track(song_id)
        except Exception:                   # noqa: BLE001
            row = None
    changed = False
    with _lock:
        if row is None:
            changed = _listening.pop(friend_id, None) is not None
        else:
            position = request.get("position")
            position = float(position) if isinstance(position, (int, float)) \
                and not isinstance(position, bool) and 0 <= position < 1e7 else 0.0
            upcoming = request.get("next")
            upcoming = tuple(int(i) for i in upcoming[:MAX_NEXT]
                             if isinstance(i, int) and not isinstance(i, bool) and 0 < i < 2 ** 53) \
                if isinstance(upcoming, list) else ()
            before = _listening.get(friend_id)
            now = Listening(friend_id, str(friend["name"]), str(friend["person_id"]), int(row["id"]),
                            str(row["title"] or ""), str(row["artist"] or row["album_artist"] or ""),
                            position, request.get("playing") is True, upcoming)
            _listening[friend_id] = now
            changed = before is None or (before.song_id, before.playing) != (now.song_id, now.playing)
        answer: dict = {"type": "listening"}
        waiting = _invites.get(friend_id)
        if waiting is not None and not stopped:
            code, name, made = waiting
            if time.monotonic() - made < INVITE_LIFE:
                answer["join"] = {"code": code, "name": name}
            del _invites[friend_id]
    if changed:
        _tell()
    return answer


def now() -> list[Listening]:
    """Everyone listening to this PC's music at the moment, newest report first."""
    cutoff = time.monotonic() - EXPIRE
    with _lock:
        gone = [fid for fid, item in _listening.items() if item.heard_at < cutoff]
        for fid in gone:
            del _listening[fid]
        found = sorted(_listening.values(), key=lambda item: -item.heard_at)
    if gone:
        _tell()
    return found


def of(friend_id: int) -> Listening | None:
    return next((item for item in now() if item.friend_id == int(friend_id)), None)


def invite(friend_id: int, code: str, name: str) -> None:
    """Hand a listening party's code to this friend with their next report."""
    with _lock:
        _invites[int(friend_id)] = (str(code), str(name), time.monotonic())


def cancel(friend_id: int | None = None) -> None:
    with _lock:
        if friend_id is None:
            _invites.clear()
        else:
            _invites.pop(int(friend_id), None)


def forget(friend_id: int) -> None:
    """A friend removed, or their channel closed for good."""
    with _lock:
        gone = _listening.pop(int(friend_id), None) is not None
        _invites.pop(int(friend_id), None)
    if gone:
        _tell()


def songs(item: Listening) -> list[dict]:
    """Their song and what of this library comes after it, as library rows, for
    a listening party that joins them."""
    from ..music import library

    wanted = [item.song_id, *item.upcoming]
    rows = library.tracks_by_id(wanted)
    return [dict(rows[song_id]) for song_id in wanted if song_id in rows]


# --- this PC's own listening party, for friends to see and join ---------------------------

_party: dict | None = None


def set_party(info: dict | None) -> None:
    """The listening party this PC hosts ({"code", "title", "song", "people"}), or
    None. A friend's Friends page learns of it from this PC's hello, and joins
    it with one click."""
    global _party
    with _lock:
        _party = dict(info) if info else None


def party() -> dict | None:
    with _lock:
        return dict(_party) if _party else None


def friend_named(friend_id: int) -> str:
    friend = db.friend(int(friend_id))
    return str(friend["name"]) if friend is not None else "a friend"
