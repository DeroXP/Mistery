"""Listening parties: one queue of songs, and everyone hearing the same second of it.

The movie night's room is the base (sync.Hub and sync.Client): the clock
everyone agrees on, the hellos, people, pings and every limit carry over as
they are. One thing does not: a film waits for a friend whose stream has run
dry, and a listening party plays on (ListenHub._stuck). What a listening party
adds is the queue. The host's Mistery holds it, next to the room's state, and
says what is in it:

    host → guest   queue      rev, start, items [{e, id, title, artist, album,
                              duration, by}], index, total, dj, host, votes, needed
                   note       what, by, title, count, needed: who did what
                   song       n, id, token: the answer to a fetch
                   found      n, items: the answer to an ask
    guest → host   intent     action: play | pause | seek | next | previous |
                              jump | add | remove | vote | replace, and its fields
                   fetch      n, id: a token to stream one song of the queue
                   ask        n, q: search the host's music, to add from it

The room's media is the song playing: {"key", "e", "id", "title", "artist",
"album", "duration", "kind": "track"}. `e` is the queue entry (the same song
can be queued twice), `id` the song in the host's library. A song's file is
fetched like a friend's song (app/share/music.py): the guest's proxy asks for
a token when its player comes for the song, and the host's listener serves
that one file for it (server.offer), never a path the guest names.

Who does what. The DJ plays, pauses, seeks, skips, goes back and puts on
something else; that is the host, or, when the host joined a friend who was
already listening to the host's music, that friend. Everyone may add songs from
the host's music, remove the ones they added, and vote to skip: a skip happens
once half the room wants it (the DJ's own press of Next skips at once).

Songs follow each other without a gap. Every player has the queue in its own
playlist, so it moves on to the next song by itself at the end of this one; the
room moves on at the same moment (Hub._tick), dated to when the song ended, so
a player that already moved on is already where the room is.
"""

from __future__ import annotations

import collections
import itertools
import json
import logging
import math
import re
import socket
import ssl
import threading
import time
import uuid
from dataclasses import replace
from typing import Callable

from . import people as _people
from . import sync
from .guest_proxy import GuestProxy, _NOT_FOUND, _PRINTABLE_RE, _parse_fields, _read_head
from .people import FALLBACK_NAME, clean_name

_log = logging.getLogger("party.listening")

MAX_QUEUE = 1000                # entries the room holds; a "shuffle everything" is cut here
WINDOW_BEFORE = 20              # entries before the one playing that a guest is sent
WINDOW_AFTER = 240              # ...and after it: a queue message stays well under 64 KB
MAX_FOUND = 40                  # songs one search answers with
ADDS_PER_MINUTE = 30            # a guest adding songs faster than this is ignored
CHANGE_LEAD = 0.8               # a new song: time for every player to open it (plus the slowest round trip)
RESTART_AFTER = 3.0             # Previous past this far into a song starts it again
KEEP_TOKENS = 8                 # song tokens a guest's proxy keeps
FETCH_TIMEOUT = 6.0
LISTEN_OFFERS = -44             # the "friend" a party's song offers are made to (server.withdraw_all)

ACTIONS = ("play", "pause", "seek", "next", "previous", "jump", "add", "remove", "vote", "replace")
DJ_ACTIONS = ("play", "pause", "seek", "next", "previous", "jump", "replace")
NOTES = ("added", "added_next", "removed", "skipped", "vote", "put_on", "dj", "not_dj")


# --- words ------------------------------------------------------------------------------

def _name(pid: str, names: dict[str, str], me_id: str, capital: bool = True) -> str:
    if pid and pid == me_id:
        return "You" if capital else "you"
    return names.get(pid, FALLBACK_NAME) if pid else ("The party" if capital else "the party")


def note_text(note: dict, names: dict[str, str], me_id: str) -> str:
    """A note from the room as a line for the screen: "Sam added Blue in Green"."""
    what = note.get("what")
    who = _name(str(note.get("by") or ""), names, me_id)
    title = str(note.get("title") or "")
    if what == "added":
        return f"{who} added {title}" if title else f"{who} added a song"
    if what == "added_next":
        return f"{who} put {title} on next" if title else f"{who} put a song on next"
    if what == "removed":
        return f"{who} took {title} off the queue" if title else f"{who} took a song off the queue"
    if what == "put_on":
        return f"{who} put on {title}" if title else f"{who} put on something new"
    if what == "skipped":
        count = int(note.get("count") or 0)
        if count > 1:
            return f"Skipped: {count} of you wanted to"
        return f"{who} skipped {title}" if title else f"{who} skipped"
    if what == "vote":
        count, needed = int(note.get("count") or 0), int(note.get("needed") or 0)
        return f"{who} voted to skip ({count} of {needed})"
    if what == "dj":
        return "You're the DJ now" if who == "You" else f"{who} is the DJ now"
    if what == "not_dj":
        dj = _name(str(note.get("dj") or ""), names, me_id, capital=True)
        return f"{dj} is the DJ: they play, pause and skip. You can add songs and vote to skip."
    return ""


def describe(state: sync.RoomState, names: dict[str, str], me_id: str,
             before: sync.RoomState | None = None) -> str:
    """The line for a new state, in a listening party's words ("" when there
    is nothing to say, as for your own doing or a song ending into the next)."""
    if state.cause == "wait":
        return sync.describe(state, names, me_id, before)
    if not state.by or state.by == me_id:
        return ""
    who = names.get(state.by, FALLBACK_NAME)
    title = str((state.media or {}).get("title") or "")
    if state.cause == "media":
        return f"{who} played {title}" if title else f"{who} put on another song"
    if state.cause == "seek":
        return f"{who} skipped to {sync.clock_text(state.position)}"
    return sync.describe(state, names, me_id, before)


# --- the queue on the wire ---------------------------------------------------------------

def _text(value: object, limit: int) -> str:
    return clean_name(value, limit) if isinstance(value, str) else ""


def _duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0 < value <= sync.MAX_POSITION else None


def _song_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 2 ** 53:
        return None
    return value


_COLOUR_RE = re.compile(r"#[0-9A-Fa-f]{6}")


def palette_text(value: object) -> str:
    """An album's colours (library.parse_palette's JSON: dark, mid, accent), checked
    and written again, or "": a host's word for them must not be able to crash
    anything that reads them."""
    if not isinstance(value, str) or len(value) > 200:
        return ""
    try:
        colours = json.loads(value)
    except ValueError:
        return ""
    if not isinstance(colours, dict):
        return ""
    kept = {key: colours.get(key) for key in ("dark", "mid", "accent")}
    if not all(isinstance(c, str) and _COLOUR_RE.fullmatch(c) for c in kept.values()):
        return ""
    return json.dumps(kept)


def clean_song(raw: object) -> dict | None:
    """A song as the wire carries it (a search result, or a queue item without
    its entry): what is safe to show and nothing else, or None."""
    if not isinstance(raw, dict):
        return None
    song_id = _song_id(raw.get("id"))
    if song_id is None:
        return None
    return {"id": song_id, "title": _text(raw.get("title"), 200) or "Untitled",
            "artist": _text(raw.get("artist"), 120), "album": _text(raw.get("album"), 160),
            "duration": _duration(raw.get("duration"))}


def clean_item(raw: object) -> dict | None:
    """A queue item from the host: a song, its entry number and who put it there."""
    song = clean_song(raw)
    if song is None or not isinstance(raw, dict):
        return None
    entry = _song_id(raw.get("e"))
    if entry is None:
        return None
    by = raw.get("by")
    song["e"] = entry
    song["by"] = by if _people.is_person_id(by) else ""
    song["palette"] = palette_text(raw.get("palette"))
    return song


EMPTY_VIEW = {"rev": 0, "start": 0, "items": [], "index": -1, "total": 0, "dj": "", "host": "",
              "votes": [], "needed": 1}


def parse_queue(message: dict) -> dict:
    """A queue message from the host, checked. BadMessage if the frame of it is wrong;
    items that make no sense are left out."""
    items = message.get("items")
    if not isinstance(items, list) or len(items) > WINDOW_BEFORE + WINDOW_AFTER + 1:
        raise sync.BadMessage("items is not a list of songs")
    votes = message.get("votes", [])
    if not isinstance(votes, list) or len(votes) > sync.MAX_PEOPLE_ON_WIRE:
        raise sync.BadMessage("votes is not a list")
    dj, host = message.get("dj", ""), message.get("host", "")
    return {
        "rev": sync.whole(message.get("rev"), 0, 2 ** 53),
        "start": sync.whole(message.get("start"), 0, MAX_QUEUE),
        "items": [item for item in (clean_item(raw) for raw in items) if item is not None],
        "index": sync.whole(message.get("index"), -1, MAX_QUEUE),
        "total": sync.whole(message.get("total"), 0, MAX_QUEUE),
        "dj": dj if _people.is_person_id(dj) else "",
        "host": host if _people.is_person_id(host) else "",
        "votes": [pid for pid in votes if _people.is_person_id(pid)],
        "needed": sync.whole(message.get("needed", 1), 1, sync.MAX_PEOPLE_ON_WIRE),
    }


def parse_note(message: dict) -> dict:
    what = message.get("what")
    if what not in NOTES:
        raise sync.BadMessage("not a note this Mistery knows")
    by, dj = message.get("by", ""), message.get("dj", "")
    count, needed = message.get("count"), message.get("needed")
    return {"what": what, "by": by if _people.is_person_id(by) else "",
            "dj": dj if _people.is_person_id(dj) else "",
            "title": _text(message.get("title"), 200),
            "count": count if isinstance(count, int) and not isinstance(count, bool) and 0 <= count < 1000 else 0,
            "needed": needed if isinstance(needed, int) and not isinstance(needed, bool)
            and 0 <= needed < 1000 else 0}


def votes_needed(people: int) -> int:
    """Half the room, rounded up: one of two, two of three or four."""
    return max(1, math.ceil(people / 2))


# --- the host's side ------------------------------------------------------------------------

class ListenHub(sync.Hub):
    """The room of a listening party, on the host's side.

    `library` is the host's music, as three calls the hub makes on its own
    thread: song(id) → {"id", "title", "artist", "album", "duration"} or None
    for a song not playable here; search(text) → such songs; offer(id) → a
    token that fetches that song's file from the listener, or None.

    The host takes part through the same calls a guest's ListenClient offers:
    act(action, **fields), search(text), fetch_token(id), and reads
    `queue_view`, `state` and `people`. `dj` is who plays and skips; a person
    id given as `dj` takes it over when they arrive (until then the host has it).
    """

    def __init__(self, me=None, *, library, queue: list, index: int = 0, position: float = 0.0,
                 playing: bool = False, dj: str | None = None, party_id: str | None = None,
                 clock: Callable[[], float] = sync.now,
                 on_event: Callable[[sync.Event], None] | None = None,
                 title: str = "") -> None:
        me = me if me is not None else _people.me()
        party_id = party_id if isinstance(party_id, str) and sync._PARTY_ID_RE.match(party_id) \
            else uuid.uuid4().hex
        self.library = library
        self.title = title
        self._entries: list[dict] = []
        self._next_e = itertools.count(1)
        for song in list(queue)[:MAX_QUEUE]:
            entry = self._entry(song, me.id)
            if entry is not None:
                self._entries.append(entry)
        self._index = min(max(0, int(index)), len(self._entries) - 1) if self._entries else -1
        self.dj = me.id
        self._dj_wanted = dj if _people.is_person_id(dj) and dj != me.id else None
        self.votes: set[str] = set()
        self._rev = 0
        self._adds: dict[str, collections.deque] = {}
        self._queue_ended = False
        self.queue_view = dict(EMPTY_VIEW)
        self._search_n = itertools.count(1)
        self._party_prefix = party_id[:12]
        media = self._media_of(self._index)
        if media is not None and media.get("duration"):
            position = min(max(0.0, float(position)), max(0.0, media["duration"] - 0.5))
        super().__init__(me, party_id=party_id, media=media, position=position, playing=playing,
                         clock=clock, on_event=on_event)
        self._handlers.update({"fetch": self._on_fetch, "ask": self._on_ask})
        self._loop.call(self._publish_queue)

    # --- any thread ---------------------------------------------------------------------

    def act(self, action: str, **fields) -> None:
        """The host's own press of a button, applied like a guest's intent."""
        if action not in ACTIONS:
            raise ValueError(f"not a listening party action: {action!r}")
        self._loop.call(lambda: self._act(self._host, action, dict(fields)))

    def search(self, text: str) -> int:
        """Look for songs to add; the answer comes as a "found" event with this number."""
        n = next(self._search_n)
        self._loop.call(lambda: self._events.emit(sync.Event("found", "", {
            "n": n, "items": self._found(str(text or ""))})))
        return n

    def fetch_token(self, song_id: int, timeout: float = FETCH_TIMEOUT, art: bool = False) -> str | None:
        """The host plays its own files: nothing to fetch. Here for the same calls as a guest."""
        return None

    # --- the queue ----------------------------------------------------------------------

    def _entry(self, song, by: str) -> dict | None:
        try:
            row = dict(song)
        except (TypeError, ValueError):
            return None
        song_id = _song_id(row.get("id"))
        if song_id is None:
            return None
        return {"e": next(self._next_e), "id": song_id,
                "title": _text(row.get("title"), 200) or "Untitled",
                "artist": _text(row.get("artist") or row.get("album_artist"), 120),
                "album": _text(row.get("album_title") or row.get("album"), 160),
                "duration": _duration(row.get("duration")), "by": by,
                "palette": palette_text(row.get("palette"))}

    def _media_of(self, index: int) -> dict | None:
        if not 0 <= index < len(self._entries):
            return None
        entry = self._entries[index]
        return {"key": f"{self._party_prefix}:{entry['e']}", "e": entry["e"], "id": entry["id"],
                "title": entry["title"], "artist": entry["artist"], "album": entry["album"],
                "duration": entry["duration"], "kind": "track"}

    def _view(self) -> dict:
        start = max(0, self._index - WINDOW_BEFORE)
        items = [dict(entry) for entry in self._entries[start:max(0, self._index) + WINDOW_AFTER + 1]]
        return {"rev": self._rev, "start": start, "items": items, "index": self._index,
                "total": len(self._entries), "dj": self.dj, "host": self.me.id,
                "votes": sorted(self.votes), "needed": votes_needed(len(self.people))}

    def _publish_queue(self, only=None) -> None:
        """Tell everyone the queue as it is now (or one guest, `only`, who just arrived)."""
        if only is None:
            self._rev += 1
        view = self._view()
        message = {"type": "queue", **view}
        if only is not None:
            self._send(only, message)
            return
        self.queue_view = view
        self._broadcast(message)
        self._events.emit(sync.Event("queue", "", view))

    def _note(self, what: str, by: str, title: str = "", count: int = 0, needed: int = 0) -> None:
        note = {"what": what, "by": by, "title": title, "count": count, "needed": needed}
        self._broadcast({"type": "note", **note})
        self._events.emit(sync.Event("note", note_text(note, self._names(), self.me.id), note))

    def _may_control(self, seat) -> bool:
        return seat is self._host or (seat.person is not None and seat.person.id == self.dj)

    # --- intents ------------------------------------------------------------------------

    def _on_intent(self, seat, message: dict, t: float) -> None:
        action = message.get("action")
        if action not in ACTIONS:
            raise sync.BadMessage("not an action of a listening party")
        self._act(seat, action, message)

    def _act(self, seat, action: str, fields: dict) -> None:
        if seat.phase != "in" or seat.person is None:
            return
        by = seat.person.id
        if action in DJ_ACTIONS and not self._may_control(seat):
            if seat is not self._host:
                self._send(seat, {"type": "note", "what": "not_dj", "by": by, "dj": self.dj,
                                  "title": "", "count": 0, "needed": 0})
            return
        if action in sync.INTENTS:
            position = None
            if action == "seek":
                position = sync.number(fields.get("position"), 0.0, sync.MAX_POSITION)
            if action == "play" and self._queue_ended:
                self._play_after_end(by)
                return
            self._apply_intent(seat, action, position)
        elif action == "next":
            self._skip(by)
        elif action == "previous":
            if self.state.position_at(self._clock()) > RESTART_AFTER or self._index <= 0:
                self._apply_intent(seat, "seek", 0.0)
            else:
                self._put_on(self._index - 1, by)
        elif action == "jump":
            index = sync.whole(fields.get("index"), 0, MAX_QUEUE)
            if index < len(self._entries):
                self._put_on(index, by)
        elif action == "add":
            self._add(seat, fields)
        elif action == "remove":
            self._remove(seat, fields)
        elif action == "vote":
            self._vote(seat)
        elif action == "replace":
            self._replace(by, fields)

    def _put_on(self, index: int, by: str, cause: str = "media") -> None:
        """Everyone on to another song of the queue, from its start, together."""
        t = self._clock()
        self._index = index
        self.votes.clear()
        self._queue_ended = False
        state = self.state
        playing = state.playing or state.waiting
        base = replace(state, media=self._media_of(index), playing=False, position=0.0, at=t,
                       waiting_for=())
        if playing:
            new = base.play(t, self._change_lead(), by, cause=cause)
        else:
            new = replace(base, seq=state.seq + 1, by=by, cause=cause)
        for seat in self._seats:
            # A new file for everyone: whoever was slow on the last one gets a fresh start.
            seat.drift = None
            seat.arriving = seat.pressed_through = seat.slow = seat.stall_counted = False
            seat.steady = 0.0
            seat.stalls.clear()
        self._set_state(new)
        self._publish_queue()

    def _change_lead(self) -> float:
        slowest = max(((s.rtt or 0.0) for s in self._seats if s.phase == "in"), default=0.0)
        return min(sync.LEAD_MAX, CHANGE_LEAD + slowest)

    def _skip(self, by: str, voted: int = 0) -> None:
        current = self._entries[self._index] if 0 <= self._index < len(self._entries) else None
        title = current["title"] if current else ""
        if self._index + 1 < len(self._entries):
            self._put_on(self._index + 1, by)
        else:
            # The last song: the queue has ended, until somebody adds another.
            t = self._clock()
            duration = self.state.duration or self.state.position_at(t)
            self.votes.clear()
            self._queue_ended = True
            self._set_state(replace(self.state.pause(t, by, cause="end"), position=duration))
            self._publish_queue()
        self._note("skipped", by, title, count=voted)

    def _play_after_end(self, by: str) -> None:
        """Play once the queue has run out: what was added since, or this song again."""
        if self._index + 1 < len(self._entries):
            self._put_on(self._index + 1, by)          # still paused, as the end left it
            t = self._clock()
            self._set_state(self.state.play(t, self._change_lead(), by))
            return
        t = self._clock()
        self._queue_ended = False
        self._set_state(replace(self.state, position=0.0, at=t).play(t, self._change_lead(), by))

    def _add(self, seat, fields: dict) -> None:
        song_id = _song_id(fields.get("id"))
        if song_id is None or len(self._entries) >= MAX_QUEUE:
            return
        by = seat.person.id
        if seat is not self._host:
            recent = self._adds.setdefault(by, collections.deque(maxlen=ADDS_PER_MINUTE))
            t = self._clock()
            if len(recent) == ADDS_PER_MINUTE and t - recent[0] < 60.0:
                return
            recent.append(t)
        song = self.library.song(song_id)
        entry = self._entry(song, by) if song else None
        if entry is None:
            return
        after_end = self._queue_ended
        up_next = fields.get("next") is True and self._index >= 0
        where = self._index + 1 if up_next else len(self._entries)
        self._entries.insert(where, entry)
        if self._index < 0:
            self._index = 0
            self._put_on(0, by)
        elif after_end and where == self._index + 1:
            self._put_on(where, by)
            t = self._clock()
            self._set_state(self.state.play(t, self._change_lead(), by))
        else:
            self._publish_queue()
        self._note("added_next" if up_next else "added", by, entry["title"])

    def _remove(self, seat, fields: dict) -> None:
        wanted = _song_id(fields.get("e"))
        for index, entry in enumerate(self._entries):
            if entry["e"] != wanted:
                continue
            if index <= self._index:
                return                      # playing, or played: not the queue's to change
            if not (self._may_control(seat) or entry["by"] == seat.person.id):
                return
            del self._entries[index]
            self._publish_queue()
            self._note("removed", seat.person.id, entry["title"])
            return

    def _vote(self, seat) -> None:
        pid = seat.person.id
        if self._may_control(seat):
            self._skip(pid)
            return
        if self._index < 0 or self._queue_ended:
            return
        if pid in self.votes:
            self.votes.discard(pid)
            self._publish_queue()
            return
        self.votes.add(pid)
        needed = votes_needed(len(self.people))
        if len(self.votes) >= needed:
            self._skip(pid, voted=len(self.votes))
        else:
            self._publish_queue()
            self._note("vote", pid, count=len(self.votes), needed=needed)

    def _replace(self, by: str, fields: dict) -> None:
        ids = fields.get("ids")
        if not isinstance(ids, list):
            return
        entries = []
        for raw in ids[:MAX_QUEUE]:
            song_id = _song_id(raw)
            song = self.library.song(song_id) if song_id is not None else None
            entry = self._entry(song, by) if song else None
            if entry is not None:
                entries.append(entry)
        if not entries:
            return
        index = fields.get("index", 0)
        index = index if isinstance(index, int) and not isinstance(index, bool) \
            and 0 <= index < len(entries) else 0
        self._entries = entries
        was_playing = self.state.playing or self.state.waiting
        self._put_on(index, by)
        if not was_playing and fields.get("play", True) is True:
            t = self._clock()
            self._set_state(self.state.play(t, self._change_lead(), by))
        self._note("put_on", by, _text(fields.get("title"), 200) or entries[index]["title"])

    # --- fetching and searching ---------------------------------------------------------

    def _on_fetch(self, seat, message: dict, t: float) -> None:
        n = sync.whole(message.get("n"), 0, 2 ** 53)
        song_id = _song_id(message.get("id"))
        art = message.get("art") is True
        token = None
        if song_id is not None and any(entry["id"] == song_id for entry in self._entries):
            try:
                token = self.library.art(song_id) if art else self.library.offer(song_id)
            except Exception:               # noqa: BLE001 - a file gone is a song skipped, not a party over
                _log.exception("listening party: offering a song failed")
        reply = {"type": "song", "n": n, "id": song_id or 0, "art": art}
        if token:
            reply["token"] = str(token)
        self._send(seat, reply)

    def _on_ask(self, seat, message: dict, t: float) -> None:
        n = sync.whole(message.get("n"), 0, 2 ** 53)
        self._send(seat, {"type": "found", "n": n, "items": self._found(_text(message.get("q"), 80))})

    def _found(self, text: str) -> list[dict]:
        if not text.strip():
            return []
        try:
            rows = self.library.search(text)
        except Exception:                   # noqa: BLE001
            _log.exception("listening party: search failed")
            return []
        found = []
        for row in rows:
            song = clean_song(row)
            if song is not None:
                found.append(song)
            if len(found) >= MAX_FOUND:
                break
        return found

    # --- the room's own doings ------------------------------------------------------------

    def _set_state(self, new: sync.RoomState) -> None:
        before, self.state = self.state, new
        self._broadcast({"type": "state", "state": new.to_wire()})
        self._events.emit(sync.Event("state", describe(new, self._names(), self.me.id, before), new))

    def _hello(self, seat, message: dict, t: float) -> None:
        super()._hello(seat, message, t)
        if seat.phase != "in" or seat.person is None:
            return
        if self._dj_wanted and seat.person.id == self._dj_wanted:
            self._dj_wanted = None
            self.dj = seat.person.id
            self._publish_queue()
            self._note("dj", seat.person.id)
        else:
            self._publish_queue(only=seat)
            if len(self.people) > 1:
                self._publish_queue()       # half the room has changed

    def _gone(self, seat, how: str) -> None:
        pid = seat.person.id if seat.person is not None else ""
        was_in = seat.phase == "in"
        super()._gone(seat, how)
        if not was_in or not pid:
            return
        self.votes.discard(pid)
        if pid == self.dj:
            self.dj = self.me.id
            self._note("dj", self.me.id)
        if self.votes and len(self.votes) >= votes_needed(len(self.people)):
            self._skip(sorted(self.votes)[0], voted=len(self.votes))
        else:
            self._publish_queue()

    def _stuck(self, t: float, since: float | None = None, already: tuple[str, ...] = ()) -> list:
        """Nobody: a listening party does not stop for anyone's connection.

        A film waits for a friend whose stream has run dry, because the scene
        they would miss is the evening. A song is not that. With a guest across
        the internet the host's music stood still for everybody each time that
        guest's player opened a song or sought, 4-7 s at a time through a relay
        40-120 ms away (features7/listen/probe_slow_link.py), which is the worse
        interruption; and a guest who is behind comes in where the party is as
        soon as their player has it (listen_session._MusicFollower). A guest's
        own pause was already theirs alone. So is their buffering.
        """
        return []

    def _count_stalls(self, t: float, since: float) -> None:
        """Nor is anybody found too slow for the party and gone on without, with
        a notice to everyone: nobody was being waited for."""

    def _tick(self, t: float) -> None:
        state = self.state
        duration = state.duration
        if (self._ending is None and state.moving_at(t) and duration is not None
                and state.position_at(t) >= duration):
            if self._index + 1 < len(self._entries):
                # On to the next song, dated to the moment this one ended: every
                # player moved on to it by itself, without a gap, at that moment.
                ended_at = state.at + (duration - state.position) / (state.rate or 1.0)
                self._index += 1
                self.votes.clear()
                self._set_state(sync.RoomState(
                    media=self._media_of(self._index), playing=True, position=0.0,
                    at=min(ended_at, t), rate=state.rate, seq=state.seq + 1, by="", cause="next"))
                self._publish_queue()
            else:
                self._queue_ended = True
                self.votes.clear()
                self._set_state(replace(state.pause(t, "", cause="end"), position=duration))
                self._publish_queue()
        super()._tick(t)


# --- a guest's side ----------------------------------------------------------------------------

class ListenClient(sync.Client):
    """A listening party from a guest's side: the room's Client, plus the queue.

    act(action, **fields) asks the host (a DJ's play or skip, anyone's add or
    vote); search(text) asks what the host's music has (the answer is a "found"
    event); fetch_token(id) gets a song's token for this guest's proxy, and
    blocks (never call it on Qt's thread). `queue_view` is the queue as the
    host last said it.
    """

    def __init__(self, me=None, *, clock: Callable[[], float] = sync.now,
                 on_event: Callable[[sync.Event], None] | None = None, connect=None) -> None:
        super().__init__(me, clock=clock, on_event=on_event, connect=connect)
        self._handlers.update({"queue": self._on_queue, "note": self._on_note,
                               "song": self._on_song, "found": self._on_found})
        self.queue_view = dict(EMPTY_VIEW)
        self._numbers = itertools.count(1)
        self._fetches: dict[int, list] = {}         # n -> [threading.Event, token or None]
        self._fetch_lock = threading.Lock()

    def act(self, action: str, **fields) -> None:
        if action not in ACTIONS:
            raise ValueError(f"not a listening party action: {action!r}")
        message = {"type": "intent", "action": action, **fields}
        self._loop.call(lambda: self._send_if_in(message))

    def search(self, text: str) -> int:
        n = next(self._numbers)
        message = {"type": "ask", "n": n, "q": str(text or "")[:80]}
        self._loop.call(lambda: self._send_if_in(message))
        return n

    def fetch_token(self, song_id: int, timeout: float = FETCH_TIMEOUT, art: bool = False) -> str | None:
        """A token that streams this song of the queue from the host (or, with
        `art`, its album's cover), or None."""
        if self.ended.is_set():
            return None
        n = next(self._numbers)
        waiting = [threading.Event(), None]
        with self._fetch_lock:
            self._fetches[n] = waiting
        message = {"type": "fetch", "n": n, "id": int(song_id)}
        if art:
            message["art"] = True
        self._loop.call(lambda: self._send_if_in(message))
        waiting[0].wait(timeout)
        with self._fetch_lock:
            self._fetches.pop(n, None)
        return waiting[1]

    def _send_if_in(self, message: dict) -> None:
        if self._phase == "in":
            self._send(message)

    def _on_state(self, message: dict) -> None:
        state = sync.RoomState.from_wire(message.get("state"))
        if state.seq <= self.state.seq:
            return
        before, self.state = self.state, state
        names = self.names()
        self._events.emit(sync.Event("state", describe(state, names, self.me.id, before), state))

    def names(self) -> dict[str, str]:
        present = {p["id"] for p in self.people}
        return {p["id"]: p["name"] for p in self.people} | \
            {pid: n for pid, n in self._came.items() if pid not in present}

    def _on_queue(self, message: dict) -> None:
        view = parse_queue(message)
        if view["rev"] < self.queue_view.get("rev", 0):
            return
        self.queue_view = view
        self._events.emit(sync.Event("queue", "", view))

    def _on_note(self, message: dict) -> None:
        note = parse_note(message)
        self._events.emit(sync.Event("note", note_text(note, self.names(), self.me.id), note))

    def _on_song(self, message: dict) -> None:
        n = sync.whole(message.get("n"), 0, 2 ** 53)
        token = message.get("token")
        with self._fetch_lock:
            waiting = self._fetches.get(n)
        if waiting is None:
            return
        if isinstance(token, str) and re.fullmatch(r"[0-9a-f]{16,128}", token):
            waiting[1] = token
        waiting[0].set()

    def _on_found(self, message: dict) -> None:
        n = sync.whole(message.get("n"), 0, 2 ** 53)
        items = message.get("items")
        if not isinstance(items, list):
            raise sync.BadMessage("items is not a list")
        found = [song for song in (clean_song(raw) for raw in items[:MAX_FOUND]) if song is not None]
        self._events.emit(sync.Event("found", "", {"n": n, "items": found}))

    def _finish(self, reason: str) -> None:
        super()._finish(reason)
        with self._fetch_lock:
            waiting = list(self._fetches.values())
        for each in waiting:
            each[0].set()                   # nobody waits on a party that is over


# --- a guest's songs ------------------------------------------------------------------------------

_SONG_RE = re.compile(r"/t/(?P<id>[1-9][0-9]{0,15})")


class PartyTunnel(GuestProxy):
    """Where a guest's player plays the party's songs from:

        http://127.0.0.1:<port>/t/<the host's song id>

    and a token is asked of the room when the player comes for the song (to
    play it, or to open it early for a gapless change), kept, and asked for
    again if the host has since let it go (a bare 404). Everything else is
    GuestProxy's: the host's certificate checked, Range passed through,
    a transfer cut part way resumed where it stopped.
    """

    def __init__(self, invite, room, address: str | None = None, host_name: str | None = None,
                 connect=None) -> None:
        super().__init__(invite, address=address, host_name=host_name, connect=connect)
        self._room = room
        self._talk = threading.Lock()
        self._tokens: collections.OrderedDict[int, str] = collections.OrderedDict()

    def song_url(self, song_id: int) -> str:
        return f"http://127.0.0.1:{self.port}/t/{int(song_id)}"

    def _serve(self, sock: socket.socket) -> None:
        self._track(sock, True)
        try:
            head, _ = _read_head(sock, time.monotonic() + self.head_timeout, self.max_head)
            lines = head.split(b"\r\n")
            parts = lines[0].split(b" ")
            fields = _parse_fields(lines[1:])
            host_ok = fields.get("host") in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")
            match = None
            if len(parts) == 3 and parts[0] in (b"GET", b"HEAD") and host_ok:
                try:
                    match = _SONG_RE.fullmatch(parts[1].decode("ascii"))
                except UnicodeDecodeError:
                    match = None
            wanted_range = fields.get("range")
            if match is None or (wanted_range is not None and not _PRINTABLE_RE.fullmatch(wanted_range)):
                sock.sendall(_NOT_FOUND)
                return
            self._relay(sock, parts[0].decode("ascii"), f"/t/{int(match.group('id'))}", wanted_range)
        except (OSError, ssl.SSLError, ValueError) as problem:
            _log.debug("a song request from the player ended: %s", problem)
        finally:
            self._track(sock, False)
            try:
                sock.close()
            except OSError:
                pass
            with self._lock:
                self._active -= 1

    def _open(self, method: str, route: str, wanted_range: str | None):
        found = _SONG_RE.fullmatch(route)
        if found is None:
            return super()._open(method, route, wanted_range)
        song_id = int(found.group("id"))
        for fresh in (False, True):
            token = self._token_for(song_id, fresh=fresh)
            if token is None:
                self.last_error = self.last_error or "The host's Mistery wouldn't send that song."
                return None
            upstream = super()._open(method, f"/m/{token}/media", wanted_range)
            if upstream is None or upstream.status != 404 or fresh:
                return upstream
            self._track(upstream.sock, False)
            upstream.close()
        return None

    def _token_for(self, song_id: int, *, fresh: bool = False) -> str | None:
        with self._talk:
            if not fresh and song_id in self._tokens:
                self._tokens.move_to_end(song_id)
                return self._tokens[song_id]
        token = self._room.fetch_token(song_id)
        if token is None:
            return None
        with self._talk:
            self._tokens[song_id] = token
            self._tokens.move_to_end(song_id)
            while len(self._tokens) > KEEP_TOKENS:
                self._tokens.popitem(last=False)
        return token


# --- following the room ---------------------------------------------------------------------------

class MusicDrift(sync.DriftController):
    """The film's DriftController, tuned for music.

    A film nudges its speed by 5 %: nobody hears a voice 5 % faster, and it
    closes a second in 20 s. A song 5 % faster is a different tempo, audibly;
    3 % is at the edge of noticing on a song you know, and it closes the 0.75 s
    past which it seeks instead in 25 s. A seek in music is a click in the
    sound, not a frozen picture, so it comes sooner than for a film (0.75 s
    against 1.5 s). There are no frames: time-pos is the sound's own clock.

    The dead band is narrower than a film's 80 ms: there is no frame to be
    half of, and each player may sit anywhere in it, so two players can be
    twice it apart. At 60 ms a guest who had just caught up played 83 ms
    ahead of a host sitting at the other edge (features7/listen); at 40 ms a
    nudge that closes it takes a second and a half at 3 %, which nobody hears.
    """

    DEAD_BAND = 0.04
    SETTLE = 0.01
    AFTER_SEEK_BAND = 0.025
    NUDGE = 0.03
    SEEK_BEYOND = 0.75
