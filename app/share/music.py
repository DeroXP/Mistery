"""A friend's songs in the music player: one address per song, and a token only
when mpv comes for it.

The music player gives mpv the whole queue up front (player.py _rebuild: one
loadfile a song), while a friend's PC hands out a token per song asked for,
at most sharing_max_streams (3) on one channel at a time: asking for a fourth
takes back the oldest (server.py _send_play). So the address mpv is given
names the song, not a token,

    http://127.0.0.1:<port>/t/<their song id>

and the token is asked for when mpv comes for that song, to play it or to open
it early for a gapless change, kept, and asked for again if their PC has since
taken it back (their answer is then a bare 404). Everything else is
GuestProxy's: the connection pinned to their certificate, Range passed through
for seeking, a transfer cut part way resumed from the byte it stopped at. Like
a film's stream (playback.py), the channel is pinged while it is in use.

One Tunnel per friend, from their first song queued until none of theirs is
left (tunnel(), close_unused()). Nothing here touches Qt.
"""

from __future__ import annotations

import collections
import logging
import re
import socket
import ssl
import threading
import time

from .. import db
from ..party.guest_proxy import GuestProxy, _NOT_FOUND, _PRINTABLE_RE, _parse_fields, _read_head
from . import client
from .playback import PING_EVERY, _Way

_log = logging.getLogger("share")

_SONG_RE = re.compile(r"/t/(?P<id>[1-9][0-9]{0,11})")
KEEP_TOKENS = 3                 # their PC keeps no more than this for one channel


class Tunnel(GuestProxy):
    """The local address a friend's songs are played from."""

    def __init__(self, friend_id: int) -> None:
        friend = db.friend(friend_id)
        if friend is None:
            raise client.ShareError("They are not your friend any more.")
        self.friend_id = int(friend_id)
        # A token nobody has: GuestProxy wants one, and every real request is
        # given the right one in _open.
        way = _Way(friend["lan_ip"], friend["wan_ip"], int(friend["port"] or 42170), "0" * 26,
                   bytes.fromhex(friend["pin"]))
        super().__init__(way, host_name=friend["name"])
        self._channel: client.Channel | None = None
        self._talk = threading.Lock()           # one request at a time on the channel
        self._tokens: collections.OrderedDict[int, str] = collections.OrderedDict()
        self._closed = threading.Event()
        self._pinger: threading.Thread | None = None

    def song_url(self, remote_id: int) -> str:
        return f"http://127.0.0.1:{self.port}/t/{int(remote_id)}"

    # --- mpv's side -------------------------------------------------------------------------

    def _serve(self, sock: socket.socket) -> None:
        """A song asked for by its address: checked the way GuestProxy checks
        mpv's requests, then relayed with its route standing for the song."""
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

    # --- their side --------------------------------------------------------------------------

    def _open(self, method: str, route: str, wanted_range: str | None):
        """GuestProxy's, with a song's route turned into its token's first."""
        found = _SONG_RE.fullmatch(route)
        if found is None:
            return super()._open(method, route, wanted_range)
        remote_id = int(found.group("id"))
        for fresh in (False, True):
            token = self._token_for(remote_id, fresh=fresh)
            if token is None:
                return None
            upstream = super()._open(method, f"/m/{token}/media", wanted_range)
            if upstream is None or upstream.status != 404 or fresh:
                return upstream
            # Taken back since: they keep only a few per channel. Once more,
            # with a new one.
            self._track(upstream.sock, False)
            upstream.close()
        return None

    def _token_for(self, remote_id: int, *, fresh: bool = False) -> str | None:
        with self._talk:
            if not fresh and remote_id in self._tokens:
                self._tokens.move_to_end(remote_id)
                return self._tokens[remote_id]
            try:
                channel = self._ensure_channel()
                answer = channel.play("track", remote_id)
            except client.ShareError as problem:
                self.last_error = str(problem)
                self._drop_channel()
                return None
            except OSError as problem:
                self.last_error = str(problem)
                self._drop_channel()
                return None
            token = str(answer["token"])
            self._tokens[remote_id] = token
            self._tokens.move_to_end(remote_id)
            while len(self._tokens) > KEEP_TOKENS:
                self._tokens.popitem(last=False)
            return token

    def _ensure_channel(self) -> client.Channel:
        """With _talk held."""
        if self._channel is None:
            friend = db.friend(self.friend_id)
            if friend is None:
                raise client.ShareError("They are not your friend any more.")
            self._channel = client.Channel(friend).open()
            if self._pinger is None or not self._pinger.is_alive():
                self._pinger = threading.Thread(target=self._keep, name="share-songs", daemon=True)
                self._pinger.start()
        return self._channel

    def _drop_channel(self) -> None:
        """With _talk held: a channel that failed, and the tokens that went with it."""
        channel, self._channel = self._channel, None
        self._tokens.clear()
        if channel is not None:
            channel.close()

    def _keep(self) -> None:
        while not self._closed.wait(PING_EVERY):
            with self._talk:
                if self._channel is None:
                    return
                try:
                    self._channel.ask({"type": "ping"}, expect="pong", timeout=20.0)
                except (client.ShareError, OSError):
                    self._drop_channel()
                    return

    def stop(self) -> None:
        self._closed.set()
        super().stop()
        with self._talk:
            self._drop_channel()


# --- one per friend ---------------------------------------------------------------------------

_tunnels: dict[int, Tunnel] = {}
_lock = threading.Lock()


def tunnel(friend_id: int) -> Tunnel:
    """The friend's Tunnel, started if it was not. ShareError for a friend who
    has gone. Quick: nothing is asked of their PC until mpv asks for a song."""
    with _lock:
        found = _tunnels.get(int(friend_id))
        if found is None:
            found = Tunnel(friend_id)
            found.start()
            _tunnels[int(friend_id)] = found
        return found


def close_unused(in_use: set[int]) -> None:
    """Stop the Tunnels of friends with none of their songs left in the queue."""
    with _lock:
        gone = [fid for fid in _tunnels if fid not in in_use]
        stopping = [_tunnels.pop(fid) for fid in gone]
    for each in stopping:
        each.stop()


def close_all() -> None:
    close_unused(set())
