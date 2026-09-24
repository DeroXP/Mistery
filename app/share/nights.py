"""A friend's movie night on this PC's film: this PC holds it, nobody here watches.

A friend browsing this library presses "Watch together" on one of its films or
episodes. Their Mistery asks this PC over the friend channel (`night`,
app/share/server.py), and this PC holds a movie night on the listener sharing
already has open: the room (sync.Hub, with nobody of this PC's in it), and the
film served from this disk straight to each friend, in the quality each picks,
exactly as in any movie night. The friend who asked joins at once and passes
the code on. It is a KIND_FRIENDS code, and the listener lets in only this PC's
own friends, room and film alike (PartyServer.host_party's `admit`, asked about
the certificate each connection shows), so a code passed any further opens
nothing. The owner decided that: a friend's film, watched together, but only
with people the owner has added too.

One at a time, on the one listener. A friend asking for the same film while its
movie night is on gets the same code (they are watching together); anything
else is told the PC is busy, as it is while the owner holds a movie night of
their own on the same listener.

It ends EMPTY_GRACE after the last friend leaves (long enough for one whose
Wi-Fi dropped to get back in), or FIRST_GRACE after it began if nobody came.
While it is on, the PC stays awake (sharer._keep), the updater waits for it
(night.lock, updater/liveness.py), and sharing does not let go
of the listener (Sharer.stop waits for a movie night to end).

Nothing here needs Qt: the Mistery with no window holds these too.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from .. import db
from ..config import settings
from ..models import MediaItem
from ..party import invite as invites
from ..party import people as _people
from ..party.media import media_for
from ..party.server import ServerError
from ..party.sync import Hub
from . import identity as ident

_log = logging.getLogger("share")

EMPTY_GRACE = 120.0         # after the last friend left
FIRST_GRACE = 180.0         # when nobody came at all: the one who asked joins at once, or failed to
WATCH_EVERY = 2.0
# In the data folder while one is on, naming this process: the updater waits
# for it rather than replacing the files under a film friends are watching
# (updater/liveness.py _night_says_running), as it waits for an open Mistery.
LOCK_NAME = "night.lock"

# Sentences for the friend's screen.
BUSY = "Their PC is holding another movie night right now. Try again when it's over."
NOT_THERE = "That is not in their library any more."
ENDED = "The movie night has ended: everyone left."
TURNED_AWAY = ("This movie night is only for the friends that PC shares its library with, "
               "and it isn't sharing with you right now.")
NO_ADDRESS = "Their PC couldn't work out an address to put in the code. Try again in a moment."
# ...and for this PC's owner, starting a movie night of their own meanwhile.
OWNER_BUSY = ("Friends are in a movie night of {title} from your library, on this PC, and it "
              "holds the movie night port until it ends. Start yours once it's over.")
# ...and for a guest's, before anything connects (session.join).
NOT_THEIR_FRIEND = ("This movie night is on the PC of somebody you haven't added as a friend in "
                    "Mistery, and only their friends can join it. Ask them for their friend code "
                    "(Friends, in the top bar), then join again.")
# ...and when their PC closed the door in the handshake: it has no certificate
# of this install's to trust, which after pairing means it removed us.
NOT_LET_IN = ("{name}'s PC didn't let you in. Only its friends can join this movie night, and it "
              "doesn't have you as one any more.")


class NightError(Exception):
    """Why a movie night could not be held here: a sentence for the friend."""


class Night:
    """One friend's movie night on this PC's film."""

    def __init__(self, listener, hub: Hub, kind: str, media_id: int, title: str, code: str,
                 asked_by: int) -> None:
        self.listener = listener
        self.hub = hub
        self.kind = kind
        self.media_id = media_id
        self.title = title
        self.code = code
        self.asked_by = asked_by            # the friend who pressed Watch together
        self.started = time.monotonic()
        self.had_guests = False
        self.empty_since: float | None = None
        self._ended = threading.Event()

    @property
    def ended(self) -> bool:
        return self._ended.is_set()

    @property
    def watching(self) -> int:
        """How many friends are in the room now."""
        return self.hub.guest_count

    def end(self, reason: str = ENDED) -> None:
        """Tell everyone, let them go, and give the listener back to sharing."""
        if self._ended.is_set():
            return
        self._ended.set()
        try:
            self.hub.end(reason)
            self.hub.wait_closed(3.0)
        finally:
            self.listener.end_party()
            _forget(self)
            _clear_night_lock()
            _log.info("share: the movie night of %s on this PC is over", self.title)


_lock = threading.Lock()
_night: Night | None = None


def current() -> Night | None:
    """The friends' movie night on this PC's film, while it is on."""
    night = _night
    return night if night is not None and not night.ended else None


def _forget(night: Night) -> None:
    global _night
    with _lock:
        if _night is night:
            _night = None


def _night_lock_path():
    from ..config import data_dir

    return data_dir() / LOCK_NAME


def _write_night_lock() -> None:
    try:
        _night_lock_path().write_text(f"{os.getpid()},{time.time()}", encoding="utf-8")
    except OSError as problem:
        _log.warning("share: could not write %s: %s", LOCK_NAME, problem)


def _clear_night_lock() -> None:
    """Take night.lock away, if it is this process's (another's is another night)."""
    try:
        path = _night_lock_path()
        if path.is_file() and path.read_text(encoding="utf-8").split(",")[0] == str(os.getpid()):
            path.unlink()
    except (OSError, IndexError):
        pass


def admit(certificate: bytes | None) -> bool:
    """The door: a certificate pairing wrote down, of a friend this PC shares
    with right now (sharing on, and not paused for them)."""
    if not certificate:
        return False
    try:
        friend = db.friend_by_pin(ident.pin_of(certificate).hex())
        return (friend is not None and bool(friend["sharing"])
                and bool(settings.get("sharing_enabled")))
    finally:
        db.close_thread_connection()


def hold(listener, friend, kind: str, media_id: int, *, position: float = 0.0,
         party_id: str | None = None) -> Night:
    """A movie night of this library's film or episode `media_id`, for
    `friend`, who asked: the one already on for it, or a new one.

    `position` and `party_id` start a new one where an earlier one got to, as
    that party (Watch together again: their Home keeps one card for it). The
    one already on is joined as it is: somebody is watching it.

    NightError, with a sentence for them, when the PC is busy or the film is
    gone. The friend's rights (sharing on, not paused) are the caller's to
    have checked, as for anything else they ask (server._answer), and so are
    their `position` (a number) and `party_id` (a party id's shape)."""
    global _night
    with _lock:
        night = _night
        if night is not None and not night.ended:
            if (night.kind, night.media_id) == (kind, media_id):
                return night
            raise NightError(BUSY)
        row = db.get_media(media_id)
        if row is None or row["kind"] != kind or row["missing"]:
            raise NightError(NOT_THERE)
        item = MediaItem.from_row(row)
        token = invites.new_token()
        try:
            listener.host_party(token, admit=admit, refusal=TURNED_AWAY)
        except ServerError:
            raise NightError(BUSY) from None      # the owner's own movie night
        try:
            from ..party import transcode

            me = _people.me()
            default = listener.set_media(item.path, item.duration or None, "auto")
            media = media_for(item, me, default, transcode.pick_encoder() is not None)
            # Inside the film, as the owner's own Continue keeps it (session.start_host).
            start = max(0.0, float(position or 0.0))
            if item.duration:
                start = min(start, max(0.0, float(item.duration) - 1.0))
            hub = Hub(me, party_id=party_id, media=media, position=start, playing=False,
                      present=False)
            listener.sync_handler = hub.accept
            try:
                code = invites.encode(invites.Invite(
                    getattr(listener, "wan_ip", None), getattr(listener, "lan_ip", None),
                    int(listener.port), token, ident.identity().pin[:invites.PIN_BYTES],
                    kind=invites.KIND_FRIENDS))
            except invites.InviteError:
                raise NightError(NO_ADDRESS) from None      # a PC on no network at all
        except BaseException:
            listener.end_party()
            raise
        night = Night(listener, hub, kind, media_id, media.get("title") or item.title, code,
                      int(friend["id"]))
        _night = night
        _write_night_lock()
    threading.Thread(target=_watch, args=(night,), name="share-night", daemon=True).start()
    _log.info("share: %s asked for a movie night of %s on this PC", friend["name"], night.title)
    return night


# --- a guest's side: joining one on a friend's PC ------------------------------

def host_friend(pin: bytes):
    """The friend whose PC a KIND_FRIENDS code's pin is (the leading bytes of
    their certificate's fingerprint), or None: only their friends can join."""
    pin = bytes(pin)
    if len(pin) < invites.PIN_BYTES:
        return None
    for row in db.friends():
        try:
            theirs = bytes.fromhex(row["pin"] or "")
        except ValueError:
            continue
        if theirs[:len(pin)] == pin:
            return row
    return None


def guest_invite(parsed, friend):
    """The code, with the friend's addresses this PC knows filled in where it
    has none: their PC may not know its internet address (the Mistery with no
    window never asks), while this one learned it the last time it reached them."""
    from dataclasses import replace

    return replace(parsed, lan_ip=parsed.lan_ip or friend["lan_ip"],
                   wan_ip=parsed.wan_ip or friend["wan_ip"])


def guest_connect():
    """How a guest reaches a friend's PC for its movie night: with this install's
    certificate shown, which is what lets it in (admit)."""
    return ident.identity().connect


def end_now(reason: str = "The movie night on their PC was ended.") -> bool:
    """End the friends' movie night on this PC, if one is on (the owner quitting
    Mistery, or pressing End on the Friends page). True when there was one."""
    night = current()
    if night is None:
        return False
    night.end(reason)
    return True


def _watch(night: Night) -> None:
    """End it once nobody is watching: EMPTY_GRACE after the last friend left,
    FIRST_GRACE after it began if nobody came."""
    while not night.ended:
        time.sleep(WATCH_EVERY)
        if night.ended:
            break
        if night.hub.closed.is_set():
            night.end()
            break
        now = time.monotonic()
        if night.watching:
            night.had_guests = True
            night.empty_since = None
            continue
        if night.empty_since is None:
            night.empty_since = now
        since = night.empty_since if night.had_guests else night.started
        if now - since >= (EMPTY_GRACE if night.had_guests else FIRST_GRACE):
            night.end()
