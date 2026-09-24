"""What a guest's mpv actually plays from: plain HTTP on 127.0.0.1.

mpv cannot check a certificate against a pin, and a movie night's certificate
is signed by nobody, so the guest's player never talks to the host itself. It
plays http://127.0.0.1:<port>/media, and every request it makes is sent on to
the host over tls.connect(), which checks the pin before a byte of the request
goes out. The token stays in here too: it never appears in mpv's command line,
log or window title.

Addresses: the host's LAN address first with a short timeout (a friend on the
same network), then the internet one; whichever answered is tried first next
time, so only the first request of the evening pays for a LAN that isn't there.

The quality is the guest's own choice (the original, or 1080p / 720p when their
connection or PC can't take it) and rides in the URL mpv plays. mpv's request
is checked here as strictly as the host checks it (server.parse_media_query),
and the host is asked in the proxy's own words: only a checked quality and
start time go on, so nothing else in mpv's query string can reach the host.

Range headers go through untouched, since mpv seeks with them. When the host's
side of a file transfer drops part way (Wi-Fi, a router, the host's 30-minute
limit on a guest who stopped reading), the rest is asked for again from the
byte it stopped at, and mpv never sees the gap; a stream that changed in
between (a different ETag) is never spliced onto the old one. A transcoded
stream has no bytes to resume from: when it ends early, mpv sees the end, and
the player opens it again at the time it had reached.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import ssl
import threading
import time

from . import tls
from .server import QUALITY_CHOICES, parse_media_query
from .sync import OFFLINE, no_network, unreachable

_log = logging.getLogger("party.proxy")

_LOCAL_RE = re.compile(r"/(?:(?P<media>media)(?:\?(?P<query>[!-~]{1,64}))?|subs/(?P<sub>\d{1,3}))")
_CONTENT_RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
_PRINTABLE_RE = re.compile(r"[ -~]{1,200}")
_LANGUAGE_RE = re.compile(r"[A-Za-z]{2,3}")
# What the host says that mpv needs to hear; nothing else is passed on.
_RELAYED = ("content-type", "content-length", "content-range", "accept-ranges", "etag", "retry-after")

_NOT_FOUND = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class _Upstream:
    """One request's connection to the host, with its response head read."""

    def __init__(self, sock, status: int, reason: str, fields: dict[str, str], leftover: bytes) -> None:
        self.sock = sock
        self.status = status
        self.reason = reason
        self.fields = fields
        self.leftover = leftover        # body bytes that came in with the head

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _read_head(sock, deadline: float, limit: int) -> tuple[bytes, bytes]:
    """(head, whatever of the body came with it), reading at most `limit` bytes
    of head before `deadline`."""
    buffer = b""
    while True:
        end = buffer.find(b"\r\n\r\n")
        if end > limit or (end < 0 and len(buffer) > limit):
            raise ValueError("head too big")
        if end >= 0:
            return buffer[:end], buffer[end + 4:]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("head too slow")
        sock.settimeout(remaining)
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed before the head ended")
        buffer += chunk


def _media_query(quality: str | None, t: str | None) -> str:
    """"?quality=1080p&t=600", or whichever part there is, or "": always in
    this order, whatever order mpv's request had them in."""
    parts = ([f"quality={quality}"] if quality else []) + ([f"t={t}"] if t else [])
    return "?" + "&".join(parts) if parts else ""


def _parse_fields(lines: list[bytes]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in lines:
        name, colon, value = line.partition(b":")
        # A bare \r or \n inside a value would come out as a line of its own in
        # what is passed on to mpv.
        if not colon or b"\n" in line or b"\r" in line:
            raise ValueError("malformed header")
        key = name.strip().decode("latin-1").lower()
        if key in fields:
            raise ValueError(f"repeated header {key}")
        fields[key] = value.strip().decode("latin-1")
    return fields


class GuestProxy:
    """The guest's local end of a movie night's stream.

        proxy = GuestProxy(invite, host_name="Sam")   # address= the one sync reached, if known
        url = proxy.start()                        # "http://127.0.0.1:<p>/media": the host's default
        mpv.load(proxy.media_url(quality="original"), start_at=t)    # the file; mpv seeks with Range
        mpv.load(proxy.media_url(t, "1080p"), options={"rebase-start-time": "no"})   # a transcode from t
        for sub in proxy.subtitles():              # off Qt's thread: it asks the host
            mpv.command("sub-add", proxy.subtitle_url(sub["n"]), "auto", sub["title"], sub["lang"])
        proxy.stop()

    A transcoded stream needs rebase-start-time=no: its timestamps start at the
    film's own time, and mpv would otherwise show a stream opened at 600 as 0.

    `last_error` holds the latest reason the host could not be reached, as a
    sentence; `address` the host address that last answered; `host_name` the
    name that sentence calls the host by (the sync Client learns it on joining).
    """

    lan_timeout = 1.5            # a LAN host answers in a few milliseconds
    wan_timeout = 6.0
    head_timeout = 10.0          # mpv's request
    # The host's answer. A transcode answers only once ffmpeg has its first
    # bytes: 0.5-2.0 s on the PC this was built on, longer on a PC encoding in
    # software, and the host gives ffmpeg 30 s.
    answer_timeout = 30.0
    upstream_idle = 60.0         # the host sending nothing while mpv wants more
    body_stall = 30 * 60.0       # mpv reading nothing (paused); as on the host
    max_connections = 16
    max_head = 16 * 1024
    chunk = 256 * 1024
    resume_waits = (0.2, 0.5, 1.0, 2.0, 4.0)
    busy_waits = (0.5, 1.0, 2.0, 2.0, 2.0)

    def __init__(self, invite, address: str | None = None, host_name: str | None = None,
                 connect=None) -> None:
        self._invite = invite
        # tls.connect, or with this install's certificate shown too, for a movie
        # night on a friend's PC that lets in only its friends (sync.Client's too).
        self._connect_to = connect
        token = invite.token
        self._token_hex = token.hex() if isinstance(token, (bytes, bytearray)) else str(token).lower()
        self.address = address
        self.host_name = host_name
        self.last_error: str | None = None
        self.port: int | None = None
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._sockets: set = set()           # every live socket, so stop() can wake its thread
        self._active = 0

    # --- lifecycle ------------------------------------------------------------

    def start(self) -> str:
        if self._listener is None:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", 0))    # this machine only, never the network
            listener.listen(16)
            listener.settimeout(0.5)
            self._listener = listener
            self.port = listener.getsockname()[1]
            self._thread = threading.Thread(target=self._accept_loop, name="party-proxy", daemon=True)
            self._thread.start()
        return self.media_url()

    def stop(self) -> None:
        self._stopping.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            live = list(self._sockets)
        for sock in live:
            try:
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def media_url(self, t: float | None = None, quality: str | None = None) -> str:
        """The film for mpv to play. `quality` is this guest's own choice,
        'original', '1080p' or '720p', and None takes the host's default
        (ValueError for anything else: a bug, not a guest's input). `t` is
        where a transcoded stream starts, in seconds. The original is sent
        whole and ignores it: start that one with mpv's own start option."""
        if quality is not None and quality not in QUALITY_CHOICES:
            raise ValueError(f"a movie night sends {', '.join(QUALITY_CHOICES)}, not {quality!r}")
        seconds = None
        if t is not None and t > 0:
            seconds = f"{min(t, 999999.0):.3f}".rstrip("0").rstrip(".")
        return f"http://127.0.0.1:{self.port}/media" + _media_query(quality, seconds)

    def subtitle_url(self, number: int) -> str:
        return f"http://127.0.0.1:{self.port}/subs/{int(number)}"

    def subtitles(self) -> list[dict]:
        """The subtitle files beside the host's film, [{"n", "title", "lang"}],
        for mpv's sub-add <subtitle_url(n)> auto <title> <lang>. Asked of the
        host over the pinned connection, so it blocks for up to a few seconds:
        not on Qt's thread. [] when there are none or the host cannot answer.
        What comes back is checked like anything else off the network."""
        upstream = self._open("GET", f"/m/{self._token_hex}/subs", None)
        if upstream is None:
            return []
        try:
            length = upstream.fields.get("content-length", "")
            if upstream.status != 200 or not length.isdigit() or int(length) > 64 * 1024:
                return []
            body = upstream.leftover
            while len(body) < int(length):
                chunk = upstream.sock.recv(int(length) - len(body))
                if not chunk:
                    return []
                body += chunk
            listed = json.loads(body.decode("utf-8"))
        except (OSError, ssl.SSLError, ValueError):
            return []
        finally:
            self._track(upstream.sock, False)
            upstream.close()
        out = []
        for entry in listed[:32] if isinstance(listed, list) else []:
            if not isinstance(entry, dict):
                continue
            number, title, language = entry.get("n"), entry.get("title"), entry.get("lang")
            if (isinstance(number, int) and not isinstance(number, bool) and 0 <= number < 1000
                    and isinstance(title, str) and isinstance(language, str)):
                out.append({"n": number,
                            "title": "".join(c for c in title[:80] if c.isprintable()),
                            "lang": language if _LANGUAGE_RE.fullmatch(language) else ""})
        return out

    # --- mpv's side -------------------------------------------------------------

    def _track(self, sock, add: bool) -> None:
        with self._lock:
            if add:
                self._sockets.add(sock)
            else:
                self._sockets.discard(sock)

    def _accept_loop(self) -> None:
        listener = self._listener
        while not self._stopping.is_set() and listener is not None:
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    break
                time.sleep(0.05)
                continue
            with self._lock:
                full = self._active >= self.max_connections
                if not full:
                    self._active += 1
            if full:
                client.close()
                continue
            threading.Thread(target=self._serve, args=(client,), name="party-proxy-conn",
                             daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        self._track(client, True)
        try:
            head, _ = _read_head(client, time.monotonic() + self.head_timeout, self.max_head)
            lines = head.split(b"\r\n")
            parts = lines[0].split(b" ")
            fields = _parse_fields(lines[1:])
            # A web page can make the browser request 127.0.0.1 too, and with a
            # DNS name rebound to 127.0.0.1 read the answer. It cannot make the
            # browser say this Host.
            host_ok = fields.get("host") in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")
            match = None
            if len(parts) == 3 and parts[0] in (b"GET", b"HEAD") and host_ok:
                try:
                    match = _LOCAL_RE.fullmatch(parts[1].decode("ascii"))
                except UnicodeDecodeError:
                    match = None
            wanted_range = fields.get("range")
            asked = parse_media_query(match.group("query")) if match is not None else None
            if asked is None or (wanted_range is not None and not _PRINTABLE_RE.fullmatch(wanted_range)):
                client.sendall(_NOT_FOUND)
                return
            if match.group("sub") is not None:
                route = f"/m/{self._token_hex}/subs/{match.group('sub')}"
            else:
                # Written afresh from the checked quality and time, never
                # copied from mpv's request.
                route = f"/m/{self._token_hex}/media" + _media_query(*asked)
            self._relay(client, parts[0].decode("ascii"), route, wanted_range)
        except (OSError, ssl.SSLError, ValueError) as exc:
            _log.debug("request from the player ended: %s", exc)
        finally:
            self._track(client, False)
            try:
                client.close()
            except OSError:
                pass
            with self._lock:
                self._active -= 1

    # --- the host's side ------------------------------------------------------------

    def _candidates(self) -> list[tuple[str, float]]:
        invite = self._invite
        order: list[tuple[str, float]] = []
        for address, timeout in ((self.address, self.wan_timeout),
                                 (invite.lan_ip, self.lan_timeout),
                                 (invite.wan_ip, self.wan_timeout)):
            if address and address not in [a for a, _ in order]:
                order.append((str(address), timeout))
        return order

    def _unreachable(self) -> str:
        """What a guest reads when no address of the host's answered. From
        outside the host's home the usual reason is a port the router does not
        forward: the router of the PC this was built on did not answer UPnP at
        all (12 searches in 2 s), so a forward made by hand is how friends
        elsewhere get in, and a forward pointing at the PC's old address looks
        the same as a good one from the host's side. At the host's place it is
        Windows Firewall, which asks the host once and, told no, keeps out
        everyone, even at home. The words are sync.unreachable's, by name: the
        ones this guest read if joining had failed the same way."""
        return unreachable(self._invite.port, bool(self._invite.wan_ip), self.host_name)

    def _not_coming_through(self) -> str:
        """What a guest reads when the host's Mistery took the connection (the pin
        matched) but its answer never came right: cut off part way, too slow, not
        HTTP. The session puts it on the picture while it opens the stream again,
        so it says that, and only that: every sentence a guest sees keeps
        addresses and error codes out, which go to the log instead."""
        name = "".join(c for c in str(self.host_name or "")[:40] if c.isprintable()).strip()
        whose = f"{name}'s" if name else "the host's"
        return f"The film isn't coming through from {whose} PC right now. Trying again…"

    def _open(self, method: str, route: str, wanted_range: str | None) -> _Upstream | None:
        """Connect to the host (pin checked by tls.connect), send the request and
        read the response head. None when no address answered; last_error says why."""
        port = self._invite.port
        candidates = self._candidates()
        offline = bool(candidates)      # every address failed for want of any network, so far
        for address, timeout in candidates:
            if self._stopping.is_set():
                return None
            try:
                sock = (self._connect_to or tls.connect)(address, port, self._invite.pin, timeout)
            except tls.PinMismatch:
                offline = False
                self.last_error = ("The host's certificate does not match the invite. "
                                   "Ask your friend for a new code.")
                _log.warning("pin mismatch at %s:%d", address, port)
                continue
            except (OSError, ssl.SSLError) as exc:
                offline = offline and no_network(exc)
                self.last_error = self._unreachable()
                _log.info("could not reach the host at %s:%d: %s", address, port, exc)
                continue
            self._track(sock, True)
            try:
                request = (f"{method} {route} HTTP/1.1\r\nHost: {address}:{port}\r\n"
                           + (f"Range: {wanted_range}\r\n" if wanted_range else "")
                           + "Connection: close\r\n\r\n")
                sock.settimeout(self.head_timeout)
                sock.sendall(request.encode("ascii"))
                head, leftover = _read_head(sock, time.monotonic() + self.answer_timeout, self.max_head)
                lines = head.split(b"\r\n")
                status = lines[0].split(b" ", 2)
                if len(status) < 2 or not status[0].startswith(b"HTTP/1.") or not status[1].isdigit():
                    raise ValueError("not an HTTP answer")
                fields = _parse_fields(lines[1:])
            except (OSError, ssl.SSLError, ValueError) as exc:
                self._track(sock, False)
                try:
                    sock.close()
                except OSError:
                    pass
                offline = False
                self.last_error = self._not_coming_through()
                _log.info("the host at %s:%d did not answer properly: %s", address, port, exc)
                continue
            self.address = address
            self.last_error = None
            sock.settimeout(self.upstream_idle)
            reason = status[2].decode("latin-1") if len(status) > 2 else ""
            return _Upstream(sock, int(status[1]), reason, fields, leftover)
        if offline:
            self.last_error = OFFLINE
        return None

    def _relay(self, client: socket.socket, method: str, route: str, wanted_range: str | None) -> None:
        upstream = self._open(method, route, wanted_range)
        # 503 is the host saying "in a moment": every transcode slot is taken
        # for the instant the whole room reopens its streams after a seek, or
        # it is between films. mpv would take it as a dead end; wait instead.
        for wait in self.busy_waits:
            if upstream is None or upstream.status != 503 or self._stopping.is_set():
                break
            self._track(upstream.sock, False)
            upstream.close()
            time.sleep(wait)
            upstream = self._open(method, route, wanted_range)
        if upstream is None:
            client.sendall(_BAD_GATEWAY)
            return
        try:
            head = [f"HTTP/1.1 {upstream.status} {upstream.reason}".rstrip()]
            head += [f"{name.title()}: {upstream.fields[name]}" for name in _RELAYED if name in upstream.fields]
            head += ["Cache-Control: no-store", "Connection: close", "", ""]
            client.settimeout(self.body_stall)
            client.sendall("\r\n".join(head).encode("latin-1"))
            if method == "HEAD" or upstream.status not in (200, 206):
                return
            self._pipe(client, upstream, route)
        finally:
            self._track(upstream.sock, False)
            upstream.close()

    def _pipe(self, client: socket.socket, upstream: _Upstream, route: str) -> None:
        fields = upstream.fields
        length = int(fields["content-length"]) if fields.get("content-length", "").isdigit() else None
        etag = fields.get("etag")
        resumable = length is not None and etag is not None and fields.get("accept-ranges") == "bytes"
        first = 0
        if upstream.status == 206:
            found = _CONTENT_RANGE_RE.fullmatch(fields.get("content-range", ""))
            if found is None:
                resumable = False
            else:
                first = int(found.group(1))
        delivered = 0
        since_resume = 0
        resumes = 0
        pending = upstream.leftover
        current = upstream
        try:
            while length is None or delivered < length:
                if pending:
                    data, pending = pending, b""
                else:
                    try:
                        data = current.sock.recv(self.chunk)
                    except (OSError, ssl.SSLError):
                        data = b""
                if data:
                    if length is not None:
                        data = data[:length - delivered]
                    client.sendall(data)         # raises when mpv hangs up: we stop too
                    delivered += len(data)
                    since_resume += len(data)
                    if since_resume > (1 << 20):
                        resumes = 0              # a fresh run of luck
                    continue
                if not resumable or resumes >= len(self.resume_waits) or self._stopping.is_set():
                    break                        # mpv sees the end, or a short body
                time.sleep(self.resume_waits[resumes])
                resumes += 1
                since_resume = 0
                if current is not upstream:
                    self._track(current.sock, False)
                    current.close()
                start, last = first + delivered, first + length - 1
                current = self._open("GET", route, f"bytes={start}-{last}")
                if current is None:
                    break
                again = _CONTENT_RANGE_RE.fullmatch(current.fields.get("content-range", ""))
                if (current.status != 206 or current.fields.get("etag") != etag
                        or again is None or int(again.group(1)) != start):
                    _log.info("host's file changed during a resume; ending this request")
                    break
                _log.info("resumed the stream at byte %d", start)
                pending = current.leftover
        finally:
            if current is not None and current is not upstream:
                self._track(current.sock, False)
                current.close()
