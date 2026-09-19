"""The room: one film or episode, one place in it, everyone together.

The host's Mistery is the room. Nobody else changes it directly: a guest's
play, pause or seek is an *intent*, sent to the host, which applies intents one
at a time in the order they arrive and tells everyone the new *state* — the
guest who asked included. Every player, the host's own too, then follows the
state. One place where the truth lives is what stops four players arguing about
where the film is, and it is why "anyone can pause" needs no voting: two people
pressing at once are simply two intents, applied in turn.

On the wire (inside the TLS connection, after the one line server.py routes on,
``MISTERY-SYNC/1 <token-hex>``) it is one JSON object per line, UTF-8. The host
speaks first, so nothing a guest says can be swallowed by whatever read that
first line. Each side's first message is a hello carrying the protocol range it
can speak; that is how a 1.1 guest and a 1.2 host get a sentence instead of a
crash.

    guest → host   hello      proto, proto_min, app, id, name
                   intent     action: play | pause | seek, position (seek only)
                   buffering  on
                   ping       n, t0, and optionally rtt, pos, pos_at, seq
                   bye
    host → guest   hello      proto, proto_min, app, role: "host"
                   welcome    proto, party, you, state, people, now
                   refused    reason (a sentence)
                   state      state (see RoomState.to_wire)
                   people     people: [{id, name, host, buffering, slow, drift, rtt}]
                   pong       n, t0, t1, t2
                   end        reason (a sentence)

Unknown message types and unknown fields are ignored, so a later version can add
both without a new protocol number. Everything a peer can send is bounded: line
length, messages per second, people per room, time to say hello, and time
without a word before somebody counts as gone.

Time. A state says where the film is at a moment on the *host's* clock —
"1834.2 s at host time 5120.40, playing". Each guest learns how its own clock
relates to the host's from ping and pong (ClockSync), so every player can work
out where the room is right now, whenever the message happened to arrive. A
play is scheduled a little in the future — a quarter of a second plus the
slowest guest's round trip — so everyone starts together instead of in the
order the news reached them. The tolerance is 5 ms between any two players'
clocks for that moment: a simulation with guests' clocks hours apart and
routes of 1-30 ms each way over busy Wi-Fi stays within 3.1 ms, where trusting
the latest ping was 120 ms out. Real mpv on this PC, the host's playing its
file and two guests' through their proxies, started 5-18 ms apart and stayed
within 43-68 ms of each other over 20 s (95 % of the time within 35-55 ms;
three runs; a reading is itself a frame, 42 ms, coarse). What is left after
a start is taken up by nudging the speed, or by a seek when it is big
(DriftController). Somebody buffering for more than a second pauses the room,
"waiting for Sam…", until they have been ready for half a second; play while
somebody is already stuck waits for them at once. Not for ever, though: a
friend whose connection cannot keep up with the film is waited for twice in
three minutes, then the room goes on without them and says why; play pressed
while the room waits goes on without them until they have played half a
minute without a stall; and a friend who joins while the film plays is not
waited for until their player has caught up with the room.

Threads. A Hub and a Client each own one thread, and that thread owns their
sockets. OpenSSL does not allow one TLS connection to be read and written from
two threads at once, so nothing else touches a socket: other threads post work
to that thread and wake it through a socket pair. Nothing here blocks the
caller except Hub.accept, on purpose (see there). Events for the Qt side come
out through `on_event`, called on that thread (emit a Qt signal from it), and
through a bounded queue the Qt side can `drain()` instead.
"""

from __future__ import annotations

import errno
import json
import logging
import math
import queue
import re
import selectors
import socket
import ssl
import statistics
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Callable

from .. import __version__ as APP_VERSION
from . import people as _people
from .people import FALLBACK_NAME, Person, clean_name, is_person_id, unique_name

_log = logging.getLogger("party.sync")

PROTOCOL = 1            # what this Mistery speaks
PROTOCOL_MIN = 1        # the oldest it can still speak
# The line server.py routes on. It never changes: versions live in the hellos,
# where both sides can still say something useful when they disagree.
PREAMBLE = "MISTERY-SYNC/1"

# The clock everything here is measured on. Not time.monotonic: under Python
# 3.12 on Windows that is GetTickCount64, which moves in 15.6 ms steps (measured
# resolution 0.015625 s) — a third of a film frame of noise in every clock
# sample. perf_counter is QueryPerformanceCounter: 100 ns steps, as monotonic.
now = time.perf_counter

# --- limits -------------------------------------------------------------------

# The longest thing a guest ever says is its hello, about 180 bytes. 2 KB is
# room for a later version's extra fields and nothing like room for mischief.
MAX_GUEST_LINE = 2048
# The host says more: a welcome with ten people and the media is about 3 KB.
MAX_HOST_LINE = 64 * 1024
# The room's size. Not this owner's upload: ten friends on the heaviest episode
# in the library (21.4 Mbit/s, measured) take a quarter of their 909 Mbit/s.
# But on a 20 Mbit/s upload one friend on that episode is already all of it,
# and server.py's limits (48 connections) are sized for ten guests, each with a
# sync channel, a player's stream and the one a seek opens before the old closes.
MAX_GUESTS = 10
MAX_PENDING_HELLOS = 8      # connections that have not said hello yet
HELLO_TIMEOUT = 5.0         # a Mistery says hello within milliseconds
PING_EVERY = 2.0
QUICK_PINGS = 6             # 0.1 s apart right after joining: a good clock within a second
# Silence, then gone. A guest pings every 2 s and the host answers each ping
# and sends the people list every 5 s, so 8 s is four missed pings: long
# enough for Wi-Fi to recover from a hiccup (TCP retries at 0.3, 0.6, 1.2 and
# 2.4 s), short enough that a room waiting for a friend whose PC died does not
# wait long.
LOST_AFTER = 8.0
PEOPLE_EVERY = 5.0
# Messages a guest may send: 30 a second, bursts of 60. Holding an arrow key
# repeats at most 30 seeks a second (Windows' fastest key repeat), the seek bar
# seeks once, on release, and nothing else a person does comes close.
RATE = 30.0
BURST = 60.0
MAX_STRIKES = 20            # unusable messages before a guest is shown the door
MAX_OUTBOX = 256 * 1024     # unsent bytes to one guest: they have stopped reading
TICK = 0.1                  # how often the loops look at their timers
LEAD_MIN = 0.25             # see Hub._lead
SEEK_LEAD = 0.6             # ...after a seek, when every player has to get there first
LEAD_MAX = 1.5
# "If someone is buffering for more than a moment": a stall under a second
# leaves that player less than a second behind, which the speed nudge takes
# back unnoticed within 20 s — stopping everyone for it would be the bigger
# interruption. Past a second, the others would be watching ahead of them.
BUFFER_GRACE = 1.0
# A room waiting for somebody carries on once they have been ready for this
# long, not the moment they first say so. A player that has refilled is sent
# on to the room's frame (DriftController's hold), and on a slow link that seek
# waits for data too: through a 0.5 Mbit/s link, three runs of
# fix-engine/probe_slow_wait.py saw the guest ready for 0.12-0.14 s before the
# seek made it busy again, and a room that carried on at once stopped again
# 1.3 s later, twice before the link came back. Counted as stalls, those were
# enough for the rule below to give up on him (the end-to-end test's "the room
# carries on by itself once he is ready", which failed once). Half a second
# covers that seek and the hold's one re-check 0.3 s after it, and after a wait
# of a second or more nobody notices it.
READY_HOLD = 0.5
# Somebody whose stream cannot keep up with the film would stop everybody every
# few seconds: the review's Jo, on 1.5 Mbit/s for a 2.1 Mbit/s film, ran dry
# every 1.3-6 s and the room stopped nine times in his first minute. So the room
# waits for the same person's stalls twice within three minutes; at the third it
# goes on without them, tells everyone why, and lets them catch up as their
# stream allows. Twice in three minutes is also when their own Mistery offers
# them a lighter stream, where one exists (session.py), so the offer comes first.
STALLS_BEFORE_GOING_ON = 2
STALL_WINDOW = 180.0
# ...and waits for them again once they have played this long without a stall
# while the room played, and their stalls have aged out of the window (see
# Hub._count_steady_play). The first half goes for play pressed while the room
# waited for somebody too: after it, the review's Jo stopped everyone again
# 2.06 s later, because his first recovery used to end it. Half a minute is
# ten of his stalls, and a friend whose Wi-Fi only hiccuped gets there
# without noticing.
STEADY_PLAY = 30.0
# A friend who joins while the film plays (or while the room waits for
# somebody, about to play) is not waited for until their player has caught up
# with the room: a first open through a slow connection took 25.8 s, and
# everyone sat on "Waiting for Jo…" for 20.6 s of it. Within a second of the
# room is caught up: the nudge takes the rest back unnoticed.
CAUGHT_UP = 1.0
CLOSE_WAIT = 1.0            # a last word gets this long to reach the other side
EVENT_BACKLOG = 256
MAX_POSITION = 1e7          # seconds; 115 days
MAX_CLOCK = 1e12            # a host clock value (perf_counter: seconds since boot)
MAX_PEOPLE_ON_WIRE = 32

INTENTS = ("play", "pause", "seek")
CAUSES = ("start", "play", "pause", "seek", "wait", "resume", "media", "end")
_PARTY_ID_RE = re.compile(r"^[0-9a-f]{32}$")


# --- messages -----------------------------------------------------------------

class BadMessage(ValueError):
    """A line that is not a message we can use. Dropped; never fatal by itself."""


def _refuse_constant(name: str) -> float:
    raise BadMessage(f"{name} is not a number")


def encode(message: dict) -> bytes:
    return (json.dumps(message, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            .encode("utf-8") + b"\n")


def decode(line: bytes) -> dict:
    """One line off the wire, as a dict with a "type" — or BadMessage.

    json.loads alone would accept NaN and Infinity (then every position after
    them is NaN) and raise RecursionError, not ValueError, on "[[[[…" nested
    a few thousand deep.
    """
    try:
        message = json.loads(line.decode("utf-8"), parse_constant=_refuse_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise BadMessage(f"unreadable ({exc.__class__.__name__})") from None
    if not isinstance(message, dict):
        raise BadMessage("not an object")
    kind = message.get("type")
    if not isinstance(kind, str) or not 0 < len(kind) <= 32:
        raise BadMessage("no type")
    return message


def number(value: object, low: float, high: float) -> float:
    """A finite number within [low, high], or BadMessage. true is not 1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadMessage("not a number")
    if isinstance(value, int) and abs(value) > 10 ** 15:
        raise BadMessage("out of range")
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise BadMessage("out of range")
    return value


def whole(value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise BadMessage("not a whole number in range")
    return value


def _short_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value[:limit] if ch.isprintable())


def clean_media(media: object) -> dict | None:
    """What is being watched, kept to what is safe to pass around and show.

    The host's session decides what goes in. "key" ("<host person id>:<host
    media id>"), "title", "duration", "kind" and "quality" mean something here
    and to the player; anything else it adds rides along as long as it is a
    short plain value. A guest keeps only this much of whatever arrived.
    """
    if not isinstance(media, dict):
        return None
    out: dict = {}
    for key, value in list(media.items())[:24]:
        if not isinstance(key, str) or not 0 < len(key) <= 32:
            continue
        if isinstance(value, str):
            value = clean_name(value, 300) if key == "title" else _short_text(value, 300)
        elif isinstance(value, bool) or value is None:
            pass
        elif isinstance(value, int):
            if abs(value) > 10 ** 15:
                continue
        elif isinstance(value, float):
            if not math.isfinite(value) or abs(value) > 1e15:
                continue
        else:
            continue
        out[key] = value
    duration = out.get("duration")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float))
                                 or not 0 < duration <= MAX_POSITION):
        out["duration"] = None
    return out


def parse_people(raw: object) -> list[dict]:
    """A people list from the host, cleaned. Entries that make no sense are left out.
    "slow" is true for somebody the room has stopped waiting for because their
    stream kept running dry; a host that does not say so means false."""
    if not isinstance(raw, list):
        raise BadMessage("people is not a list")
    people = []
    seen = set()
    for entry in raw[:MAX_PEOPLE_ON_WIRE]:
        if not isinstance(entry, dict) or not is_person_id(entry.get("id")) or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        people.append({
            "id": entry["id"],
            "name": clean_name(entry.get("name")) or FALLBACK_NAME,
            "host": entry.get("host") is True,
            "buffering": entry.get("buffering") is True,
            "slow": entry.get("slow") is True,
            "drift": _maybe_number(entry.get("drift"), -MAX_POSITION, MAX_POSITION),
            "rtt": _maybe_number(entry.get("rtt"), 0.0, 60.0),
        })
    return people


def _maybe_number(value: object, low: float, high: float) -> float | None:
    try:
        return number(value, low, high)
    except BadMessage:
        return None


def _check_intent(action: str, position: float | None) -> tuple[str, float | None]:
    """What the Qt side asked for, or ValueError: a bug there, not a network problem."""
    if action not in INTENTS:
        raise ValueError(f"not an intent: {action!r}")
    if action != "seek":
        return action, None
    try:
        position = float(position)      # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError(f"a seek needs a position, not {position!r}") from None
    if not math.isfinite(position):
        raise ValueError("a seek needs a position")
    return action, _position(position)


def _position(value: float) -> float:
    """A place in a film, within [0, MAX_POSITION]; NaN counts as 0."""
    return 0.0 if math.isnan(value) else min(max(0.0, value), MAX_POSITION)


def _token_hex(token: object) -> str:
    if isinstance(token, (bytes, bytearray)):
        return bytes(token).hex()
    text = str(token).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{16,128}", text):
        raise ValueError("the token is not hex")
    return text


# --- words --------------------------------------------------------------------

def clock_text(seconds: float) -> str:
    """1:02:03, or 12:34 under an hour."""
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def and_list(names: list[str]) -> str:
    """"Sam", "Sam and Alex", "Sam, Alex and you"."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def describe(state: RoomState, names: dict[str, str], me_id: str,
             before: RoomState | None = None) -> str:
    """The overlay's line for a new state: "Sam paused", "Waiting for Sam…".

    "" for what you did yourself — you know — and for changes nobody made.
    `before` is the state this one replaces: play pressed while the room was
    waiting for you went on without you, and you are told so, not just that
    somebody pressed play while your picture stood still.
    """
    if state.cause == "wait":
        who = ["you" if pid == me_id else names.get(pid, FALLBACK_NAME) for pid in state.waiting_for]
        if "you" in who:                  # "Sam and you", never "you and Sam"
            who.remove("you")
            who.append("you")
        return f"Waiting for {and_list(who)}…" if who else ""
    if not state.by or state.by == me_id:
        return ""
    who = names.get(state.by, FALLBACK_NAME)
    if state.cause == "play" and before is not None and before.waiting and me_id in before.waiting_for:
        return f"{who} went on without you. You'll catch up as soon as your player is ready."
    title = (state.media or {}).get("title") or ""
    return {
        "play": f"{who} pressed play",
        "pause": f"{who} paused",
        "seek": f"{who} skipped to {clock_text(state.position)}",
        "resume": f"{who} is ready",
        "media": f"{who} put on {title}" if title else f"{who} put on something new",
    }.get(state.cause, "")


def going_on_without(name: str | None, remote: bool = True) -> str:
    """What everyone is told when the room stops waiting for somebody whose
    stream keeps running dry (Hub, STALLS_BEFORE_GOING_ON): about `name`, or
    about you when name is None. `remote` is False for the host, whose film
    comes off their own disk (or a network drive) rather than over a connection.
    Said once, when it happens, so nobody reads "Waiting for Jo…" again and
    again without knowing why it stopped.
    """
    what = "connection" if remote else "player"
    if name is None:
        return (f"Your {what} can't keep up with this film, so the others carry on without waiting "
                "for you. You'll catch up with them whenever it can.")
    return f"{name}'s {what} can't keep up with this film, so everyone carries on without waiting for them."


def version_sentence(*, for_host: bool, host_is_newer: bool, host_app: str, guest_app: str,
                     guest_name: str = "") -> str:
    """What to tell somebody when two Misterys cannot talk to each other.

    A version number on its own means nothing to anyone, so the sentence says
    who has to update. `for_host` picks whose screen it is for.
    """
    host_app = host_app or "an unknown version"
    guest_app = guest_app or "an unknown version"
    if for_host:
        if host_is_newer:
            return (f"{guest_name} could not join: their Mistery ({guest_app}) is older than "
                    f"yours ({host_app}). They need to update it to watch together.")
        return (f"{guest_name} could not join: their Mistery ({guest_app}) is newer than "
                f"yours ({host_app}). Update Mistery to watch together.")
    if host_is_newer:
        return (f"This movie night needs a newer Mistery: the host has {host_app}, you have "
                f"{guest_app}. Update Mistery, then join again.")
    return (f"The host's Mistery ({host_app}) is older than yours ({guest_app}) and cannot "
            f"talk to it. Ask them to update Mistery, then join again.")


# --- the room -----------------------------------------------------------------

@dataclass(frozen=True)
class RoomState:
    """Where the room is. Pure data, and pure arithmetic on it.

    `position` is where the film is at host time `at`. While `playing` it moves
    on from there at `rate`, so an `at` in the future means "hold `position`
    until then, then go": that is how a start is scheduled. Every change is made
    by the host and carries the next `seq`, `by` (the person id of whoever did
    it; "" when the room did it itself) and `cause`, one of CAUSES.
    `waiting_for` holds the ids a paused room is waiting for; it carries on by
    itself when they are all ready, unless somebody pauses it for real first.
    """

    media: dict | None = None
    playing: bool = False
    position: float = 0.0
    at: float = 0.0
    rate: float = 1.0
    seq: int = 0
    by: str = ""
    cause: str = ""
    waiting_for: tuple[str, ...] = ()

    @property
    def duration(self) -> float | None:
        value = (self.media or {}).get("duration")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return None
        return float(value)

    def _clamp(self, position: float) -> float:
        duration = self.duration
        if duration is not None:
            position = min(position, duration)
        return max(0.0, position)

    def position_at(self, host_time: float) -> float:
        """Where the film is at `host_time`: held before a scheduled start, moving after."""
        position = self.position
        if self.playing and host_time > self.at:
            position += (host_time - self.at) * self.rate
        return self._clamp(position)

    def moving_at(self, host_time: float) -> bool:
        return self.playing and host_time >= self.at

    def play(self, host_now: float, lead: float, by: str, cause: str = "play") -> RoomState:
        return replace(self, playing=True, position=self.position_at(host_now),
                       at=host_now + max(0.0, lead), seq=self.seq + 1, by=by, cause=cause,
                       waiting_for=())

    def pause(self, host_now: float, by: str, cause: str = "pause",
              waiting_for: tuple[str, ...] | list[str] = ()) -> RoomState:
        return replace(self, playing=False, position=self.position_at(host_now), at=host_now,
                       seq=self.seq + 1, by=by, cause=cause, waiting_for=tuple(waiting_for))

    def seek(self, position: float, host_now: float, lead: float, by: str) -> RoomState:
        """A playing room holds at the new place until everyone has had time to get there.
        A room paused while it waits for somebody keeps waiting for them."""
        position = self._clamp(position)
        if self.playing:
            return replace(self, position=position, at=host_now + max(0.0, lead),
                           seq=self.seq + 1, by=by, cause="seek", waiting_for=())
        return replace(self, position=position, at=host_now, seq=self.seq + 1, by=by,
                       cause="seek")

    @property
    def waiting(self) -> bool:
        return not self.playing and bool(self.waiting_for)

    def to_wire(self) -> dict:
        return {"media": self.media, "playing": self.playing, "position": self.position,
                "at": self.at, "rate": self.rate, "seq": self.seq, "by": self.by,
                "cause": self.cause, "waiting": list(self.waiting_for)}

    @classmethod
    def from_wire(cls, raw: object) -> RoomState:
        """A state from the host, checked field by field. BadMessage if any is wrong."""
        if not isinstance(raw, dict):
            raise BadMessage("state is not an object")
        playing = raw.get("playing")
        if not isinstance(playing, bool):
            raise BadMessage("playing is not true or false")
        by = raw.get("by", "")
        if by != "" and not is_person_id(by):
            raise BadMessage("by is not a person")
        cause = raw.get("cause", "")
        if not isinstance(cause, str):
            raise BadMessage("cause is not text")
        waiting = raw.get("waiting", [])
        if (not isinstance(waiting, list) or len(waiting) > MAX_PEOPLE_ON_WIRE
                or not all(is_person_id(pid) for pid in waiting)):
            raise BadMessage("waiting is not a list of people")
        return cls(
            media=clean_media(raw.get("media")),
            playing=playing,
            position=number(raw.get("position"), 0.0, MAX_POSITION),
            at=number(raw.get("at"), -MAX_CLOCK, MAX_CLOCK),
            rate=number(raw.get("rate", 1.0), 0.25, 4.0),
            seq=whole(raw.get("seq"), 0, 2 ** 53),
            by=by,
            cause=cause if cause in CAUSES else "",      # a later version's cause: no notice
            waiting_for=tuple(waiting),
        )


# --- clocks -------------------------------------------------------------------

class ClockSync:
    """Where the host's clock is, seen from here: NTP's arithmetic on ping and pong.

    A ping carries our send time t0; the host stamps when it arrived (t1) and
    when the pong left (t2); we stamp when the pong arrived (t3). The round trip
    is (t3 - t0) - (t2 - t1), and the host's clock minus ours is
    ((t1 - t0) + (t2 - t3)) / 2 — exact when the trip there took as long as the
    trip back, and wrong by at most half the round trip when it did not. So the
    quickest round trips are the ones to believe: a ping that sat 80 ms in a
    Wi-Fi queue one way can be 40 ms out, while the quickest few of the last 16
    almost never are. The offset is the median of the 4 quickest of the last
    16 — about 30 s of pings, short enough that two PCs' crystals drifting
    apart (50 parts per million at worst, 1.6 ms over those 30 s) do not matter.

    Measured in a model of busy Wi-Fi (8 ms of queueing each way on average,
    and a 50-150 ms retry on 5 % of trips): within 5.2 ms from the first second
    on and 3.1 ms once 30 s of pings are in, worst of 40 runs, where trusting
    the latest ping was 87 ms out. With 2 ms of queueing, within 1.3 ms. Real
    Clients over TLS on one PC: 76-143 µs.

    What no ping can see is a route slower one way than the other: an upload
    that is 10 ms slower than the download leaves 5 ms of error however many
    pings there are. `error` is the honest bound on all of it: half the slowest
    round trip among the samples the offset came from.
    """

    def __init__(self, keep: int = 16, use: int = 4) -> None:
        self._samples: deque[tuple[float, float]] = deque(maxlen=keep)   # (rtt, offset)
        self._use = use
        self._lock = threading.Lock()
        self.offset: float | None = None
        self.rtt: float | None = None        # typical round trip: the median of those kept
        self.error: float | None = None      # how wrong `offset` can be, at most

    def add(self, t0: float, t1: float, t2: float, t3: float) -> bool:
        """One ping and its pong. False (and ignored) when the times make no sense."""
        rtt = (t3 - t0) - (t2 - t1)
        if not (t3 >= t0 and t2 >= t1 and -0.001 <= rtt <= 10.0):
            return False
        offset = ((t1 - t0) + (t2 - t3)) / 2
        with self._lock:
            self._samples.append((max(0.0, rtt), offset))
            quickest = sorted(self._samples)[: self._use]
            self.offset = statistics.median(o for _, o in quickest)
            self.error = quickest[-1][0] / 2
            self.rtt = statistics.median(r for r, _ in self._samples)
        return True

    def seed(self, host_time: float, local_time: float) -> None:
        """A first guess from one timestamp (the welcome's), until pongs arrive.
        Wrong by however long that message took to arrive."""
        with self._lock:
            if self.offset is None:
                self.offset = host_time - local_time

    @property
    def samples(self) -> int:
        return len(self._samples)

    @property
    def ready(self) -> bool:
        return len(self._samples) >= 3

    def host_time(self, local: float) -> float:
        return local + (self.offset or 0.0)

    def local_time(self, host: float) -> float:
        return host - (self.offset or 0.0)


# --- following the room -------------------------------------------------------

@dataclass(frozen=True)
class Correction:
    """What a player should do now to be where the room is.

    Apply all of it, and none of it as an intent: this is the room's doing, not
    the person's. `wake_in`, while a start is scheduled, is how many seconds
    from the sample until the player should start. Then unpause first — if the
    room's seq has not changed — and read the position afterwards: a reading
    is a round trip to mpv (15 ms here, more when the PC is busy), and taking
    one before the start made each player that much later, by a different amount.
    """

    pause: bool
    seek_to: float | None
    speed: float
    error: float | None           # the player minus the room, in seconds; negative is behind
    wake_in: float | None = None


class DriftController:
    """Keeps one player where the room says it should be.

    Give it every fresh position the player reports, with the host-clock time
    that position was true at (Client.host_time of the moment it arrived), and
    do what the Correction says. While the room plays:

      within 80 ms   leave it alone. mpv reports the time of the frame on
                     screen, so at 24 fps two readings of a perfectly placed
                     player can be a frame (42 ms) apart; chasing that would
                     be chasing noise. 80 ms is two frames, and friends in
                     different houses compare notes over a voice call, which
                     lags by about that much itself.
      to 1.5 s       play 5 % faster or slower until within 20 ms. mpv keeps
                     the pitch, so voices do not change, and 5 % closes a
                     second in 20 s. Stopping at 20 ms rather than at the
                     80 ms it started from is what keeps it from stopping at the
                     edge and starting again on the next noisy reading.
      beyond         seek, to where the room will be when the seek lands. A
                     seek is a visible stall: through a guest's proxy on this
                     PC, 0.1-0.22 s for 4 s ahead (mpv already had it) and
                     0.21-0.34 s for a 15-minute jump (a new request, a new TLS
                     connection; 0.42-0.48 s while other programs kept the PC
                     busy); across the internet add a handshake's round trips.
                     So it is only worth it for gaps a nudge would take half a
                     minute to close. How long seeks take is learned from each.

    The decisions use the median of the last three errors, so one late reading
    (a garbage-collection pause, a busy pipe) cannot start or stop anything.

    The room's clock is not quite what mpv reports: after a start, three real
    players on this PC read 20-56 ms ahead of it (their medians over 20 s,
    three runs), mostly all alike. mpv's clock follows the sound card, the
    likely reason, so other PCs may differ from each other by a few tens of
    milliseconds that nothing here can see; the dead band has room for it.

    A seek lands where it lands — with real mpv, anywhere within a couple of
    frames of the aim — and a leftover under 80 ms would otherwise stay for the
    rest of the film. So the first judgment after a seek uses 30 ms instead of
    the dead band, and a nudge finishes the job.

    A start is a landing too, and judged the same way: after the room holds
    (a pause, a wait, a scheduled start), after a player is seen paused while
    the room plays, and after a new stream is opened. Players on this PC read
    20-56 ms ahead of the room once started, each by its own amount, and the
    dead band left each where it was: after a run of stops in the review's
    evening, two players sat 52-57 ms apart at the median (88-94 ms at the 95th
    percentile, past two frames) for 90 s, each inside the band, until the next
    common start. Now anything past 30 ms from the room after a start is taken
    to within 20 ms of it (fix-engine/test_start_alignment.py: players starting
    15 and 58 ms ahead of the room end up within a frame of each other).

    `frame` is how long one frame of the video lasts (1 / fps). mpv's time-pos
    is the start of the frame on screen — readings of real mpv step exactly one
    frame, 41–42 ms at 23.976 fps — so a position read at an arbitrary moment
    is on average half a frame early, and every player the controller has
    settled would sit half a frame away from every player that started exactly
    on time. Half a frame is added back while playing. Leave it at 0 if your
    positions are stamped at the moment the frame changed.

    A seek is judged landed once the player is seen moving again at about the
    room's rate: mpv reports the seek's target as the position while a seek is
    still under way, which would otherwise look like arriving instantly.

    While the room is paused, or waiting for a scheduled start, every player
    seeks once to the room's exact position (unless already within 5 ms), so
    all of them rest on the same frame and the next start lines them up. Left
    where each happened to stop — each notices the pause at its own next look —
    three real mpv rested one or two frames apart (41-84 ms, in 15 pauses out
    of 15, three runs) and started 38-97 ms apart; from the common frame, on
    one frame, and 0-17 ms apart (an estimate good to about 10 ms). It is a
    seek of a frame or two, which mpv serves from what it has already read —
    through a guest's proxy it cost no new request.

    mpv's exact seek shows the first frame no more than 5 ms before its aim
    (probed: 4.9 ms into a frame shows that frame, 5.1 ms the next, from any
    distance and either way). Aimed at the room's moment, every player rested
    on the frame after it (+31 to +36 ms, all three alike, in two runs) and the
    whole room then ran that far ahead, eating into the dead band. So it aims
    half a frame early, for the nearest frame: 12 ms into a frame, it rested
    12 ms before the moment, where aiming at the moment rested 29 ms after.

    And it checks: a held player more than half a frame (and 10 ms) from the
    moment, once it has stopped seeking, is sent there again 0.3 s after the
    last try, three tries for one moment at most; not knowing the frame
    length, 0.1 s is the tolerance. That is for a seek that goes missing. It
    cannot take a player where mpv will not go: at one spot of the test's
    episode, the two B-frames just before an I-frame (20:27.810 and .852), an
    exact seek through a guest's proxy showed the I-frame every time, from
    anywhere, where the host's player on the file showed the B-frame
    (probe_hold_seek; 30 other seeks through a proxy all landed). There the
    guests rest two frames from the host, and after the start the nudge
    takes it back.
    """

    DEAD_BAND = 0.08
    SETTLE = 0.02
    AFTER_SEEK_BAND = 0.03
    NUDGE = 0.05
    SEEK_BEYOND = 1.5
    EXACT = 0.005           # a held player this close to the room's position is on its frame
    HOLD_RETRY = 0.1        # ...one further off than this after its seek (frame unknown) is not
    HOLD_RECHECK = 0.3      # a held seek is looked at again this long after it was sent
    HOLD_TRIES = 3          # ...and sent at most this many times for one moment
    SEEK_WAIT = 5.0         # a seek that has not landed by now is judged afresh

    def __init__(self, frame: float = 0.0) -> None:
        self.frame = frame
        self.speed = 1.0
        self.seek_lead = 0.3        # how far ahead to aim; learned from real seeks
        self.seeks = 0
        self.nudges = 0
        self._landing: tuple[float, float] | None = None   # (where we sent it, host time asked)
        self._landed = False                               # the next judgment is a landing's
        self._held: tuple[float, float, int] | None = None  # (room position sought while held, when, tries)
        self._errors: deque[float] = deque(maxlen=3)
        self._last: tuple[float, float] | None = None      # (host time, position) for "moving?"

    def reset(self, frame: float | None = None) -> None:
        """Forget everything: a new file was opened (with this frame length, if given).
        Where it starts is judged as a landing."""
        self.__init__(self.frame if frame is None else frame)
        self._landed = True

    def update(self, state: RoomState, host_now: float, position: float | None, *,
               paused: bool, seeking: bool = False) -> Correction:
        """`position` of the local player at host time `host_now` (None while nothing is
        loaded), whether it is paused, and whether it is mid-seek or refilling its cache."""
        target = state.position_at(host_now)
        if not state.moving_at(host_now):
            return self._hold(state, host_now, target, position, seeking)

        self._held = None
        if position is None or seeking:
            self._last = None
            return Correction(False, None, self.speed, None)
        error = position + self.frame / 2 - target
        if paused:
            # Starting. Unpause now and judge drift once it is really playing:
            # a paused player falls further behind with every reading. Where
            # it starts is judged as a landing.
            self._errors.clear()
            self._last = None
            self._landed = True
            seek_to = None
            if abs(error) > self.SEEK_BEYOND and self._landing is None:
                seek_to = self._seek(state, host_now, target)
            return Correction(False, seek_to, self.speed, error)

        moving = self._moving(host_now, position, state.rate)
        if self._landing is not None:
            aimed, asked = self._landing
            if not moving and host_now - asked < self.SEEK_WAIT:
                return Correction(False, None, self.speed, error)
            self._landing = None
            self._landed = True
            if abs(error) < self.SEEK_BEYOND:
                # Aimed `seek_lead` ahead, landed `error` ahead: the seek took
                # seek_lead - error. Move halfway towards that for next time.
                self.seek_lead = min(3.0, max(0.05, self.seek_lead - error * 0.5))

        self._errors.append(error)
        smoothed = statistics.median(self._errors)
        if abs(smoothed) > self.SEEK_BEYOND:
            return Correction(False, self._seek(state, host_now, target), self.speed, error)
        band = self.DEAD_BAND
        if self._landed:
            if len(self._errors) < 3:
                return Correction(False, None, self.speed, error)   # three readings, then judge it
            band = self.AFTER_SEEK_BAND
            self._landed = False
        if self.speed != state.rate:
            faster = self.speed > state.rate
            if (faster and smoothed >= -self.SETTLE) or (not faster and smoothed <= self.SETTLE):
                self.speed = state.rate
        elif abs(smoothed) > band:
            self.speed = state.rate * (1 - self.NUDGE if smoothed > 0 else 1 + self.NUDGE)
            self.nudges += 1
        return Correction(False, None, self.speed, error)

    def _hold(self, state: RoomState, host_now: float, target: float, position: float | None,
              seeking: bool) -> Correction:
        self._errors.clear()
        self._last = None
        self._landing = None            # a seek made while playing no longer matters
        self._landed = True             # ...and where the start puts it is judged as a landing
        self.speed = state.rate
        wake_in = (state.at - host_now) if state.playing else None
        if position is None or seeking:
            return Correction(True, None, self.speed, None, wake_in)
        error = position - target
        held = self._held
        if held is None or abs(held[0] - target) > self.EXACT:
            tries = 0                       # a new moment: one seek, unless already on it
            go = abs(error) > self.EXACT
        else:
            tries = held[2]                 # the same moment: again only if the last did not land
            near = self.frame / 2 + 0.01 if self.frame else self.HOLD_RETRY
            go = (abs(error) > near and tries < self.HOLD_TRIES
                  and host_now - held[1] > self.HOLD_RECHECK)
        seek_to = None
        if go:
            # Half a frame early: mpv shows the first frame no more than 5 ms
            # before the aim, so this lands on the frame nearest the room's moment.
            seek_to = max(0.0, target - self.frame / 2)
            self._held = (target, host_now, tries + 1)
            self.seeks += 1
        return Correction(True, seek_to, self.speed, error, wake_in)

    def _seek(self, state: RoomState, host_now: float, target: float) -> float:
        aim = target + self.seek_lead * state.rate
        duration = state.duration
        if duration is not None:
            aim = min(aim, duration)
        self._landing = (aim, host_now)
        self._errors.clear()
        self._last = None
        self.speed = state.rate
        self.seeks += 1
        return aim

    def _moving(self, host_now: float, position: float, rate: float) -> bool:
        """Whether the player has been advancing at about the room's rate lately."""
        last = self._last
        if last is None or host_now - last[0] > 2.0 or host_now < last[0]:
            self._last = (host_now, position)
            return False
        elapsed = host_now - last[0]
        if elapsed < 0.15:
            return False        # too close together to tell moving from a frame of jitter
        self._last = (host_now, position)
        moved = position - last[1]
        return 0.5 * rate * elapsed <= moved <= 1.5 * rate * elapsed


# --- events -------------------------------------------------------------------

@dataclass(frozen=True)
class Event:
    """Something the Qt side may want to show or act on.

    kind      connecting  a guest's progress ("Trying your network…"); data:
                          {"step": "lan" | "internet" | "hello", "address"}
              connected   a guest is in the room; data: {"party", "host" (a Person),
                          "state", "people", "media", "address"}
              state       the room changed; data: the RoomState
              people      who is here changed; data: the people list
              notice      a sentence and nothing else (someone could not join)
              ended       this movie night is over for us; text: why ("" if we ended it)
    text      a short sentence for the overlay, or "" when there is nothing to say
    """

    kind: str
    text: str = ""
    data: Any = None


class _Events:
    """on_event, and a bounded queue for a Qt side that prefers to drain."""

    def __init__(self, callback: Callable[[Event], None] | None) -> None:
        self._callback = callback
        self._queue: deque[Event] = deque(maxlen=EVENT_BACKLOG)

    def emit(self, event: Event) -> None:
        self._queue.append(event)
        if self._callback is not None:
            try:
                self._callback(event)
            except Exception:
                _log.exception("movie night: an event handler failed")

    def drain(self) -> list[Event]:
        out = []
        while True:
            try:
                out.append(self._queue.popleft())
            except IndexError:
                return out


# --- one connection -----------------------------------------------------------

class _Link:
    """One sync connection: whole lines in, bytes out, never blocking.

    Owned by one loop thread. Reads go on until the socket would block, because
    TLS can hold decrypted bytes that select() cannot see — but at most
    READ_BUDGET bytes a turn, with `more` set to come back, so one guest pouring
    data in can neither starve the others nor pile up lines without limit.
    Writes keep the exact chunk OpenSSL was given until it takes it, because a
    TLS write that has to wait must be repeated with the same bytes.
    """

    READ_BUDGET = 64 * 1024

    def __init__(self, sock, peer: object, max_line: int, clock: Callable[[], float]) -> None:
        sock.setblocking(False)
        try:
            # Nagle's algorithm holds a small write back until the last one is
            # acknowledged, and Windows acknowledges up to 200 ms late: a ping
            # sent right after a buffering report could sit that long, which is
            # exactly the noise ClockSync exists to keep out.
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass                            # a socket pair in a test, say
        self.sock = sock
        self.peer = peer
        self.max_line = max_line
        self._clock = clock
        self._in = bytearray()
        self._skipping = False          # inside a line that was too long
        self._out = bytearray()
        self._inflight = b""
        self.received_at = 0.0          # when the last bytes arrived: t1 / t3 for the clock
        self.wants_write = False        # a TLS read that must wait for the socket to be writable
        self.more = False               # stopped at the budget: read again without waiting
        self.closing = False
        self.close_by = 0.0
        self.shut = False
        self.closed = False

    def fileno(self) -> int:
        return self.sock.fileno()

    def receive(self) -> tuple[list[bytes], int, bool]:
        """(complete lines that have arrived, how many over-long ones were dropped,
        whether the connection is still open). Lines before the end are kept."""
        lines: list[bytes] = []
        oversized = 0
        taken = 0
        self.wants_write = False
        self.more = False
        while True:
            if taken >= self.READ_BUDGET:
                self.more = True
                return lines, oversized, True
            try:
                chunk = self.sock.recv(16384)
            except (ssl.SSLWantReadError, BlockingIOError, InterruptedError):
                return lines, oversized, True
            except ssl.SSLWantWriteError:
                self.wants_write = True
                return lines, oversized, True
            except OSError:
                return lines, oversized, False
            if not chunk:
                return lines, oversized, False
            taken += len(chunk)
            self.received_at = self._clock()
            if self.shut:
                continue                    # only waiting for their end now
            buffer = self._in
            buffer += chunk
            start = 0
            while True:
                end = buffer.find(b"\n", start)
                if end < 0:
                    break
                if self._skipping:
                    self._skipping = False  # the tail of the over-long line
                elif end - start > self.max_line:
                    oversized += 1
                elif end > start:
                    lines.append(bytes(buffer[start:end]))
                start = end + 1
            del buffer[:start]
            if len(buffer) > self.max_line:
                # Too long and no end in sight: drop it as it comes rather than
                # hold it all to find out how long it is.
                buffer.clear()
                if not self._skipping:
                    self._skipping = True
                    oversized += 1

    def send(self, data: bytes) -> None:
        if not self.closed and not self.shut:
            self._out += data

    @property
    def unsent(self) -> int:
        return len(self._inflight) + len(self._out)

    def flush(self) -> bool:
        """Write what the socket takes now. False when the connection is broken."""
        while self._inflight or self._out:
            if not self._inflight:
                self._inflight = bytes(self._out[:16384])
                del self._out[:len(self._inflight)]
            try:
                sent = self.sock.send(self._inflight)
            except (ssl.SSLWantWriteError, ssl.SSLWantReadError, BlockingIOError, InterruptedError):
                return True
            except OSError:
                return False
            self._inflight = self._inflight[sent:]
        return True

    def begin_close(self, deadline: float) -> None:
        self.closing = True
        self.close_by = deadline

    def shut_when_sent(self) -> None:
        """Once the last word is out, say we are done: the other side reads all of
        it, then the end. Closing outright with their data unread makes Windows
        reset the connection, and a reset can throw away our last word unread."""
        if self.closing and not self.shut and not self.unsent:
            try:
                self.sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            self.shut = True

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.sock.close()
            except OSError:
                pass


class _Loop:
    """A selector, a wake-up socket pair and a queue of work from other threads."""

    def __init__(self) -> None:
        self.selector = selectors.DefaultSelector()
        # Loopback only (socketpair binds 127.0.0.1 on Windows), so no firewall prompt.
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self.selector.register(self._wake_r, selectors.EVENT_READ, None)
        self._work: queue.SimpleQueue = queue.SimpleQueue()
        self.stopped = False

    def call(self, work: Callable[[], None]) -> None:
        """Run `work` on the loop's thread, soon. Safe from any thread."""
        if self.stopped:
            return
        self._work.put(work)
        try:
            self._wake_w.send(b"\0")
        except OSError:
            pass                # full (the loop is awake anyway) or closed

    def run_work(self) -> None:
        try:
            while self._wake_r.recv(4096):
                pass
        except OSError:
            pass
        while True:
            try:
                work = self._work.get_nowait()
            except queue.Empty:
                return
            try:
                work()
            except Exception:
                _log.exception("movie night: a queued call failed")

    def watch(self, link: _Link, data: object) -> None:
        self.selector.register(link.sock, selectors.EVENT_READ, data)

    def update_interest(self, link: _Link, data: object) -> None:
        wanted = selectors.EVENT_READ
        if link.unsent or link.wants_write:
            wanted |= selectors.EVENT_WRITE
        try:
            key = self.selector.get_key(link.sock)
        except (KeyError, ValueError):
            return
        if key.events != wanted:
            try:
                self.selector.modify(link.sock, wanted, data)
            except (KeyError, ValueError, OSError):
                pass

    def forget(self, link: _Link) -> None:
        try:
            self.selector.unregister(link.sock)
        except (KeyError, ValueError, OSError):
            pass
        link.close()

    def close(self) -> None:
        self.stopped = True
        for sock in (self._wake_r, self._wake_w):
            try:
                sock.close()
            except OSError:
                pass
        try:
            self.selector.close()
        except OSError:
            pass


# --- the host -----------------------------------------------------------------

class _Seat:
    """One person's place in the room: the host's own (no connection) or a guest's."""

    def __init__(self, link: _Link | None, t: float, person: Person | None = None) -> None:
        self.link = link
        self.person = person                # a guest's arrives with their hello
        self.phase = "in" if link is None else "hello"      # hello | in | closing
        self.app = APP_VERSION if link is None else ""
        self.hello_by = t + HELLO_TIMEOUT
        self.heard_at = t
        self.buffering_since: float | None = None
        self.ready_since = t                # buffering last turned off (READY_HOLD)
        # Who the room does not wait for, and why (Hub._stuck):
        self.arriving = False               # joined while the film played (or was about to), not caught up yet
        self.pressed_through = False        # someone chose play while the room waited for them
        self.slow = False                   # their stream kept running dry: STALLS_BEFORE_GOING_ON
        self.steady = 0.0                   # seconds played without a stall since either of those
        self.stalls: deque[float] = deque(maxlen=8)     # when they stalled while the room played
        self.stall_counted = False          # the stall under way is in `stalls` already
        self.drift: float | None = None
        self.rtt: float | None = None
        self.strikes = 0
        self.tokens = BURST
        self.tokens_at = t
        self.done = threading.Event()       # Hub.accept returns once this is set

    @property
    def who(self) -> str:
        return self.person.name if self.person else f"a guest at {self.link.peer if self.link else '?'}"


class Hub:
    """The room, on the host's side. One per movie night.

    `accept` is server.sync_handler. The host's own player takes part through
    the same calls a guest's Client offers — intent, play, pause, seek,
    set_buffering, report_position — and reads the same things: `state`,
    `people`, `host_time()`, `room_position()`, events.

        hub = Hub(me, party_id=..., media={"key", "title", "duration", ...},
                  position=where_it_got_to, playing=False, on_event=...)
        server.sync_handler = hub.accept
        ...
        hub.end()                  # tells everyone, returns at once
        hub.wait_closed()          # every guest told and let go; CLOSE_WAIT at most
        server.stop()              # stopping the server first cuts the sync
                                   # channels, and guests hear "lost", not "ended"
    """

    def __init__(self, me: Person | None = None, *, party_id: str | None = None,
                 media: dict | None = None, position: float = 0.0, playing: bool = False,
                 clock: Callable[[], float] = now,
                 on_event: Callable[[Event], None] | None = None) -> None:
        self.me = me if me is not None else _people.me()
        self.party_id = party_id if isinstance(party_id, str) and _PARTY_ID_RE.match(party_id) \
            else uuid.uuid4().hex
        self._clock = clock
        self._events = _Events(on_event)
        t = clock()
        self._host = _Seat(None, t, self.me)
        self._seats: list[_Seat] = [self._host]
        self._came: dict[str, str] = {self.me.id: self.me.name}
        state = RoomState(media=clean_media(media), position=_position(float(position)), at=t,
                          by=self.me.id, cause="start")
        self.state = state.play(t, LEAD_MIN, self.me.id, cause="start") if playing else state
        self.people: list[dict] = []
        self._people_sent_at = t
        self._ticked_at = t                 # the last _tick, for counting steady play
        self._ending: str | None = None
        self.closed = threading.Event()
        self._loop = _Loop()
        self._refresh_people()
        self._handlers = {"intent": self._on_intent, "buffering": self._on_buffering,
                          "ping": self._on_ping, "bye": self._on_bye}
        self._thread = threading.Thread(target=self._run, name="party-hub", daemon=True)
        self._thread.start()

    # --- for the server and the Qt side (any thread) --------------------------

    def accept(self, sock, peer: object = None) -> None:
        """server.sync_handler: take one guest's connection; return when it is over.

        It holds the calling thread — and so the socket — for as long as the
        guest stays. That is right for a server that closes a connection once
        its handler returns and harmless for one that does not: either way the
        connection lives exactly as long as the guest is in the room.
        """
        if self.closed.is_set() or self._loop.stopped:
            _close_quietly(sock)
            return
        try:
            seat = _Seat(_Link(sock, peer, MAX_GUEST_LINE, self._clock), self._clock())
        except OSError:
            _close_quietly(sock)
            return
        self._loop.call(lambda: self._adopt(seat))
        while not seat.done.wait(0.5):
            if not self._thread.is_alive():
                break
        if seat.link is not None:
            seat.link.close()

    def intent(self, action: str, position: float | None = None) -> None:
        """The host's own play, pause or seek, applied exactly like a guest's."""
        action, position = _check_intent(action, position)
        self._loop.call(lambda: self._apply_intent(self._host, action, position))

    def play(self) -> None:
        self.intent("play")

    def pause(self) -> None:
        self.intent("pause")

    def seek(self, position: float) -> None:
        self.intent("seek", position)

    def set_buffering(self, on: bool) -> None:
        """The host's own player cannot play right now (or can again)."""
        on = bool(on)
        self._loop.call(lambda: self._buffering(self._host, on))

    def report_position(self, position: float, local: float | None = None) -> None:
        """Where the host's player is, for the people list's drift column."""
        at = self._clock() if local is None else local
        seq = self.state.seq
        self._loop.call(lambda: self._position_report(self._host, position, at, seq))

    def set_media(self, media: dict, position: float = 0.0, playing: bool = True) -> None:
        """Everyone moves on to something else: Next episode together."""
        position = _position(float(position))
        playing = bool(playing)
        self._loop.call(lambda: self._new_media(media, position, playing))

    def end(self, reason: str = "The host ended the movie night.") -> None:
        """Tell everyone, close every connection, stop. Returns at once."""
        self._loop.call(lambda: self._end(reason))

    def wait_closed(self, timeout: float = 3.0) -> bool:
        """True once every guest has been let go. Blocks: not on Qt's thread,
        where `closed` (a threading.Event) can be polled instead."""
        return self.closed.wait(timeout)

    def host_time(self, local: float | None = None) -> float:
        return self._clock() if local is None else local

    def room_position(self, local: float | None = None) -> float:
        return self.state.position_at(self.host_time(local))

    def members(self) -> list[Person]:
        """Everyone who came, host first, with the name each last used."""
        return [Person(pid, name) for pid, name in list(self._came.items())]

    def members_json(self) -> str:
        return _people.members_json(self.members())

    def drain(self) -> list[Event]:
        return self._events.drain()

    @property
    def guest_count(self) -> int:
        return sum(1 for p in self.people if not p["host"])

    # --- the loop (its own thread from here down) -----------------------------

    def _run(self) -> None:
        loop = self._loop
        try:
            while True:
                unread = [s for s in self._seats if s.link is not None and s.link.more]
                try:
                    ready = loop.selector.select(0 if unread else TICK)
                except OSError:
                    ready = []              # a socket closed under select; the pass below sorts it
                for key, mask in ready:
                    if key.data is None:
                        loop.run_work()
                    else:
                        self._service(key.data, mask)
                for seat in unread:         # stopped at their read budget last time round
                    if seat.link is not None and seat.link.more and not seat.link.closed:
                        self._service(seat, selectors.EVENT_READ)
                loop.run_work()
                self._tick(self._clock())
                self._flush_all()
                if self._ending is not None and all(s.link is None for s in self._seats):
                    break
        except Exception:
            _log.exception("movie night: the hub stopped")
            self._events.emit(Event("ended", "The movie night stopped: something went wrong "
                                             "on this PC (the log has the details)."))
        finally:
            for seat in self._seats:
                if seat.link is not None:
                    loop.forget(seat.link)
                seat.done.set()
            loop.close()
            self.closed.set()

    def _adopt(self, seat: _Seat) -> None:
        if sum(s.phase == "hello" for s in self._seats) >= MAX_PENDING_HELLOS:
            seat.link.close()               # somebody is opening connections for the sake of it
            seat.done.set()
            return
        try:
            self._loop.watch(seat.link, seat)
        except (ValueError, OSError):
            seat.link.close()
            seat.done.set()
            return
        self._seats.append(seat)
        if self._ending is not None:
            self._farewell(seat, "This movie night has ended.")
            return
        self._send(seat, {"type": "hello", "proto": PROTOCOL, "proto_min": PROTOCOL_MIN,
                          "app": APP_VERSION, "role": "host"})

    def _service(self, seat: _Seat, mask: int) -> None:
        link = seat.link
        if link is None or link.closed:
            return
        if mask & selectors.EVENT_READ or link.wants_write:
            lines, oversized, alive = link.receive()
            for line in lines:
                if seat.phase == "closing":
                    break
                self._on_line(seat, line)
            for _ in range(oversized):
                self._strike(seat, "a message over the size limit")
            if not alive:
                if seat.phase == "in":
                    self._gone(seat, "lost")
                self._close(seat)
                return
        if mask & selectors.EVENT_WRITE and not link.flush():
            if seat.phase == "in":
                self._gone(seat, "lost")
            self._close(seat)

    def _on_line(self, seat: _Seat, line: bytes) -> None:
        try:
            message = decode(line)
        except BadMessage as exc:
            self._strike(seat, str(exc))
            return
        t = self._clock()
        kind = message["type"]
        if seat.phase == "hello":
            if kind != "hello":
                self._farewell(seat, "Your Mistery did not say hello first.")
                return
            self._hello(seat, message, t)
            return
        if seat.phase != "in":
            return
        if not self._allow(seat, t):
            self._strike(seat, "more messages a second than a person could send")
            return
        seat.heard_at = t
        handler = self._handlers.get(kind)
        if handler is None:
            return                          # a later version's message: not ours to judge
        try:
            handler(seat, message, t)
        except BadMessage as exc:
            self._strike(seat, f"{kind}: {exc}")

    def _allow(self, seat: _Seat, t: float) -> bool:
        seat.tokens = min(BURST, seat.tokens + (t - seat.tokens_at) * RATE)
        seat.tokens_at = t
        if seat.tokens < 1.0:
            return False
        seat.tokens -= 1.0
        return True

    def _strike(self, seat: _Seat, why: str) -> None:
        seat.strikes += 1
        if seat.strikes <= 3:
            _log.warning("movie night: dropped a message from %s: %s", seat.who, why)
        if seat.strikes >= MAX_STRIKES and seat.phase != "closing":
            _log.warning("movie night: %s sent %d unusable messages; disconnecting",
                         seat.who, seat.strikes)
            if seat.phase == "in":
                self._gone(seat, "lost")
            self._farewell(seat, "Your Mistery sent the host too many messages it could not use.")

    def _hello(self, seat: _Seat, message: dict, t: float) -> None:
        name = clean_name(message.get("name")) or FALLBACK_NAME
        try:
            proto = whole(message.get("proto"), 0, 1_000_000)
            proto_min = whole(message.get("proto_min", proto), 0, proto)
        except BadMessage:
            self._farewell(seat, "Your Mistery's hello made no sense to the host's.")
            return
        app = _short_text(message.get("app"), 32)
        if proto_min > PROTOCOL or proto < PROTOCOL_MIN:
            host_is_newer = proto < PROTOCOL_MIN
            words = dict(host_is_newer=host_is_newer, host_app=APP_VERSION, guest_app=app,
                         guest_name=name)
            self._events.emit(Event("notice", version_sentence(for_host=True, **words)))
            self._farewell(seat, version_sentence(for_host=False, **words))
            return
        pid = message.get("id")
        if not is_person_id(pid):
            self._farewell(seat, "Your Mistery did not say who you are.")
            return
        if pid == self.me.id:
            self._farewell(seat, "That is the host's own Mistery: you are already in this movie night.")
            return
        replaced = next((s for s in self._seats if s is not seat and s.phase == "in"
                         and s.person and s.person.id == pid), None)
        guests = [s for s in self._seats if s.phase == "in" and s.link is not None and s is not replaced]
        if len(guests) >= MAX_GUESTS:
            self._events.emit(Event("notice", f"{name} could not join: the movie night is full "
                                              f"({MAX_GUESTS} friends)."))
            self._farewell(seat, f"This movie night is full: {MAX_GUESTS} friends are already watching.")
            return
        came_before = pid in self._came
        if replaced is not None:
            # The same person again — a rejoin after their Wi-Fi dropped, before
            # the old connection timed out. The new connection wins, quietly.
            replaced.phase = "closing"
            self._send(replaced, {"type": "end", "reason": "You joined this movie night again "
                                                          "from somewhere else."})
            replaced.link.begin_close(t + CLOSE_WAIT)
        taken = {s.person.name.casefold() for s in self._seats
                 if s.phase == "in" and s.person and s is not seat}
        seat.person = Person(pid, unique_name(name, taken))
        seat.phase = "in"
        seat.app = app
        seat.heard_at = t
        # Joining while the film plays, or while the room waits to play on:
        # nobody is stopped for this player's first open, or for its catching
        # up (_position_report ends that).
        seat.arriving = self.state.playing or self.state.waiting
        if replaced is not None:
            # Their connection, and so their stream, is the one of a moment ago.
            seat.pressed_through, seat.slow = replaced.pressed_through, replaced.slow
            seat.steady, seat.stalls = replaced.steady, replaced.stalls
        self._came[pid] = seat.person.name
        self._refresh_people()
        self._send(seat, {"type": "welcome", "proto": min(PROTOCOL, proto), "party": self.party_id,
                          "you": pid, "state": self.state.to_wire(), "people": self.people,
                          "now": self._clock()})
        _log.info("movie night: %s joined (Mistery %s)", seat.person.name, app or "?")
        text = "" if replaced is not None else \
            (f"{seat.person.name} is back" if came_before else f"{seat.person.name} joined")
        self._events.emit(Event("people", text, list(self.people)))
        self._broadcast_people(skip=seat)
        # A new connection starts out not buffering: its Client reports only
        # changes, so a state carried over from the old connection would never
        # be taken back, and a room waiting for them would wait for good. It
        # says so straight after the welcome if its player is still loading,
        # within READY_HOLD.
        self._maybe_resume(pid, t)

    def _on_intent(self, seat: _Seat, message: dict, t: float) -> None:
        action = message.get("action")
        if action not in INTENTS:
            raise BadMessage("not play, pause or seek")
        position = number(message.get("position"), 0.0, MAX_POSITION) if action == "seek" else None
        self._apply_intent(seat, action, position)

    def _apply_intent(self, seat: _Seat, action: str, position: float | None) -> None:
        if seat.phase != "in" or seat.person is None:
            return
        t = self._clock()
        state = self.state
        by = seat.person.id
        if action == "play":
            if state.playing:
                return
            if state.waiting:
                # Play while we wait for Sam means go without Sam: he catches up
                # when his stream is ready, and cannot stop the room again until
                # he has played STEADY_PLAY without a stall. His first recovery
                # used to be enough, and a stream that recovers only to run dry
                # again stopped the room 2.06 s after play was pressed. Somebody
                # on the list who is ready already (within READY_HOLD) was not
                # gone without, and is waited for as before.
                for other in self._seats:
                    if other.person and other.person.id in state.waiting_for \
                            and other.buffering_since is not None:
                        other.pressed_through = True
                        other.steady = 0.0
            else:
                stuck = self._stuck(t)
                if stuck:
                    # Somebody's player has been stuck for a while already: starting
                    # would only stop again a second later. Wait for them now, and
                    # carry on by itself when they are ready; play again goes without.
                    self._set_state(state.pause(t, stuck[0].person.id, cause="wait",
                                                waiting_for=[s.person.id for s in stuck]))
                    return
            new = state.play(t, self._lead(), by)
        elif action == "pause":
            if not state.playing and not state.waiting:
                return
            new = state.pause(t, by)            # a real pause: no carrying on by itself
        else:
            new = state.seek(position, t, self._lead(seek=True), by)
        self._set_state(new)

    def _lead(self, seek: bool = False) -> float:
        """How far ahead to schedule a start: a quarter second for the players to
        react, plus the slowest guest's round trip — the state needs half of it
        to arrive, and the rest is margin. 0.25 s on a LAN; 0.4 s with a friend
        150 ms away. A paused player is already resting on the room's frame, so
        that is all a play needs (measured: starts 1-16 ms apart).

        After a seek every player has to get there first. A 15-minute jump took
        0.21-0.34 s to land on this PC, from the file or through a guest's
        proxy, after the up to 0.1 s a player takes to notice; with 0.25 s of
        lead, and other programs busy on the PC, all three started 87-144 ms
        late. So a seek gets 0.6 s: a moment longer on the new frame, then
        everyone together — on time in every run since, busy PC or not (jumps
        of up to 0.48 s)."""
        slowest = max(((s.rtt or 0.0) for s in self._seats if s.phase == "in"), default=0.0)
        return min(LEAD_MAX, (SEEK_LEAD if seek else LEAD_MIN) + slowest)

    def _set_state(self, new: RoomState) -> None:
        before, self.state = self.state, new
        self._broadcast({"type": "state", "state": new.to_wire()})
        self._events.emit(Event("state", describe(new, self._names(), self.me.id, before), new))

    def _on_buffering(self, seat: _Seat, message: dict, t: float) -> None:
        on = message.get("on")
        if not isinstance(on, bool):
            raise BadMessage("on is not true or false")
        self._buffering(seat, on)

    def _buffering(self, seat: _Seat, on: bool) -> None:
        if seat.phase != "in" or seat.person is None or on == (seat.buffering_since is not None):
            return
        t = self._clock()
        if on:
            seat.buffering_since = t
            seat.stall_counted = False      # a new spell: counted once it outlasts the grace
            seat.steady = 0.0
        else:
            seat.buffering_since = None
            seat.ready_since = t
            self._maybe_resume(seat.person.id, t)
        self._refresh_people()
        self._broadcast_people()
        self._events.emit(Event("people", "", list(self.people)))

    def _stuck(self, t: float, since: float | None = None,
               already: tuple[str, ...] = ()) -> list[_Seat]:
        """Who the room should wait for at `t`: everyone buffering for BUFFER_GRACE
        (counted from `since` when that is later — a start scheduled for then), and
        everyone in `already` who still is. Not somebody whose player is busy for
        a moment: every player seeks to the held frame when the room pauses, and
        a room waiting for Alex used to take in each of those in passing, then
        send two more states and a "Waiting for Sam…" nobody needed. And never
        somebody the room goes on without: still arriving, pressed through, or
        too slow for the film (see _Seat)."""
        stuck = []
        for seat in self._seats:
            if seat.phase != "in" or seat.person is None or seat.buffering_since is None \
                    or seat.arriving or seat.pressed_through or seat.slow:
                continue
            start = seat.buffering_since if since is None else max(seat.buffering_since, since)
            if seat.person.id in already or t - start >= BUFFER_GRACE:
                stuck.append(seat)
        return stuck

    def _maybe_resume(self, ready_id: str | None, t: float) -> None:
        """A room waiting for somebody carries on by itself once everyone it waits
        for is gone, or has been ready for READY_HOLD. Looked at when somebody
        is ready or gone (`ready_id`: news about somebody the room is not
        waiting for changes nothing), and at every tick while it waits (None).

        Somebody ready for less than that stays on the list. When somebody
        leaves the list, anyone else stuck past the grace by then joins it, as
        before READY_HOLD; nobody joins a list nobody has left, so a wait whose
        other players are slow to reach the held frame says nothing new, and a
        tick that finds nothing new sends nothing. "Sam is ready" names whoever
        was ready last, and nobody when nobody is: the one waited for left, or
        came back on a new connection and is arriving, which the room does not
        wait for (_hello)."""
        state = self.state
        if not state.waiting or (ready_id is not None and ready_id not in state.waiting_for):
            return
        present = {s.person.id: s for s in self._seats if s.phase == "in" and s.person is not None}
        stuck = [s.person.id for s in self._stuck(t, already=state.waiting_for)]
        remaining = [pid for pid in state.waiting_for if pid in present and (
            pid in stuck or (present[pid].buffering_since is None
                             and t - present[pid].ready_since < READY_HOLD))]
        if len(remaining) == len(state.waiting_for):
            return                              # all still loading, or ready for under READY_HOLD
        still = remaining + [pid for pid in stuck if pid not in remaining]
        if still:
            self._set_state(replace(state, waiting_for=tuple(still), seq=state.seq + 1, cause="wait"))
            return
        ready = [present[pid] for pid in state.waiting_for
                 if pid in present and present[pid].buffering_since is None]
        last = max(ready, key=lambda s: s.ready_since, default=None)
        self._set_state(state.play(t, self._lead(), last.person.id if last else "", cause="resume"))

    def _on_ping(self, seat: _Seat, message: dict, t: float) -> None:
        n = whole(message.get("n"), 0, 2 ** 53)
        t0 = number(message.get("t0"), -MAX_CLOCK, MAX_CLOCK)
        t1 = seat.link.received_at
        if "rtt" in message and message["rtt"] is not None:
            seat.rtt = number(message["rtt"], 0.0, 60.0)
        if message.get("pos") is not None and message.get("pos_at") is not None:
            self._position_report(seat, number(message["pos"], 0.0, MAX_POSITION),
                                  number(message["pos_at"], -MAX_CLOCK, MAX_CLOCK),
                                  _maybe_whole(message.get("seq")))
        self._send(seat, {"type": "pong", "n": n, "t0": t0, "t1": t1, "t2": self._clock()})
        seat.link.flush()                   # now, so t2 is when it really left

    def _position_report(self, seat: _Seat, position: float, at: float, seq: int | None) -> None:
        """How far this person's player is from the room, if they were following this
        state; and a newcomer within CAUGHT_UP of it has arrived. A player reports
        only once its picture is up, so a first open never counts as caught up."""
        if seq is not None and seq != self.state.seq:
            return
        seat.drift = position - self.state.position_at(at)
        if seat.arriving and abs(seat.drift) <= CAUGHT_UP:
            seat.arriving = False
            _log.info("movie night: %s has caught up with the room", seat.who)

    def _on_bye(self, seat: _Seat, message: dict, t: float) -> None:
        self._gone(seat, "left")
        self._farewell(seat, None)

    def _new_media(self, media: dict, position: float, playing: bool) -> None:
        t = self._clock()
        state = replace(self.state, media=clean_media(media), playing=False,
                        position=position, at=t, waiting_for=())
        if playing:
            # Everyone has a new stream to open; a longer lead, and the buffering
            # grace, cover whoever takes longest.
            state = state.play(t, LEAD_MAX, self.me.id, cause="media")
        else:
            state = replace(state, seq=state.seq + 1, by=self.me.id, cause="media")
        for seat in self._seats:
            # Everyone opens the new one together, and gets a fresh start with it:
            # a slow friend's next episode may be lighter than this one.
            seat.drift = None
            seat.arriving = seat.pressed_through = seat.slow = seat.stall_counted = False
            seat.steady = 0.0
            seat.stalls.clear()
        self._set_state(state)
        self._refresh_people()
        self._broadcast_people()

    def _end(self, reason: str) -> None:
        if self._ending is not None:
            return
        self._ending = reason
        for seat in list(self._seats):
            if seat.link is not None and seat.phase != "closing":
                seat.phase = "closing"
                self._send(seat, {"type": "end", "reason": reason})
                seat.link.begin_close(self._clock() + CLOSE_WAIT)
        self._seats = [s for s in self._seats if s.link is not None]
        self._events.emit(Event("ended", ""))

    # --- leaving --------------------------------------------------------------

    def _gone(self, seat: _Seat, how: str) -> None:
        """Somebody is out of the room: "left" said bye, "lost" did not."""
        if seat.phase != "in" or seat.person is None:
            return
        seat.phase = "closing"
        seat.buffering_since = None
        _log.info("movie night: %s %s", seat.person.name, "left" if how == "left" else "was lost")
        self._refresh_people()
        text = f"{seat.person.name} left" if how == "left" else f"Lost {seat.person.name}'s connection"
        self._events.emit(Event("people", text, list(self.people)))
        self._broadcast_people()
        self._maybe_resume(seat.person.id, self._clock())

    def _farewell(self, seat: _Seat, reason: str | None) -> None:
        """A last word, if any, then close gently."""
        if seat.link is None:
            return
        if reason:
            self._send(seat, {"type": "refused", "reason": reason})
        seat.phase = "closing"
        seat.link.begin_close(self._clock() + CLOSE_WAIT)

    def _close(self, seat: _Seat) -> None:
        if seat.link is not None:
            self._loop.forget(seat.link)
        if seat in self._seats:
            self._seats.remove(seat)
        seat.done.set()

    # --- timers and output ----------------------------------------------------

    def _tick(self, t: float) -> None:
        for seat in list(self._seats):
            if seat.link is None:
                continue
            if seat.phase == "hello" and t > seat.hello_by:
                self._farewell(seat, "Your Mistery did not say hello in time.")
            elif seat.phase == "in" and t - seat.heard_at > LOST_AFTER:
                self._gone(seat, "lost")
                self._close(seat)
            elif seat.phase == "closing":
                seat.link.flush()
                seat.link.shut_when_sent()
                if t > seat.link.close_by:
                    self._close(seat)
        if self._ending is not None:
            return

        # Bounded: a loop held up for a while (a busy PC) did not watch anybody
        # play steadily through it.
        passed = min(max(0.0, t - self._ticked_at), 0.5)
        self._ticked_at = t
        state = self.state
        if state.moving_at(t):
            self._count_stalls(t, since=state.at)
            self._count_steady_play(t, passed)
            late = self._stuck(t, since=state.at)
            if late:
                self._set_state(state.pause(t, late[0].person.id, cause="wait",
                                            waiting_for=[s.person.id for s in late]))
            elif state.duration is not None and state.position_at(t) >= state.duration:
                self._set_state(state.pause(t, "", cause="end"))
        elif state.waiting:
            self._maybe_resume(None, t)         # the ready ones may have held for READY_HOLD by now

        if t - self._people_sent_at >= PEOPLE_EVERY:
            self._refresh_people()
            self._broadcast_people()
            self._events.emit(Event("people", "", list(self.people)))

    def _count_stalls(self, t: float, since: float) -> None:
        """Each time somebody's player is stuck past BUFFER_GRACE while the room
        plays (counted from `since`, the start), once a spell, whether or not the
        room waits for it: somebody gone without since a press of play who keeps
        running dry is found out the same way. The third within STALL_WINDOW:
        the room goes on without them. A newcomer's arriving is not a stall."""
        for seat in self._seats:
            if (seat.phase != "in" or seat.person is None or seat.arriving or seat.stall_counted
                    or seat.buffering_since is None
                    or t - max(seat.buffering_since, since) < BUFFER_GRACE):
                continue
            seat.stall_counted = True
            _forget_old_stalls(seat, t)
            seat.stalls.append(t)
            if len(seat.stalls) > STALLS_BEFORE_GOING_ON and not seat.slow:
                self._go_on_without(seat)

    def _go_on_without(self, seat: _Seat) -> None:
        """Stop waiting for somebody whose stream cannot keep up, and tell everyone:
        the host here, the guests through the people list's "slow" (each Client
        words it for its own screen)."""
        seat.slow = True
        seat.steady = 0.0
        _log.info("movie night: %s stalled %d times in %.0f s; going on without them",
                  seat.who, len(seat.stalls), STALL_WINDOW)
        self._refresh_people()
        self._broadcast_people()
        mine = seat is self._host
        text = going_on_without(None if mine else seat.person.name, remote=not mine)
        self._events.emit(Event("people", text, list(self.people)))

    def _count_steady_play(self, t: float, passed: float) -> None:
        """Time played without a stall by whoever the room goes on without. After
        STEADY_PLAY of it, a press of play is over. Somebody too slow for the
        film is waited for again once, as well, their stalls have aged out of
        STALL_WINDOW to where the next one would be waited for: a friend whose
        stream runs dry every 40 s would otherwise be let back in after each
        steady half minute only to be gone without again at the next stall,
        with the same notice every time."""
        for seat in self._seats:
            if seat.phase != "in" or not (seat.pressed_through or seat.slow) \
                    or seat.buffering_since is not None:
                continue
            seat.steady += passed
            if seat.steady < STEADY_PLAY:
                continue
            if seat.pressed_through:
                seat.pressed_through = False
                _log.info("movie night: %s played %.0f s without a stall; waited for again",
                          seat.who, STEADY_PLAY)
            if seat.slow:
                _forget_old_stalls(seat, t)
                if len(seat.stalls) < STALLS_BEFORE_GOING_ON:
                    seat.slow = False
                    _log.info("movie night: %s is keeping up again; waited for again", seat.who)
                    self._refresh_people()
                    self._broadcast_people()
                    self._events.emit(Event("people", "", list(self.people)))

    def _send(self, seat: _Seat, message: dict) -> None:
        link = seat.link
        if link is None or link.closed:
            return
        link.send(encode(message))
        if link.unsent > MAX_OUTBOX and seat.phase != "closing":
            _log.warning("movie night: %s stopped reading; disconnecting", seat.who)
            self._gone(seat, "lost")
            self._close(seat)

    def _broadcast(self, message: dict, skip: _Seat | None = None) -> None:
        data = encode(message)
        for seat in list(self._seats):
            if seat.phase == "in" and seat.link is not None and seat is not skip:
                seat.link.send(data)
                if seat.link.unsent > MAX_OUTBOX:
                    self._gone(seat, "lost")
                    self._close(seat)

    def _broadcast_people(self, skip: _Seat | None = None) -> None:
        self._people_sent_at = self._clock()
        self._broadcast({"type": "people", "people": self.people}, skip)

    def _flush_all(self) -> None:
        for seat in list(self._seats):
            link = seat.link
            if link is None or link.closed:
                continue
            if not link.flush():
                if seat.phase == "in":
                    self._gone(seat, "lost")
                self._close(seat)
                continue
            if seat.phase == "closing":
                link.shut_when_sent()
            self._loop.update_interest(link, seat)

    def _refresh_people(self) -> None:
        self.people = [{
            "id": seat.person.id,
            "name": seat.person.name,
            "host": seat.link is None,
            "buffering": seat.buffering_since is not None,
            "slow": seat.slow,
            "drift": None if seat.drift is None else round(seat.drift, 3),
            "rtt": None if seat.rtt is None else round(seat.rtt, 4),
        } for seat in self._seats if seat.phase == "in" and seat.person is not None]

    def _names(self) -> dict[str, str]:
        return {p["id"]: p["name"] for p in self.people} | \
            {pid: name for pid, name in self._came.items() if pid not in {p["id"] for p in self.people}}


def _maybe_whole(value: object) -> int | None:
    try:
        return whole(value, 0, 2 ** 53)
    except BadMessage:
        return None


def _forget_old_stalls(seat: _Seat, t: float) -> None:
    while seat.stalls and t - seat.stalls[0] > STALL_WINDOW:
        seat.stalls.popleft()


def _close_quietly(sock) -> None:
    try:
        sock.close()
    except OSError:
        pass


# --- a guest ------------------------------------------------------------------

class _Failed(Exception):
    """Connecting did not work; the message is the sentence to show."""


def unreachable(port: int, internet: bool, host: str | None = None,
                other_certificate: bool = False) -> str:
    """What to tell a guest whose Mistery reached neither of the host's addresses.

    There is no telling which of the usual causes it is, so it names them and
    who can fix each. A friend elsewhere needs the host's port forwarded: on
    this owner's network UPnP does not answer, so forwarding by hand is how
    anyone outside the house gets in. And a forward already made is checked by
    nothing: without UPnP the host's Mistery cannot see it, and one pointing at
    the PC's old address (routers hand addresses out again) looks exactly like
    a working one from the host's side. Only a friend who cannot get in finds
    out, so the sentence says what the host should check then. A friend at the
    host's place is most likely stopped by Windows Firewall on the host's PC —
    a Cancel on the question Windows asks the first time, or a network marked
    Public.

    Two more causes look exactly the same from here, and the review met both:
    a movie night that has ended (a code pasted after the host ended it finds
    nobody listening), and the guest's own network gone (a PC with no network
    at all is told so on its own: OFFLINE). So the sentence names both. A code
    with no internet address cannot work from elsewhere whatever anybody
    forwards: the host's Mistery found no internet address when the movie
    night started, a VPN on their PC being the usual reason, and only a new
    code can carry one, so a friend elsewhere is told to ask for it.

    `other_certificate`: something answered at the host's LAN address with a
    certificate that is not the code's, and then the internet address did not
    answer. Away from the host's house that can be another device of your own
    that happens to have that address, so it cannot decide the sentence by
    itself (the internet's silence may be the whole story). But at the host's
    place it is their Mistery with a newer movie night on the same port, the
    code an old one: the review's friend with the code from before Hana
    reopened Mistery was told to have a port forwarded, which no router could
    help. So a friend at their place is told that first, and nothing about the
    firewall, which let the answer through.

    `host` is the host's name when it is known: not while joining (an invite
    carries addresses, not people), but GuestProxy says the same, by name, when
    the stream cannot reach the host later on.
    """
    name = "".join(c for c in str(host or "")[:40] if c.isprintable()).strip()
    whose = f"{name}'s" if name else "the host's"
    firewall = ("Windows Firewall on their PC may be blocking Mistery — they can allow it in "
                "Windows Security.")
    if internet and other_certificate:
        return (f"Couldn't reach {whose} PC. If you're at their place: something answered there, but "
                "not the movie night this code is for, so the code is probably from an earlier one; "
                "ask them for the new code. If you're not at their place, their router needs port "
                f"{port} forwarded to their PC — their movie night panel shows how — and if it is, "
                "they should check it still points at their PC. And check your own internet "
                "connection.")
    if internet:
        return (f"Couldn't reach {whose} PC. If you're not at their place, their router needs "
                f"port {port} forwarded to their PC — their movie night panel shows how. If it's "
                "forwarded already, they should check it still points at their PC: routers "
                f"sometimes give a PC a new address. If you are at their place, {firewall} "
                "If their movie night has ended, or they started a new one, ask them for the new "
                "code. And check your own internet connection.")
    return (f"Couldn't reach {whose} PC. This code has no internet address in it, so it only "
            f"works at their place. If you're there, {firewall} If you're elsewhere, this code "
            "can't reach them: when they started, their Mistery couldn't find their internet "
            "address (a VPN on their PC is the usual reason), so they'll need to start the movie "
            "night again and send you the new code.")


def no_network(exc: BaseException) -> bool:
    """Whether a connection failed because this PC has no network at all.

    Windows answers at once, WSAENETUNREACH (10051), when nothing routes
    anywhere: the Wi-Fi off, the cable out. Measured here with a destination
    that routes nowhere (0.0.0.1): the error in 15 ms (one tick of Windows'
    timer), nothing sent. Anything else (a refusal, silence) could be either
    side's doing."""
    return isinstance(exc, OSError) and (exc.errno == errno.ENETUNREACH
                                         or getattr(exc, "winerror", None) == 10051)


# A certificate that is not the code's as the last word: the code's only
# address, or its internet address after the LAN one. Almost always the host's
# own Mistery with a newer movie night on the same port (the port stays 42170
# from one night to the next), so the code is an old one. Trying again cannot
# help, so the Join box offers no "Try again" for it. (A wrong certificate at
# the LAN address followed by silence is unreachable(other_certificate=True).)
NOT_THEM =("Something answered at the host's address, but its certificate doesn't match the "
            "code, so nothing was sent to it. The code is probably from an earlier movie night: "
            "ask the host for a fresh one.")
TURNED_DOWN = ("The host's Mistery would not take this code. It may be from an earlier movie "
               "night — ask the host for a fresh one.")
NO_ANSWER = "The host's Mistery did not answer. Try joining again in a moment."
LOST_HOST = "Lost the connection to the host."
OFFLINE = "This PC isn't connected to any network right now. Check its Wi-Fi or network cable."


class Client:
    """The room, from a guest's side: connect with an invite, then follow and ask.

    `connect` returns at once; how it goes arrives as events ("connecting" with
    a sentence at each step, then "connected" or "ended" with the reason).
    After that it offers what Hub offers: `state`, `people`, `host_time()`,
    `room_position()`, `clock`, and intent / play / pause / seek,
    set_buffering, report_position, and `leave`. `address` is the host address
    that answered: give it to GuestProxy, so the stream does not try an address
    that already failed.

    Every local play, pause and seek can go straight to `intent`: the host takes
    30 a second (bursts of 60) from each guest, more than a held arrow key
    sends. Past that it drops them, and a guest that keeps it up is disconnected.

    A room that keeps waiting for you — states with cause "wait" and your id in
    `waiting_for` — is the cue to offer a lighter stream (1080p, 720p) rather
    than stop everybody again.
    """

    # How long each of the host's addresses gets, connecting and the TLS
    # handshake together. A host on the same network answers in milliseconds or
    # not at all; across the internet a handshake is three round trips. The
    # same as GuestProxy's, which tries the same addresses. A test can shorten
    # them on one instance.
    lan_timeout = 1.5
    wan_timeout = 6.0

    def __init__(self, me: Person | None = None, *, clock: Callable[[], float] = now,
                 on_event: Callable[[Event], None] | None = None) -> None:
        self.me = me if me is not None else _people.me()
        self._clock = clock
        self._events = _Events(on_event)
        self.clock = ClockSync()
        self.state = RoomState()
        self.people: list[dict] = []
        self.party_id = ""
        self.host: Person | None = None
        self.address: str | None = None     # the host address that answered
        self.proto = PROTOCOL
        self.joined = threading.Event()
        self.ended = threading.Event()
        self.reason = ""
        self._came: dict[str, str] = {}
        self._loop = _Loop()
        self._link: _Link | None = None
        self._phase = "idle"            # idle | hello | welcome | in | closing
        self._deadline = 0.0
        self._heard_at = 0.0
        self._next_ping = 0.0
        self._quick_left = 0
        self._ping_n = 0
        self._pings: dict[int, float] = {}
        self._buffering = False
        self._report: tuple[float, float, int] | None = None
        self._strikes = 0
        self._leaving = False
        self._thread: threading.Thread | None = None
        self._handlers = {"state": self._on_state, "people": self._on_people,
                          "pong": self._on_pong, "end": self._on_end, "refused": self._on_refused}

    # --- any thread -----------------------------------------------------------

    def connect(self, invite) -> None:
        """Join the movie night an Invite (app.party.invite) points at. Returns at once."""
        self._start(lambda: self._open_invite(invite))

    def connect_socket(self, sock, token: bytes | str) -> None:
        """The same, over a connection the caller has already opened (TLS or not)."""
        token_hex = _token_hex(token)
        self._start(lambda: (sock, token_hex))

    def intent(self, action: str, position: float | None = None) -> None:
        action, position = _check_intent(action, position)
        self._loop.call(lambda: self._send_intent(action, position))

    def play(self) -> None:
        self.intent("play")

    def pause(self) -> None:
        self.intent("pause")

    def seek(self, position: float) -> None:
        self.intent("seek", position)

    def set_buffering(self, on: bool) -> None:
        """This player cannot play right now (opening, seeking, refilling), or can again."""
        on = bool(on)
        self._loop.call(lambda: self._set_buffering(on))

    def report_position(self, position: float, local: float | None = None) -> None:
        """Where this player is, sent with the next ping so the host can show who is behind.
        Only a picture that is on screen, never one still opening: joining mid-film,
        this is also how the host learns the player has caught up with the room
        (Hub._position_report), and so should wait for it from then on."""
        local = self._clock() if local is None else local
        try:
            self._report = (float(position), self.host_time(local), self.state.seq)
        except (TypeError, ValueError):
            pass

    def leave(self) -> None:
        """Say bye and go. Returns at once; `ended` is set when it has.
        While still connecting, it stops as soon as the attempt under way returns."""
        self._leaving = True
        self._loop.call(self._leave)

    def wait_joined(self, timeout: float) -> bool:
        """For callers that are not the Qt thread (tests, tools): joined, or ended trying."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.joined.wait(0.02):
                return True
            if self.ended.is_set():
                return False
        return self.joined.is_set()

    def host_time(self, local: float | None = None) -> float:
        return self.clock.host_time(self._clock() if local is None else local)

    def room_position(self, local: float | None = None) -> float:
        return self.state.position_at(self.host_time(local))

    def members(self) -> list[Person]:
        return [Person(pid, name) for pid, name in list(self._came.items())]

    def members_json(self) -> str:
        return _people.members_json(self.members())

    def drain(self) -> list[Event]:
        return self._events.drain()

    # --- connecting -----------------------------------------------------------

    def _start(self, opener: Callable[[], tuple[object, str]]) -> None:
        if self._thread is not None:
            raise RuntimeError("a Client connects once; make another for another movie night")
        self._thread = threading.Thread(target=self._run, args=(opener,), name="party-client",
                                        daemon=True)
        self._thread.start()

    def _open_invite(self, invite) -> tuple[object, str]:
        """The host's LAN address first, then its internet address.

        LAN first because a friend on the same Wi-Fi usually cannot reach the
        host through its own public address (most routers do not loop that
        back), and it is faster. The LAN address answering with the wrong
        certificate is normal away from the host's network — some other device
        there has that address — so a wrong certificate decides the sentence
        only when it came from the last address tried: then the code itself is
        not this host's (NOT_THEM: probably from an earlier movie night). Where
        the last word was silence or a refusal, unreachable() says what to do,
        with the ended-movie-night and own-connection causes the review met,
        and, when the LAN address did answer with another certificate first,
        with "an earlier movie night" first for a friend at the host's place.

        Every address failing for want of any network at all is told apart too
        (OFFLINE): that is this PC's own doing, not the host's.
        """
        from . import tls

        token_hex = _token_hex(invite.token)
        port = int(invite.port)
        steps = [("lan", "Trying your network…", invite.lan_ip, self.lan_timeout),
                 ("internet", "Trying the internet…", invite.wan_ip, self.wan_timeout)]
        if invite.lan_ip and invite.lan_ip == invite.wan_ip:
            # A PC with a public address of its own: one try, with the internet's patience.
            steps = steps[1:]
        last = ""                               # how the last address tried went
        offline = True                          # ...every failure so far was this PC's own network
        other_certificate = False               # ...some address answered with a certificate not the code's
        for step, text, address, timeout in steps:
            if not address or self._leaving:
                continue
            self._events.emit(Event("connecting", text, {"step": step, "address": address}))
            try:
                sock = tls.connect(address, port, invite.pin, timeout)
            except tls.PinMismatch:
                last, offline, other_certificate = "not them", False, True
                _log.info("movie night: %s:%s answered with another certificate", address, port)
                continue
            except Exception as exc:        # refused, timed out, reset, handshake failed
                last, offline = "no answer", offline and no_network(exc)
                _log.info("movie night: %s:%s did not work: %s", address, port, exc)
                continue
            self.address = address
            return sock, token_hex
        if not last:
            raise _Failed("This code has no address in it. Ask the host for a fresh one.")
        if last == "not them":
            raise _Failed(NOT_THEM)
        if offline:
            raise _Failed(OFFLINE)
        raise _Failed(unreachable(port, internet=bool(invite.wan_ip), other_certificate=other_certificate))

    def _run(self, opener: Callable[[], tuple[object, str]]) -> None:
        loop = self._loop
        try:
            try:
                sock, token_hex = opener()
            except _Failed as exc:
                self._finish("" if self._leaving else str(exc))
                return
            if self._leaving:
                _close_quietly(sock)
                self._finish("")
                return
            self._link = _Link(sock, "host", MAX_HOST_LINE, self._clock)
            loop.watch(self._link, self._link)
            self._link.send(f"{PREAMBLE} {token_hex}\n".encode("ascii"))
            self._phase = "hello"
            self._deadline = self._clock() + HELLO_TIMEOUT
            self._events.emit(Event("connecting", "Saying hello…",
                                    {"step": "hello", "address": self.address}))
            while True:
                unread = self._link.more
                try:
                    ready = loop.selector.select(0 if unread else TICK)
                except OSError:
                    ready = []
                for key, mask in ready:
                    if key.data is None:
                        loop.run_work()
                    else:
                        self._service(mask)
                if self._link.more and not self._link.closed:
                    self._service(selectors.EVENT_READ)
                loop.run_work()
                self._tick(self._clock())
                link = self._link
                if link.closed:
                    break
                if not link.flush():
                    self._lost()
                    break
                link.shut_when_sent()
                loop.update_interest(link, link)
        except Exception:
            _log.exception("movie night: the connection to the host stopped")
            self._finish("The movie night stopped: something went wrong on this PC "
                         "(the log has the details).")
        finally:
            if self._link is not None:
                loop.forget(self._link)
            loop.close()
            if not self.ended.is_set():
                self._finish(LOST_HOST)

    def _service(self, mask: int) -> None:
        link = self._link
        lines, oversized, alive = link.receive()
        for line in lines:
            if self._phase == "closing":
                break
            self._on_line(line)
        for _ in range(oversized):
            self._strike("a message over the size limit")
        if not alive:
            if self._phase == "hello":
                self._finish(TURNED_DOWN)
            elif self._phase != "closing":
                self._lost()
            link.close()
            return
        if mask & selectors.EVENT_WRITE and not link.flush():
            self._lost()
            link.close()

    def _on_line(self, line: bytes) -> None:
        try:
            message = decode(line)
        except BadMessage as exc:
            self._strike(str(exc))
            return
        kind = message["type"]
        self._heard_at = self._link.received_at
        try:
            if kind == "refused" and self._phase in ("hello", "welcome"):
                self._on_refused(message)
            elif kind == "end" and self._phase in ("hello", "welcome"):
                self._on_end(message)
            elif self._phase == "hello":
                if kind != "hello":
                    self._finish("Something answered at that address, but it is not a movie night.")
                else:
                    self._on_hub_hello(message)
            elif self._phase == "welcome":
                if kind == "welcome":
                    self._on_welcome(message)
            elif self._phase == "in":
                handler = self._handlers.get(kind)
                if handler is not None:
                    handler(message)
        except BadMessage as exc:
            self._strike(f"{kind}: {exc}")

    def _strike(self, why: str) -> None:
        self._strikes += 1
        if self._strikes <= 3:
            _log.warning("movie night: dropped a message from the host: %s", why)
        if self._strikes >= MAX_STRIKES and self._phase != "closing":
            self._finish("The host's Mistery keeps sending things this one cannot read. "
                         "One of you may need to update.")

    def _on_hub_hello(self, message: dict) -> None:
        try:
            proto = whole(message.get("proto"), 0, 1_000_000)
            proto_min = whole(message.get("proto_min", proto), 0, proto)
        except BadMessage:
            self._finish("The host's Mistery said hello in a way this one cannot read.")
            return
        # Our hello goes out whatever the versions, so the host can tell whoever
        # is hosting who tried to join and what they need to do.
        self._send({"type": "hello", "proto": PROTOCOL, "proto_min": PROTOCOL_MIN,
                    "app": APP_VERSION, "id": self.me.id, "name": self.me.name})
        if proto_min > PROTOCOL or proto < PROTOCOL_MIN:
            self._finish(version_sentence(for_host=False, host_is_newer=proto_min > PROTOCOL,
                                          host_app=_short_text(message.get("app"), 32),
                                          guest_app=APP_VERSION))
            return
        self.proto = min(PROTOCOL, proto)
        self._phase = "welcome"
        self._deadline = self._clock() + HELLO_TIMEOUT

    def _on_welcome(self, message: dict) -> None:
        state = RoomState.from_wire(message.get("state"))
        people = parse_people(message.get("people"))
        party = message.get("party")
        if not isinstance(party, str) or not _PARTY_ID_RE.match(party):
            raise BadMessage("no party id")
        sent = number(message.get("now"), -MAX_CLOCK, MAX_CLOCK)
        self.clock.seed(sent, self._link.received_at)
        self.party_id = party
        self.state = state
        self.people = people
        host = next((p for p in people if p["host"]), None)
        self.host = Person(host["id"], host["name"]) if host else None
        for person in people:
            self._came[person["id"]] = person["name"]
        self._phase = "in"
        self._next_ping = self._clock()
        self._quick_left = QUICK_PINGS
        if self._buffering:
            self._send({"type": "buffering", "on": True})
        self.joined.set()
        _log.info("movie night: joined %s's party %s", self.host.name if self.host else "?", party[:8])
        self._events.emit(Event("connected", "", {"party": party, "host": self.host,
                                                  "state": state, "people": list(people),
                                                  "media": state.media, "address": self.address}))

    def _on_state(self, message: dict) -> None:
        state = RoomState.from_wire(message.get("state"))
        if state.seq <= self.state.seq:
            return                          # older news than what we have
        before, self.state = self.state, state
        names = {p["id"]: p["name"] for p in self.people} | \
            {pid: n for pid, n in self._came.items() if pid not in {p["id"] for p in self.people}}
        self._events.emit(Event("state", describe(state, names, self.me.id, before), state))

    def _on_people(self, message: dict) -> None:
        people = parse_people(message.get("people"))
        before = {p["id"]: p["name"] for p in self.people}
        after = {p["id"]: p["name"] for p in people}
        news = [f"{name} joined" for pid, name in after.items()
                if pid not in before and pid != self.me.id]
        news += [f"{name} left" for pid, name in before.items()
                 if pid not in after and pid != self.me.id]
        # The room has stopped waiting for somebody whose stream keeps running
        # dry: said once, when it happens, in this screen's words.
        was_slow = {p["id"] for p in self.people if p.get("slow")}
        news += [going_on_without(None if p["id"] == self.me.id else p["name"], remote=not p["host"])
                 for p in people if p["slow"] and p["id"] not in was_slow]
        self.people = people
        for pid, name in after.items():
            self._came[pid] = name
        self._events.emit(Event("people", "; ".join(news), list(people)))

    def _on_pong(self, message: dict) -> None:
        n = whole(message.get("n"), 0, 2 ** 53)
        t0 = self._pings.pop(n, None)
        if t0 is None:
            return                          # not a ping of ours, or too old to care
        t1 = number(message.get("t1"), -MAX_CLOCK, MAX_CLOCK)
        t2 = number(message.get("t2"), -MAX_CLOCK, MAX_CLOCK)
        self.clock.add(t0, t1, t2, self._link.received_at)

    def _on_end(self, message: dict) -> None:
        reason = clean_name(message.get("reason"), 300) or "The host ended the movie night."
        self._finish(reason)

    def _on_refused(self, message: dict) -> None:
        reason = clean_name(message.get("reason"), 300) or TURNED_DOWN
        self._finish(reason)

    # --- sending --------------------------------------------------------------

    def _send(self, message: dict) -> None:
        if self._link is not None and self._phase != "closing":
            self._link.send(encode(message))

    def _send_intent(self, action: str, position: float | None) -> None:
        if self._phase != "in":
            _log.info("movie night: %s before joining was dropped", action)
            return
        message = {"type": "intent", "action": action}
        if action == "seek":
            message["position"] = position
        self._send(message)

    def _set_buffering(self, on: bool) -> None:
        if on == self._buffering:
            return
        self._buffering = on
        if self._phase == "in":
            self._send({"type": "buffering", "on": on})

    def _ping(self, t: float) -> None:
        self._ping_n += 1
        message = {"type": "ping", "n": self._ping_n, "t0": t}
        if self.clock.rtt is not None:
            message["rtt"] = round(self.clock.rtt, 5)
        report = self._report
        if report is not None:
            position, at, seq = report
            if math.isfinite(position) and 0 <= position <= MAX_POSITION and math.isfinite(at):
                message.update(pos=position, pos_at=at, seq=seq)
            self._report = None
        self._pings[self._ping_n] = t
        for stale in [n for n in self._pings if n < self._ping_n - 8]:
            del self._pings[stale]
        self._send(message)
        self._link.flush()                  # now, so t0 is when it really left

    # --- timers and endings ---------------------------------------------------

    def _tick(self, t: float) -> None:
        link = self._link
        if self._phase in ("hello", "welcome") and t > self._deadline:
            self._finish(NO_ANSWER)
        elif self._phase == "in":
            if t - self._heard_at > LOST_AFTER:
                self._lost()
            elif t >= self._next_ping:
                self._ping(t)
                if self._quick_left > 0:
                    self._quick_left -= 1
                    self._next_ping = t + 0.1
                else:
                    self._next_ping = t + PING_EVERY
        elif self._phase == "closing" and link is not None:
            link.shut_when_sent()
            if t > link.close_by:
                link.close()

    def _leave(self) -> None:
        if self._phase == "in":
            self._send({"type": "bye"})
        self._finish("")

    def _lost(self) -> None:
        self._finish(LOST_HOST)

    def _finish(self, reason: str) -> None:
        """This movie night is over for us: say why once, then close gently."""
        if self.ended.is_set():
            return
        self.reason = reason
        self._phase = "closing"
        if self._link is not None:
            self._link.begin_close(self._clock() + CLOSE_WAIT)
        if reason:
            _log.info("movie night: ended: %s", reason)
        self.ended.set()
        self._events.emit(Event("ended", reason))
