"""Discord Rich Presence — show what you're watching on your profile.

Discord exposes a local IPC socket (a named pipe on Windows). Frames are a
4-byte little-endian opcode, a 4-byte little-endian length, then JSON:

    op 0  HANDSHAKE   {"v": 1, "client_id": "..."}
    op 1  FRAME       {"cmd": "SET_ACTIVITY", "args": {...}, "nonce": "..."}
    op 2  CLOSE

All pipe work happens on a worker thread, every read and write on the pipe has
a deadline, and every failure is swallowed: Discord being closed, hung, or
never installed must never disturb playback or keep Mistery from quitting.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import queue
import re
import struct
import sys
import threading
import time
import uuid

if sys.platform == "win32":
    # The same private-but-stable module multiprocessing uses for its own
    # pipes: it is the only way in the standard library to do overlapped I/O,
    # which is what lets a pipe read give up.
    import _winapi
else:
    _winapi = None

_log = logging.getLogger("discord")

_KEY_UNSAFE = re.compile(r"[^a-z0-9]+")


# Discord's Art Assets want 16:9, at least 512x288; 1024x576 is the size the
# upload page asks for. Nothing in a film library is that shape — posters are
# 2:3 and backdrops are usually 2.40:1 — so art is composed to fit rather than
# copied and left to be rejected or cropped by the uploader.
ART_SIZE = (1024, 576)


def compose_wide_art(source: str, destination: str, backdrop: str = "") -> bool:
    """Write a 1024x576 16:9 card. False if nothing could be read.

    The poster sits whole in the middle, over a blurred, darkened frame made
    from the backdrop where there is one and from the poster itself otherwise.
    Two reasons for that rather than just cropping the backdrop: a 16:9 crop of
    a portrait poster throws away the faces and the title, and Discord draws
    this small enough that a film still is often unrecognisable while a poster
    still reads. Every title ends up looking the same, too.
    """
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    except ImportError:
        _log.warning("Pillow is not installed; cannot build Discord art")
        return False

    width, height = ART_SIZE
    try:
        with Image.open(source) as opened:
            poster = opened.convert("RGB")

            background_source = poster
            opened_backdrop = None
            if backdrop:
                try:
                    opened_backdrop = Image.open(backdrop)
                    background_source = opened_backdrop.convert("RGB")
                except Exception:
                    background_source = poster

            canvas = ImageOps.fit(background_source, ART_SIZE, Image.Resampling.LANCZOS)
            canvas = canvas.filter(ImageFilter.GaussianBlur(24))
            canvas = ImageEnhance.Brightness(canvas).enhance(0.45)
            if opened_backdrop is not None:
                opened_backdrop.close()

            scale = height / poster.height
            fitted_width = max(1, round(poster.width * scale))
            if fitted_width >= width:
                # A source already at least 16:9 has nothing to frame.
                canvas = ImageOps.fit(poster, ART_SIZE, Image.Resampling.LANCZOS)
            else:
                canvas.paste(
                    poster.resize((fitted_width, height), Image.Resampling.LANCZOS),
                    ((width - fitted_width) // 2, 0),
                )
            canvas.save(destination, "PNG")
            return True
    except Exception as exc:
        _log.warning("could not build Discord art from %s: %s", source, exc)
        return False


def asset_key(title: str) -> str:
    """Turn a title into a Discord asset name, or '' if it can't be one.

    Discord asset names are lowercase, 2-32 characters, letters/digits/dashes.
    Deriving the name from the title means the file you upload and the key the
    player asks for line up without anything to configure.

    A longer name is cut to 25 characters and ends in six of a hash of the
    whole of it. A plain cut at 32 gave "Harry Potter and the Deathly Hallows
    Part 1" and "... Part 2" the same name, so the export kept one poster and
    both films showed it.
    """
    slug = _slug(title)
    if len(slug) > 32:
        digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:6]
        slug = f"{slug[:25].strip('-')}-{digest}"
    return slug if len(slug) >= 2 else ""


def _slug(title: str) -> str:
    return _KEY_UNSAFE.sub("-", (title or "").lower()).strip("-")


def _legacy_asset_key(title: str) -> str:
    """The name asset_key gave before long names carried a hash.

    Art already uploaded under it keeps working, so nobody has to export and
    upload again just because Mistery changed how it spells long titles.
    """
    slug = _slug(title)[:32].strip("-")
    return slug if len(slug) >= 2 else ""

OP_HANDSHAKE = 0
OP_FRAME = 1
OP_CLOSE = 2

_RECONNECT_DELAY = 30.0     # don't hammer the pipe when Discord is closed
# A rejected handshake (a mistyped Application ID) doubles the wait each time,
# up to this. Retrying every 30 s logged 120 warnings an hour for an ID that
# was never going to work; Settings already shows the reason.
_MAX_RECONNECT_DELAY = 30 * 60.0
_REPLY_TIMEOUT = 5.0        # Discord answers a handshake in milliseconds
_WRITE_TIMEOUT = 5.0
_ASSET_RETRY = 10 * 60.0    # after the art list could not be fetched
_STOP_WAIT = 0.5            # how long stop() lets the worker close its pipe
_POLL_MS = 100              # how often a pipe wait checks for stop()
_MAX_PIPES = 10


def _pipe_path(index: int) -> str:
    if sys.platform == "win32":
        return rf"\\.\pipe\discord-ipc-{index}"
    base = (os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("TMPDIR") or "/tmp")
    return os.path.join(base, f"discord-ipc-{index}")


class _Pipe:
    """One connection to Discord's named pipe, where nothing waits for good.

    It is opened overlapped, so every read and write can be abandoned, at its
    deadline or as soon as the run it belongs to is stopped. The pipe used to
    be a plain open() file. When Discord accepted the connection and never
    answered (hung, still starting, mid-update, or another program sitting on
    discord-ipc-0), the worker sat in read() with no way out, and the close()
    that stop() then made from the UI thread waited on that read. Quitting
    Mistery, or unticking presence, froze the window until Discord died.
    Only the worker thread that opened a pipe ever touches it now.
    """

    def __init__(self, path: str) -> None:
        self._handle = _winapi.CreateFile(
            path, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0, _winapi.NULL,
            _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL,
        )

    def _finish(self, overlapped, error: int, stop: threading.Event,
                deadline: float) -> int:
        """Wait out one overlapped call and return the bytes it moved."""
        if error == _winapi.ERROR_IO_PENDING:
            while _winapi.WaitForSingleObject(
                overlapped.event, _POLL_MS
            ) != _winapi.WAIT_OBJECT_0:
                if stop.is_set() or time.monotonic() >= deadline:
                    overlapped.cancel()
                    try:
                        overlapped.GetOverlappedResult(True)
                    except OSError:
                        pass
                    raise TimeoutError("Discord did not answer in time")
        transferred, error = overlapped.GetOverlappedResult(True)
        if error:
            raise OSError(f"Discord pipe error {error}")
        return transferred

    def write(self, data: bytes, stop: threading.Event, deadline: float) -> None:
        overlapped, error = _winapi.WriteFile(self._handle, data, overlapped=True)
        if self._finish(overlapped, error, stop, deadline) != len(data):
            raise OSError("short write to Discord")

    def read(self, size: int, stop: threading.Event, deadline: float) -> bytes:
        data = b""
        while len(data) < size:
            overlapped, error = _winapi.ReadFile(
                self._handle, size - len(data), overlapped=True
            )
            self._finish(overlapped, error, stop, deadline)
            chunk = bytes(overlapped.getbuffer())
            if not chunk:
                raise OSError("Discord closed the pipe")
            data += chunk
        return data

    def waiting(self) -> int:
        """Bytes Discord has sent that nobody has read. OSError once it's gone."""
        return _winapi.PeekNamedPipe(self._handle)[0]

    def close(self) -> None:
        try:
            _winapi.CloseHandle(self._handle)
        except OSError:
            pass


class _Run:
    """One start() to stop() of the worker, with its own flag and inbox.

    stop() can't always wait for the worker, which may be inside the
    discord.com art lookup for several seconds, so that thread is left to
    notice on its own. Presence turned straight back on used to find the
    shared running flag set again. The old thread carried on beside the new
    one, both took updates off one queue, and each overwrote the other's
    pipe. A run's state is its own now: an old thread sees only its own
    stopped flag, never takes a newer run's updates, and closes only the pipe
    it opened.
    """

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.inbox: queue.Queue = queue.Queue(maxsize=8)
        self.connected = False
        self.thread: threading.Thread | None = None


class DiscordPresence:
    """Fire-and-forget presence updates. Safe to call when Discord is absent."""

    def __init__(self, client_id: str) -> None:
        self.client_id = (client_id or "").strip()
        self._run: _Run | None = None
        self._next_attempt = 0.0
        self._rejections = 0
        self._reject_reason: str | None = None
        self._last_problem: str | None = None
        # Which art the application actually has. An asset key Discord doesn't
        # know shows *no* image at all rather than falling back, so per-title
        # posters are only used once we've confirmed they exist.
        self._assets: set[str] | None = None
        self._assets_retry_at = 0.0

    # --- lifecycle ----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.client_id)

    @property
    def connected(self) -> bool:
        run = self._run
        return run is not None and run.connected

    def start(self) -> None:
        if not self.enabled or self._run is not None:
            return
        self._next_attempt = 0.0            # turned on means try now
        run = _Run()
        run.thread = threading.Thread(
            target=self._loop, args=(run,), name="discord-rpc", daemon=True
        )
        self._run = run
        run.thread.start()

    def stop(self) -> None:
        """Ask the worker to finish. Never touches its pipe from this thread.

        Every wait in the worker checks the flag at least ten times a second,
        so it has normally closed the pipe before the short join is up. The
        exception is the discord.com art lookup, which can't be interrupted.
        That thread is left to finish the lookup and close its own pipe, which
        is safe because a later start() gets a run of its own.
        """
        run, self._run = self._run, None
        if run is None:
            return
        run.stop.set()
        try:
            run.inbox.put_nowait(None)          # wake it now, not at its next poll
        except queue.Full:
            pass
        if run.thread is not None and run.thread is not threading.current_thread():
            run.thread.join(timeout=_STOP_WAIT)

    # --- public API ---------------------------------------------------------

    def known_assets(self) -> set[str] | None:
        """The art uploaded to this application, or None if it can't be read.

        Discord publishes an application's asset list without authentication,
        so this needs no token and no permission from the user's account.

        Only a list that was actually read is kept. A failed request is tried
        again after _ASSET_RETRY: remembering the failure meant one update
        sent before the network was up (Mistery started at login, or just
        after resume) turned per-title art off until the next restart.
        """
        if self._assets is not None:
            return self._assets
        if not self.enabled or time.monotonic() < self._assets_retry_at:
            return None
        self._assets_retry_at = time.monotonic() + _ASSET_RETRY
        try:
            import requests

            response = requests.get(
                f"https://discord.com/api/v9/oauth2/applications/"
                f"{self.client_id}/assets",
                timeout=6,
            )
            if response.status_code != 200:
                _log.info("could not list Discord art (HTTP %s)", response.status_code)
                return None
            self._assets = {
                str(entry.get("name", "")).lower()
                for entry in response.json()
                if entry.get("name")
            }
            _log.info("Discord application has %d image(s)", len(self._assets))
        except Exception as exc:                # never disturb playback
            _log.info("could not list Discord art: %s", exc)
            return None
        return self._assets

    def _resolve_art(self, payload: dict) -> dict:
        """Downgrade a per-title key to the app icon unless it really exists.

        Works on a copy. The update is kept after it's sent, to be sent again
        if Discord restarts, and it must still carry the per-title key then:
        by that time the art list may have become readable.
        """
        activity = (payload.get("args") or {}).get("activity")
        if not isinstance(activity, dict):
            return payload
        key = (activity.get("assets") or {}).get("large_image")
        if not key or key == "mistery":
            return payload
        payload = copy.deepcopy(payload)
        assets = payload["args"]["activity"]["assets"]
        known = self.known_assets()
        # Unknown list means we could not check — fall back to the icon we know
        # is configured rather than gamble on a blank card.
        if known is None:
            assets["large_image"] = "mistery"
        elif key not in known:
            # Both callers derive the key from the text shown on the image
            # (the film, show or album), so the old spelling of that same
            # title can still be found among art uploaded before long names
            # carried a hash.
            text = str(assets.get("large_text") or "")
            legacy = _legacy_asset_key(text) if asset_key(text) == key else ""
            assets["large_image"] = legacy if legacy and legacy in known else "mistery"
        return payload

    def set_watching(
        self,
        title: str,
        subtitle: str = "",
        remaining: float | None = None,
        paused: bool = False,
        image_text: str = "",
        image_key: str = "",
    ) -> None:
        """Show a title. `remaining` drives Discord's countdown."""
        if not self.enabled:
            return
        activity: dict = {
            "type": 3,                       # Watching
            "details": (title or "Something")[:128],
        }
        state = subtitle.strip() if subtitle else ""
        if paused:
            state = f"Paused — {state}" if state else "Paused"
        if state:
            activity["state"] = state[:128]
        if remaining and remaining > 0 and not paused:
            activity["timestamps"] = {"end": int(time.time() + remaining)}
        activity["assets"] = {
            "large_image": (image_key or "").lower() or "mistery",
            "large_text": (image_text or "Mistery")[:128],
        }
        self._send({
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(uuid.uuid4()),
        })

    def set_listening(
        self,
        title: str,
        artist: str = "",
        album: str = "",
        remaining: float | None = None,
        paused: bool = False,
        image_key: str = "",
    ) -> None:
        """'Listening to' — Discord's type 2, shown with the song and a countdown."""
        if not self.enabled:
            return
        activity: dict = {"type": 2, "details": (title or "Music")[:128]}
        state = artist.strip() if artist else ""
        if paused:
            state = f"Paused — {state}" if state else "Paused"
        if state:
            activity["state"] = state[:128]
        if remaining and remaining > 0 and not paused:
            activity["timestamps"] = {"end": int(time.time() + remaining)}
        activity["assets"] = {
            "large_image": (image_key or "").lower() or "mistery",
            "large_text": (album or "Mistery")[:128],
        }
        self._send({
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": activity},
            "nonce": str(uuid.uuid4()),
        })

    def clear(self) -> None:
        if not self.enabled:
            return
        self._send({
            "cmd": "SET_ACTIVITY",
            "args": {"pid": os.getpid(), "activity": None},
            "nonce": str(uuid.uuid4()),
        })

    def _send(self, payload: dict) -> None:
        run = self._run
        if run is None:
            # Presence is off. Queueing anyway left a backlog that was played
            # back to Discord, oldest first, when it was turned on again.
            return
        try:
            run.inbox.put_nowait(payload)
        except queue.Full:
            # Presence is cosmetic; drop the oldest rather than block playback.
            try:
                run.inbox.get_nowait()
                run.inbox.put_nowait(payload)
            except (queue.Empty, queue.Full):
                pass

    # --- worker -------------------------------------------------------------

    def _loop(self, run: _Run) -> None:
        latest: dict | None = None      # waiting to be sent
        shown: dict | None = None       # the activity Discord last accepted
        pipe: _Pipe | None = None
        try:
            while not run.stop.is_set():
                try:
                    payload = run.inbox.get(timeout=0.5)
                except queue.Empty:
                    payload = None
                if payload is not None:
                    latest = payload

                if pipe is not None and not self._still_open(pipe, run):
                    # Discord quit or restarted, as it does to update and
                    # around sleep. The activity went with the old connection,
                    # so the profile stays blank until it is sent again.
                    # Nothing noticed before the next write failed, and for a
                    # film that is the next pause, maybe two hours later.
                    _log.info("Discord closed the connection; will reconnect")
                    pipe.close()
                    pipe, run.connected = None, False
                    if latest is None and shown is not None:
                        latest = dict(shown, nonce=str(uuid.uuid4()))

                if latest is None or run.stop.is_set():
                    continue
                if pipe is None:
                    pipe = self._connect(run)
                    if pipe is None:
                        continue            # retry later, keep `latest` pending
                    run.connected = True
                # Resolved here, on the worker: looking the art up is a network call.
                frame = self._resolve_art(latest)
                if run.stop.is_set():
                    break
                try:
                    self._write(pipe, OP_FRAME, frame, run)
                except OSError:
                    _log.debug("Discord write failed; will reconnect")
                    pipe.close()
                    pipe, run.connected = None, False
                    continue
                # A cleared activity needs nothing sent after a restart.
                has_activity = (latest.get("args") or {}).get("activity") is not None
                shown, latest = (latest if has_activity else None), None
        finally:
            run.connected = False
            if pipe is not None:
                if run.stop.is_set():
                    self._flush_clear(pipe, run, latest)
                pipe.close()

    def _flush_clear(self, pipe: _Pipe, run: _Run, latest: dict | None) -> None:
        """Send the clear() that came just before stop(), if it was the last word.

        Unticking presence and quitting both clear and then stop at once, and
        the stop flag ends the loop before it gets to the clear. Discord drops
        a closed connection's activity anyway; this makes it explicit. A write
        that can't complete straight away is abandoned (the flag is set), so
        this never holds the worker up.
        """
        while True:
            try:
                queued = run.inbox.get_nowait()
            except queue.Empty:
                break
            if queued is not None:
                latest = queued
        if latest is None or (latest.get("args") or {}).get("activity") is not None:
            return
        try:
            self._write(pipe, OP_FRAME, latest, run)
        except OSError:
            pass

    def _still_open(self, pipe: _Pipe, run: _Run) -> bool:
        """False once Discord has closed its end. Reads what it sent meanwhile.

        Discord answers every SET_ACTIVITY. Nothing here needs the answers,
        but left unread they fill the pipe's buffer.
        """
        try:
            waiting = pipe.waiting()
            if waiting:
                pipe.read(min(waiting, 65536), run.stop,
                          time.monotonic() + _REPLY_TIMEOUT)
            return True
        except OSError:
            return False

    def _connect(self, run: _Run) -> _Pipe | None:
        now = time.monotonic()
        if _winapi is None or now < self._next_attempt:
            return None
        self._next_attempt = now + _RECONNECT_DELAY

        for index in range(_MAX_PIPES):
            path = _pipe_path(index)
            try:
                pipe = _Pipe(path)
            except OSError:
                continue
            try:
                self._write(pipe, OP_HANDSHAKE, {"v": 1, "client_id": self.client_id}, run)
                opcode, reply = self._read(pipe, run)
            except (OSError, ValueError) as exc:
                pipe.close()
                if run.stop.is_set():
                    return None
                # Accepted and then silent or garbled: Discord hung, starting
                # or updating, or another program holding this name. The next
                # pipe may be the real Discord.
                self._problem(f"no answer to the Discord handshake on {path}: {exc}")
                continue
            # A good handshake comes back as a FRAME carrying evt=READY.
            # A rejection (bad client id) arrives as CLOSE with a reason.
            if opcode == OP_FRAME and (reply or {}).get("evt") == "READY":
                _log.info("connected to Discord on %s", path)
                self._rejections = 0
                self._reject_reason = None
                self._last_problem = None
                return pipe
            pipe.close()
            if opcode == OP_CLOSE:
                self._reject_reason = str((reply or {}).get("message") or "rejected")
                self._rejections += 1
                self._next_attempt = time.monotonic() + min(
                    _RECONNECT_DELAY * 2 ** min(self._rejections, 10),
                    _MAX_RECONNECT_DELAY,
                )
                self._problem(f"Discord rejected the handshake: {self._reject_reason}")
                return None
            self._problem(f"unexpected Discord handshake reply: op={opcode} {reply}")
        return None

    def _problem(self, text: str) -> None:
        """Log a connection problem when it changes, not on every retry."""
        if text != self._last_problem:
            self._last_problem = text
            _log.warning("%s", text)
        else:
            _log.debug("%s", text)

    def _write(self, pipe: _Pipe, opcode: int, payload: dict, run: _Run) -> None:
        body = json.dumps(payload).encode("utf-8")
        pipe.write(struct.pack("<II", opcode, len(body)) + body, run.stop,
                   time.monotonic() + _WRITE_TIMEOUT)

    def _read(self, pipe: _Pipe, run: _Run) -> tuple[int, dict | None]:
        """(opcode, payload). Opcode matters: CLOSE means we were rejected."""
        deadline = time.monotonic() + _REPLY_TIMEOUT
        opcode, length = struct.unpack("<II", pipe.read(8, run.stop, deadline))
        if length > 1 << 20:
            raise ValueError(f"a {length}-byte frame is not from Discord")
        body = pipe.read(length, run.stop, deadline) if length else b"{}"
        reply = json.loads(body.decode("utf-8", "replace"))
        return opcode, reply if isinstance(reply, dict) else None
