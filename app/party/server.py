"""The one listener: the film over HTTP, and the sync channel, on one port.

A movie night opens a single port on the host's router, so everything comes in
through it, inside TLS with this session's own certificate (a guest has checked
it against the pin in the invite before sending a byte). After the handshake
the first line decides:

    GET /m/<token-hex>/media        the film, at the quality this guest asks
                                    for (?quality=original|1080p|720p, the
                                    host's default when it does not say): the
                                    file itself, with Range requests (mpv seeks
                                    with them), or ffmpeg's Matroska starting
                                    at t=<seconds>, also in the query
    GET /m/<token-hex>/subs/<n>     a text subtitle file beside the film
    GET /m/<token-hex>/subs         which there are: [{"n", "title", "lang"}] as
                                    JSON (the sync channel's media description
                                    carries plain values only, not lists)
    MISTERY-SYNC/1 <token-hex>      handed to sync_handler(sock, peer)

Anything else, or a wrong token, is closed having said nothing useful: HTTP
gets a 404 with no body, anything else just the close. The file served is only
ever the one set_media() chose; nothing a guest sends is turned into a path.

The port is open to the whole internet for the evening, and scanners find open
ports within minutes, so everything is bounded. A connection that has not yet
shown the token is a stranger's: at most 4 of those from one address and 16 in
all, and each gets 10 s for the TLS handshake and 10 s for its first line and
headers, however slowly the bytes trickle in (8 KB of head at most). Once the
token checks out it no longer counts against its address, so five friends in
one house, behind one router, are not turned away; 48 connections in all.

When all 16 stranger places are taken, a new stranger (from an address still
under its 4) takes the place of the one that has been waiting longest, rather
than being turned away. A friend is a stranger only for the moment between the
handshake and the token, milliseconds on a LAN and a round trip or two over
the internet, while somebody holding places open has to keep them idle for
seconds. Four addresses holding all 16 used to keep every friend out for as
long as they kept at it (a reviewer's finding); now they have to open more
than 16 connections in the time one friend's handshake takes to get in its way.
"""

from __future__ import annotations

import collections
import hmac
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import find_ffmpeg
from . import transcode

_log = logging.getLogger("party.server")

SYNC_GREETING = b"MISTERY-SYNC/1"
# What a guest can ask /media for. Each guest chooses their own: here the
# host's upload is not the limit (909 Mbit/s measured, against 11.7 for the
# heaviest film); a friend's download, or a PC that cannot decode 4K HEVC
# smoothly, is.
QUALITY_CHOICES = ("original", *transcode.QUALITIES)

_TARGET_RE = re.compile(
    r"/m/(?P<token>[0-9a-f]{1,128})/"
    r"(?:(?P<media>media)(?:\?(?P<query>[!-~]{1,64}))?|subs/(?P<sub>\d{1,3})|(?P<index>subs))")
_SECONDS_RE = re.compile(r"\d{1,6}(?:\.\d{1,3})?")
_RANGE_RE = re.compile(r"bytes=(\d{0,19})-(\d{0,19})", re.IGNORECASE)
_HEADER_NAME_RE = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+")

_CONTENT_TYPES = {
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".mp4": "video/mp4",
    ".m4v": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".ts": "video/mp2t", ".m2ts": "video/mp2t",
}
# Text only: an image subtitle (.sub/.idx) is two files and not worth a route.
_SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt"}
_MAX_SUBTITLE_FILES = 32
_MAX_SUBTITLE_BYTES = 8 * 1024 * 1024       # ASS with its styling runs to a few hundred KB

_NOT_FOUND = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_UNAVAILABLE = (b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n"
                b"Retry-After: 2\r\nConnection: close\r\n\r\n")


class ServerError(OSError):
    """The port could not be opened. The message is a sentence for the host."""


class _Refused(Exception):
    """Close the connection; the reason goes to the log and the counts."""


@dataclass(frozen=True)
class _Media:
    path: str
    size: int
    mtime: float
    duration: float
    quality: str                 # original | 1080p | 720p
    generation: int
    subtitles: tuple[str, ...]   # full paths, in the order guests number them


class _Connection:
    __slots__ = ("sock", "address", "stranger", "since", "generation", "default", "stream")

    def __init__(self, sock, address: str) -> None:
        self.sock = sock
        self.address = address
        self.stranger = True                  # until it shows the token
        self.since = time.monotonic()         # accepted then: the longest-waiting stranger goes first
        self.generation: int | None = None    # set while it serves the film
        # The host's default quality it is sending, for a guest who did not
        # choose one; None when the guest chose.
        self.default: str | None = None
        self.stream: transcode.Stream | None = None


def _abort(sock) -> None:
    """Wake whichever thread is blocked on this socket, from any thread.

    socket.socket's own shutdown, not SSLSocket's: that one drops the TLS object
    from under a thread that may be in the middle of reading with it. A TCP
    shutdown makes the blocked read or write fail, and the owner cleans up.
    """
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except OSError:
        pass


_SEPARATORS = " ._-[("
_NOT_LANGUAGES = {"sdh", "cc", "hi", "sub", "subs"}   # Film.en.sdh.srt, Film.en.hi.srt


def _sidecar_subtitles(path: str) -> tuple[str, ...]:
    """Text subtitles beside the film named after it: Film.srt, Film.en.srt,
    Film [English].ass. Named *after* it, not merely containing its name, so
    a guest watching 1.mkv is not handed 10.srt: another file is not theirs."""
    folder, name = os.path.split(path)
    stem = os.path.splitext(name)[0].lower()
    try:
        entries = sorted(os.listdir(folder or "."))
    except OSError:
        return ()
    found = []
    for entry in entries:
        base, ext = os.path.splitext(entry)
        rest = base.lower()[len(stem):]
        if (ext.lower() in _SUBTITLE_EXTS and base.lower().startswith(stem)
                and (not rest or rest[0] in _SEPARATORS)):
            full = os.path.join(folder, entry)
            if os.path.isfile(full):
                found.append(full)
        if len(found) >= _MAX_SUBTITLE_FILES:
            break
    return tuple(found)


def _subtitle_label(video: str, subtitle: str) -> tuple[str, str]:
    """(title, language) for a guest's subtitle menu, from the file name alone:
    Film.en.forced.srt beside Film.mkv is ('en forced', 'en'). Only what the
    name adds to the film's is shown; the host's folders stay the host's. The
    language comes first by convention (Film.en.sdh.srt is English, not 'sdh')."""
    stem = os.path.splitext(os.path.basename(video))[0]
    base, ext = os.path.splitext(os.path.basename(subtitle))
    words = [w for w in re.split(r"[\s._\-\[\]()]+", base[len(stem):]) if w]
    language = next((w.lower() for w in words
                     if w.isascii() and w.isalpha() and len(w) in (2, 3) and w.lower() not in _NOT_LANGUAGES), "")
    return (" ".join(words) or ext.lstrip(".").upper()), language


def _parse_range(value: str, size: int) -> tuple[int, int] | None:
    """(first, last) byte for one `bytes=` range, or None when it cannot be served.

    One range only: mpv never asks for more, and a multipart answer is a lot of
    code for no player. Everything the RFC calls invalid, and every range that
    starts past the end, is refused (416) rather than ignored, because ignoring
    it would mean sending the whole film to a client that asked for a piece.
    """
    match = _RANGE_RE.fullmatch(value.strip())
    if not match or (not match.group(1) and not match.group(2)):
        return None
    first, last = match.group(1), match.group(2)
    if not first:                                   # bytes=-500: the last 500
        suffix = int(last)
        if suffix == 0 or size == 0:
            return None
        return max(0, size - suffix), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if end < start or start >= size:
        return None
    return start, min(end, size - 1)


def parse_media_query(query: str | None) -> tuple[str | None, str | None] | None:
    """(quality, t) from the query of a /media request, or None when it is not
    one to answer; a missing part is None, and no query at all is the host's
    default from the start.

    Strict, like everything a guest sends: `quality` exactly 'original', '1080p'
    or '720p', `t` seconds (up to six digits, up to three decimals), each at
    most once, in either order, and nothing else. A near miss (1080P, a second
    quality, an empty value) is refused rather than guessed at. `t` is where a
    transcoded stream starts; the original is sent whole and ignores it. The
    guest's proxy checks mpv's requests with this same function, so the two
    ends cannot disagree about what a request means.
    """
    if query is None:
        return None, None
    found: dict[str, str] = {}
    for part in query.split("&"):
        name, equals, value = part.partition("=")
        if not equals or name in found:
            return None
        if (name == "quality" and value in QUALITY_CHOICES) or (name == "t" and _SECONDS_RE.fullmatch(value)):
            found[name] = value
        else:
            return None
    return found.get("quality"), found.get("t")


class PartyServer:
    """ONE TLS listener on ONE port for a movie night's host.

        server = PartyServer(identity, token, port)      # bind="0.0.0.0" for real
        server.sync_handler = hub.accept                 # (sock, peer)
        quality = server.set_media(path, duration, "auto")   # the default: 'original'
        port = server.start()
        ...
        server.stop()

    Threads, one per connection, and never Qt's: nothing here touches Qt.
    """

    # Class attributes so a test can shorten them on one instance.
    handshake_timeout = 10.0
    head_timeout = 10.0          # the whole request head, however slowly it comes
    # How long a send may wait on a guest who is not reading. A paused player
    # stops reading once its cache is full, and a movie night paused for dinner
    # should find its stream still there; a dead connection is dropped by TCP
    # itself long before this.
    body_stall = 30 * 60.0
    first_byte_timeout = 30.0    # ffmpeg's first output; measured 0.45-2.0 s
    # Strangers: connections that have not shown the token. A guest's own are
    # strangers for the few milliseconds of a handshake, so 4 per address is
    # plenty for them and all a scanner gets.
    max_strangers_per_address = 4
    max_strangers = 16
    # Everyone: ten guests (the room's limit) with a player and a sync channel
    # each, plus the connection a seek opens before the old one has closed.
    max_connections = 48
    # Each transcode is an ffmpeg: 1.2 cores for a 4K HDR film, and an encoder
    # session (consumer NVIDIA cards allow 8 at once, across every program).
    # One cap for every guest, whichever quality each asked for; a guest
    # watching the original takes no slot.
    max_transcodes = 6
    max_line = 4096
    max_head = 8192
    max_headers = 64
    chunk = 256 * 1024

    def __init__(self, identity, token: bytes | str, port: int, bind: str = "0.0.0.0") -> None:
        self.sync_handler = None
        self._context: ssl.SSLContext = identity.server_context()
        token_hex = token.hex() if isinstance(token, (bytes, bytearray)) else str(token).lower()
        self._token = token_hex.encode("ascii")
        self._bind = bind
        self._wanted_port = int(port)
        self.port: int | None = None
        self._listener: socket.socket | None = None
        self._accepting: threading.Thread | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._connections: dict[int, _Connection] = {}
        self._strangers: collections.Counter = collections.Counter()   # address -> count
        self._transcodes = 0
        self._media: _Media | None = None
        self._generation = 0
        self.refusals: collections.Counter = collections.Counter()     # reason -> count

    # --- what is being shared ------------------------------------------------

    def set_media(self, path: str | Path, duration: float | None, quality: str = "auto") -> str:
        """Share this file from now on; returns the host's default quality, the
        one a guest who does not ask gets: 'original', '1080p' or '720p'
        ('auto' is the original, see transcode.resolve_quality). Guests need
        that answer: a transcoded stream is opened at a time and has no length
        of its own. Any guest can ask for another with ?quality=.

        Connections still sending the previous file are closed, so a guest can
        never keep pulling a film the host has moved on from. When only the
        default changes, the guests who took the default are cut off (their
        player reopens at the new one) and those who chose their own quality
        carry on. The same file at the same quality again changes nothing and
        cuts nobody off. A transcoded default tests the encoders here (0.23 s
        the first time, cached after), so call it off Qt's thread.
        """
        path = os.fspath(path)
        stat = os.stat(path)                      # a missing file is the caller's error
        resolved = transcode.resolve_quality(quality, stat.st_size, duration)
        if resolved != "original" and not (find_ffmpeg() and transcode.pick_encoder()):
            _log.warning("no working ffmpeg/H.264 encoder: sending %s as the original", path)
            resolved = "original"
        subtitles = _sidecar_subtitles(path)
        with self._lock:
            current = self._media
            same_file = current is not None and (current.path, current.size, current.mtime) == (
                path, stat.st_size, stat.st_mtime)
            if not same_file:
                self._generation += 1             # another file: a new ETag, so no resume splices
            media = _Media(path, stat.st_size, stat.st_mtime, float(duration or 0.0),
                           resolved, self._generation, subtitles)
            self._media = media
            stale = [c for c in self._connections.values() if self._stale(c, media)]
        for connection in stale:
            self._close_connection(connection)
        # Any guest may switch to a transcode at any moment, so test the
        # encoders and probe the file now rather than while the first of them
        # waits for a picture: 0.23 s and 0.09 s measured, on top of the
        # 0.6-2.0 s a warm transcode takes to give its first bytes (the longer
        # the further back the film's previous keyframe is).
        threading.Thread(target=self._warm_up, args=(path,), name="party-probe", daemon=True).start()
        _log.info("sharing %s, %s unless a guest asks otherwise, %d subtitle file(s)",
                  os.path.basename(path), resolved, len(subtitles))
        return resolved

    @staticmethod
    def _warm_up(path: str) -> None:
        if transcode.pick_encoder():
            transcode.describe(path)

    @staticmethod
    def _stale(connection: _Connection, media: _Media | None) -> bool:
        """Sending what the host no longer shares: another file, or the host's
        old default to a guest who took the default. A guest who chose their
        own quality keeps it through a change of default."""
        if connection.generation is None:
            return False
        return (media is None or connection.generation != media.generation
                or (connection.default is not None and connection.default != media.quality))

    @property
    def quality(self) -> str | None:
        """The host's default: what a guest who does not ask for a quality gets."""
        media = self._media
        return media.quality if media else None

    def subtitles(self) -> list[dict]:
        """The external subtitles guests can fetch at /subs/<n>:
        [{"n": 0, "title": "en forced", "lang": "en"}, ...]. A guest gets the
        same list from GuestProxy.subtitles()."""
        media = self._media
        return self._subtitle_list(media) if media is not None else []

    @staticmethod
    def _subtitle_list(media: _Media) -> list[dict]:
        out = []
        for number, path in enumerate(media.subtitles):
            title, language = _subtitle_label(media.path, path)
            out.append({"n": number, "title": title, "lang": language})
        return out

    # --- lifecycle ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._listener is not None and not self._stopping.is_set()

    def start(self) -> int:
        """Listen, and return the port (the one asked for, or the one the OS
        chose for port 0). ServerError says what to do if the port is taken."""
        if self._listener is not None:
            return self.port
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt":
            # Without this, another program on the PC could bind the same port
            # with SO_REUSEADDR and be handed some of the guests' connections.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind((self._bind, self._wanted_port))
            listener.listen(16)
        except OSError as exc:
            listener.close()
            raise ServerError(self._bind_error(exc)) from exc
        listener.settimeout(0.5)          # how often the accept loop looks at _stopping
        self._listener = listener
        self.port = listener.getsockname()[1]
        self._accepting = threading.Thread(target=self._accept_loop, name="party-accept", daemon=True)
        self._accepting.start()
        _log.info("movie night listening on %s:%d", self._bind, self.port)
        return self.port

    def _bind_error(self, exc: OSError) -> str:
        port = self._wanted_port
        code = getattr(exc, "winerror", None) or exc.errno
        if code in (10048, 98, 48):              # WSAEADDRINUSE / EADDRINUSE
            return (f"Port {port} is already in use by another program. "
                    "Choose another port in Settings → Movie night.")
        if code in (10013, 13):                  # WSAEACCES: reserved, often by Hyper-V
            return (f"Windows would not let Mistery use port {port}; another program or "
                    "Windows itself has reserved it. Choose another port in Settings → Movie night.")
        return f"Could not open port {port}: {exc.strerror or exc}."

    def stop(self) -> None:
        """Close the port and every connection, and stop every ffmpeg. The sync
        channels handed to sync_handler are closed too if their handler is still
        running on one of our threads, so stop the hub first to say goodbye."""
        self._stopping.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            connections = list(self._connections.values())
        for connection in connections:
            self._close_connection(connection)
        if self._accepting is not None and self._accepting is not threading.current_thread():
            self._accepting.join(timeout=2.0)
        _log.info("movie night listener closed")

    def _close_connection(self, connection: _Connection) -> None:
        stream = connection.stream
        if stream is not None:
            stream.close()
        _abort(connection.sock)

    # --- connections ------------------------------------------------------------

    def _refuse(self, reason: str, address: str) -> None:
        with self._lock:
            self.refusals[reason] += 1
        _log.debug("refused %s: %s", address, reason)

    def _accept_loop(self) -> None:
        listener = self._listener
        while not self._stopping.is_set() and listener is not None:
            try:
                raw, peer = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stopping.is_set():
                    break
                time.sleep(0.05)                 # e.g. out of handles for a moment
                continue
            address = peer[0]
            connection = _Connection(raw, address)
            evicted = None
            with self._lock:
                full = (len(self._connections) >= self.max_connections
                        or self._strangers[address] >= self.max_strangers_per_address)
                if not full and sum(self._strangers.values()) >= self.max_strangers:
                    # Every stranger's place is taken: the one that has waited
                    # longest makes room (see the module's docstring).
                    evicted = min((c for c in self._connections.values() if c.stranger),
                                  key=lambda c: c.since, default=None)
                    if evicted is None:
                        full = True
                    else:
                        self._forget_stranger(evicted)
                if not full:
                    self._connections[id(connection)] = connection
                    self._strangers[address] += 1
            if evicted is not None:
                # Its own thread wakes to a dead socket and cleans up after it.
                self._refuse("waited longest while strangers filled every place", evicted.address)
                self._close_connection(evicted)
            if full:
                self._refuse("too many connections", address)
                raw.close()
                continue
            threading.Thread(target=self._serve, args=(connection, peer),
                             name="party-conn", daemon=True).start()

    def _serve(self, connection: _Connection, peer) -> None:
        raw = connection.sock
        conn = None
        handed_over = False
        try:
            # Sync messages are small and latency is the point; the film is
            # sent in 256 KB writes that fill segments anyway.
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            raw.settimeout(self.handshake_timeout)   # a deadline for the whole handshake
            conn = self._context.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
            connection.sock = conn
            if self._stopping.is_set():
                return
            conn.do_handshake()
            deadline = time.monotonic() + self.head_timeout
            first = self._read_line(conn, deadline)
            if first.startswith(SYNC_GREETING + b" "):
                handed_over = self._hand_to_sync(conn, connection, peer, first)
            elif first.endswith((b" HTTP/1.1", b" HTTP/1.0")):
                self._http(conn, connection, first, deadline)
            else:
                raise _Refused("not a request")
        except _Refused as refused:
            self._refuse(str(refused), connection.address)
        except (OSError, ssl.SSLError, ValueError) as exc:
            # Scanners, a TLS client with a different idea, a guest who seeked away.
            _log.debug("connection from %s ended: %s", connection.address, exc)
        finally:
            with self._lock:
                self._connections.pop(id(connection), None)
            self._known(connection)
            if not handed_over:
                for sock in (conn, raw):
                    if sock is not None:
                        try:
                            sock.close()
                        except OSError:
                            pass

    def _read_line(self, conn, deadline: float) -> bytes:
        """One line, without reading a byte past it (the sync channel's first
        message may be in the same packet, and it belongs to sync_handler).
        The deadline covers the whole head, so a client trickling one byte every
        few seconds is cut off like one that sends nothing."""
        line = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Refused("request head too slow")
            conn.settimeout(remaining)
            try:
                byte = conn.recv(1)
            except (socket.timeout, TimeoutError):
                raise _Refused("request head too slow") from None
            if not byte:
                raise _Refused("closed mid-request")
            if byte == b"\n":
                return bytes(line[:-1] if line.endswith(b"\r") else line)
            line += byte
            if len(line) > self.max_line:
                raise _Refused("line too long")

    def _token_ok(self, given: bytes) -> bool:
        return hmac.compare_digest(given, self._token)

    def _known(self, connection: _Connection) -> None:
        """No longer a stranger: it showed the token (or it is gone)."""
        with self._lock:
            self._forget_stranger(connection)

    def _forget_stranger(self, connection: _Connection) -> None:
        """Take it out of the strangers' count, once. With the lock held."""
        if connection.stranger:
            connection.stranger = False
            self._strangers[connection.address] -= 1
            if self._strangers[connection.address] <= 0:
                del self._strangers[connection.address]

    def _hand_to_sync(self, conn, connection: _Connection, peer, line: bytes) -> bool:
        parts = line.split(b" ")
        handler = self.sync_handler
        if len(parts) != 2 or not self._token_ok(parts[1]):
            raise _Refused("wrong token")
        if handler is None:
            raise _Refused("no sync handler")
        self._known(connection)
        conn.settimeout(None)                 # the sync channel keeps its own time
        # From here the socket is the handler's. It may keep this thread for the
        # life of the channel (the connection's slot stays taken until it
        # returns) or give the socket to a thread of its own and return.
        handler(conn, peer)
        return True

    # --- HTTP --------------------------------------------------------------------

    def _read_headers(self, conn, deadline: float) -> dict[str, list[str]]:
        headers: dict[str, list[str]] = {}
        total = 0
        for _ in range(self.max_headers + 1):
            line = self._read_line(conn, deadline)
            if not line:
                return headers
            total += len(line) + 2
            if total > self.max_head:
                raise _Refused("request head too big")
            name, colon, value = line.partition(b":")
            if not colon or not _HEADER_NAME_RE.fullmatch(name):
                raise _Refused("malformed header")
            headers.setdefault(name.decode("ascii").lower(), []).append(
                value.strip().decode("latin-1"))
        raise _Refused("too many headers")

    def _http(self, conn, connection: _Connection, first: bytes, deadline: float) -> None:
        parts = first.split(b" ")
        if len(parts) != 3:
            raise _Refused("not a request")
        method, target, _version = parts
        headers = self._read_headers(conn, deadline)
        try:
            match = _TARGET_RE.fullmatch(target.decode("ascii"))
        except UnicodeDecodeError:
            match = None
        asked = parse_media_query(match.group("query")) if match is not None else None
        # A quality nobody offers gets the same bare 404 as a wrong token: the
        # port says nothing to anyone about what it would have answered.
        if (method not in (b"GET", b"HEAD") or asked is None
                or not self._token_ok(match.group("token").encode("ascii"))):
            conn.settimeout(5.0)
            conn.sendall(_NOT_FOUND)
            raise _Refused("wrong token or path")
        self._known(connection)
        asked_quality, asked_t = asked
        with self._lock:
            # Read together, so set_media either sees this connection as
            # serving the old film or default (and closes it) or it gets the
            # new one.
            media = self._media
            if media is not None and match.group("media"):
                connection.generation = media.generation
                connection.default = None if asked_quality else media.quality
        if media is None or self._stopping.is_set():
            conn.settimeout(5.0)
            conn.sendall(_UNAVAILABLE)
            return
        head_only = method == b"HEAD"
        conn.settimeout(self.body_stall)
        if match.group("index"):
            body = json.dumps(self._subtitle_list(media)).encode("utf-8")
            self._send_head(conn, "200 OK", [("Content-Type", "application/json"),
                                             ("Content-Length", str(len(body)))])
            if not head_only:
                conn.sendall(body)
            return
        if match.group("sub") is not None:
            self._send_subtitle(conn, media, int(match.group("sub")), head_only)
            return
        quality = asked_quality or media.quality
        if quality == "original":
            self._send_file(conn, media, headers.get("range"), head_only)
        else:
            self._send_transcode(conn, connection, media, quality, float(asked_t or 0), head_only)

    def _send_head(self, conn, status: str, fields: list[tuple[str, str]]) -> None:
        lines = [f"HTTP/1.1 {status}", *(f"{k}: {v}" for k, v in fields),
                 "Cache-Control: no-store", "Connection: close", "", ""]
        conn.sendall("\r\n".join(lines).encode("ascii"))

    def _send_file(self, conn, media: _Media, ranges: list[str] | None, head_only: bool) -> None:
        with open(media.path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            etag = f'"{media.generation}-{size}-{int(media.mtime)}"'
            fields = [("Content-Type", _CONTENT_TYPES.get(Path(media.path).suffix.lower(),
                                                          "application/octet-stream")),
                      ("Accept-Ranges", "bytes"), ("ETag", etag)]
            # A unit other than bytes must be ignored (RFC 9110 14.2), not refused.
            if ranges is None or not all(r.lower().startswith("bytes=") for r in ranges):
                start, end, status = 0, size - 1, "200 OK"
            else:
                wanted = _parse_range(ranges[0], size) if len(ranges) == 1 else None
                if wanted is None:
                    self._send_head(conn, "416 Range Not Satisfiable",
                                    [("Content-Range", f"bytes */{size}"), ("Content-Length", "0")])
                    return
                start, end = wanted
                status = "206 Partial Content"
                fields.append(("Content-Range", f"bytes {start}-{end}/{size}"))
            length = max(0, end - start + 1)
            fields.append(("Content-Length", str(length)))
            self._send_head(conn, status, fields)
            if head_only or length == 0:
                return
            handle.seek(start)
            buffer = bytearray(self.chunk)
            view = memoryview(buffer)
            remaining = length
            # Read and send a piece at a time: a 60 GB remux never sits in memory.
            while remaining > 0:
                count = handle.readinto(view[:min(self.chunk, remaining)])
                if not count:
                    break                          # the file shrank under us
                conn.sendall(view[:count])
                remaining -= count

    def _send_transcode(self, conn, connection: _Connection, media: _Media, quality: str, start: float,
                        head_only: bool) -> None:
        if media.duration > 0:
            start = min(start, max(0.0, media.duration - 1.0))
        fields = [("Content-Type", "video/x-matroska"), ("Accept-Ranges", "none")]
        if head_only:
            self._send_head(conn, "200 OK", fields)
            return
        with self._lock:
            busy = self._transcodes >= self.max_transcodes
            if not busy:
                self._transcodes += 1
        if busy:
            conn.sendall(_UNAVAILABLE)
            raise _Refused("too many transcodes")
        stream = None
        try:
            try:
                stream = transcode.Stream(media.path, start, quality)
            except OSError as exc:
                _log.warning("could not start ffmpeg: %s", exc)
                conn.sendall(_UNAVAILABLE)
                return
            connection.stream = stream
            # stop() or set_media() may have come while ffmpeg was starting,
            # before there was a stream for them to close.
            if self._stopping.is_set() or self._stale(connection, self._media):
                return
            # Wait for ffmpeg's first bytes before answering, so a file it cannot
            # read gets a 503 the guest can act on rather than an empty 200.
            watchdog = threading.Timer(self.first_byte_timeout, stream.close)
            watchdog.daemon = True
            watchdog.start()
            try:
                chunk = stream.read(self.chunk)
            finally:
                watchdog.cancel()
            if not chunk:
                _log.warning("ffmpeg gave nothing for %s at %.0f s: %s",
                             os.path.basename(media.path), start, stream.errors() or "no message")
                conn.sendall(_UNAVAILABLE)
                return
            _log.info("transcoding %s for %s from %.0f s at %s", os.path.basename(media.path),
                      connection.address, start, quality)
            self._send_head(conn, "200 OK", fields)
            while chunk:
                conn.sendall(chunk)
                chunk = stream.read(self.chunk)
        finally:
            if stream is not None:
                stream.close()
            with self._lock:
                self._transcodes -= 1

    def _send_subtitle(self, conn, media: _Media, number: int, head_only: bool) -> None:
        if number >= len(media.subtitles):
            conn.sendall(_NOT_FOUND)
            raise _Refused("no such subtitle")
        with open(media.subtitles[number], "rb") as handle:
            body = handle.read(_MAX_SUBTITLE_BYTES + 1)
        if len(body) > _MAX_SUBTITLE_BYTES:
            conn.sendall(_NOT_FOUND)
            raise _Refused("subtitle file too big")
        self._send_head(conn, "200 OK", [("Content-Type", "text/plain"),
                                         ("Content-Length", str(len(body)))])
        if not head_only:
            conn.sendall(body)
