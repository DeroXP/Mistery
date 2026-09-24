"""Playing something of a friend's: what holds the stream open while you watch.

A friend's Mistery hands out a token for one film or song when asked
(server.py, "play"), and takes it back when the channel it was asked on closes
or goes quiet for IDLE_TIMEOUT. So the channel stays open for as long as the
film plays, with a ping every PING_EVERY seconds, and is closed (with a "stop"
first) when you stop. The film itself comes the way a movie night guest's does:
mpv plays http://127.0.0.1:<port>/media from a GuestProxy, which carries each
request to their PC over a connection pinned to their certificate. The token
never reaches mpv, its command line or its log.

    stream = Stream(friend_id, "movie", 12)
    url = stream.open()                 # blocking: the channel, the token, the proxy
    ...mpv plays url...
    stream.close()

Nothing here touches Qt, and open() waits on the network: a worker thread's.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from .. import db
from ..party.guest_proxy import GuestProxy
from . import client

_log = logging.getLogger("share")

PING_EVERY = 240.0      # well inside the server's IDLE_TIMEOUT (15 minutes)


@dataclass(frozen=True)
class _Way:
    """What GuestProxy needs to reach their PC: the shape of a movie night
    invite, filled in from the friend's row and the answer to "play"."""

    lan_ip: str | None
    wan_ip: str | None
    port: int
    token: str
    pin: bytes


class Stream:
    """One thing of a friend's, playing."""

    def __init__(self, friend_id: int, kind: str, remote_id: int, quality: str | None = None) -> None:
        self.friend_id = int(friend_id)
        self.kind = kind
        self.remote_id = int(remote_id)
        self.quality = quality
        self.answer: dict = {}
        self.proxy: GuestProxy | None = None
        self._channel: client.Channel | None = None
        self._lock = threading.Lock()
        # One request at a time on the channel: a ping and the closing "stop"
        # would otherwise read each other's answers.
        self._talk = threading.Lock()
        self._closed = threading.Event()
        self._pinger: threading.Thread | None = None

    @property
    def duration(self) -> float:
        value = self.answer.get("duration")
        return float(value) if isinstance(value, (int, float)) and value > 0 else 0.0

    @property
    def subtitles(self) -> list[dict]:
        listed = self.answer.get("subtitles")
        return listed if isinstance(listed, list) else []

    def open(self) -> str:
        """Ask their PC for it and return the URL for mpv. ShareError (or
        Unreachable) with a sentence for the screen when it can't be had."""
        friend = db.friend(self.friend_id)
        if friend is None:
            raise client.ShareError("They are not your friend any more.")
        channel = client.Channel(friend).open()
        try:
            answer = channel.play(self.kind, self.remote_id, self.quality)
            port = answer.get("port")
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise client.ShareError("Their Mistery did not say where to fetch it from.")
            # The address that answered first: a friend at home is reached at
            # home, and the proxy tries the rest only if that stops working.
            here = channel.address
            way = _Way(lan_ip=here if here == friend["lan_ip"] else friend["lan_ip"],
                       wan_ip=here if here == friend["wan_ip"] else friend["wan_ip"],
                       port=port, token=str(answer["token"]), pin=bytes.fromhex(friend["pin"]))
            proxy = GuestProxy(way, address=here, host_name=friend["name"])
            proxy.start()
        except BaseException:
            channel.close()
            raise
        with self._lock:
            if self._closed.is_set():               # closed while this was opening
                channel.close()
                proxy.stop()
                raise client.ShareError("Stopped.")
            self.answer, self.proxy, self._channel = answer, proxy, channel
        self._pinger = threading.Thread(target=self._keep, name="share-stream", daemon=True)
        self._pinger.start()
        return proxy.media_url(quality=self.quality if self.quality in ("1080p", "720p") else None)

    def media_url(self, t: float | None = None) -> str:
        """The URL again, for a transcode opened again at `t`."""
        proxy = self.proxy
        if proxy is None:
            raise client.ShareError("That stream is closed.")
        return proxy.media_url(t, self.quality if self.quality in ("1080p", "720p") else None)

    def subtitle_url(self, number: int) -> str | None:
        proxy = self.proxy
        return proxy.subtitle_url(number) if proxy is not None else None

    def _keep(self) -> None:
        """Ping, so their PC keeps the token alive for as long as this plays."""
        while not self._closed.wait(PING_EVERY):
            with self._lock:
                channel = self._channel
            if channel is None:
                return
            try:
                with self._talk:
                    if self._closed.is_set():
                        return
                    channel.ask({"type": "ping"}, expect="pong", timeout=20.0)
            except (client.ShareError, OSError) as problem:
                # The film may still play for a while from what mpv holds; the
                # proxy says the rest when the next request finds nobody there.
                _log.info("share: lost the channel to friend %d while playing: %s",
                          self.friend_id, problem)
                return

    def close(self) -> None:
        """Stop: the token goes back, the channel closes, the proxy stops."""
        self._closed.set()
        with self._lock:
            channel, self._channel = self._channel, None
            proxy, self.proxy = self.proxy, None
            token = self.answer.get("token")
        if proxy is not None:
            proxy.stop()
        if channel is not None:
            with self._talk:
                if token:
                    channel.stop(str(token))
                channel.close()
