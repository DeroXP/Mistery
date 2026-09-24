"""This Mistery asking a friend's for their library, a picture, or a film.

The other end of app/share/server.py. One `Channel` is one connection to one
friend: opened when you look at their library, kept while you browse, closed
when you leave. Nothing here blocks Qt — every call waits on a socket, so it
belongs on a worker thread.

Their home address is tried first and their internet address second, the way a
movie night guest does it, and whichever answered is remembered for next time.
Their certificate is checked against the fingerprint written down when you
paired, before a byte goes out, and this end shows its own so they know who is
calling.
"""

from __future__ import annotations

import json
import logging
import time

from .. import db
from ..party.tls import PinMismatch
from . import catalog
from . import identity as ident

_log = logging.getLogger("share")

PROTOCOL = 1
MAX_LINE = 4 * 1024 * 1024          # a catalogue of thousands of songs, once
MAX_ART = 12 * 1024 * 1024
CONNECT_TIMEOUT = 8.0
REPLY_TIMEOUT = 30.0


class ShareError(Exception):
    """Something a person should be told, in words they can act on."""


class Unreachable(ShareError):
    """Their PC did not answer at either address."""


def unreachable_words(name: str | None, port: int, internet: bool) -> str:
    """What to say when neither of a friend's addresses answered.

    The movie night's sentence (sync.unreachable) talks about a code and a
    movie night; a friend is neither. The usual causes are the same, though:
    their PC is off or asleep, or sharing is off there, or (from outside their
    home) their router does not forward the port. Without an internet address
    for them, only their home network can reach them at all.
    """
    who = f"{name}'s PC" if name else "their PC"
    first = f"Couldn't reach {who}: it may be off or asleep, or sharing is switched off there."
    if internet:
        return (f"{first} If you're not at their place, their router has to forward port {port} "
                "to their PC: their Friends page shows how.")
    return (f"{first} Mistery only knows where it is on their home network so far, so from "
            "anywhere else it can't be reached yet.")


class Channel:
    """A conversation with one friend's Mistery."""

    def __init__(self, friend, *, timeout: float = CONNECT_TIMEOUT) -> None:
        self.friend = friend
        self.timeout = timeout
        self.sock = None
        self.address: str | None = None
        self.their_hello: dict = {}
        self._buffer = bytearray()

    # --- opening and closing -------------------------------------------------

    def __enter__(self) -> "Channel":
        self.open()
        return self

    def __exit__(self, *exception) -> None:
        self.close()

    def open(self) -> "Channel":
        """Connect, greet, and remember where they answered."""
        friend = self.friend
        port = int(friend["port"] or 42170)
        pin = bytes.fromhex(friend["pin"])
        me = ident.identity()
        problems: list[Exception] = []
        for address in (friend["lan_ip"], friend["wan_ip"]):
            if not address:
                continue
            try:
                sock = me.connect(address, port, pin, timeout=self.timeout)
            except PinMismatch as problem:
                # Somebody else at that address: their old home address given to
                # another PC, most likely. Try the other one rather than stop.
                problems.append(problem)
                continue
            except OSError as problem:
                problems.append(problem)
                continue
            self.sock = sock
            self.address = address
            sock.sendall(b"MISTERY-SHARE/1\n")
            self.their_hello = self.ask({"type": "hello"}, expect="hello")
            db.update_friend(friend["id"], last_seen=time.time())
            name = self.their_hello.get("name")
            if isinstance(name, str) and name and name != friend["name"]:
                db.update_friend(friend["id"], name=name[:64])      # they renamed themselves
            return self
        if any(isinstance(problem, PinMismatch) for problem in problems):
            raise Unreachable(str(next(p for p in problems if isinstance(p, PinMismatch))))
        raise Unreachable(unreachable_words(friend["name"], port, bool(friend["wan_ip"])))

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.sendall(b'{"type":"bye"}\n')
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # --- asking --------------------------------------------------------------

    def ask(self, request: dict, *, expect: str, timeout: float = REPLY_TIMEOUT) -> dict:
        """One request, one answer. A refusal comes back as ShareError."""
        if self.sock is None:
            raise ShareError("That connection is closed.")
        self.sock.sendall(json.dumps(request, separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False).encode("utf-8") + b"\n")
        answer = self._read(timeout)
        kind = answer.get("type")
        if kind == "refused":
            raise ShareError(str(answer.get("reason") or "They said no."))
        if kind != expect:
            raise ShareError("Their Mistery answered something unexpected.")
        return answer

    def hello(self) -> dict:
        return self.their_hello

    def catalog(self, marks: dict | None = None) -> dict:
        """Their library, or only what has changed since those marks."""
        held = marks if marks is not None else catalog.held_marks(self.friend["id"])
        return self.ask({"type": "catalog", "marks": held}, expect="catalog", timeout=120.0)

    def refresh(self) -> dict:
        """Fetch what has changed and store it. Returns what changed."""
        answer = self.catalog()
        return catalog.apply(self.friend["id"], answer)

    def art(self, kind: str, remote_id: int, which: str = "poster") -> tuple[bytes, str] | None:
        """One picture of theirs: (bytes, content type), or None when there is none."""
        answer = self.ask({"type": "art", "kind": kind, "id": int(remote_id), "which": which},
                          expect="art", timeout=60.0)
        size = answer.get("bytes")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ART:
            return None
        blob = self._read_bytes(size, 60.0)
        return blob, str(answer.get("content_type") or "image/jpeg")

    def play(self, kind: str, remote_id: int, quality: str | None = None) -> dict:
        """Ask to play something: a token for the HTTP routes, and its length.

        What comes back is what a movie night guest gets: a token, a port, and
        the quality. The player then fetches it through the same guest proxy,
        which is why nothing here streams anything itself.
        """
        answer = self.ask({"type": "play", "kind": kind, "id": int(remote_id),
                           "quality": quality}, expect="play")
        token = answer.get("token")
        if not isinstance(token, str) or not token:
            raise ShareError("Their Mistery did not send a way to play that.")
        return answer

    def night(self, kind: str, remote_id: int, *, at: float | None = None,
              party: str | None = None) -> dict:
        """Ask their PC to hold a movie night of one of their films or episodes,
        for their friends: {"code", "title", "watching"}. The code is a
        KIND_FRIENDS invite, joined like any other (session.join). `at` and
        `party` ask for one watched before again: from where it got to, as the
        same party (Home's Watch together again)."""
        request = {"type": "night", "kind": kind, "id": int(remote_id)}
        if at is not None:
            request["at"] = float(at)
        if party:
            request["party"] = str(party)
        answer = self.ask(request, expect="night", timeout=30.0)
        code = answer.get("code")
        if not isinstance(code, str) or not code:
            raise ShareError("Their Mistery did not send a code for the movie night.")
        return answer

    def stop(self, token: str) -> None:
        try:
            self.ask({"type": "stop", "token": token}, expect="stop", timeout=10.0)
        except (ShareError, OSError):
            pass                        # they will drop it themselves soon enough

    # --- reading -------------------------------------------------------------

    def _read(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buffer:
            self._fill(deadline)
        line, _, rest = bytes(self._buffer).partition(b"\n")
        self._buffer.clear()
        self._buffer += rest
        try:
            answer = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ShareError("Their Mistery sent something unreadable.") from None
        if not isinstance(answer, dict) or not isinstance(answer.get("type"), str):
            raise ShareError("Their Mistery sent something unreadable.")
        return answer

    def _read_bytes(self, size: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self._buffer) < size:
            self._fill(deadline)
        blob = bytes(self._buffer[:size])
        del self._buffer[:size]
        return blob

    def _fill(self, deadline: float) -> None:
        left = deadline - time.monotonic()
        if left <= 0 or self.sock is None:
            raise ShareError("Their Mistery stopped answering.")
        self.sock.settimeout(left)
        try:
            chunk = self.sock.recv(65536)
        except (TimeoutError, OSError) as problem:
            raise ShareError("Their Mistery stopped answering.") from problem
        if not chunk:
            raise ShareError("Their Mistery closed the connection.")
        if len(self._buffer) + len(chunk) > MAX_LINE + MAX_ART:
            raise ShareError("Their Mistery sent far too much.")
        self._buffer += chunk


def refresh(friend) -> dict:
    """Open a channel, take what has changed, close it. For a background pass."""
    with Channel(friend) as channel:
        done = channel.refresh()
    _log.info("share: %s's library: %s", friend["name"], done)
    return done
