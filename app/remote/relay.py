"""The one connection this PC keeps open to the website, for the phone remote.

A WebSocket to <site>/relay/pc (server/relay.py), opened by this PC, so nothing
has to be forwarded on the router and it works behind any network. The website
hands over each request a phone makes, numbered; api.py answers it on a worker
thread, and the answer goes back as a "head", then the body in binary pieces,
then "end". No more than WINDOW bytes of one answer go out ahead of what the
phone has taken (the website gives room back with "more"), so one slow phone
holds back its own film and nothing else.

Reconnects by itself, waiting longer each time the website cannot be reached
(up to a minute). Nothing here touches Qt.
"""

from __future__ import annotations

import base64
import json
import logging
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .. import __version__
from . import link

_log = logging.getLogger("remote")

CHUNK = 64 * 1024
WINDOW = 1024 * 1024
WORKERS = 8


@dataclass
class Reply:
    status: int
    headers: dict = field(default_factory=dict)
    body: Iterable[bytes] = ()


class _Room:
    """How much more of one answer the website will take now."""

    def __init__(self) -> None:
        self.left = WINDOW
        self.cancelled = False
        self.changed = threading.Condition()

    def take(self, size: int, stopped: threading.Event) -> bool:
        with self.changed:
            while self.left < size and not self.cancelled and not stopped.is_set():
                self.changed.wait(1.0)
            if self.cancelled or stopped.is_set():
                return False
            self.left -= size
            return True

    def give(self, size: int) -> None:
        with self.changed:
            self.left += max(0, size)
            self.changed.notify_all()

    def cancel(self) -> None:
        with self.changed:
            self.cancelled = True
            self.changed.notify_all()


class Relay:
    def __init__(self, handler: Callable[..., Reply], on_status: Callable[[str], None] | None = None) -> None:
        self.handler = handler
        self.on_status = on_status
        self.status = "starting"            # starting | connected | away | full | stopped
        self.detail = ""
        self._stop = threading.Event()
        self._socket = None
        self._send = threading.Lock()
        self._rooms: dict[int, _Room] = {}
        self._rooms_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(WORKERS, thread_name_prefix="remote-answer")
        self._thread = threading.Thread(target=self._run, name="remote-relay", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        socket = self._socket
        if socket is not None:
            try:
                socket.close()
            except Exception:                   # noqa: BLE001 - going anyway
                pass
        with self._rooms_lock:
            for room in self._rooms.values():
                room.cancel()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._say("stopped")

    def _say(self, status: str, detail: str = "") -> None:
        if (status, detail) == (self.status, self.detail):
            return
        self.status, self.detail = status, detail
        if self.on_status is not None:
            try:
                self.on_status(status)
            except Exception:                   # noqa: BLE001 - a listener's problem
                _log.exception("remote: status listener failed")

    # --- the connection -----------------------------------------------------------------

    def _run(self) -> None:
        from websockets.sync.client import connect

        wait = 2.0
        while not self._stop.is_set():
            try:
                with connect(link.relay_url(), open_timeout=15, close_timeout=3, max_size=4 * 1024 * 1024,
                             ping_interval=20, ping_timeout=30,
                             user_agent_header=f"Mistery/{__version__}") as socket:
                    self._socket = socket
                    socket.send(json.dumps({"t": "hello", "secret": link.secret(), "version": __version__}))
                    first = json.loads(socket.recv(timeout=15))
                    if first.get("t") == "full":
                        self._say("full", "The website has as many PCs connected as it allows. Trying again later.")
                        wait = 300.0
                        raise ConnectionError("the website is full")
                    if first.get("t") != "ready" or first.get("id") != link.remote_id():
                        raise ConnectionError("the website did not take this PC")
                    self._say("connected")
                    _log.info("remote: connected to %s", link.site())
                    wait = 2.0
                    while not self._stop.is_set():
                        try:
                            message = socket.recv(timeout=1.0)
                        except TimeoutError:
                            continue
                        if isinstance(message, str):
                            self._heard(message)
            except Exception as problem:            # noqa: BLE001 - away, and tried again
                if self._stop.is_set():
                    break
                if self.status != "full":
                    self._say("away", f"Can't reach {link.site()}: {problem.__class__.__name__}")
                _log.info("remote: connection to the website ended: %s", problem)
            finally:
                self._socket = None
                with self._rooms_lock:
                    for room in self._rooms.values():
                        room.cancel()
                    self._rooms.clear()
            self._stop.wait(wait)
            wait = min(60.0, wait * 2) if self.status != "full" else wait

    def _heard(self, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            return
        if not isinstance(message, dict) or not isinstance(message.get("n"), int):
            return
        n = message["n"]
        kind = message.get("t")
        if kind == "req":
            room = _Room()
            with self._rooms_lock:
                self._rooms[n] = room
            self._pool.submit(self._answer, n, message, room)
        elif kind == "more":
            with self._rooms_lock:
                room = self._rooms.get(n)
            if room is not None and isinstance(message.get("bytes"), int):
                room.give(message["bytes"])
        elif kind == "cancel":
            with self._rooms_lock:
                room = self._rooms.pop(n, None)
            if room is not None:
                room.cancel()

    def _send_text(self, message: dict) -> None:
        socket = self._socket
        if socket is None:
            raise ConnectionError("not connected")
        with self._send:
            socket.send(json.dumps(message, separators=(",", ":")))

    def _send_bytes(self, n: int, data: bytes) -> None:
        socket = self._socket
        if socket is None:
            raise ConnectionError("not connected")
        with self._send:
            socket.send(struct.pack(">I", n) + data)

    def _answer(self, n: int, message: dict, room: _Room) -> None:
        reply = None
        try:
            try:
                body = base64.b64decode(message.get("body") or "", validate=True)
            except ValueError:
                body = b""
            headers = {str(k).lower(): str(v) for k, v in (message.get("headers") or {}).items()}
            reply = self.handler(str(message.get("method") or "GET"), str(message.get("path") or "/"),
                                 str(message.get("query") or ""), headers, body)
            self._send_text({"t": "head", "n": n, "status": reply.status, "headers": reply.headers})
            if str(message.get("method")) != "HEAD":
                for piece in reply.body:
                    for start in range(0, len(piece), CHUNK):
                        part = piece[start:start + CHUNK]
                        if not room.take(len(part), self._stop):
                            return
                        self._send_bytes(n, part)
            self._send_text({"t": "end", "n": n})
        except Exception as problem:                # noqa: BLE001 - one answer, not the connection
            if not room.cancelled:
                _log.info("remote: an answer failed: %s", problem)
                try:
                    self._send_text({"t": "fail", "n": n})
                except Exception:                   # noqa: BLE001
                    pass
        finally:
            close = getattr(reply.body, "close", None) if reply is not None else None
            if callable(close):
                close()
            with self._rooms_lock:
                self._rooms.pop(n, None)


# --- the one relay of this process ---------------------------------------------------------

_current: Relay | None = None
_lock = threading.Lock()
listeners: list[Callable[[str], None]] = []


def _tell(status: str) -> None:
    for listener in list(listeners):
        listener(status)


def start() -> Relay:
    """Connect to the website (once), answering with api.handle."""
    global _current
    from . import api

    with _lock:
        if _current is None or _current.status == "stopped":
            _current = Relay(api.handle, _tell)
            _current.start()
        return _current


def stop() -> None:
    global _current
    with _lock:
        relay, _current = _current, None
    if relay is not None:
        relay.stop()
    from . import api

    api.forget()


def current() -> Relay | None:
    return _current


def start_if_wanted() -> bool:
    from ..config import settings

    if settings.get("remote_enabled"):
        start()
        return True
    stop()
    return False
