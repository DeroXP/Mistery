"""Listening parties, for the Qt side: one ListeningSession per window.

The engine is app/party/listening.py (the room, with its queue) on top of the
movie night's (sync, server, invite, upnp, stun). This is the glue: it starts
a party (the host) or joins one (a guest), runs everything that blocks on
worker threads, turns the room's news into signals, and keeps this window's
music player where the room is.

    session = ListeningSession(player, window)
    session.start()                  # the queue playing now, for friends to join
    session.join(code)               # a friend's party, with their code
    session.join_them(listening)     # a friend on your music (share/presence.py)
    session.leave()                  # the host ends it for everyone

While a party is on, the player asks the session before doing anything a
person asked for (MusicPlayer.set_party): the DJ's play, pause, skip and seek
become the room's, a guest's Next is a vote to skip, a guest's pause is their
own (the party plays on, and play catches them up), and a new album or Add to
queue goes into the party's queue. The session moves the player itself only
through its _party_* methods and mpv.

Following the room. A thread per player (_MusicFollower), like a movie night's:
ten times a second it reads where mpv is, asks listening.MusicDrift what to do
and does it. What is new for music is the queue. Every player has the party's
queue in its own mpv playlist, so at the end of a song it moves on to the next
by itself, without a gap, and the room moves on at the same moment (the hub
dates the new song to when the old one ended). For the moment in between, when
one side has moved on and the other has not yet, the follower waits instead of
reopening anything. Any other difference between the song playing here and the
room's (a skip, a jump, a song put on) is a new queue for the player, opened
paused a little ahead of where the room will be, and started on the room's clock.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import threading
import time
from collections import deque
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal

from .. import db
from ..config import settings
from . import listening, sync
from . import people as _people
from .people import Person

_log = logging.getLogger("party.listen")

perf = time.perf_counter

# A guest's queue entries have no row in this library: an id none can have
# (a friend's songs use -(friend * 1e9 + id), well above this), and "party",
# which keeps them out of Liked Songs, play counts and the saved session.
PARTY_SONG_ID = 7_000_000_000_000_000
# Where a song is opened, ahead of the room, so it can start on the room's clock:
# the host's own file opens in a few tens of milliseconds, a guest's through the
# proxy in a few hundred (a token, a TLS connection, the first bytes).
OPEN_AHEAD = {"host": 0.35, "guest": 1.0}
ACTIVITY_KEEP = 40
BUSY = ("A movie night is on, so a listening party can't start until it's over: the "
        "two share the one port friends come in through.")
FRIEND_SONGS = ("Your own music can't go in {host}'s party: they can't hear it. "
                "Search their music in the party panel, or leave the party to play yours.")
NOT_YOURS = "That song is from a friend's library. A listening party plays songs from your own music."


class _Refused(Exception):
    """A party that could not start; the message is the sentence to show."""


class _Relay(QObject):
    """News from the party's threads to Qt's thread (queued)."""

    event = Signal(int, object)                 # generation, sync.Event
    done = Signal(object, object, object)       # callback, result, error
    status = Signal(int, str)                   # generation, what a start is doing now
    realign = Signal(int)                       # generation: the follower wants the player moved
    art = Signal(int, int, str)                 # generation, song id, a cover's file
    user = Signal(int, str)                     # generation, "pause" | "play": a media key, mpv's own
    trouble = Signal(int, str)


@dataclass
class _Job:
    songs: list[dict]
    index: int
    position: float
    playing: bool
    dj: str | None
    friend_id: int | None
    title: str
    port: int
    me: Person
    lan: str | None
    generation: int
    result: dict = field(default_factory=dict)


class _HostLibrary:
    """The host's music, as the room asks for it (on the room's thread)."""

    def __init__(self, server) -> None:
        self.server = server

    @staticmethod
    def _row(song_id: int):
        from ..music import library

        return library.track(int(song_id))

    def song(self, song_id: int) -> dict | None:
        row = self._row(song_id)
        if row is None:
            return None
        return {"id": row["id"], "title": row["title"], "artist": row["artist"] or row["album_artist"],
                "album": row["album_title"] or row["album"], "duration": row["duration"]}

    def search(self, text: str) -> list[dict]:
        from ..music import library

        return [{"id": row["id"], "title": row["title"], "artist": row["artist"] or row["album_artist"],
                 "album": row["album_title"] or row["album"], "duration": row["duration"]}
                for row in library.search(text)[:listening.MAX_FOUND]]

    def offer(self, song_id: int) -> str | None:
        row = self._row(song_id)
        if row is None or not row["path"] or not os.path.isfile(row["path"]):
            return None
        return self.server.offer(row["path"], row["duration"], listening.LISTEN_OFFERS)

    def art(self, song_id: int) -> str | None:
        """A token for the cover of the song's album, the one this library shows."""
        row = self._row(song_id)
        cover = row["cover"] if row is not None else None
        if not cover or not os.path.isfile(cover):
            return None
        return self.server.offer(cover, None, listening.LISTEN_OFFERS)


class _ArtFetcher:
    """The covers of a party's songs, for a guest: each fetched once, through the
    party's proxy, into a folder of this party's that goes when it ends. Only
    pictures are kept (JPEG, PNG, WebP by their first bytes), and only up to 12 MB."""

    MAX = 12 * 1024 * 1024

    def __init__(self, room, tunnel, folder: Path, done: Callable[[int, str], None]) -> None:
        self._room = room
        self._tunnel = tunnel
        self.folder = folder
        self._done = done
        self._wanted: "queue.Queue[int]" = queue.Queue()
        self._seen: set[int] = set()
        self.paths: dict[int, str] = {}
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._run, name="party-art", daemon=True)
        self._thread.start()

    def want(self, song_ids) -> None:
        for song_id in song_ids:
            if song_id not in self._seen:
                self._seen.add(song_id)
                self._wanted.put(int(song_id))

    def stop(self) -> None:
        self._stopping.set()
        shutil.rmtree(self.folder, ignore_errors=True)

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                song_id = self._wanted.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                path = self._fetch(song_id)
            except Exception:                   # noqa: BLE001 - a missing cover is a placeholder
                _log.info("listening party: a cover did not come through", exc_info=True)
                path = None
            if path and not self._stopping.is_set():
                self.paths[song_id] = path
                self._done(song_id, path)

    def _fetch(self, song_id: int) -> str | None:
        token = self._room.fetch_token(song_id, art=True)
        tunnel = self._tunnel
        if not token or tunnel is None:
            return None
        upstream = tunnel._open("GET", f"/m/{token}/media", None)
        if upstream is None:
            return None
        try:
            length = upstream.fields.get("content-length", "")
            if upstream.status != 200 or not length.isdigit() or not 0 < int(length) <= self.MAX:
                return None
            body = bytearray(upstream.leftover)
            while len(body) < int(length):
                chunk = upstream.sock.recv(min(262144, int(length) - len(body)))
                if not chunk:
                    return None
                body += chunk
        finally:
            tunnel._track(upstream.sock, False)
            upstream.close()
        head = bytes(body[:12])
        if head.startswith(b"\xff\xd8\xff"):
            suffix = ".jpeg"                    # not .jpg: the bar looks for a -sm.jpg beside a .jpg
        elif head.startswith(b"\x89PNG"):
            suffix = ".png"
        elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            suffix = ".webp"
        else:
            return None
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{int(song_id)}{suffix}"
        path.write_bytes(bytes(body))
        return str(path)


# --- following the room ------------------------------------------------------------------

@dataclass(frozen=True)
class _Local:
    e: int | None           # the party entry the player is on
    next_e: int | None
    prev_e: int | None
    loading: bool


class _MusicFollower:
    """Keeps the music player where the room is, on a thread of its own."""

    POLL = 0.1
    GAPLESS = 1.5           # a song ending into the next: how long either side may be ahead
    USER_AFTER = 0.8        # a pause this long after our own last one was not ours
    STUCK = 8.0             # a song still opening after this long is put in again

    def __init__(self, room, player, *, role: str, may_control: Callable[[], bool],
                 realign: Callable[[], None], user: Callable[[str], None],
                 trouble: Callable[[str], None]) -> None:
        self.room = room
        self.player = player
        self.role = role
        self._may_control = may_control
        self._realign = realign
        self._user = user
        self._trouble = trouble
        self.drift = listening.MusicDrift(0.0)
        self.tuned_out = False
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._set_pause: bool | None = None
        self._set_at = 0.0
        self._speed = 1.0
        self._buffering = False
        self._grace_until: float | None = None
        self._asked_for: int | None = None
        self._asked_at = -1e9
        self._mismatch_since: float | None = None
        self._loading_since: float | None = None
        # Where the song was opened (opened()): where it plays from when it
        # starts. See _step for why a paused player's own time-pos is not.
        self._opened_at: float | None = None
        self._late = 0                  # opens the room overtook before they were ready
        self.readings: deque[tuple[float, float, int]] = deque(maxlen=4000)
        self.starts: deque[tuple[float, float]] = deque(maxlen=100)    # (aimed at, went), perf
        self.thread = threading.Thread(target=self._run, name="party-music", daemon=True)

    # --- Qt's thread ------------------------------------------------------------------

    def start(self) -> None:
        self.thread.start()

    def wake(self) -> None:
        self._wake.set()

    def opened(self, position: float) -> None:
        """The session put the room's song in the player, paused at `position`."""
        self._opened_at = float(position)
        self._set_pause = True
        self._set_at = perf()
        self._asked_for = None
        self._mismatch_since = None
        self.drift.reset(0.0)
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stopping.set()
        self._wake.set()
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout)
        mpv = self.player._mpv
        if self._speed != 1.0 and mpv is not None and mpv.is_running:
            mpv.set_speed(1.0)              # the nudge was the room's, not this player's
        if self._buffering:
            self._buffering = False
            self.room.set_buffering(False)

    # --- its own thread ----------------------------------------------------------------

    def _run(self) -> None:
        try:
            while not self._stopping.is_set():
                wait = self._step()
                if wait > 0 and not self._stopping.is_set():
                    self._wake.wait(wait)
                    self._wake.clear()
        except Exception:
            _log.exception("listening party: following the room stopped")
            self._trouble("follower")

    def _local(self) -> _Local:
        player = self.player
        queue, index = player._queue, player._index

        def e_at(i: int) -> int | None:
            if 0 <= i < len(queue):
                value = queue[i].get("party_e")
                return value if isinstance(value, int) else None
            return None
        return _Local(e_at(index), e_at(index + 1), e_at(index - 1), bool(player._loading))

    def _pause(self, mpv, on: bool) -> None:
        mpv.set_property("pause", bool(on))
        self._set_pause = bool(on)
        self._set_at = perf()

    def _set_buffering(self, on: bool) -> None:
        if on != self._buffering:
            self._buffering = on
            self.room.set_buffering(on)

    def _step(self) -> float:
        room = self.room
        state = room.state
        media = state.media or {}
        want = media.get("e")
        if want is None or self.tuned_out:
            self._set_buffering(False)
            return 0.3
        mpv = self.player._mpv
        local = self._local()
        if mpv is None or not mpv.is_running or local.e != want:
            return self._align(state, want, local, mpv)
        self._grace_until = None
        self._mismatch_since = None
        if local.loading:
            # Opening. A song that never opens (the host's file gone, a proxy that
            # cannot reach them) is put in again, every few seconds, not waited on.
            now = perf()
            if self._loading_since is None:
                self._loading_since = now
            elif now - self._loading_since > self.STUCK and now - self._asked_at > self.STUCK:
                self._asked_at = now
                self._loading_since = None
                self._realign()
            return 0.05
        self._loading_since = None
        paused = bool(mpv.cached("pause", False))
        if (self._set_pause is not None and paused != self._set_pause
                and perf() - self._set_at > self.USER_AFTER and not mpv.cached("idle-active", False)):
            # Paused or played by something that is not the follower: a media key,
            # or Windows' media overlay, which talk to mpv directly.
            self._set_pause = paused
            self._set_at = perf()
            self._user("pause" if paused else "play")
            return 0.2
        stamp = perf()
        reply = mpv.command_sync("get_property", "time-pos", timeout=0.5)
        data = reply.get("data") if reply.get("error") == "success" else None
        position = float(data) if isinstance(data, (int, float)) and not isinstance(data, bool) else None
        host_now = room.host_time(stamp)
        busy = bool(mpv.cached("seeking", False)) or bool(mpv.cached("paused-for-cache", False))
        if position is not None:
            self.readings.append((stamp, position, state.seq))

        if paused and state.moving_at(host_now):
            # Starting while the room plays (just joined, a guest catching up, a
            # player seen paused): never "play now and correct after". The song
            # is opened a little ahead of the room, and starts the moment the
            # room gets there.
            #
            # From where it was opened, not from its time-pos: mpv's exact seek
            # plays from exactly there, but while paused a FLAC reports the
            # stream's seek point before it, 190 ms early here. Planned from
            # that reading, every catch-up started 150-190 ms ahead of the room
            # (probe_tune_in.py, three runs of three) and took 5 s of nudging
            # to come back.
            self._set_buffering(busy or position is None)
            if busy or position is None:
                return 0.05
            held = self._opened_at
            if held is not None:
                ahead = (held - state.position_at(host_now)) / (state.rate or 1.0)
                if -0.02 <= ahead < 3.0:
                    self._opened_at = None
                    self._late = 0
                    return self._start_at(mpv, stamp + max(0.0, ahead), state.seq)
                if ahead < 0:
                    self._late += 1     # the room got there first: open further ahead next time
            # Anywhere else: open the song again, ahead of the room.
            self._opened_at = None
            now = perf()
            if self._asked_for != want or now - self._asked_at > 2.0:
                self._asked_for = want
                self._asked_at = now
                self._realign()
            return 0.1
        if not paused:
            self._opened_at = None

        correction = self.drift.update(state, host_now, position, paused=paused, seeking=busy)
        self._set_buffering(busy or position is None)
        if correction.seek_to is not None:
            mpv.command("seek", correction.seek_to, "absolute", "exact")
        if correction.speed != self._speed:
            self._speed = correction.speed
            mpv.set_speed(correction.speed)
        if correction.pause != paused:
            self._pause(mpv, correction.pause)
        if correction.pause and correction.wake_in is not None and state.playing and 0 < correction.wake_in < 3.0:
            return self._start_at(mpv, stamp + correction.wake_in, state.seq)
        if position is not None and not paused and not busy:
            room.report_position(position, local=stamp)
        return self.POLL

    def _start_at(self, mpv, moment: float, seq: int) -> float:
        """Sleep to a scheduled start, then go, if the room has not changed its mind."""
        while not self._stopping.is_set():
            left = moment - perf()
            if left <= 0:
                break
            if self.room.state.seq != seq:
                return 0.0
            time.sleep(min(left, 0.02) if left < 0.05 else min(left - 0.03, 0.2))
        if self.room.state.seq == seq and not self._stopping.is_set():
            self._pause(mpv, False)
            self.starts.append((moment, perf()))
            self.drift.reset(0.0)           # where it starts is judged as a landing
        return 0.05

    def _align(self, state: sync.RoomState, want: int, local: _Local, mpv) -> float:
        now = perf()
        running = mpv is not None and mpv.is_running
        if running and local.e is not None and not local.loading:
            host_now = self.room.host_time(now)
            target = state.position_at(host_now)
            duration = state.duration or 0.0
            finishing = False
            if local.next_e == want and state.playing and target < self.GAPLESS:
                finishing = True        # the room moved on first; mpv follows at the end of its song
            elif local.prev_e == want and state.moving_at(host_now):
                if duration and duration - target < self.GAPLESS:
                    finishing = True    # mpv moved on first; the room follows at the end of the song
                elif self._may_control():
                    # The DJ skipped with a media key or the overlay: everyone goes.
                    if self._asked_for != -want:
                        self._asked_for = -want
                        self.room.act("next")
                    return 0.3
            if finishing:
                if self._grace_until is None:
                    self._grace_until = now + self.GAPLESS
                if now < self._grace_until:
                    return 0.05
        self._grace_until = None
        if self._mismatch_since is None:
            self._mismatch_since = now      # one reading may be of a queue half written
            return 0.1
        if now - self._mismatch_since < 0.15:
            return 0.1
        if self._asked_for != want or now - self._asked_at > 3.0:
            self._asked_for = want
            self._asked_at = now
            self._realign()
        return 0.2


# --- the session -------------------------------------------------------------------------

class ListeningSession(QObject):
    """The listening party of one window: host or guest, or neither.

    Signals
        changed         role, phase, people, queue, the DJ: anything a panel shows
        message(str)    a short line: "Sam joined", "Sam added Blue in Green"
        error(str)      something did not work, as a sentence (then ended)
        ended(str)      the party is over here; why ("" when we ended it)
        found(int, list)  songs of the host's music, for a search (its number)
    """

    changed = Signal()
    message = Signal(str)
    error = Signal(str)
    ended = Signal(str)
    found = Signal(int, list)

    # As PartySession's: tests put the listener on 127.0.0.1 and pretend the router.
    bind = "0.0.0.0"
    lan_address: str | None = None
    gateway_url: str | None = None
    stun_servers = None
    stun_timeout = 2.0
    upnp_timeout = 4.0
    borrow_from_sharing = True

    def __init__(self, player, window=None, parent: QObject | None = None) -> None:
        super().__init__(parent if parent is not None else (window if isinstance(window, QObject) else player))
        self._player = player
        self._window = window
        self.me: Person | None = None
        self._relay = _Relay(self)
        self._relay.event.connect(self._on_event)
        self._relay.done.connect(lambda callback, result, error: callback(result, error))
        self._relay.status.connect(self._on_status)
        self._relay.realign.connect(self._on_realign)
        self._relay.user.connect(self._on_user)
        self._relay.trouble.connect(self._on_trouble)
        self._relay.art.connect(self._on_art)
        self._generation = 0
        self._reset()
        self._renew = QTimer(self)
        self._renew.setInterval(10 * 60 * 1000)
        self._renew.timeout.connect(self._renew_if_due)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def _reset(self) -> None:
        self._role: str | None = None
        self._phase = "idle"                    # idle | starting | joining | on
        self._status = ""
        self._room = None
        self._server = None
        self._borrowed = False
        self._mapping = None
        self._tunnel = None
        self._invite = None
        self._invite_code = ""
        self._invite_link = ""
        self._port: int | None = None
        self._port_status = None
        self._follower: _MusicFollower | None = None
        self._rows: dict[int, dict] = {}        # host: library rows by song id
        self._activity: deque[str] = deque(maxlen=ACTIVITY_KEEP)
        self._friend_id: int | None = None      # guest: the host is this friend; host: joined this friend
        self._title = ""
        self._before = None                     # guest: the queue they had before joining
        self._quiet_join = False
        self._restarts = 0
        self._start_entries: list[dict] = []    # host: the player's entries the party began with
        self._art: _ArtFetcher | None = None    # guest: the songs' covers

    # --- what panels read ------------------------------------------------------------------

    @property
    def role(self) -> str | None:
        return self._role

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def status(self) -> str:
        return self._status

    @property
    def on(self) -> bool:
        return self._phase == "on"

    @property
    def people(self) -> list[dict]:
        room = self._room
        return list(room.people) if room is not None and self._phase == "on" else []

    @property
    def me_id(self) -> str:
        return self.me.id if self.me is not None else _people.person_id()

    @property
    def queue_view(self) -> dict:
        room = self._room
        return dict(room.queue_view) if room is not None else dict(listening.EMPTY_VIEW)

    @property
    def state(self) -> sync.RoomState | None:
        return self._room.state if self._room is not None else None

    @property
    def invite_code(self) -> str:
        return self._invite_code

    @property
    def invite_link(self) -> str:
        return self._invite_link

    @property
    def port_status(self):
        return self._port_status

    @property
    def activity(self) -> list[str]:
        return list(self._activity)

    @property
    def tuned_out(self) -> bool:
        return bool(self._follower is not None and self._follower.tuned_out)

    def names(self) -> dict[str, str]:
        room = self._room
        if room is None:
            return {}
        names = getattr(room, "names", None)
        if callable(names):
            return names()
        return room._names() if hasattr(room, "_names") else {}

    @property
    def dj_id(self) -> str:
        return str(self.queue_view.get("dj") or "")

    @property
    def is_dj(self) -> bool:
        return self._role == "host" or (self._role == "guest" and self.dj_id == self.me_id)

    @property
    def dj_name(self) -> str:
        dj = self.dj_id
        if not dj:
            return ""
        return "you" if dj == self.me_id else self.names().get(dj, _people.FALLBACK_NAME)

    @property
    def host_name(self) -> str:
        if self._role == "host":
            return (self.me or _people.me()).name
        host = getattr(self._room, "host", None) if self._room is not None else None
        if host is not None:
            return host.name
        if self._friend_id is not None:
            friend = db.friend(self._friend_id)
            if friend is not None:
                return str(friend["name"])
        return ""

    @property
    def title(self) -> str:
        """"Your listening party", "Sam's listening party"."""
        if self._role == "host":
            return "Your listening party"
        host = self.host_name
        return f"{host}'s listening party" if host else "A listening party"

    def listeners_text(self) -> str:
        """"Listening with Sam and Jo", "Just you so far"."""
        others = [p["name"] for p in self.people if p["id"] != self.me_id]
        if not others:
            return "Just you so far"
        return "Listening with " + sync.and_list(others)

    # --- starting ---------------------------------------------------------------------------

    def _busy_reason(self) -> str | None:
        if self._role is not None:
            return "A listening party is already on. Leave it before you start another."
        window = self._window
        movie = getattr(window, "_party", None) if window is not None else None
        role = getattr(movie, "role", None) if movie is not None else None
        if role == "host":
            return BUSY
        if role == "guest":
            # The party's music would play over the film.
            return "You're in a movie night. Leave it first, then listen together."
        return None

    def start(self, *, songs: list | None = None, index: int | None = None,
              position: float | None = None, playing: bool | None = None,
              dj: str | None = None, friend_id: int | None = None, title: str = "") -> bool:
        """Start a listening party: of the queue playing now, by default, from the song
        on and where it is. Returns at once: False (with `error`) when it cannot even
        begin; `changed` once the code is ready, `error` then `ended` if it fails."""
        busy = self._busy_reason()
        if busy:
            self.error.emit(busy)
            return False
        player = self._player
        player._party_untag()
        if songs is None:
            queue, at = player.queue, player.index
            current = player.current
            if current is None:
                self.error.emit("Play something first: a listening party plays your queue, "
                                "and friends hear it with you.")
                return False
            if current.get("friend") or current.get("party"):
                self.error.emit(NOT_YOURS)
                return False
            songs = [t for t in queue[max(0, at):] if not t.get("friend") and not t.get("party")]
            # The player's own entries: they become the party's where they are
            # (_adopt_host_queue), so the song playing never stops for it.
            self._start_entries = songs[:listening.MAX_QUEUE]
            index = 0
            position = player.position if position is None else position
            playing = player.is_playing if playing is None else playing
        songs = [dict(song) for song in songs][:listening.MAX_QUEUE]
        if not songs:
            self.error.emit("There's nothing of yours in the queue to play.")
            return False
        me = self.me or _people.me()
        self._generation += 1
        self._role = "host"
        self._phase = "starting"
        self._friend_id = friend_id
        self._rows = {int(song["id"]): song for song in songs if song.get("id")}
        port = int(settings.get("party_port", 42170) or 0)
        self._status = "Getting the listening party ready…"
        job = _Job(songs=songs, index=int(index or 0), position=float(position or 0.0),
                   playing=bool(playing), dj=dj, friend_id=friend_id, title=title, port=port, me=me,
                   lan=self.lan_address, generation=self._generation)
        self.changed.emit()
        self.message.emit(self._status)
        self._in_background(lambda: self._host_setup(job), lambda result, error: self._host_ready(job, error))
        return True

    def join_them(self, friend_listening) -> bool:
        """Join a friend who is listening to this PC's music (share/presence.py): a
        party on this PC, with them as its DJ, from their song and where it has got
        to. They are handed the code with their next report and join by themselves,
        so their music carries on and this PC's falls in step with it."""
        from ..share import presence

        songs = presence.songs(friend_listening)
        if not songs:
            self.error.emit("That song isn't in your library any more.")
            return False
        friend = db.friend(friend_listening.friend_id)
        if friend is None:
            return False
        # The room starts a quarter of a second after it is made (sync.LEAD_MIN):
        # where their song will be by then, so they do not have to wait for it.
        ahead = sync.LEAD_MIN if friend_listening.playing else 0.0
        return self.start(songs=songs, index=0, position=friend_listening.position_now() + ahead,
                          playing=friend_listening.playing, dj=str(friend["person_id"]),
                          friend_id=int(friend["id"]))

    def _host_setup(self, job: _Job) -> None:
        """Everything that blocks, on a worker thread: the certificate, the listener,
        the router and STUN."""
        from . import invite, server as server_mod, tls, upnp
        from .session import PartySession
        from ..share import sharer as share_sharer

        made = job.result
        lan = job.lan or upnp.lan_ip()
        token = invite.new_token()
        lender = share_sharer.current() if self.borrow_from_sharing else None
        borrowed = lender.lend() if lender is not None else None
        if borrowed is not None:
            from ..share import identity as share_identity

            try:
                borrowed.host_party(token)
            except server_mod.ServerError:
                raise _Refused(BUSY) from None
            server, pin = borrowed, share_identity.identity().pin
            made["borrowed"] = True
        else:
            identity = tls.new_identity([lan] if lan else [])
            server, pin = server_mod.PartyServer(identity, token, job.port, bind=self.bind), identity.pin
        made["server"] = server
        hub = listening.ListenHub(job.me, library=_HostLibrary(server), queue=job.songs, index=job.index,
                                  position=job.position, playing=job.playing, dj=job.dj,
                                  on_event=self._sync_events(job.generation), title=job.title)
        made["hub"] = hub
        server.sync_handler = hub.accept
        if borrowed is not None:
            port = borrowed.port
            self._relay.status.emit(job.generation, "Finding your internet address…")
            status, mapping = PartySession._open_port(self, port, lan, lent=lender.mapping or False)
        else:
            try:
                port = server.start()
            except server_mod.ServerError as exc:
                raise _Refused(str(exc)) from None
            self._relay.status.emit(job.generation, f"Asking your router to open port {port}…")
            status, mapping = PartySession._open_port(self, port, lan)
        made["mapping"] = mapping
        try:
            code = invite.encode(invite.Invite(status.wan_ip, lan, port, token, pin, kind=invite.KIND_LISTEN))
        except invite.InviteError as exc:
            raise _Refused(str(exc)) from None
        made.update(port=port, status=status, code=code, link=invite.web_link(code))

    def _host_ready(self, job: _Job, error) -> None:
        made = job.result
        if job.generation != self._generation or self._role != "host":
            self._in_background(lambda: _stop_everything("host", made.get("hub"), made.get("server"),
                                                         made.get("mapping"), None, made.get("borrowed")))
            return
        if error is not None:
            if not isinstance(error, _Refused):
                _log.error("listening party: starting failed", exc_info=error)
            text = str(error) if isinstance(error, _Refused) else f"The listening party couldn't start: {error}"
            self._in_background(lambda: _stop_everything("host", made.get("hub"), made.get("server"),
                                                         made.get("mapping"), None, made.get("borrowed")))
            self._end(text, failed=True)
            return
        self._room = made["hub"]
        self._server = made["server"]
        self._borrowed = bool(made.get("borrowed"))
        self._mapping = made.get("mapping")
        self._port = made["port"]
        self._port_status = made["status"]
        self._invite_code = made["code"]
        self._invite_link = made["link"]
        self._phase = "on"
        self._status = ""
        if self._mapping is not None:
            self._renew.start()
        self._adopt_host_queue(job)
        self._player.set_party(self)
        self._start_follower()
        self._publish()
        if job.friend_id is not None:
            from ..share import presence

            presence.invite(job.friend_id, self._invite_code, (self.me or _people.me()).name)
            friend = db.friend(job.friend_id)
            name = friend["name"] if friend is not None else "them"
            self._say(f"Joining {name}… you'll hear what they hear in a moment.")
        else:
            self._say("Listening party on. Send your friends the code, or they can join "
                      "from their Friends page.")
        self.changed.emit()
        _log.info("listening party: hosting on port %s (%s)", self._port, self._port_status.state)

    def _adopt_host_queue(self, job: _Job) -> None:
        """The host's music is already playing the songs the party started with:
        mark them as the party's entries (1, 2, … in the room), so nothing reopens."""
        player = self._player
        mine, self._start_entries = self._start_entries, []
        if job.friend_id is not None or not mine:
            return                          # joining a friend: the follower opens their song
        wanted = (self._room.state.media or {}).get("e")
        # The room kept playing while the port opened (a few seconds): it may be a
        # song or two further on, as the player is by itself.
        if not isinstance(wanted, int) or not 0 < wanted <= len(mine) or mine[wanted - 1] is not player.current:
            return
        for number, entry in enumerate(mine, start=1):
            entry["party_e"] = number
        player._party_set_queue(mine, wanted - 1)
        player._party_context(self._context())

    def join(self, code: str, *, friend_id: int | None = None, quiet: bool = False) -> bool:
        """Join the listening party a code points at. Returns at once: False (with
        `error`) for a code that cannot be used; `changed` once in."""
        busy = self._busy_reason()
        if busy:
            self.error.emit(busy.replace("start another", "join another"))
            return False
        from . import invite

        try:
            parsed = invite.decode(code)
        except invite.InviteError as exc:
            self.error.emit(str(exc))
            return False
        if parsed.kind != invite.KIND_LISTEN:
            self.error.emit("That's not a listening party's code.")
            return False
        if friend_id is None:
            from ..share import nights

            friend = nights.host_friend(parsed.pin)
            friend_id = int(friend["id"]) if friend is not None else None
        me = self.me or _people.me()
        self._generation += 1
        self._role = "guest"
        self._phase = "joining"
        self._friend_id = friend_id
        self._quiet_join = quiet
        self._invite = parsed
        self._invite_code = invite.encode(parsed)
        self._invite_link = invite.web_link(parsed)
        client = listening.ListenClient(me, on_event=self._sync_events(self._generation))
        self._room = client
        self._status = "Trying your network…"
        self.changed.emit()
        client.connect(parsed)
        return True

    def _on_joined(self) -> None:
        client = self._room
        self._tunnel = listening.PartyTunnel(self._invite, client, address=client.address,
                                             host_name=self.host_name or None)
        self._tunnel.start()
        from ..config import data_dir

        generation, relay = self._generation, self._relay
        self._art = _ArtFetcher(client, self._tunnel, data_dir() / "party-art" / (client.party_id[:12] or "party"),
                                lambda song_id, path: relay.art.emit(generation, song_id, path))
        player = self._player
        if player.current is not None and not player.current.get("party"):
            self._before = (player.queue, player.index, player.position, player.context)
        self._phase = "on"
        self._status = ""
        player.set_party(self)
        self._adopt_guest_queue()
        self._start_follower()
        host = self.host_name
        if self._quiet_join:
            self._say(f"{host or 'Your friend'} joined you: you're listening together now")
        else:
            self._say(f"You joined {host}'s listening party" if host else "You joined the listening party")
        self.changed.emit()

    def _adopt_guest_queue(self) -> None:
        """Joined by a friend who is on this song already (share/presence.py): the
        song playing is the party's, and keeps playing."""
        room, player = self._room, self._player
        media = room.state.media or {}
        current = player.current
        if (current is None or self._friend_id is None or current.get("friend") != self._friend_id
                or current.get("remote_id") != media.get("id")):
            return
        view = room.queue_view
        index = self._view_index(view, media.get("e"))
        if index is None:
            return
        current["party_e"] = media.get("e")
        entries = [self._entry_for(item) for item in view["items"]]
        entries[index] = current
        player._party_set_queue(entries, index)
        player._party_context(self._context())

    # --- the player's side -------------------------------------------------------------------

    def _context(self) -> dict:
        return {"kind": "party", "title": self.title, "id": None}

    def _entry_for(self, item: dict) -> dict:
        """A queue item of the room's, as an entry for this player."""
        song_id = int(item["id"])
        if self._role == "host":
            row = self._rows.get(song_id)
            if row is None:
                from ..music import library

                found = library.track(song_id)
                row = dict(found) if found is not None else {
                    "id": song_id, "path": "", "title": item["title"], "artist": item["artist"],
                    "duration": item["duration"], "state": "ready"}
                self._rows[song_id] = row
            entry = dict(row)
        else:
            tunnel = self._tunnel
            art = self._art
            entry = {"id": -(PARTY_SONG_ID + song_id), "party": True, "party_song": song_id,
                     "path": tunnel.song_url(song_id) if tunnel is not None else "",
                     "title": item["title"], "artist": item["artist"], "album": item["album"],
                     "album_title": item["album"], "album_id": None,
                     "cover": art.paths.get(song_id) if art is not None else None,
                     "palette": item.get("palette") or None,
                     "duration": item["duration"] or 0.0, "state": "ready"}
        entry["party_e"] = item["e"]
        entry["party_by"] = item.get("by") or ""
        return entry

    @staticmethod
    def _view_index(view: dict, e) -> int | None:
        return next((i for i, item in enumerate(view.get("items") or []) if item["e"] == e), None)

    def _start_follower(self) -> None:
        if self._follower is not None or self._room is None:
            return
        generation = self._generation
        relay = self._relay
        self._follower = _MusicFollower(
            self._room, self._player, role=self._role or "guest", may_control=lambda: self.is_dj,
            realign=lambda: relay.realign.emit(generation),
            user=lambda what: relay.user.emit(generation, what),
            trouble=lambda what: relay.trouble.emit(generation, what))
        self._follower.start()

    def _on_realign(self, generation: int) -> None:
        """Put the room's song in the player, paused a little ahead of where the room
        will be once it is open; the follower starts it on the room's clock."""
        if generation != self._generation or self._phase != "on" or self._follower is None:
            return
        room = self._room
        state = room.state
        media = state.media or {}
        view = room.queue_view
        index = self._view_index(view, media.get("e"))
        if index is None:
            if not media.get("id"):
                return
            items, index = [{**media, "by": ""}], 0
        else:
            items = view["items"]
        entries = [self._entry_for(item) for item in items]
        host_now = room.host_time(perf())
        position = state.position_at(host_now)
        if state.playing:
            ahead = OPEN_AHEAD["host" if self._role == "host" else "guest"]
            position += min(4.0, ahead * (1.5 ** self._follower._late))
        duration = media.get("duration")
        if isinstance(duration, (int, float)) and duration > 0:
            position = min(position, max(0.0, duration - 0.25))
        self._player._party_load(entries, index, position, self._context())
        self._follower.opened(position)
        self._want_art(items, index)

    def _on_queue(self) -> None:
        """The queue changed: around the song playing, the player's queue follows."""
        follower, room, player = self._follower, self._room, self._player
        if follower is None or room is None:
            self.changed.emit()
            return
        media = room.state.media or {}
        current = player.current
        view = room.queue_view
        index = self._view_index(view, media.get("e"))
        if current is not None and index is not None and current.get("party_e") == media.get("e"):
            # Only when the songs around this one changed: a vote or a newcomer
            # sends the queue too, and writing mpv's playlist again for it cut
            # off the next song it was already opening (gapless prefetch).
            wanted = [item["e"] for item in view["items"]]
            have = [entry.get("party_e") for entry in player.queue]
            if wanted != have or player.index != index:
                entries = [self._entry_for(item) for item in view["items"]]
                player._party_set_queue(entries, index)
        if index is not None:
            self._want_art(view["items"], index)
        follower.wake()
        self.changed.emit()

    def _want_art(self, items: list, index: int) -> None:
        if self._art is not None:
            self._art.want([int(item["id"]) for item in items[max(0, index - 1):index + 4]])

    def _on_art(self, generation: int, song_id: int, path: str) -> None:
        """A cover arrived: on every entry of that song, and the page shows it."""
        if generation != self._generation:
            return
        player = self._player
        current = player.current
        for entry in (*player._queue, *player._original):
            if entry.get("party_song") == song_id:
                entry["cover"] = path
        if current is not None and current.get("party_song") == song_id:
            player.track_changed.emit(current)
        else:
            player.track_updated.emit(next((e for e in player._queue if e.get("party_song") == song_id), None))

    # --- what a person does (the player asks) -----------------------------------------------------

    def toggle(self) -> None:
        if self._role == "guest" and not self.is_dj:
            self._tune(in_=self.tuned_out)
            return
        state = self.state
        if state is not None:
            self._act("pause" if (state.playing or state.waiting) else "play")

    def play(self) -> None:
        if self._role == "guest" and not self.is_dj:
            self._tune(in_=True)
        else:
            self._act("play")

    def pause(self) -> None:
        if self._role == "guest" and not self.is_dj:
            self._tune(in_=False)
        else:
            self._act("pause")

    def next(self) -> None:
        self._act("next" if self.is_dj else "vote")

    def vote(self) -> None:
        self._act("vote")

    def previous(self) -> None:
        if self.is_dj:
            self._act("previous")
        else:
            self._say(f"{self._dj_title()} is the DJ: they go back and skip. You can vote to skip.")

    def seek(self, seconds: float) -> None:
        if self.is_dj:
            self._act("seek", position=max(0.0, float(seconds)))

    def jump(self, local_index: int) -> None:
        """Play now, on an Up next song: the room's queue position of that entry."""
        entry = self._player.queue[local_index] if 0 <= local_index < len(self._player.queue) else None
        e = entry.get("party_e") if entry is not None else None
        view = self.queue_view
        position = self._view_index(view, e)
        if position is None:
            return
        if self.is_dj:
            self._act("jump", index=view["start"] + position)
        else:
            self._say(f"{self._dj_title()} is the DJ: ask them to play it, or vote to skip.")

    def remove(self, local_index: int) -> None:
        entry = self._player.queue[local_index] if 0 <= local_index < len(self._player.queue) else None
        if entry is not None and isinstance(entry.get("party_e"), int):
            self._act("remove", e=entry["party_e"])

    def add(self, track, next: bool = False) -> None:
        """Add to queue, Play next: into the party's queue, if the song is the host's."""
        song_id = self._host_song(dict(track))
        if song_id is None:
            self._say(FRIEND_SONGS.format(host=self.host_name or "the host") if self._role == "guest"
                      else NOT_YOURS)
            return
        self._act("add", id=song_id, next=bool(next))

    def add_song(self, song_id: int, next: bool = False) -> None:
        """A song of the host's, from the party panel's search."""
        self._act("add", id=int(song_id), next=bool(next))

    def takes_over(self, tracks, start, context, shuffle: bool = False) -> bool:
        """Play on an album, a playlist, Shuffle: the DJ's becomes the party's queue.
        A guest playing their own music leaves the party to do it (said so)."""
        rows = [dict(track) for track in tracks]
        ids = [self._host_song(row) for row in rows]
        if self.is_dj and rows and all(song_id is not None for song_id in ids):
            if shuffle:
                import random

                random.shuffle(ids)
                index = 0
            else:
                index = start if isinstance(start, int) and 0 <= start < len(ids) else 0
            title = str((context or {}).get("title") or "")
            self._act("replace", ids=ids[:listening.MAX_QUEUE], index=index, title=title)
            return True
        if self._role == "guest":
            host = self.host_name or "the"
            self._say(f"You left {host}'s listening party to play your own music.")
            self._end("", keep_music=True)
            return False
        self._say(NOT_YOURS if self._role == "host" else "")
        return True

    def _host_song(self, row: dict) -> int | None:
        """The host's song id for a queue entry or library row, or None if the host
        doesn't have it."""
        if row.get("party_song"):
            return int(row["party_song"])
        if self._role == "host":
            if row.get("friend") or row.get("party"):
                return None
            try:
                return int(row["id"])
            except (KeyError, TypeError, ValueError):
                return None
        if self._friend_id is not None and row.get("friend") == self._friend_id and row.get("remote_id"):
            return int(row["remote_id"])
        return None

    def search(self, text: str) -> int:
        room = self._room
        return room.search(text) if room is not None and self._phase == "on" else 0

    def _dj_title(self) -> str:
        name = self.dj_name
        return name[:1].upper() + name[1:] if name else "The host"

    def _act(self, action: str, **fields) -> None:
        room = self._room
        if room is None or self._phase != "on":
            return
        room.act(action, **fields)

    def _tune(self, in_: bool) -> None:
        """A guest's own pause: the party plays on without them, and play catches up."""
        follower = self._follower
        mpv = self._player._mpv
        if follower is None:
            return
        if in_:
            if not follower.tuned_out:
                return
            follower.tuned_out = False
            follower.drift.reset(0.0)
            follower.wake()
            self._say("Back with the party")
        else:
            follower.tuned_out = True
            if mpv is not None and mpv.is_running:
                follower._pause(mpv, True)
            self._say("Paused for you. The party plays on: press play to catch up.")
        self.changed.emit()

    def _on_user(self, generation: int, what: str) -> None:
        """mpv paused or played by a media key or Windows' media overlay."""
        if generation != self._generation or self._phase != "on":
            return
        if what == "pause":
            self.pause()
        else:
            self.play()

    def _on_trouble(self, generation: int, what: str) -> None:
        if generation != self._generation or self._follower is None:
            return
        if what == "follower" and self._restarts < 3:
            self._restarts += 1
            follower, self._follower = self._follower, None
            follower.stop()
            self._start_follower()

    def sleep_fired(self) -> None:
        self.leave()

    # --- the room's news (Qt's thread) -------------------------------------------------------------

    def _sync_events(self, generation: int) -> Callable:
        relay = self._relay

        def deliver(event) -> None:
            follower = self._follower
            if follower is not None and event.kind in ("state", "queue"):
                follower.wake()
            relay.event.emit(generation, event)
        return deliver

    def _on_status(self, generation: int, text: str) -> None:
        if generation == self._generation and self._phase in ("starting", "joining") and text:
            self._status = text
            self.message.emit(text)
            self.changed.emit()

    def _on_event(self, generation: int, event) -> None:
        if generation != self._generation or self._role is None:
            return
        kind = event.kind
        if kind == "connecting":
            self._status = event.text
            if event.text:
                self.message.emit(event.text)
            self.changed.emit()
        elif kind == "connected":
            self._on_joined()
        elif kind == "state":
            if event.text:
                self._say(event.text)
            self._publish()
            self.changed.emit()
        elif kind == "queue":
            self._on_queue()
            self._publish()
        elif kind == "note":
            if event.text:
                self._say(event.text)
            self.changed.emit()
        elif kind == "people":
            if event.text:
                self._say(event.text)
            self._publish()
            self.changed.emit()
        elif kind == "found":
            data = event.data or {}
            self.found.emit(int(data.get("n") or 0), list(data.get("items") or []))
        elif kind == "notice":
            if event.text:
                self._say(event.text)
        elif kind == "ended":
            self._on_ended(event.text)

    def _on_ended(self, reason: str) -> None:
        if self._phase in ("joining", "starting"):
            self._end(reason or "The listening party ended before you got in.", failed=True)
            return
        if self._role == "guest" and reason in ("The host ended the movie night.", ""):
            host = self.host_name
            reason = f"{host} ended the listening party." if host else "The listening party ended."
        elif self._role == "guest" and reason == sync.LOST_HOST:
            host = self.host_name
            reason = f"Lost the connection to {host}'s listening party." if host else \
                "Lost the connection to the listening party."
        self._end(reason, by_room=True)

    def _say(self, text: str) -> None:
        if text:
            self._activity.append(text)
            self.message.emit(text)

    def _publish(self) -> None:
        """Friends see a party on this PC from its hello (share/presence.py)."""
        if self._role != "host" or self._phase != "on":
            return
        from ..share import presence

        media = (self.state.media or {}) if self.state is not None else {}
        presence.set_party({"code": self._invite_code, "song": str(media.get("title") or ""),
                            "artist": str(media.get("artist") or ""),
                            "people": len(self.people), "host": (self.me or _people.me()).name})

    # --- ending -------------------------------------------------------------------------------------

    def leave(self) -> None:
        """End it (the host, for everyone) or leave it (a guest). Returns at once."""
        if self._role is not None:
            self._end("")

    def _end(self, reason: str, by_room: bool = False, failed: bool = False,
             keep_music: bool = False) -> None:
        if self._role is None:
            return
        role, room, server, mapping, tunnel = self._role, self._room, self._server, self._mapping, self._tunnel
        borrowed, friend_id, before = self._borrowed, self._friend_id, self._before
        follower, self._follower = self._follower, None
        art, self._art = self._art, None
        if art is not None:
            art.stop()
        if follower is not None:
            follower.stop()
        self._generation += 1
        self._renew.stop()
        player = self._player
        if player.party is self:
            player.set_party(None)
        if role == "host":
            from ..share import presence

            presence.set_party(None)
            if friend_id is not None:
                presence.cancel(friend_id)
            player._party_untag()
            player._party_context({"kind": "queue", "title": "Your queue", "id": None})
        elif not keep_music:
            # The party's songs came from the host through this party's proxy,
            # which goes now: the music stops, and what was playing before comes back.
            player.stop()
            if before is not None:
                queue, index, position, context = before
                if queue and 0 <= index < len(queue):
                    player._party_load(queue, index, position, context)
        self._reset()
        self._in_background(lambda: _stop_everything(role, room, server, mapping, tunnel, borrowed))
        self.changed.emit()
        if failed:
            self.error.emit(reason)
        elif by_room and reason:
            self.message.emit(reason)
        self.ended.emit(reason)

    def shutdown(self) -> None:
        """Mistery is quitting: the same, finished before this returns."""
        if self._role is None:
            return
        role, room, server, mapping, tunnel = self._role, self._room, self._server, self._mapping, self._tunnel
        borrowed = self._borrowed
        follower, self._follower = self._follower, None
        if follower is not None:
            follower.stop(1.0)
        art, self._art = self._art, None
        if art is not None:
            art.stop()
        self._generation += 1
        if self._player.party is self:
            self._player.set_party(None)
        self._player._party_untag()
        if role == "host":
            from ..share import presence

            presence.set_party(None)
            presence.cancel()
        self._reset()
        _stop_everything(role, room, server, mapping, tunnel, borrowed, quick=True)
        self.ended.emit("")

    def _renew_if_due(self) -> None:
        mapping = self._mapping
        if mapping is None or not mapping.lease or time.time() < mapping.expires - 20 * 60:
            return
        from . import upnp

        self._in_background(lambda: upnp.renew(mapping), lambda result, error: None)

    def _in_background(self, work: Callable, done: Callable | None = None) -> None:
        relay = self._relay

        def run() -> None:
            try:
                result, error = work(), None
            except BaseException as exc:        # noqa: BLE001 - handed to done()
                result, error = None, exc
            finally:
                try:
                    db.close_thread_connection()
                except Exception:               # noqa: BLE001
                    pass
            if done is not None:
                try:
                    relay.done.emit(done, result, error)
                except RuntimeError:
                    pass
        threading.Thread(target=run, name="party-listen-work", daemon=True).start()


def _stop_everything(role, room, server, mapping, tunnel, borrowed, quick: bool = False) -> None:
    """The movie night's order (session._stop_everything), and this party's song
    offers withdrawn from a listener that library sharing keeps."""
    from .session import _stop_everything as stop

    if role == "host" and server is not None:
        try:
            server.withdraw_all(listening.LISTEN_OFFERS)
        except Exception:                       # noqa: BLE001
            _log.exception("listening party: withdrawing its songs failed")
    stop(role, room, server, mapping, tunnel, quick=quick, borrowed=bool(borrowed))


# --- telling a friend what of theirs is playing here --------------------------------------------

class PresenceReporter(QObject):
    """Says to a friend's PC which of their songs this player is on (share/presence.py),
    every few seconds while it is, over the channel the songs come through; and
    hears back, when they join, the code of their listening party.

    Off with Settings' share_presence. Nothing is said while this player is in a
    listening party already, and a friend whose Mistery predates it is not asked again.
    """

    join_offered = Signal(int, str, str)        # friend id, code, their name
    _answered = Signal(int, object)

    def __init__(self, player, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._player = player
        self._last: dict[int, tuple] = {}       # friend id -> (song, playing, sent at)
        self._busy: set[int] = set()
        self._answered.connect(self._on_answer)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.tick)
        self._timer.start()
        player.track_changed.connect(lambda _track: QTimer.singleShot(200, self.tick))
        player.state_changed.connect(lambda: QTimer.singleShot(200, self.tick))

    def _current(self) -> tuple[int, int] | None:
        player = self._player
        current = player.current
        if (current is None or player.party is not None or not current.get("friend")
                or not current.get("remote_id") or not settings.get("share_presence", True)):
            return None
        return int(current["friend"]), int(current["remote_id"])

    def tick(self) -> None:
        from ..share import presence

        now = time.monotonic()
        current = self._current()
        for friend_id in list(self._last):
            if current is None or current[0] != friend_id:
                del self._last[friend_id]
                self._send(friend_id, {"stopped": True})
        if current is None:
            return
        friend_id, song = current
        player = self._player
        playing = player.is_playing
        last = self._last.get(friend_id)
        every = presence.REPORT_PLAYING if playing else presence.REPORT_PAUSED
        if last is None or last[0] != song or last[1] != playing or now - last[2] >= every:
            upcoming = [int(t["remote_id"]) for t in player.upcoming
                        if t.get("friend") == friend_id and t.get("remote_id")][:presence.MAX_NEXT]
            self._last[friend_id] = (song, playing, now)
            self._send(friend_id, {"id": song, "position": round(float(player.position), 2),
                                   "playing": bool(playing), "next": upcoming})

    def _send(self, friend_id: int, payload: dict) -> None:
        from ..share import music

        tunnel = music.existing(friend_id)
        if tunnel is None or tunnel.presence_unknown or friend_id in self._busy:
            return
        self._busy.add(friend_id)
        answered = self._answered

        def run() -> None:
            try:
                answer = tunnel.report(payload)
            except Exception:                   # noqa: BLE001 - a report is never worth an error
                _log.exception("listening: telling a friend what plays failed")
                answer = None
            finally:
                db.close_thread_connection()
            try:
                answered.emit(friend_id, answer)
            except RuntimeError:
                pass
        threading.Thread(target=run, name="share-presence", daemon=True).start()

    def _on_answer(self, friend_id: int, answer) -> None:
        self._busy.discard(friend_id)
        join = answer.get("join") if isinstance(answer, dict) else None
        if isinstance(join, dict) and isinstance(join.get("code"), str):
            name = _people.clean_name(join.get("name")) or "Your friend"
            self.join_offered.emit(friend_id, join["code"], name)
