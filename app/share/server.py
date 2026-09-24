"""What a friend may ask this PC for, and what it will never answer.

This is the other end of app/share/client.py, and it runs on the listener a
movie night already uses (app/party/server.py routes `MISTERY-SHARE/1` here).
By the time a line reaches this module, TLS has already settled who is asking:
the certificate on the connection was matched against one written down when you
paired. Anyone else never gets this far.

What a friend can ask for:

    hello                     who this is, and whether music is shared
    catalog {marks}           the library, or "nothing has changed"
    art {kind, id, which}     one picture, as bytes after the answer line
    play {kind, id, quality}  a token to fetch that film or song with, over
                              the same HTTP routes a movie night guest uses
    night {kind, id}          a movie night of that film or episode on this PC,
                              for this PC's friends only: its code (nights.py);
          {at, party}         one again: from there, as the same party
    stop {token}              done with it
    bye                       goodbye

What it will not answer, whoever asks:

  - anything at all without a certificate it knows (the listener sees to that);
  - a path. A friend names a kind and a number, which is looked up in this
    library; nothing they send is ever turned into a file name;
  - anything while sharing is off, or while that friend is paused, which are
    two different sentences on their screen;
  - music, when music is not shared;
  - more than `sharing_max_streams` things at once: asking for another stops
    the oldest rather than adding to it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from pathlib import Path

from .. import db
from ..config import settings
from ..party.server import PAIR_GREETING, SHARE_GREETING
from ..party import people
from . import catalog, identity as ident, pairing

_log = logging.getLogger("share")

PROTOCOL = 1
MAX_LINE = 16 * 1024
MAX_ART = 12 * 1024 * 1024          # a 4K backdrop is under 2 MB; this is a ceiling, not a target
IDLE_TIMEOUT = 15 * 60.0            # a friend browsing leaves the channel open between clicks
MAX_REQUESTS = 5000
_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp"}
_PARTY_ID = re.compile(r"[0-9a-f]{32}")

# Sentences for the friend's screen.
OFF = "They have library sharing switched off at the moment."
PAUSED = "They have paused sharing with you for now."
NOT_THERE = "That is not in their library any more."
NO_MUSIC = "They are not sharing music."


class Refused(Exception):
    """A request that goes no further, with a sentence for the other end."""


def make_handler(listener):
    """The callback app/party/server.py hands a friend's connection to.

    It holds the listener because playing something means making an offer on
    it: a token, on the port already open, that fetches one file and nothing
    else.
    """

    def handle(sock, peer, line: bytes, certificate: bytes | None) -> None:
        try:
            if line.startswith(PAIR_GREETING):
                _pair(sock, peer, line, listener)
            elif line.startswith(SHARE_GREETING):
                _share(sock, peer, certificate, listener)
        except (OSError, Refused, pairing.PairError) as problem:
            _log.debug("share: connection from %s ended: %s", _address(peer), problem)
        except Exception:                       # noqa: BLE001 - one connection, not the app
            _log.exception("share: connection from %s failed", _address(peer))
        finally:
            try:
                sock.close()
            except OSError:
                pass

    return handle


# --- pairing ------------------------------------------------------------------

def _pair(sock, peer, line: bytes, listener) -> None:
    """Somebody with a code. They have no certificate here yet; the code is
    what they have, and pairing.accept decides whether it is the right one."""
    _, _, secret = line.partition(b" ")
    try:
        offered = bytes.fromhex(secret.decode("ascii").strip())
    except (ValueError, UnicodeDecodeError):
        raise Refused("not a code") from None
    sock.settimeout(pairing.EXCHANGE_TIMEOUT)
    friend_id = pairing.accept(sock, peer, offered, port=listener.port or 0,
                               lan_ip=getattr(listener, "lan_ip", None),
                               wan_ip=getattr(listener, "wan_ip", None))
    # The listener's trust store has to learn their certificate, and a Friends
    # page that is open wants to say who just arrived (app/share/sharer.py).
    paired = getattr(listener, "on_paired", None)
    if paired is not None:
        paired(friend_id)


# --- a friend's channel --------------------------------------------------------

def _share(sock, peer, certificate: bytes | None, listener) -> None:
    friend = _whose(certificate)
    if friend is None:
        # The listener only lets a recognised certificate this far, so this is
        # a friend removed between the handshake and now.
        _send(sock, {"type": "refused", "request": "hello", "reason": "unknown"})
        return
    _log.info("share: %s opened a channel from %s", friend["name"], _address(peer))
    db.update_friend(friend["id"], last_seen=time.time())
    _remember_address(friend, peer)

    tokens: list[str] = []
    buffer = bytearray()
    deadline = time.monotonic() + IDLE_TIMEOUT
    try:
        for _ in range(MAX_REQUESTS):
            request = _read(sock, buffer, deadline)
            if request is None or request.get("type") == "bye":
                return
            deadline = time.monotonic() + IDLE_TIMEOUT
            try:
                _answer(sock, friend, request, listener, tokens)
            except Refused as refused:
                _send(sock, {"type": "refused", "request": str(request.get("type"))[:32],
                             "reason": str(refused)})
    finally:
        for token in tokens:
            listener.withdraw(token)


def _answer(sock, friend, request: dict, listener, tokens: list[str]) -> None:
    kind = request.get("type")
    if kind == "hello":
        _send(sock, {"type": "hello", "protocol": PROTOCOL,
                     "person_id": ident.identity().person_id, "name": people.display_name(),
                     "music": bool(settings.get("sharing_music", True)),
                     "sharing": _sharing_with(friend)})
        return
    if not _sharing_with(friend):
        raise Refused(OFF if not settings.get("sharing_enabled") else PAUSED)
    if kind == "catalog":
        answer = catalog.for_friend(request.get("marks"))
        _send(sock, {"type": "catalog", **answer})
    elif kind == "art":
        _send_art(sock, request)
    elif kind == "play":
        _send_play(sock, friend, request, listener, tokens)
    elif kind == "night":
        _send_night(sock, friend, request, listener)
    elif kind == "stop":
        token = request.get("token")
        if isinstance(token, str) and token in tokens:
            listener.withdraw(token)
            tokens.remove(token)
        _send(sock, {"type": "stop"})
    elif kind == "ping":
        _send(sock, {"type": "pong", "at": time.time()})
    else:
        raise Refused("Their Mistery does not know that request.")


def _send_art(sock, request: dict) -> None:
    """One picture: an answer line saying how many bytes, then the bytes."""
    path = _art_path(request)
    if not path:
        _send(sock, {"type": "art", "bytes": 0})
        return
    try:
        size = os.path.getsize(path)
        if size > MAX_ART:
            _send(sock, {"type": "art", "bytes": 0})
            return
        with open(path, "rb") as handle:
            blob = handle.read(MAX_ART)
    except OSError:
        _send(sock, {"type": "art", "bytes": 0})
        return
    _send(sock, {"type": "art", "bytes": len(blob),
                 "content_type": _IMAGE_TYPES.get(Path(path).suffix.lower(), "image/jpeg"),
                 "mark": catalog._art_mark(path)})
    sock.sendall(blob)


def _send_play(sock, friend, request: dict, listener, tokens: list[str]) -> None:
    kind, row = _item(request)
    path = row["path"]
    if not path or not os.path.isfile(path):
        raise Refused(NOT_THERE)
    quality = request.get("quality")
    if quality not in ("original", "1080p", "720p", None):
        raise Refused("That is not a quality this Mistery offers.")
    limit = max(1, int(settings.get("sharing_max_streams", 3) or 3))
    while len(tokens) >= limit:
        # Their own oldest goes, not somebody else's: one friend opening a
        # third film stops their first, and nobody else notices.
        listener.withdraw(tokens.pop(0))
    token = listener.offer(path, row["duration"], friend["id"], quality or "original")
    tokens.append(token)
    _send(sock, {"type": "play", "token": token, "port": listener.port,
                 "kind": kind, "id": row["id"], "duration": row["duration"],
                 "quality": quality or "original",
                 "subtitles": listener.subtitles(token) if kind != "track" else []})
    _log.info("share: %s is playing %s", friend["name"], os.path.basename(path))


def _send_night(sock, friend, request: dict, listener) -> None:
    """A movie night on this PC's film for this PC's friends (app/share/nights.py):
    its code, which the friend who asked joins with and may pass on. With `at`
    and `party` it is one they watched before, started again (Watch together
    again, on their Home): from where it got to, and as the same party, so it
    stays one movie night on everyone's Home."""
    from . import nights

    kind, row = _item(request)
    if kind not in ("movie", "episode") or not row["path"] or not os.path.isfile(row["path"]):
        raise Refused(NOT_THERE)
    try:
        night = nights.hold(listener, friend, kind, int(row["id"]), position=_start(request),
                            party_id=_party(request))
    except nights.NightError as problem:
        raise Refused(str(problem)) from None
    _send(sock, {"type": "night", "code": night.code, "title": night.title,
                 "watching": night.watching})


# --- what a request is allowed to mean ----------------------------------------

def _item(request: dict):
    """The library row a friend's (kind, id) means, or Refused. Never a path."""
    kind = request.get("kind")
    number = request.get("id")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise Refused(NOT_THERE)
    if kind in ("movie", "episode"):
        row = db.query_one("SELECT id, path, duration, kind FROM media WHERE id = ? AND kind = ? "
                           "AND missing = 0", (number, kind))
    elif kind == "track":
        if not settings.get("sharing_music", True):
            raise Refused(NO_MUSIC)
        row = db.query_one("SELECT id, path, duration FROM tracks WHERE id = ? AND state = 'ready' "
                           "AND missing = 0", (number,))
    else:
        raise Refused(NOT_THERE)
    if row is None:
        raise Refused(NOT_THERE)
    return kind, row


def _start(request: dict) -> float:
    """Where a movie night asked for again starts: a number of seconds, or the
    beginning. JSON lets a line say NaN, Infinity or a number with 400 digits,
    and none of those is a place in a film (nights.hold keeps it inside this one)."""
    at = request.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return 0.0
    try:
        seconds = float(at)
    except OverflowError:
        return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


def _party(request: dict) -> str | None:
    """The party a movie night asked for again continues: a party id's own shape
    (32 hex digits, sync.Hub's), or None for a new one."""
    party = request.get("party")
    return party if isinstance(party, str) and _PARTY_ID.fullmatch(party) else None


def _art_path(request: dict) -> str | None:
    """The picture a friend asked for, looked up the same way. None for none."""
    kind = request.get("kind")
    number = request.get("id")
    which = request.get("which") if request.get("which") in ("poster", "backdrop") else "poster"
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return None
    if kind in ("movie", "episode"):
        row = db.query_one(f"SELECT {which} AS art FROM media WHERE id = ? AND kind = ?",
                           (number, kind))
    elif kind == "show":
        row = db.query_one(f"SELECT {which} AS art FROM shows WHERE id = ?", (number,))
    elif kind == "album":
        if not settings.get("sharing_music", True):
            return None
        row = db.query_one("SELECT cover AS art FROM albums WHERE id = ?", (number,))
    else:
        return None
    return row["art"] if row and row["art"] else None


def _sharing_with(friend) -> bool:
    fresh = db.friend(friend["id"])
    return bool(settings.get("sharing_enabled")) and bool(fresh and fresh["sharing"])


def _whose(certificate: bytes | None):
    """Which friend a connection belongs to, by the certificate it showed."""
    if not certificate:
        return None
    return db.friend_by_pin(ident.pin_of(certificate).hex())


def _remember_address(friend, peer) -> None:
    """Where they answered from, so this PC can call them back later."""
    address = _address(peer)
    if not address:
        return
    column = "lan_ip" if _is_private(address) else "wan_ip"
    if friend[column] != address:
        db.update_friend(friend["id"], **{column: address})


def _is_private(address: str) -> bool:
    import ipaddress

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True
    return parsed.is_private or parsed.is_loopback


def _address(peer: object) -> str:
    return peer[0] if isinstance(peer, tuple) and peer else ""


# --- lines --------------------------------------------------------------------

def _send(sock, message: dict) -> None:
    sock.sendall(json.dumps(message, separators=(",", ":"), ensure_ascii=False,
                            allow_nan=False).encode("utf-8") + b"\n")


def _read(sock, buffer: bytearray, deadline: float) -> dict | None:
    """One request, or None when they hang up. Bounded in size and in time."""
    while b"\n" not in buffer:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        sock.settimeout(left)
        try:
            chunk = sock.recv(8192)
        except (TimeoutError, OSError):
            return None
        if not chunk:
            return None
        buffer += chunk
        if len(buffer) > MAX_LINE:
            raise Refused("That request is too long.")
    line, _, rest = bytes(buffer).partition(b"\n")
    buffer.clear()
    buffer += rest
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Refused("That is not a request.") from None
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise Refused("That is not a request.")
    return message
