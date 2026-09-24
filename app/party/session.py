"""Movie night, for the Qt side: one PartySession per window.

The engine beside this file (invite, upnp, stun, tls, server, transcode,
guest_proxy, people, sync) knows nothing of Qt. This is the glue. It starts a
movie night (the host) or joins one (a guest), does everything that blocks on
worker threads, turns the room's news into signals and into lines on the
picture, keeps this window's player where the room is, and keeps the party's
place in party_progress.

    session = PartySession(window)          # the MainWindow, or a bare PlayerView
    session.start_host(item, "auto")        # returns at once; `changed` as it goes
    session.join(code)                      # the same, for a guest
    session.rejoin()                        # a guest whose connection went: the same code again
    session.leave()                         # the host ends it for everyone

What it never does is move anybody's own progress. While a movie night runs
the player writes no resume point, marks nothing watched and counts no play:
PlayerView checks `_party_item` wherever it would, for as long as that title
stays on screen, even after the movie night itself has ended. The only thing
written is the party's own row in party_progress, on every side, every 10 s
and at every pause, seek, wait and end, with the room's position rather than
this player's: the room is where everyone is, the player is only near it.

Following the room. Every player in a movie night, the host's own included,
follows the room the same way: a thread per player (_Follower) reads mpv's
position ten times a second, asks sync.DriftController what to do and does
it, without ever sending it back as an intent. That is the recipe
test_sync_mpv measured (players 43-68 ms apart over 20 s), with what a real
player adds to it: opening a stream where the room will be by the time it is
ready, a transcoded stream that can only seek by being opened again, and
telling the room when this player is stuck. In two PlayerViews on this PC, a
host's and a guest's, over a minute (features3/player, several runs): 0-10 ms
apart at the median, 31-32 ms at the 95th percentile, never 43 ms apart; they
start 0-17 ms apart, and a pause reaches both players 4-22 ms after the key.
Against the room's own clock both are 23-50 ms off at the median (p95 43-70
ms, counting half a frame for the frame on screen; they start 21-60 ms
early), the same way for both, so it never shows between them: it is inside
the controller's 80 ms dead band and left there.

Nothing here blocks Qt's thread for longer than an IPC write: connecting,
the handshake, the router, STUN, stopping the server and the proxy all run on
worker threads, and their results come back through queued signals.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QCoreApplication, QObject, Qt, QTimer, Signal

from .. import db
from ..config import settings
from ..models import MediaItem
from . import people as _people
from . import sync
from .people import Person

_log = logging.getLogger("party.session")

perf = time.perf_counter        # the clock sync measures with (sync.now)

# How often the party's place is written while nothing else happens. Every
# pause, seek, wait and end writes it at once as well, so a crash loses at
# most 10 s of where the party got to.
SAVE_EVERY_MS = 10_000

# The lease the router gives a forward is 3 hours (upnp.LEASE); a movie night
# of two films outlasts it. Looked at every 10 minutes, renewed in the last 20.
RENEW_CHECK_MS = 10 * 60 * 1000
RENEW_BEFORE = 20 * 60

QUALITY_CHOICES = ("original", "1080p", "720p")

# A guest's stream that stalls twice within 3 minutes (a stall and the room
# waiting for it are one), is offered a lighter stream ("Your stream keeps
# pausing — switch to 1080p?"): at most once every 10 minutes, never for its
# own first 2 s after an open (one 1080p open in six paused 0.55 s for its
# cache a second in, measured by the stream builder, and that is not a
# pattern). A PC that cannot decode the original smoothly does not stall, it
# drops frames: 24 in 10 s (a tenth of them at 24 fps) counts the same.
STRUGGLE_WINDOW = 180.0
STRUGGLES_TO_OFFER = 2
SAME_STRUGGLE = 5.0
OFFER_AGAIN_AFTER = 600.0
DROPS_TO_OFFER = 24


# --- what the host panel shows about the port ------------------------------------------------

@dataclass(frozen=True)
class PortStatus:
    """Whether friends can reach this PC, and why not.

    state   checking   the port is being opened
            upnp       the router opened it: friends anywhere can join
            manual     the router did not, but the invite carries the internet
                       address, so a port forwarded by hand works
            lan        no internet address at all: the code works at home only
            cgnat      the provider shares one address between homes; nothing
                       from outside can reach this PC, by hand or otherwise
            failed     the router gave the port to another device
    text is the sentence to show, with the real port and the real LAN address.

    forwarded is the owner's word that they forwarded the port to this PC by
    hand (Settings → Movie night, party_forwarded), whatever the state, for the
    panel to word it by. Only their word: without UPnP nothing here can check a
    forward. With it, "manual" is one calm line that still claims nothing:
    "You've forwarded port 42170 to this PC."
    PartySession.port_status keeps it the setting as it is now, so ticking it
    during a movie night ("I've done this" on the panel) rewords it at once.
    """

    state: str
    text: str
    port: int | None = None
    lan_ip: str | None = None
    wan_ip: str | None = None       # what the invite carries; None when nothing outside can use it
    router: str | None = None       # the router's address, where its settings page usually is
    vpn: str | None = None          # the VPN carrying this PC's traffic, if any (upnp.vpn())
    forwarded: bool = False         # the owner says the port is forwarded to this PC by hand

    @property
    def reason(self) -> str:
        return self.text

    def __str__(self) -> str:
        return self.text


class _Refused(Exception):
    """A movie night that could not start; the message is the sentence to show."""


@dataclass
class _Source:
    """What this player opens for the room's media, and how.

    kind is "file" when mpv can seek in it by itself (the host's own file, or
    the original through the guest's proxy, which mpv seeks with Range
    requests) and "transcode" when it cannot (ffmpeg's stream from a start
    time: moving elsewhere means opening it again there). url(aim) gives
    (url, start_at, per-file options) for a stream that should show `aim`.
    """

    kind: str
    key: str
    url: Callable[[float], tuple[str, float, dict]]
    quality: str = "original"


@dataclass
class _Opening:
    aim: float
    seq: int
    key: str
    started: float
    expected: float
    loads_before: int
    reason: str
    told: bool = False          # the session has heard this open is slow


@dataclass
class _Job:
    """A movie night being started, as the worker thread sees it."""

    item: MediaItem
    quality: str
    port: int
    party_id: str | None
    position: float
    me: Person
    lan: str | None
    generation: int
    result: dict = field(default_factory=dict)


# --- following the room -------------------------------------------------------------------

class _Follower:
    """Keeps one player where the room is, on a thread of its own.

    Ten times a second: read mpv's position (a round trip to mpv, stamped with
    when it was asked for), hand it to sync.DriftController, do what the
    Correction says, and tell the room where this player is. A start the room
    schedules is made first and read after, so the reading's round trip (12-14
    ms here) does not make every player that much late by a different amount.

    Opening. A stream is opened paused, aimed where the room will be once it
    is ready: the room's position plus how long this kind of open took the last
    three times (0.5 s for a file, 1.7 s for a transcode until there are any)
    plus a second. Once mpv shows the frame there it waits for the room to
    arrive and starts on the room's clock. The stream builder measured this
    recipe at -0.42 to +0.07 s from the room in 12 switches of 12, against
    1.4-2.8 s behind for reopening at the room's position and playing at once.
    Here a guest's switch to 1080p mid-episode showed a still picture for
    2.7 s and started 21-117 ms ahead of the room, the nudge's to take back:
    over the next 20 s it was 20-61 ms off at the 95th percentile (several
    runs). While a room is paused there is nothing to aim ahead of: it opens
    at the held position and the controller's hold takes it from there.

    A transcoded stream cannot seek (mpv's own seek 400 s ahead stays where it
    was). What mpv has already read it can seek in (its cache keeps both sides
    of the position), so a correction lands there when it can: a room seek 5 s
    back was in step 1.4-1.5 s later, with no new ffmpeg. Anywhere else it is
    opened again by the same recipe: 5 minutes ahead, in step 3.3-4.8 s later. A
    held player on a transcode is left up to 0.25 s off rather than reopened
    for a frame: the nudge takes that back within 5 s of the start.

    The room hears "buffering" when mpv is seeking or waiting for its cache.
    The hub waits a second before it stops everyone, so a seek's quarter of a
    second passes unnoticed. An open is not reported unless it is still not
    ready 2 s after it should have been: a switch to 1080p takes 1-4 s, and the
    whole room pausing for one friend's choice is the wrong way round.
    """

    POLL = 0.1
    MARGIN = 1.0
    READY_WINDOW = (-0.05, 2.0)         # a frame this close to the aim counts as there
    LATE = 2.0
    # An open still not ready this long after it was expected, or a stall this
    # long, is a connection that cannot carry this stream: the session offers a
    # lighter one then, without waiting for a second time. Nothing is reopened
    # for being slow. Opening the 4K film at 25:00 through a guest's proxy reads
    # 32 MB (mpv starts from the file's previous index point): 0.7 s here, and
    # 25.7 s over a 10 Mbit/s connection, which a reopen would only start again.
    SLOW = 8.0
    FIRST_OPEN = {"file": 0.5, "transcode": 1.7}
    TRANSCODE_HOLD_SLACK = 0.25
    CACHE_EDGE = 0.5                    # a seek this close to the end of what is cached may not land

    def __init__(self, room, mpv, frame: float, source: _Source | None,
                 on_trouble: Callable[[str], None]) -> None:
        self.room = room
        self.mpv = mpv
        self.frame = frame
        self.controller = sync.DriftController(frame=frame)
        self.source = source
        self._on_trouble = on_trouble
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._wanted: tuple[_Source, str] | None = None
        self._opening: _Opening | None = None
        self._start: tuple[float, int] | None = None     # (perf moment, room seq) of a start
        self._speed = 1.0
        self._buffering = False
        self._stalled = False
        self._stall_began = 0.0
        self._stall_told = False
        self.stalls = 0
        self._drops: deque[tuple[float, int]] = deque(maxlen=4)      # (perf, frames dropped so far)
        self._drops_told = -1e9
        self._last_ready = perf()
        self._opens: dict[str, deque[float]] = {"file": deque(maxlen=3), "transcode": deque(maxlen=3)}
        self._loaded = 0                # file-loaded events seen, counted on mpv's pipe thread
        # For the tests and the report: (perf when asked, position, room seq),
        # how late each start command went out, and each open's time to its frame.
        self.readings: deque[tuple[float, float, int]] = deque(maxlen=4000)
        self.starts: list[float] = []
        self.open_times: list[tuple[str, str, float]] = []
        self.reopens = 0
        self._count = self._count_load       # one object, so disconnect finds it
        mpv.file_loaded.connect(self._count, Qt.ConnectionType.DirectConnection)
        self.thread = threading.Thread(target=self._run, name="party-follow", daemon=True)

    # --- from Qt's thread ---------------------------------------------------------

    def start(self) -> None:
        self.thread.start()

    def open(self, source: _Source, reason: str = "open") -> None:
        """Open the room's media from this source, where the room will be."""
        with self._lock:
            self._wanted = (source, reason)
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stopping.set()
        self._wake.set()
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(timeout)
        try:
            self.mpv.file_loaded.disconnect(self._count)
        except (RuntimeError, TypeError):
            pass
        if self._speed != 1.0:
            self.mpv.set_speed(1.0)         # the nudge is the room's, not this player's
        if self._buffering:
            self._buffering = False
            self.room.set_buffering(False)

    @property
    def buffering(self) -> bool:
        return self._buffering

    @property
    def opening(self) -> bool:
        return self._opening is not None or self._wanted is not None

    # --- its own thread -------------------------------------------------------------

    def _count_load(self) -> None:
        self._loaded += 1

    def _run(self) -> None:
        try:
            while not self._stopping.is_set():
                wait = self._step()
                if wait > 0 and not self._stopping.is_set():
                    self._wake.wait(wait)
                    self._wake.clear()
        except Exception:
            _log.exception("movie night: following the room stopped")
            self._on_trouble("follower")

    def _read(self, name: str):
        reply = self.mpv.command_sync("get_property", name, timeout=1.0)
        return reply.get("data") if reply.get("error") == "success" else None

    def _step(self) -> float:
        mpv = self.mpv
        if not mpv.is_running:
            return 0.25
        # A start the room scheduled: made first, read after (sync.Correction).
        start = self._start
        if start is not None:
            moment, seq = start
            left = moment - perf()
            if self.room.state.seq != seq:
                self._start = None              # the room changed its mind: judge afresh
            elif left > 0.1:
                return left - 0.1               # woken early by news: looked at again then
            else:
                if left > 0:
                    time.sleep(left)            # a high-resolution sleep: the start is the point
                self._start = None
                state = self.room.state
                if state.seq == seq and state.playing:
                    mpv.set_property("pause", False)
                    self.starts.append(perf() - moment)
        with self._lock:
            wanted, self._wanted = self._wanted, None
        if wanted is not None:
            self._begin_open(*wanted)

        asked = perf()
        position = self._read("time-pos")
        if self._stopping.is_set():
            return 0.0
        paused = bool(mpv.cached("pause"))
        seeking = bool(mpv.cached("seeking"))
        stalled = bool(mpv.cached("paused-for-cache"))
        state = self.room.state
        host_now = self.room.host_time(asked)
        if position is not None:
            self.readings.append((asked, float(position), state.seq))
        if self._opening is not None:
            return self._continue_open(state, host_now, position, seeking or stalled, asked)
        if self.source is not None and (state.media or {}).get("key") not in (None, self.source.key):
            # The room has moved on to something new (Next episode together) and
            # the player is about to open it: the old file is not chased there.
            return self.POLL

        self._note_stall(stalled, state, host_now)
        self._set_buffering(seeking or stalled)
        if self._at_the_end(state, host_now):
            return self.POLL
        correction = self.controller.update(state, host_now, position, paused=paused,
                                            seeking=seeking or stalled)
        if correction.pause != paused:
            mpv.set_property("pause", correction.pause)
        if correction.seek_to is not None:
            self._seek(correction, position, state)
        if self._opening is None and correction.speed != self._speed:
            mpv.set_speed(correction.speed)
            self._speed = correction.speed
        if position is not None:
            self.room.report_position(float(position), asked)
            if not paused and state.moving_at(host_now):
                self._note_drops()
        wake_in = correction.wake_in
        if self._opening is None and wake_in is not None and 0 < wake_in <= self.POLL + 0.1:
            self._start = (asked + wake_in, state.seq)
            return max(0.0, self._start[0] - perf() - 0.1)
        return self.POLL

    def _at_the_end(self, state: sync.RoomState, host_now: float) -> bool:
        """mpv holds the last frame (--keep-open), paused. Unpaused there it pauses
        itself again; that is not drift, and the room is about to end too."""
        duration = state.duration
        return bool(duration and self.mpv.cached("eof-reached")
                    and state.position_at(host_now) >= duration - 2.0)

    def _seek(self, correction: sync.Correction, position: float | None, state: sync.RoomState) -> None:
        target = correction.seek_to
        if self.source is None or self.source.kind != "transcode" or self._cached(target):
            self.mpv.seek_absolute(target)
            return
        if correction.pause and position is not None and \
                abs(position - target) <= self.TRANSCODE_HOLD_SLACK:
            return
        self.reopens += 1
        self._begin_open(self.source, "seek")

    def _cached(self, target: float) -> bool:
        """Whether mpv can land on target from what it has already read."""
        cache = self._read("demuxer-cache-state")
        ranges = cache.get("seekable-ranges") if isinstance(cache, dict) else None
        for span in ranges or ():
            try:
                if float(span["start"]) + self.CACHE_EDGE <= target <= float(span["end"]) - self.CACHE_EDGE:
                    return True
            except (KeyError, TypeError, ValueError):
                continue
        return False

    def _expected(self, kind: str) -> float:
        took = self._opens.get(kind) or ()
        return sum(took) / len(took) if took else self.FIRST_OPEN.get(kind, 1.0)

    def _track_options(self, same_stream: bool) -> dict:
        """The audio and subtitles this player has on now, to keep across a reopen.
        The same stream has the same tracks in the same order, so their numbers
        carry over; another quality has other numbers (a transcode keeps four
        audio tracks at most), so the languages do instead."""
        options: dict[str, str] = {}
        visible = self.mpv.cached("sub-visibility")
        if isinstance(visible, bool):
            options["sub-visibility"] = "yes" if visible else "no"
        if same_stream:
            aid = self.mpv.cached("aid")
            if isinstance(aid, int) and not isinstance(aid, bool):
                options["aid"] = str(aid)
            sid = self.mpv.cached("sid")
            if isinstance(sid, int) and not isinstance(sid, bool):
                options["sid"] = str(sid)
            elif sid is False or sid == "no":
                options["sid"] = "no"
            return options
        for kind, name in (("audio", "alang"), ("sub", "slang")):
            chosen = next((t.get("lang") for t in self.mpv.tracks(kind) if t.get("selected")), None)
            if chosen:
                options[name] = str(chosen)
        return options

    def _begin_open(self, source: _Source, reason: str) -> None:
        state = self.room.state
        now = perf()
        host_now = self.room.host_time(now)
        same_stream = self.source is not None and self.source.kind == source.kind \
            and self.source.key == source.key and self.source.quality == source.quality
        expected = self._expected(source.kind)
        aim = state.position_at(host_now + expected + self.MARGIN) if state.playing \
            else state.position_at(host_now)
        duration = state.duration
        if duration:
            aim = min(aim, max(0.0, duration - 1.0))
        url, start_at, options = source.url(aim)
        if reason != "open" and self.source is not None and self.source.key == source.key:
            options = {**self._track_options(same_stream), **options}
        self.source = source
        self._start = None
        self._opening = _Opening(aim=aim, seq=state.seq, key=source.key, started=now,
                                 expected=expected, loads_before=self._loaded, reason=reason)
        self.mpv.load(url, start_at=start_at, options=options)
        self.mpv.pause()
        if self._speed != state.rate:
            self.mpv.set_speed(state.rate)
            self._speed = state.rate
        self.controller.reset(self.frame)
        _log.info("movie night: opening %s (%s) at %.2f, expecting %.2f s", source.kind, reason,
                  aim, expected)

    def _continue_open(self, state: sync.RoomState, host_now: float, position, busy: bool,
                       asked: float) -> float:
        opening = self._opening
        if state.seq != opening.seq:
            opening.seq = state.seq
            if (state.media or {}).get("key") != opening.key:
                return self.POLL            # new media: the Qt side hands over its source
            moved = state.cause == "seek" or (
                not state.playing and abs(state.position_at(host_now) - opening.aim) > self.TRANSCODE_HOLD_SLACK
                and state.position_at(host_now) < opening.aim)
            if moved and self.source is not None:
                self._begin_open(self.source, "moved")
                return self.POLL
        low, high = self.READY_WINDOW
        ready = (self._loaded > opening.loads_before and position is not None and not busy
                 and opening.aim + low <= float(position) <= opening.aim + high)
        now = perf()
        waited = now - opening.started
        if ready:
            self._opening = None
            self._opens[self.source.kind].append(waited)
            self.open_times.append((self.source.kind, opening.reason, waited))
            self._last_ready = now
            self._set_buffering(False)
            if state.playing:
                # Hold on the frame at the aim until the room arrives there, then go.
                behind = max(0.0, opening.aim - state.position) / (state.rate or 1.0)
                arrives = max(state.at, host_now) if state.position >= opening.aim else state.at + behind
                if state.moving_at(host_now) and state.position_at(host_now) >= opening.aim:
                    arrives = host_now      # late already: go now, the controller catches up
                self._start = (asked + (arrives - host_now), state.seq)
                return max(0.0, self._start[0] - perf() - 0.1)
            return self.POLL                # a paused room: the controller holds from here
        if state.playing and waited > opening.expected + self.MARGIN + self.LATE:
            self._set_buffering(True)       # really stuck: the room waits (after its second of grace)
        if waited > opening.expected + self.MARGIN + self.SLOW and not opening.told:
            opening.told = True
            self._on_trouble("slow")
        return 0.05

    def _set_buffering(self, on: bool) -> None:
        if on != self._buffering:
            self._buffering = on
            self.room.set_buffering(on)

    def _note_stall(self, stalled: bool, state: sync.RoomState, host_now: float) -> None:
        """Tell the session about a stall while the room plays; not in the first
        2 s after an open, which is the open settling rather than the stream."""
        began = stalled and not self._stalled
        self._stalled = stalled
        now = perf()
        if began:
            self._stall_began = now
            self._stall_told = False
            if state.moving_at(host_now) and now - self._last_ready >= 2.0:
                self.stalls += 1
                self._on_trouble("stall")
        elif stalled and not self._stall_told and now - self._stall_began > self.SLOW:
            self._stall_told = True
            self._on_trouble("slow")

    def _note_drops(self) -> None:
        """Frames dropped because this PC could not decode or show them in time,
        looked at every 5 s while playing: a stream that never stalls can still
        be too much for the PC it plays on (4K HEVC 10-bit, Dolby Vision)."""
        now = perf()
        if self._drops and now - self._drops[-1][0] < 5.0:
            return
        counts = [self._read(name) for name in ("frame-drop-count", "decoder-frame-drop-count")]
        total = sum(int(c) for c in counts if isinstance(c, int) and not isinstance(c, bool))
        self._drops.append((now, total))
        older = [count for when, count in self._drops if now - when >= 9.5]
        if (older and total - older[-1] >= DROPS_TO_OFFER and now - self._last_ready > 2.0
                and now - self._drops_told > STRUGGLE_WINDOW):
            self._drops_told = now
            self._on_trouble("drops")


# --- the session ----------------------------------------------------------------------------

class _Relay(QObject):
    """Carries news from the movie night's threads to Qt's thread (queued)."""

    event = Signal(int, object)                 # generation, sync.Event
    done = Signal(object, object, object)       # callback, result, error
    trouble = Signal(int, str)                  # generation, what the follower saw
    status = Signal(int, str)                   # generation, what a start is doing now


class PartySession(QObject):
    """The movie night of one window: host or guest, or neither.

    Signals
        changed         role, people, invite, port status, title: anything a
                        panel shows. Also emitted as a start or join goes along.
        message(str)    a short line: "Trying your network…", "Sam paused",
                        "Alex joined", "Sam could not join: …"
        error(str)      something did not work, as a sentence (then ended)
        ended(str)      the movie night is over here; why ("" when we ended it)
    """

    changed = Signal()
    message = Signal(str)
    error = Signal(str)
    ended = Signal(str)

    # Where the listener listens and how the port gets opened. Tests set these
    # on the instance: 127.0.0.1 (0.0.0.0 makes Windows Firewall ask the owner
    # a question on their desktop), a pretend router, a pretend STUN server.
    bind = "0.0.0.0"
    lan_address: str | None = None      # None: upnp.lan_ip(), the Ethernet or Wi-Fi card's
    gateway_url: str | None = None      # None: find the router with SSDP
    stun_servers = None                 # None: stun.SERVERS; () asks nobody
    stun_timeout = 2.0
    upnp_timeout = 4.0
    # Hold the movie night on library sharing's listener when sharing has the
    # port (app/share/sharer.py). Tests of the movie night on its own turn it off.
    borrow_from_sharing = True

    _cleaned_up = False                 # upnp.cleanup_stale(), once per run

    def __init__(self, window=None, parent: QObject | None = None) -> None:
        owner = window if window is not None else parent
        player = getattr(owner, "player", None)
        if player is None:
            player, opener = owner, None
        else:
            opener = getattr(owner, "play", None)     # MainWindow.play: the page, the cover, the music
        if parent is None and isinstance(owner, QObject):
            parent = owner
        super().__init__(parent)
        self._player = player
        self._open_item = opener if callable(opener) else (lambda item, start_at=None: player.play(item, start_at))
        self.me: Person | None = None       # None: people.me() when a movie night starts

        self._relay = _Relay(self)
        self._relay.event.connect(self._on_event)
        self._relay.done.connect(self._on_done)
        self._relay.trouble.connect(self._on_trouble)
        self._relay.status.connect(self._on_status)

        self._generation = 0
        # A guest's way back in, should the connection go: (the invite, the
        # host's name, the stream they had chosen). Taken when they get in. It
        # outlives an end only when the connection went (or a "Join again" that
        # could still work did not get in), for "Join again" on the picture,
        # the one thing that uses it: the player holds that offer, and drops it
        # when it closes. Any other end drops this too; the next join replaces it.
        self._way_back: tuple[object, str, str | None] | None = None
        self._reset_state()

        self._save_timer = QTimer(self)
        self._save_timer.setInterval(SAVE_EVERY_MS)
        self._save_timer.timeout.connect(self._save_progress)
        self._renew_timer = QTimer(self)
        self._renew_timer.setInterval(RENEW_CHECK_MS)
        self._renew_timer.timeout.connect(self._renew_if_due)

        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)
        if not PartySession._cleaned_up:
            PartySession._cleaned_up = True
            threading.Thread(target=self._cleanup_stale, name="party-cleanup", daemon=True).start()

    def _reset_state(self) -> None:
        self._role: str | None = None
        self._phase = "idle"                # idle | starting | joining | on
        self._status = ""
        self._room = None                   # the Hub (host) or the Client (guest)
        self._server = None
        self._borrowed = False              # host: _server is library sharing's, lent
        self._mapping = None
        self._proxy = None
        self._invite = None
        self._invite_code = ""
        self._invite_link = ""
        self._port: int | None = None
        self._port_status: PortStatus | None = None
        self._item: MediaItem | None = None         # host: what is being shared
        self._quality_default = "original"          # host: the default for guests who don't choose
        self._media: dict = {}
        self._party_id = ""
        self._follower: _Follower | None = None
        self._stream_quality: str | None = None     # guest: their own choice; None = the host's
        self._subtitles: list[dict] = []
        self._subtitles_for = ""
        self._next_items: dict[str, MediaItem] = {}
        self._struggles: deque[float] = deque(maxlen=8)
        self._offered_at = -1e9
        self._retries: list[float] = []
        self._restarts = 0
        self._attached = False
        self._rejoining = False                     # guest: "Join again" from the picture, connecting
        self._guest_connect = None                  # guest of a friend's PC: connect showing our certificate
        self._friend_host: str | None = None        # ...and that friend's name, the room having nobody of theirs

    # --- what panels read ----------------------------------------------------------------

    @property
    def role(self) -> str | None:
        """"host", "guest", or None. Set as soon as a start or a join begins."""
        return self._role

    @property
    def phase(self) -> str:
        """idle | starting | joining | on"""
        return self._phase

    @property
    def status(self) -> str:
        """What a start or a join is doing now: "Trying your network…"."""
        return self._status

    @property
    def people(self) -> list[dict]:
        """[{"id", "name", "host", "buffering", "drift", "rtt"}], the host first.
        Empty until the room is up (a guest: until the host has said welcome)."""
        room = self._room
        if room is None or self._phase != "on":
            return []
        return list(room.people)

    @property
    def me_id(self) -> str:
        return self.me.id if self.me is not None else _people.person_id()

    @property
    def invite_code(self) -> str:
        return self._invite_code

    @property
    def invite_link(self) -> str:
        return self._invite_link

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def port_status(self) -> PortStatus | None:
        """What the host panel says about the port. `forwarded` is party_forwarded as
        it is now, not as it was at the start: the owner ticks "I've done this"
        under the router steps (or the box in Settings) with the movie night
        already on, and the panel it rewords reads this."""
        status = self._port_status
        forwarded = bool(settings.get("party_forwarded", False))
        if status is not None and status.forwarded != forwarded:
            text = status.text
            if status.state == "manual":
                text = _manual_text(status.port, status.lan_ip, forwarded,
                                    bool(settings.get("party_upnp", True)))
            status = self._port_status = replace(status, forwarded=forwarded, text=text)
        return status

    @property
    def media(self) -> dict:
        """The room's media description: key, title, duration, kind, quality,
        transcode, fps, show, code, year, name."""
        return dict(self._media)

    @property
    def media_title(self) -> str:
        return str(self._media.get("title") or "")

    @property
    def needed_mbps(self) -> float | None:
        """Mbit/s of upload one friend takes at the host's default quality, to a
        tenth ("about 12 Mbit/s per friend"); None when unknown or not hosting."""
        item = self._item
        if self._role != "host" or item is None:
            return None
        from . import transcode

        if self._quality_default in transcode.QUALITIES:
            return round(transcode.stream_bitrate(self._quality_default, item.size, item.duration) / 1e6, 1)
        return transcode.per_friend_mbps(item.size, item.duration)

    @property
    def party_id(self) -> str:
        return self._party_id

    @property
    def host_name(self) -> str:
        if self._role == "host":
            return (self.me or _people.me()).name
        room = self._room
        host = getattr(room, "host", None) if room is not None else None
        if host is not None:
            return host.name
        return getattr(self, "_friend_host", None) or ""

    @property
    def state(self) -> sync.RoomState | None:
        return self._room.state if self._room is not None else None

    @property
    def stream_quality(self) -> str:
        """A guest's stream: original, 1080p or 720p (their choice, else the host's default)."""
        return self._stream_quality or str(self._media.get("quality") or "original")

    @property
    def can_transcode(self) -> bool:
        """Whether the host can make 1080p and 720p streams (it has a working encoder)."""
        return bool(self._media.get("transcode"))

    @property
    def follower(self) -> _Follower | None:
        return self._follower

    # --- starting ---------------------------------------------------------------------------

    def start_host(self, item: MediaItem, quality: str = "auto", party_id: str | None = None,
                   start_at: float | None = None) -> bool:
        """Start a movie night of item. Returns at once: False (with `error`) when it
        cannot even begin; `changed` once the invite is ready, `error` then `ended`
        if it fails on the way.

        party_id continues an earlier movie night (the same id, the same place:
        start_at None means where that party got to). With neither, a film that
        is on screen in this player starts where it is, paused; anything else
        starts at the beginning, paused, for the host to press play once the
        friends are in.
        """
        if self._role is not None:
            self.error.emit("A movie night is already on. End it before you start another.")
            return False
        fresh = db.get_media(int(item.id)) if item is not None and item.id else None
        if fresh is not None:
            item = MediaItem.from_row(fresh)
        if item is None or not item.path or not Path(item.path).is_file():
            name = Path(item.path).name if item is not None and item.path else "That file"
            self.error.emit(f"{name} is no longer on disk.")
            return False
        me = self.me or _people.me()
        on_screen = self._player_shows(item)
        if start_at is None and party_id:
            row = db.party_progress(party_id, f"{me.id}:{item.id}")
            start_at = float(row["position"]) if row is not None else 0.0
        if start_at is None and on_screen:
            # Where the host is. The film holds there while the port opens and
            # friends arrive: a movie night starts when the host presses play.
            self._player.mpv.pause()
            start_at = float(self._player.position or 0.0)
        position = max(0.0, float(start_at or 0.0))
        if item.duration:
            position = min(position, max(0.0, item.duration - 1.0))

        self._generation += 1
        self._role = "host"
        self._phase = "starting"
        self._item = item
        self._attached = on_screen
        port = int(settings.get("party_port", 42170) or 0)
        self._port = port or None
        self._port_status = PortStatus("checking", f"Opening port {port}…" if port else "Opening a port…",
                                       port or None, forwarded=bool(settings.get("party_forwarded", False)))
        self._status = "Getting the movie night ready…"
        job = _Job(item=item, quality=str(quality or "auto"), port=port, party_id=party_id,
                   position=position, me=me, lan=self.lan_address, generation=self._generation)
        self.changed.emit()
        self.message.emit(self._status)
        self._in_background(lambda: self._host_setup(job), lambda result, error: self._host_ready(job, error))
        return True

    def _host_setup(self, job: _Job) -> None:
        """Everything that blocks, on a worker thread: the certificate, the file, the
        listener, the router and STUN. What it made goes into job.result, and is
        taken apart again here if any step fails."""
        from . import invite, server as server_mod, tls, transcode, upnp
        from ..share import sharer as share_sharer

        made = job.result
        lan = job.lan or upnp.lan_ip()
        token = invite.new_token()
        # With library sharing on, the port is already open, by sharing: the
        # movie night is held on that listener (and behind its certificate and
        # its router forward) instead of fighting it for the port.
        lender = share_sharer.current() if self.borrow_from_sharing else None
        borrowed = lender.lend() if lender is not None else None
        if borrowed is not None:
            from ..share import identity as share_identity

            try:
                borrowed.host_party(token)
            except server_mod.ServerError as exc:
                from ..share import nights

                night = nights.current()
                if night is not None:
                    # A friend's movie night on this PC's film has the listener.
                    raise _Refused(nights.OWNER_BUSY.format(title=night.title)) from None
                raise _Refused(str(exc)) from None
            server, pin = borrowed, share_identity.identity().pin
            made["borrowed"] = True
            _log.info("movie night: on library sharing's listener, port %s", borrowed.port)
        else:
            identity = tls.new_identity([lan] if lan else [])
            server, pin = server_mod.PartyServer(identity, token, job.port, bind=self.bind), identity.pin
        made["server"] = server
        try:
            default = server.set_media(job.item.path, job.item.duration or None, job.quality)
        except OSError as exc:
            raise _Refused(f"{Path(job.item.path).name} can't be shared: {exc.strerror or exc}.") from None
        transcodes = transcode.pick_encoder() is not None       # 0.23 s the first time, cached after
        media = _media_for(job.item, job.me, default, transcodes)
        hub = sync.Hub(job.me, party_id=job.party_id, media=media, position=job.position,
                       playing=False, on_event=self._sync_events(job.generation))
        made["hub"] = hub
        server.sync_handler = hub.accept
        if borrowed is not None:
            port = borrowed.port
            self._relay.status.emit(job.generation, "Finding your internet address…")
            status, mapping = self._open_port(port, lan, lent=lender.mapping or False)
        else:
            try:
                port = server.start()
            except server_mod.ServerError as exc:
                raise _Refused(str(exc)) from None
            self._relay.status.emit(job.generation, f"Asking your router to open port {port}, and "
                                    "finding your internet address…")
            status, mapping = self._open_port(port, lan)
        made["mapping"] = mapping
        try:
            code = invite.encode(invite.Invite(status.wan_ip, lan, port, token, pin))
        except invite.InviteError as exc:
            raise _Refused(str(exc)) from None
        made.update(port=port, status=status, code=code, link=invite.web_link(code), media=media,
                    default=default)

    def _open_port(self, port: int, lan: str | None, lent=None) -> tuple[PortStatus, object]:
        """Ask the router for the port while asking STUN for the internet address; the
        two share nothing, so the slower of them is all it costs (2 s here, where
        the router does not answer UPnP).

        lent: on library sharing's listener, the router is sharing's business
        and is not asked again. lent is its forward (upnp.Mapping), or False
        when it has none; either way the movie night makes no forward of its
        own, so returns no mapping to renew or take down."""
        from . import stun, upnp

        tunnel = upnp.vpn()
        forwarded = bool(settings.get("party_forwarded", False))
        if lent:
            return PortStatus("upnp", f"Your router is forwarding port {port} to this PC for "
                              "library sharing. Friends anywhere can join.", port, lan,
                              lent.external_ip, lent.router, tunnel, forwarded), None
        servers = stun.SERVERS if self.stun_servers is None else tuple(self.stun_servers)
        found: list[str | None] = []
        asker = None
        if lan and servers:
            asker = threading.Thread(target=lambda: found.append(
                stun.public_ip(lan, timeout=self.stun_timeout, servers=servers)),
                name="party-stun", daemon=True)
            asker.start()
        mapping = problem = None
        upnp_on = bool(settings.get("party_upnp", True))
        if upnp_on and lent is None:
            try:
                mapping = upnp.open_port(port, lan, gateway_url=self.gateway_url,
                                         timeout=self.upnp_timeout)
            except upnp.UpnpError as exc:
                problem = exc
            except Exception as exc:        # never worth losing the movie night over
                _log.warning("movie night: the router could not be asked: %s", exc)
                problem = exc
        if asker is not None:
            asker.join(self.stun_timeout + 1.0)
        public = found[0] if found else None
        router = getattr(problem, "router", None) or (mapping.router if mapping else None)
        if mapping is not None:
            return PortStatus("upnp", f"Your router opened port {port} for this movie night; it "
                              "closes again when the movie night ends. Friends anywhere can join.",
                              port, lan, mapping.external_ip, mapping.router, tunnel, forwarded), mapping
        kind = getattr(problem, "kind", None)
        if kind in (upnp.CGNAT, upnp.NO_INTERNET):
            # A STUN answer from behind carrier-grade NAT is the carrier's address:
            # nobody could forward a port there, so the invite does without it.
            return PortStatus("cgnat", str(problem), port, lan, None, router, tunnel, forwarded), None
        if kind == upnp.PORT_TAKEN:
            return PortStatus("failed", str(problem), port, lan, None, router, tunnel, forwarded), None
        if public:
            return PortStatus("manual", _manual_text(port, lan, forwarded, upnp_on), port, lan, public,
                              router, tunnel, forwarded), None
        if tunnel:
            text = (f"Your VPN ({tunnel}) is on, so Mistery can't find your internet address: pause "
                    "it for movie night, or let Mistery bypass it (split tunnelling). Friends at your "
                    "place can join now.")
        else:
            text = ("Mistery couldn't find your internet address, so friends elsewhere can't join "
                    "this time. Friends at your place can join now.")
        return PortStatus("lan", text, port, lan, None, router, tunnel, forwarded), None

    def _host_ready(self, job: _Job, error) -> None:
        made = job.result
        if job.generation != self._generation or self._role != "host":
            self._in_background(lambda: _dismantle(made))       # left while it was starting
            return
        if error is not None:
            if not isinstance(error, _Refused):
                _log.error("movie night: starting failed", exc_info=error)
            text = str(error) if isinstance(error, _Refused) else \
                f"The movie night couldn't start: {error}"
            self._in_background(lambda: _dismantle(made))
            self._fail(text)
            return
        self._server = made["server"]
        self._borrowed = bool(made.get("borrowed"))
        self._room = made["hub"]
        self._mapping = made.get("mapping")
        self._port = made["port"]
        self._port_status = made["status"]
        self._invite_code = made["code"]
        self._invite_link = made["link"]
        self._media = dict(made["media"])
        self._quality_default = made["default"]
        self._party_id = self._room.party_id
        self._next_items = {self._media["key"]: job.item}
        self._phase = "on"
        self._status = ""
        self._save_timer.start()
        if self._mapping is not None:
            self._renew_timer.start()
        self._save_progress()
        self.changed.emit()
        _log.info("movie night: hosting %s on port %d (%s)", job.item.title, self._port,
                  self._port_status.state)
        self._attach()
        if self._attached and self._player_shows(job.item):
            # Already on screen: the room takes over the film where it is.
            self._start_follower(self._host_source(job.item, {}), open_now=False)
        else:
            # MainWindow.play opens the player page (and pauses the music); it opens
            # the film a moment later, and a film that cannot be opened comes back
            # through player_failed().
            self._open_item(job.item, job.position)
            if self._role is None:
                return
        self._notice("Movie night on. Press play when your friends are in.")

    def join(self, code: str) -> bool:
        """Join the movie night an invite code (or a message with one, or a
        mistery://join/ link) points at. Returns at once: False (with `error`)
        for a code that cannot be used; `message` with each step, `changed` once
        in (the player opens), `error` then `ended` if it does not work."""
        if self._role is not None:
            self.error.emit("You're already in a movie night. Leave it before you join another.")
            return False
        from . import invite

        try:
            parsed = invite.decode(code)
        except invite.InviteError as exc:
            self.error.emit(str(exc))
            return False
        if parsed.kind not in invite.NIGHTS:
            # A friend code, which looks the same to a person. Name it.
            from ..share.pairing import NOT_AN_INVITE

            self.error.emit(NOT_AN_INVITE)
            return False
        if parsed.kind == invite.KIND_FRIENDS:
            # A movie night on a friend's PC, which lets in only its friends:
            # said now, before anything connects, if its PC is nobody's here.
            from ..share import nights

            friend = nights.host_friend(parsed.pin)
            if friend is None:
                self.error.emit(nights.NOT_THEIR_FRIEND)
                return False
            parsed = nights.guest_invite(parsed, friend)
        self._connect(parsed)
        return True

    def rejoin(self) -> bool:
        """"Join again", on the picture of a movie night whose connection went:
        the same movie night with the invite this session joined it with, so
        there is nothing to find and paste again.

        The reviewer's guest got back in 0.40-0.42 s once his network returned,
        but only by Esc, Join and pasting the code again, the Join box having
        emptied itself. Here it is one click: back in 110-140 ms, playing
        1.6-1.7 s and in step with the room 2.4-3.7 s after it (six runs),
        after a silence or a reset alike, with the host's Mistery in this
        process or another (features3/fix-player).

        The film stays on screen, paused on the room's last frame, and this
        session is attached to the player while it connects, so Esc, or
        anything else put on, leaves the attempt as it would leave the movie
        night. Once in, the stream opens where the room is now, as for a first
        join, in the stream quality this guest had chosen. False when there is
        no way back to take (or a movie night is already on)."""
        back = self._way_back
        if back is None or self._role is not None:
            return False
        parsed, host, quality = back
        # Before _connect, whose `changed` a panel may read them on.
        self._rejoining = True
        self._stream_quality = quality
        self._connect(parsed)
        self._attach()
        self._notice(f"Joining {_whose(host)} again…", sticky=True)
        _log.info("movie night: joining %s again", _whose(host))
        return True

    def _connect(self, parsed) -> None:
        self._generation += 1
        me = self.me or _people.me()
        # A movie night on a friend's PC: reached with this install's
        # certificate shown, which is what lets a friend in, and called theirs
        # (the room has nobody of that PC's in it to be named after).
        self._guest_connect, self._friend_host = None, None
        from . import invite

        if parsed.kind == invite.KIND_FRIENDS:
            from ..share import nights

            friend = nights.host_friend(parsed.pin)
            self._guest_connect = nights.guest_connect()
            self._friend_host = friend["name"] if friend is not None else None
            # Theirs to pass on, to that friend's other friends: nobody hosts it
            # on this side to have a code to show, so the one joined with is it.
            self._invite_code = invite.encode(parsed)
            self._invite_link = invite.web_link(parsed)
        client = sync.Client(me, on_event=self._sync_events(self._generation),
                             connect=self._guest_connect)
        self._role = "guest"
        self._phase = "joining"
        self._room = client
        self._invite = parsed
        self._status = "Trying your network…"
        self.changed.emit()
        client.connect(parsed)

    def _on_joined(self, event) -> None:
        client = self._room
        from .guest_proxy import GuestProxy      # tls is imported by now: the Client's thread did

        self._proxy = GuestProxy(self._invite, address=client.address,
                                 host_name=self.host_name or None, connect=self._guest_connect)
        self._proxy.start()
        self._media = dict(client.state.media or {})
        self._party_id = client.party_id
        self._phase = "on"
        self._status = ""
        self._save_timer.start()
        self._fetch_subtitles()
        self._save_progress()
        self._way_back = (self._invite, self.host_name, None)
        back, self._rejoining = self._rejoining, False
        host = self.host_name or "the host"
        words = f"You're back in {host}'s movie night" if back else f"You joined {host}'s movie night"
        if not back and self._friend_host and self.state is not None and not self.state.playing:
            # A movie night on a friend's PC, still where it began: nobody at
            # that PC will press play, so whoever is in says when (the code to
            # pass on is in this menu, and in the Join panel).
            words = (f"Movie night on {host}'s PC. Send the code to {host}'s friends, "
                     "and press play when they're in.")
        self.message.emit(words)
        self.changed.emit()
        self._attach()
        self._open_item(self._guest_item(), None)
        if self._role is None:
            return
        self._notice(words)

    # --- the player's side -----------------------------------------------------------------

    def _player_shows(self, item: MediaItem) -> bool:
        current = getattr(self._player, "current_item", None)
        return bool(current is not None and item is not None and current.id == item.id
                    and current.id and getattr(self._player, "_active", False))

    def _attach(self) -> None:
        self._player.set_party(self)

    def owns(self, item: MediaItem) -> bool:
        """Whether item is the movie night's own media (what the player may open for it)."""
        if item is None or self._role is None:
            return False
        if self._role == "host":
            return any(candidate.id == item.id for candidate in self._next_items.values())
        return not item.id and self._proxy is not None and str(item.path).startswith(
            f"http://127.0.0.1:{self._proxy.port}/")

    def open_media(self, options: dict | None = None) -> None:
        """PlayerView.play() hands the opening over: the follower opens the room's
        media where the room is. options are the player's own track choices."""
        options = dict(options or {})
        if self._role == "host":
            item = self._next_items.get(self._media.get("key", ""))
            if item is None:
                return
            source = self._host_source(item, options)
        else:
            source = self._guest_source(options)
            if source is None:
                return
        self._start_follower(source, open_now=True)

    def _start_follower(self, source: _Source, open_now: bool) -> None:
        room = self._room
        if room is None:
            return
        fps = self._media.get("fps")
        frame = 1.0 / float(fps) if isinstance(fps, (int, float)) and fps and fps > 0 else 0.0
        if self._follower is None:
            self._follower = _Follower(room, self._player.mpv, frame, source,
                                       self._trouble_reporter(self._generation))
            self._follower.start()
        else:
            self._follower.frame = frame
        if open_now:
            self._follower.open(source)
        else:
            self._follower.source = source

    def _host_source(self, item: MediaItem, options: dict) -> _Source:
        path = item.path
        key = self._media.get("key", "")

        def url(aim: float) -> tuple[str, float, dict]:
            opened = dict(options)
            if aim > 0:
                opened["start"] = f"+{aim:.3f}"
            return path, 0.0, opened
        return _Source("file", key, url)

    def _guest_source(self, options: dict) -> _Source | None:
        proxy = self._proxy
        if proxy is None:
            return None
        quality = self.stream_quality
        if quality not in QUALITY_CHOICES or (quality != "original" and not self.can_transcode):
            quality = "original"
        key = self._media.get("key", "")
        if quality == "original":
            def url(aim: float) -> tuple[str, float, dict]:
                opened = dict(options)
                if aim > 0:
                    opened["start"] = f"+{aim:.3f}"
                return proxy.media_url(quality="original"), 0.0, opened
            return _Source("file", key, url, "original")

        def url(aim: float) -> tuple[str, float, dict]:
            # A transcode's timestamps are the film's own (output_ts_offset);
            # without this mpv shows a stream opened at 600 as 0.65.
            return proxy.media_url(aim, quality), 0.0, {**options, "rebase-start-time": "no"}
        return _Source("transcode", key, url, quality)

    def _guest_item(self) -> MediaItem:
        """A title for the guest's player, which has no row of its own for it: id 0,
        the proxy's address as its path, the host's words and the host's length."""
        media = self._media
        year = media.get("year")
        fps = media.get("fps")
        return MediaItem(id=0, path=self._proxy.media_url(), kind=str(media.get("kind") or "movie"),
                         title=str(media.get("title") or "Movie night"),
                         year=year if isinstance(year, int) and not isinstance(year, bool) else None,
                         duration=float(media.get("duration") or 0.0),
                         fps=float(fps) if isinstance(fps, (int, float)) and not isinstance(fps, bool) else 0.0)

    def file_loaded(self) -> None:
        """The player opened a file: a guest's gets the host's subtitle files again
        (mpv drops a file's added subtitles along with the file)."""
        self._add_subtitles()

    def _add_subtitles(self) -> None:
        proxy, mpv = self._proxy, self._player.mpv
        if self._role != "guest" or proxy is None or self._subtitles_for != self._media.get("key"):
            return
        for sub in self._subtitles:
            # "auto": added, not selected. Turning them on stays each person's choice.
            mpv.command("sub-add", proxy.subtitle_url(sub["n"]), "auto", sub["title"] or "External",
                        sub["lang"] or "")

    def _fetch_subtitles(self) -> None:
        proxy, key = self._proxy, self._media.get("key", "")
        if proxy is None or not key:
            return
        generation = self._generation

        def done(found, error) -> None:
            if generation != self._generation or self._media.get("key") != key:
                return
            self._subtitles = list(found or [])
            self._subtitles_for = key
            if self._subtitles and getattr(self._player.mpv, "cached", None) and \
                    self._player.mpv.cached("path"):
                self._add_subtitles()
        self._in_background(proxy.subtitles, done)

    def room_position(self) -> float:
        room = self._room
        return room.room_position() if room is not None else 0.0

    def room_playing(self) -> bool:
        room = self._room
        return bool(room is not None and room.state.playing)

    def intent(self, action: str, position: float | None = None) -> None:
        """A play, pause or seek made on this player: to the room, never to mpv.
        The follower moves mpv once the room says so, for everyone at once."""
        room = self._room
        if room is None or self._phase != "on":
            return
        if action == "seek" and position is not None:
            duration = self._media.get("duration")
            position = max(0.0, float(position))
            if isinstance(duration, (int, float)) and duration > 0:
                position = min(position, max(0.0, float(duration) - 0.5))
        room.intent(action, position)

    def toggle(self) -> None:
        """Space, a click on the picture, the play button: the room's state decides.
        While the room waits for somebody, play means go on without them."""
        self.intent("pause" if self.room_playing() else "play")

    def change_media(self, item: MediaItem) -> bool:
        """The host moves everyone on: Next (or Previous) episode together.

        The server shares the new file first, then the room says so; every
        player, the host's own included, opens it when the room's state arrives,
        and they start together 1.5 s after (sync.LEAD_MAX)."""
        if self._role != "host" or self._phase != "on" or item is None:
            return False
        fresh = db.get_media(int(item.id)) if item.id else None
        item = MediaItem.from_row(fresh) if fresh is not None else item
        if not Path(item.path).is_file():
            self._notice(f"{Path(item.path).name} is no longer on disk.")
            return False
        me = self.me or _people.me()
        server, generation = self._server, self._generation
        wanted = str(settings.get("party_quality", "auto") or "auto")

        def share() -> tuple[str, bool]:
            from . import transcode

            default = server.set_media(item.path, item.duration or None, wanted)
            return default, transcode.pick_encoder() is not None

        def shared(result, error) -> None:
            if generation != self._generation or self._room is None:
                return
            if error is not None:
                self._notice(f"{Path(item.path).name} can't be shared: {error}")
                return
            default, transcodes = result
            media = _media_for(item, me, default, transcodes)
            self._quality_default = default
            self._next_items[media["key"]] = item
            self._room.set_media(media, 0.0, playing=True)
        self._in_background(share, shared)
        return True

    def set_stream_quality(self, quality: str) -> bool:
        """A guest's own choice of stream: original, 1080p or 720p. The player switches
        mid-film by the held recipe: a still picture for 2-4 s, then on in step."""
        if self._role != "guest" or quality not in QUALITY_CHOICES:
            return False
        if quality != "original" and not self.can_transcode:
            self._notice(f"{self.host_name or 'The host'}'s Mistery can't make a {quality} stream.")
            return False
        if quality == self.stream_quality:
            return True
        self._stream_quality = quality
        source = self._guest_source({})
        if self._follower is not None and source is not None:
            self._follower.open(source, "switch")
        self.changed.emit()
        return True

    def stream_trouble(self, what: str) -> None:
        """A guest's stream failed ("error": it could not be opened, say the host's
        transcodes were all taken) or ended before the room's end ("eof": the
        connection dropped part way): open it again a second later, where the
        room will be. From the third time within 30 s the proxy's own sentence
        goes on the picture, and it tries every 5 s."""
        follower = self._follower
        if self._role != "guest" or follower is None or (what != "error" and follower.opening):
            return
        now = time.monotonic()
        self._retries = [t for t in self._retries if now - t < 30.0] + [now]
        if len(self._retries) >= 3:
            reason = (self._proxy.last_error if self._proxy is not None else None) or \
                "The film isn't coming through at the moment. Trying again…"
            self._notice(reason, sticky=True)
        generation = self._generation
        _log.info("movie night: the stream stopped (%s); opening it again", what)
        QTimer.singleShot(1000 if len(self._retries) < 3 else 5000, lambda: self._retry(generation))

    def _retry(self, generation: int) -> None:
        follower = self._follower
        if generation != self._generation or follower is None:
            return
        source = self._guest_source({})
        if source is not None:
            follower.open(source, "retry")

    def player_failed(self, text: str) -> None:
        """The player could not open the movie night's media at all."""
        if self._role is not None:
            self._fail(text)

    def player_closed(self, shutting_down: bool = False) -> None:
        """The player closed during a movie night: the host's ends it for everyone,
        a guest leaves it. Quietly: whoever closed the player knows."""
        if self._role is None:
            return
        if shutting_down:
            self.shutdown()
            return
        self._end("")

    # --- the room's news (Qt's thread) ----------------------------------------------------------

    def _sync_events(self, generation: int) -> Callable:
        relay = self._relay

        def deliver(event) -> None:
            follower = self._follower
            if follower is not None and event.kind == "state":
                follower.wake()                 # at once, not at its next look
            relay.event.emit(generation, event)
        return deliver

    def _trouble_reporter(self, generation: int) -> Callable[[str], None]:
        relay = self._relay
        return lambda what: relay.trouble.emit(generation, what)

    def _on_event(self, generation: int, event) -> None:
        if generation != self._generation or self._role is None:
            return
        kind = event.kind
        if kind == "connecting":
            self._status = event.text
            if event.text:
                self.message.emit(event.text)
                if self._rejoining:
                    # No Join window is open for this one: the picture says what
                    # it is doing ("Trying the internet…" can take 6 s).
                    self._notice(f"Joining {_whose(self._way_back_host())} again. {event.text}",
                                 sticky=True)
            self.changed.emit()
        elif kind == "connected":
            self._on_joined(event)
        elif kind == "state":
            self._on_state(event)
        elif kind == "people":
            if event.text:
                self.message.emit(event.text)
                self._notice(event.text)
            self._player.party_refresh()
            self.changed.emit()
        elif kind == "notice":
            if event.text:
                self.message.emit(event.text)
                self._notice(event.text)
        elif kind == "ended":
            self._on_ended(event.text)

    def _on_state(self, event) -> None:
        state = event.data
        media = state.media or {}
        if event.text:
            self.message.emit(event.text)
            self._notice(event.text, sticky=state.cause == "wait")
        elif state.cause != "end":
            # The room changed by this player's own doing (so no words): a line
            # that stays up ("Waiting for Sam…", "That's the end of the
            # episode…") comes down. The room's own end, which arrives in the
            # same moment as that last line, leaves it be.
            self._notice("")
        if media.get("key") and media.get("key") != self._media.get("key"):
            self._media = dict(media)
            self._new_media()
        if state.cause in ("pause", "seek", "wait", "end", "media"):
            self._save_progress()
        if self._role == "guest" and state.cause == "wait" and self.me_id in state.waiting_for:
            self._struggle()
        self._player.party_refresh()
        self.changed.emit()

    def _new_media(self) -> None:
        """Next episode together: the room has moved on to something else."""
        self._subtitles, self._subtitles_for = [], ""
        if self._role == "host":
            item = self._next_items.get(self._media.get("key", ""))
            if item is not None:
                self._item = item
                self._player.play(item, 0.0)
        elif self._proxy is not None:
            self._fetch_subtitles()
            self._player.play(self._guest_item(), None)

    def _on_status(self, generation: int, text: str) -> None:
        if generation == self._generation and self._phase in ("starting", "joining") and text:
            self._status = text
            self.message.emit(text)
            self.changed.emit()

    def _on_trouble(self, generation: int, what: str) -> None:
        if generation != self._generation or self._role is None:
            return
        if what == "stall":
            self._struggle()
        elif what == "slow":
            # One long wait is plenty: this connection cannot carry this stream.
            lighter = self._lighter() if self._role == "guest" else None
            if lighter is not None:
                self._offer(lighter, "slow")
        elif what == "drops":
            # Too much for this PC to decode: 1080p H.264 is easy on anything,
            # whatever the original's size.
            if self._role == "guest" and self.stream_quality == "original":
                self._offer("1080p", "skipping")
        elif what == "follower" and self._follower is not None and self._restarts < 3:
            # Its thread stopped on something unexpected (the log has it): a new
            # one takes over from wherever the room is, rather than a player left
            # to drift on its own.
            self._restarts += 1
            source = self._follower.source
            self._stop_follower()
            if source is not None:
                self._start_follower(source, open_now=True)

    def _struggle(self) -> None:
        """This guest's stream stopped (or the room waited for it): after the
        second time within 3 minutes, offer a lighter one, now and then."""
        if self._role != "guest":
            return
        now = time.monotonic()
        if self._struggles and now - self._struggles[-1] < SAME_STRUGGLE:
            return                          # the stall and the room's wait for it are one
        self._struggles.append(now)
        if sum(1 for t in self._struggles if now - t < STRUGGLE_WINDOW) >= STRUGGLES_TO_OFFER:
            lighter = self._lighter()
            if lighter is not None:
                self._offer(lighter, "pausing")

    def _lighter(self) -> str | None:
        """A stream that is really lighter on the connection than this one, or None.

        A transcode is not always smaller: 1080p runs at about 9 Mbit/s with its
        audio (transcode.stream_bitrate), and half the library's episodes need
        under 5. Offered to those, it would stall more, not less. So the
        original's own rate (the host sends it as media["mbps"]) decides, with
        a tenth in hand; with no rate known, 1080p is the usual answer.
        """
        from . import transcode

        current = self.stream_quality
        if current == "1080p":
            return "720p"
        if current != "original":
            return None
        mbps = self._media.get("mbps")
        if not isinstance(mbps, (int, float)) or isinstance(mbps, bool) or mbps <= 0:
            return "1080p"
        for quality in ("1080p", "720p"):
            if mbps > transcode.stream_bitrate(quality, None, None) / 1e6 * 1.1:
                return quality
        return None

    def _offer(self, quality: str, why: str) -> None:
        now = time.monotonic()
        if self.can_transcode and now - self._offered_at > OFFER_AGAIN_AFTER:
            self._offered_at = now
            self._player.party_offer(quality, why)

    def _on_ended(self, reason: str) -> None:
        if self._phase == "joining" and self._friend_host and reason == sync.TURNED_DOWN:
            # A friend's PC that closed the door during the handshake: it does
            # not know this install's certificate any more (it removed us). Not
            # "an earlier movie night's code", which is what it means elsewhere.
            from ..share import nights

            reason = nights.NOT_LET_IN.format(name=self._friend_host)
        if self._phase in ("joining", "starting"):
            if self._rejoining:
                self._rejoin_failed(reason)
                return
            self._fail(reason or "The movie night ended before you got in.")
            return
        way_back = False
        if self._role == "guest" and reason == "The host ended the movie night." and self.host_name:
            reason = f"{self.host_name} ended the movie night."
        elif self._role == "guest" and reason == sync.LOST_HOST:
            # 8 s of silence, or the connection reset. The host's movie night is
            # most likely still on, and the same code gets back in: the reviewer's
            # guest was in again 0.40-0.42 s after his network returned, and the
            # host saw "Alex is back". So the sentence says how, with the host's
            # name, and the picture offers it ("Join again").
            reason = f"Lost the connection to {_whose(self.host_name)}. Join again with the same code."
            way_back = True
        self._end(reason, by_room=True, way_back=way_back)

    def _rejoin_failed(self, reason: str) -> None:
        """"Join again" did not get in: the picture says why, and offers it again
        unless this code can never work again.

        In words for someone who was watching a moment ago. sync's sentence for a
        code that reaches nobody is about forwarding a port and Windows Firewall,
        and both worked a moment ago: what is down is a network, theirs or the
        host's, or the movie night itself, ended while this guest was cut off
        and could not be told (a guest who pressed Join before their Wi-Fi was
        back got the port sentence from the Join window in the review, 1.5 s
        after pressing). A certificate that no longer matches (the host's
        Mistery was started again) or a token the host no longer takes is a
        code that is finished, and a movie night the host ended is over: those
        keep sync's words and get no second offer. A PC with no network at all
        keeps sync's words too (OFFLINE: check the Wi-Fi or the cable), the
        likeliest answer of all to a "Join again" pressed while a guest's own
        Wi-Fi is still down, and is offered it again."""
        host = self._way_back_host()
        if reason == "The host ended the movie night.":
            reason = f"{host} ended the movie night." if host else reason
        finished = reason in (sync.NOT_THEM, sync.TURNED_DOWN) or reason.endswith("ended the movie night.")
        port = getattr(self._invite, "port", None)
        unreached = {sync.NO_ANSWER, sync.LOST_HOST}
        if isinstance(port, int):
            unreached |= {sync.unreachable(port, internet=True), sync.unreachable(port, internet=False)}
        if reason in unreached:
            pc = f"{host}'s PC" if host else "the host's PC"
            reason = (f"Couldn't reach {pc}. Your network or theirs may still be down, or the movie "
                      "night may be over.")
        self._end(reason or "The movie night ended before you got back in.", failed=True,
                  way_back=not finished)

    # --- ending --------------------------------------------------------------------------------

    def leave(self) -> None:
        """End it (the host, for everyone) or leave it (a guest). Returns at once.
        The player closes; `changed` and then `ended("")` follow."""
        if self._role is None:
            return
        if self._player.party is self:
            self._player.stop_and_close()       # which comes back through player_closed()
            if self._role is None:
                return
        self._end("")

    def _fail(self, text: str) -> None:
        """A start or a join that did not work, or a player that could not open it."""
        self._end(text, failed=True)

    def _end(self, reason: str, by_room: bool = False, failed: bool = False,
             way_back: bool = False) -> None:
        """way_back: a guest's connection went (or joining again did not get in,
        yet): the picture offers "Join again", and the invite is kept for it."""
        if self._role is None:
            return
        was_on = self._phase == "on"
        if was_on:
            self._save_progress()
        self._stop_follower()
        role, room, server, mapping, proxy = self._role, self._room, self._server, self._mapping, self._proxy
        borrowed = self._borrowed
        back = self._way_back
        back = (back[0], back[1], self._stream_quality) if way_back and back is not None else None
        rejoining = self._rejoining
        self._generation += 1
        self._save_timer.stop()
        self._renew_timer.stop()
        player = self._player
        if player.party is self:
            if reason and (not failed or rejoining):
                # The film stays on screen with the reason: a joining again that
                # did not work too, since no Join window is open to say so.
                player.party_over(reason, self.rejoin if back is not None else None)
            else:
                player.set_party(None)
        self._reset_state()
        self._way_back = back
        self._in_background(lambda: _stop_everything(role, room, server, mapping, proxy,
                                                     borrowed=borrowed))
        self.changed.emit()
        if failed:
            self.error.emit(reason)
        elif by_room and reason:
            self.message.emit(reason)
        self.ended.emit(reason)

    def shutdown(self) -> None:
        """Mistery is quitting: the same, but finished before this returns (a few
        seconds at most, for a router that stops answering)."""
        if self._role is None:
            return
        if self._phase == "on":
            self._save_progress()
        self._stop_follower()
        role, room, server, mapping, proxy = self._role, self._room, self._server, self._mapping, self._proxy
        borrowed = self._borrowed
        self._generation += 1
        self._save_timer.stop()
        self._renew_timer.stop()
        if self._player.party is self:
            self._player.set_party(None)
        self._reset_state()
        self._way_back = None
        _stop_everything(role, room, server, mapping, proxy, quick=True, borrowed=borrowed)
        self.ended.emit("")

    def _stop_follower(self) -> None:
        follower, self._follower = self._follower, None
        if follower is not None:
            follower.stop()

    # --- keeping things ---------------------------------------------------------------------------

    def _save_progress(self) -> None:
        """The party's place, on this side: the room's position, not this player's."""
        room, media = self._room, self._media
        if room is None or self._phase != "on" or not media.get("key") or not self._party_id:
            return
        item = self._next_items.get(media["key"]) if self._role == "host" else None
        duration = media.get("duration")
        try:
            db.save_party_progress(
                self._party_id, media["key"], role=self._role, position=room.room_position(),
                duration=float(duration) if isinstance(duration, (int, float)) and duration else None,
                media_id=item.id if item is not None else None, title=media.get("title") or None,
                members=self._members_json(room, media["key"]))
        except Exception as exc:        # a locked database is not worth a movie night
            _log.warning("movie night: could not save where the party got to: %s", exc)

    def _members_json(self, room, key: str) -> str:
        """Everyone who came to this party for this film or episode: tonight's room,
        and whoever the row saved before names. Continue movie night starts the
        room with the host alone, and saving that alone wrote "Nobody else
        joined" over "With Sam and Alex" on Home (the surfaces builder's finding).
        The room's names win; nobody who came before is dropped."""
        came = room.members()
        row = db.party_progress(self._party_id, key)
        if row is not None and row["members"]:
            here = {person.id for person in came}
            came += [person for person in _people.parse_members(row["members"]) if person.id not in here]
        return _people.members_json(came)

    def _renew_if_due(self) -> None:
        mapping = self._mapping
        if mapping is None or not mapping.lease or time.time() < mapping.expires - RENEW_BEFORE:
            return
        from . import upnp

        def done(result, error) -> None:
            if error is not None:
                _log.warning("movie night: the router would not renew the port: %s", error)
        self._in_background(lambda: upnp.renew(mapping), done)

    @staticmethod
    def _cleanup_stale() -> None:
        """A forward an earlier run left open (Mistery killed mid-film) comes off now."""
        try:
            from . import upnp

            if upnp.record_path().is_file():
                upnp.cleanup_stale()
        except Exception as exc:
            _log.info("movie night: cleaning up old forwards: %s", exc)

    # --- plumbing ---------------------------------------------------------------------------------

    def _way_back_host(self) -> str:
        """The host's name, for a guest joining again: the Client has no host until
        the host says welcome, so it comes from the first time in."""
        return self._way_back[1] if self._way_back is not None and self._way_back[1] else ""

    def _notice(self, text: str, sticky: bool = False) -> None:
        self._player.party_notice(text, sticky)

    def _in_background(self, work: Callable, done: Callable | None = None) -> None:
        relay = self._relay

        def run() -> None:
            try:
                result, error = work(), None
            except BaseException as exc:      # handed to done(); nothing escapes a worker
                result, error = None, exc
            if done is not None:
                try:
                    relay.done.emit(done, result, error)
                except RuntimeError:
                    pass                        # the window is gone
        threading.Thread(target=run, name="party-work", daemon=True).start()

    @staticmethod
    def _on_done(callback, result, error) -> None:
        callback(result, error)


def _manual_text(port: int | None, lan: str | None, forwarded: bool, upnp_on: bool) -> str:
    """The "manual" sentence: the router did not open the port, and the invite
    carries the internet address all the same (STUN's answer).

    With the owner's word that the port is forwarded (party_forwarded), one calm
    line, without the steps: what they did, the host panel's own words, and
    nothing about who can get in. That is not a promise anything here could
    keep: without UPnP nothing can see a forward, and one to the PC's old
    address, or to another port, looks just the same from here. (It used to end
    "so friends anywhere should be able to join", which no screen showed, the
    panel having better words, but this is PortStatus.text and its str(), so
    the next thing to show it would have.) Without the owner's word, UPDATE 2's
    sentence, with the real port and this PC's LAN address in it."""
    if forwarded:
        return f"You've forwarded port {port} to this PC."
    first = ("Your router didn't open the port by itself." if upnp_on else
             "Mistery doesn't ask your router to open ports (Settings → Movie night).")
    return (f"{first} Friends at your place can join now. For friends elsewhere, forward TCP port "
            f"{port} to this PC ({lan}) in your router's settings.")


def _whose(host: str | None) -> str:
    """"Hana's movie night", or "the movie night" when the name is not known."""
    return f"{host}'s movie night" if host else "the movie night"


# The room's description of its film lives in media.py, which the Mistery with
# no window can import too (app/share/nights.py); the old name stays for callers.
from .media import media_for as _media_for  # noqa: E402,F401



def _dismantle(made: dict) -> None:
    """A start that failed or was abandoned: everything it made, taken apart."""
    _stop_everything("host", made.get("hub"), made.get("server"), made.get("mapping"), None,
                     borrowed=bool(made.get("borrowed")))


def _stop_everything(role, room, server, mapping, proxy, quick: bool = False,
                     borrowed: bool = False) -> None:
    """In the order sync.Hub asks for: the room first (every guest is told), then
    the listener, then the router. Blocking; a worker thread's, or shutdown's.

    borrowed: the listener is library sharing's, and goes back to it still
    open, with its friends' streams untouched. One of the movie night's own is
    closed, and if sharing wanted the port meanwhile, it gets it now."""
    try:
        if role == "host":
            if room is not None:
                room.end()
                room.wait_closed(1.5 if quick else 3.0)
            if server is not None and borrowed:
                server.end_party()
            elif server is not None:
                server.stop()
            from . import upnp

            if mapping is not None:
                upnp.close_port(mapping, timeout=2.0 if quick else 3.0)
            if not quick and upnp.record_path().is_file():
                # A forward still on record: one whose router went quiet while
                # being asked (a start abandoned mid-request, say). The router may
                # have made it all the same; now is as good a time as the next
                # start to take it off. Forwards still in use are left alone.
                upnp.cleanup_stale()
            if server is not None and not borrowed and not quick:
                # The port is free again, and the router's forward for it gone
                # (sharing asks for its own; closing ours after that would have
                # taken sharing's with it). A friend added during the movie
                # night, say, left sharing waiting for exactly this.
                from ..share import sharer as share_sharer

                share_sharer.start_if_wanted()
        else:
            if room is not None:
                room.leave()
                room.ended.wait(1.0)
            if proxy is not None:
                proxy.stop()
    except Exception:
        _log.exception("movie night: stopping it did not go cleanly")
