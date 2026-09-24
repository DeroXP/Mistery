"""Discord Rich Presence — show what you're watching on your profile.

Discord exposes a local IPC socket (a named pipe on Windows). Frames are a
4-byte little-endian opcode, a 4-byte little-endian length, then JSON:

    op 0  HANDSHAKE   {"v": 1, "client_id": "..."}
    op 1  FRAME       {"cmd": "SET_ACTIVITY", "args": {...}, "nonce": "..."}
    op 2  CLOSE

All pipe work happens on a worker thread, every read and write on the pipe has
a deadline, and every failure is swallowed: Discord being closed, hung, or
never installed must never disturb playback or keep Mistery from quitting.

The picture on the card is settled in _resolve_art. It can be a public https
URL — Discord's own media proxy fetches the picture, so artwork that already
has a public address (TMDB, TVmaze, Wikimedia) needs nothing uploaded — or the
name of an image uploaded to the application by hand, which is still the only
way to show art Mistery composed itself out of a film's own frames.
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
import urllib.parse
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

# What an uploaded image can be called: Discord's Art Assets page accepts 2-32
# characters of lowercase letters, digits, dashes and underscores. Anything
# else in the picture field is either a URL or nothing we could ask for, and
# knowing the difference saves asking discord.com about a value it can't have.
_KEY_SHAPE = re.compile(r"^[a-z0-9_-]{2,32}$")

# The longest picture URL that may go to Discord. The three places Mistery
# gets artwork from are short: a TMDB poster is about 67 characters, a TVmaze
# still about 70, and the worst Wikimedia thumbnail this library could produce
# — the file name carries the percent-encoded title twice — measured 188
# across 257 titles. 512 is nearly three times that, so nothing real is cut,
# while a runaway string can't bloat the frame or fill Discord's logs.
MAX_IMAGE_URL = 512

# Spaces, tabs, newlines and control characters have no business in a URL:
# they would either break the JSON frame or smuggle a second line into it.
_URL_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f]")

# A host name the rest of the world could look up. Written as "must end in a
# dotted name with a letters-only ending" so that "localhost", "127.0.0.1",
# "[::1]", "nas", "printer.lan" and "pc.local" all fail: Discord's proxy could
# never fetch them, and the name alone would say something about the owner's
# own network.
_PUBLIC_HOST = re.compile(
    r"^(?!.*\.(local|lan|internal|intranet|home\.arpa)$)"
    r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*"
    r"\.[a-z]{2,}$",
    re.I,
)


def public_image_url(value: str) -> str:
    """The value as a picture URL fit to hand Discord, or '' if it isn't one.

    Discord's media proxy fetches whatever URL the activity carries, so this
    string leaves the PC and is read by Discord's servers. Only something
    already published on the open internet may go, and only in a shape that
    can't carry anything else:

      * https only. http would be fetched in the clear, and a Windows path or
        a file:// URL names something on this machine, not on the web.
      * a host the world can resolve — no localhost, no LAN name, no bare IP.
      * no user:password@ in front of the host: that is a credential.
      * no query string and no fragment. All three artwork sources serve
        pictures from a plain path, so a '?' here would only ever be an API
        key, a signed link or a session token riding along.
      * at most MAX_IMAGE_URL characters, and no whitespace or control bytes.

    Returned exactly as given, never lowercased: TMDB and Wikimedia paths are
    case-sensitive and a flattened one is a 404.
    """
    text = str(value or "").strip()
    if not text or len(text) > MAX_IMAGE_URL or _URL_UNSAFE.search(text):
        return ""
    if not text.lower().startswith("https://"):
        return ""
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:                  # a malformed IPv6 literal, say
        return ""
    if parts.scheme != "https" or parts.query or parts.fragment:
        return ""
    if "@" in parts.netloc:
        return ""
    try:
        host = parts.hostname or ""
    except ValueError:
        return ""
    return text if _PUBLIC_HOST.match(host) else ""


def _asset_match(key: str, known: set[str]) -> str:
    """The name Discord holds for this key, spelled its way, or ''.

    Ignoring case belongs here and nowhere else. The keys Mistery builds are
    lowercase (asset_key), the names Discord reports are kept as it reports
    them, and what goes back on the card has to be the name it actually has —
    so the two are compared without case and Discord's spelling is what is
    sent. The exact hit is tried first; the walk only happens on a miss, over
    a few hundred names at most.
    """
    if not key or not known:
        return ""
    if key in known:
        return key
    lowered = key.lower()
    return next((name for name in known if name.lower() == lowered), "")


def _image_of(payload: dict) -> str:
    """The picture an outgoing frame carries, for comparing two of them."""
    activity = (payload.get("args") or {}).get("activity")
    if not isinstance(activity, dict):
        return ""
    return str((activity.get("assets") or {}).get("large_image") or "")


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


def album_asset_key(artist: str, album: str) -> str:
    """The asset name for an album's cover: "alb-", the album artist, the title.

    Songs used to ask for asset_key(album) alone, and nothing was ever uploaded
    under it, so it never mattered that two Greatest Hits by two artists would
    share one picture, or that an album named like a film ("Purple Rain") would
    take the film's poster or give its own away. Covers are uploaded now, so the
    artist keeps albums apart and "alb" keeps them apart from films. Films keep
    their plain names on purpose: the owner has already uploaded those.
    """
    return asset_key(f"alb {artist or ''} {album or ''}")


def list_assets(client_id: str, timeout: float = 6.0) -> set[str] | None:
    """The images uploaded to a Discord application, as Discord spells them.

    None when the list cannot be read. Discord publishes it without
    authentication, so this needs no token and nothing of the owner's account;
    it is the same list DiscordPresence.known_assets keeps, fetched once for
    the export, which asks it what is already there before writing anything.
    """
    if not client_id:
        return None
    try:
        import requests

        response = requests.get(
            f"https://discord.com/api/v9/oauth2/applications/{client_id}/assets",
            timeout=timeout)
        if response.status_code != 200:
            return None
        listed = response.json()
    except Exception as exc:                    # the export must still work offline
        _log.info("could not list Discord art: %s", exc)
        return None
    if not isinstance(listed, list):
        return None
    return {str(entry.get("name") or "") for entry in listed
            if isinstance(entry, dict) and entry.get("name")}


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
# How long a fetched list of uploaded art is trusted, and how long to wait
# before asking again after a fetch that failed. The same ten minutes for both,
# for one reason: art is uploaded by hand in a browser and then the owner comes
# back to Mistery, so noticing it within ten minutes is soon enough, while an
# evening of playback asks discord.com at most six times an hour. It used to be
# fetched once and kept for the life of the object, so art uploaded after
# Mistery started never showed up until it was restarted.
_ASSET_TTL = 10 * 60.0
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
        # The tail of a reply frame that hasn't all arrived yet. Replies are
        # drained in whatever size the pipe hands over, so the last one is
        # regularly cut in half; kept here until the rest turns up.
        self.spare = b""

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
        self._assets_at = 0.0           # when the list we have was read
        self._assets_retry_at = 0.0
        # Kept on the object rather than read straight from the module so a
        # test can turn them down and watch a refresh happen.
        self.asset_ttl = _ASSET_TTL
        self.asset_retry = _ASSET_RETRY
        # Sending a picture URL down this pipe is not something Discord
        # documents — the documented example goes through its embedded SDK —
        # so if it ever answers that it could not use one, URLs are given up
        # for the life of this object and the card falls back to uploaded art.
        # _url_nonce is the SET_ACTIVITY we are waiting to hear about.
        self._url_art = True
        self._url_nonce = ""
        # Which pipes to look for Discord on. None means the real
        # discord-ipc-0..9; a test points this at a pipe of its own so nothing
        # can reach the owner's running Discord.
        self.pipe_paths: list[str] | None = None

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

        The list is re-read every asset_ttl (ten minutes — see the constant),
        not kept for good: uploading a poster and then finding Mistery still
        showing the plain icon until the next restart was the whole reason the
        export felt like a chore. A read that fails leaves the last good list
        in place rather than blanking the card, and is not tried again for
        asset_retry, so a Discord that is down or an internet connection that
        isn't up yet costs one request every ten minutes and never spins.
        """
        now = time.monotonic()
        if self._assets is not None and now - self._assets_at < self.asset_ttl:
            return self._assets
        if not self.enabled or now < self._assets_retry_at:
            return self._assets         # stale art beats no art
        self._assets_retry_at = now + self.asset_retry
        try:
            import requests

            response = requests.get(
                f"https://discord.com/api/v9/oauth2/applications/"
                f"{self.client_id}/assets",
                timeout=6,
            )
            if response.status_code != 200:
                _log.info("could not list Discord art (HTTP %s)", response.status_code)
                return self._assets
            listed = response.json()
            if not isinstance(listed, list):
                _log.info("Discord answered the art list with %s", type(listed).__name__)
                return self._assets
            # Kept exactly as Discord spells them. This was the third place
            # that flattened a picture value, and the one that was hardest to
            # see: the name sent back has to be the name Discord holds, so
            # flattening the list here meant asking for a name that might not
            # be the one it has. _asset_match does the comparison without case
            # instead, which is where ignoring case belongs.
            self._assets = {
                str(entry.get("name") or "")
                for entry in listed
                if isinstance(entry, dict) and entry.get("name")
            }
            self._assets_at = now
            # The retry wait is for a read that failed. It is armed before the
            # request so that one which throws is covered too, and disarmed
            # here, so a read that worked is governed by asset_ttl alone. It
            # made no difference while the two numbers were both ten minutes,
            # but it meant turning asset_ttl down after the first read — the
            # only way to watch a refresh happen — bought nothing: the good
            # read had already booked the next ten minutes.
            self._assets_retry_at = now
            _log.info("Discord application has %d image(s)", len(self._assets))
        except Exception as exc:                # never disturb playback
            _log.info("could not list Discord art: %s", exc)
        return self._assets

    def _resolve_art(self, payload: dict) -> dict:
        """Settle what picture the card carries. Three rungs, in this order:

          1. a public https URL, sent exactly as given. Discord's media proxy
             fetches the picture itself, so artwork that already lives at a
             public address needs nothing uploaded anywhere.
          2. otherwise the name of an image the application really has. An
             asset name Discord doesn't know shows *no* picture at all rather
             than falling back, so a name is only sent once it is confirmed.
             This is still the only rung art Mistery composed out of a film's
             own frames can reach: that art has no public address.
          3. otherwise "mistery", the icon Settings tells you to upload.

        Works on a copy and changes nothing about this object: the worker
        re-runs it on the activity already showing to notice new art, so it
        has to be safe to call over and over. The update is kept after it is
        sent, to be sent again if Discord restarts, and it must still carry
        the original value then — by that time the art list may have become
        readable, or a URL may have started working.
        """
        activity = (payload.get("args") or {}).get("activity")
        if not isinstance(activity, dict):
            return payload
        # As a string whatever it is: this runs on the worker, and a type error
        # here would end the thread and take presence with it until restart.
        value = str((activity.get("assets") or {}).get("large_image") or "")
        if not value or value == "mistery":
            return payload
        payload = copy.deepcopy(payload)
        assets = payload["args"]["activity"]["assets"]

        # Rung 1.
        url = public_image_url(value) if self._url_art else ""
        if url:
            assets["large_image"] = url
            return payload

        # Rung 2. A value that is not a URL we can send and not shaped like an
        # asset name — a rejected URL, most often — leaves nothing to ask for,
        # so fall back on the name derived from the text on the card. Both
        # callers build that text from the same title the name comes from.
        text = str(assets.get("large_text") or "")
        key = value if _KEY_SHAPE.match(value) else asset_key(text)
        known = self.known_assets()
        if known is None:
            # Could not check — show the icon we know is configured rather
            # than gamble on a blank card.
            assets["large_image"] = "mistery"
        else:
            # Art uploaded before long names carried a hash is still found
            # under the old spelling of the same title.
            legacy = _legacy_asset_key(text) if asset_key(text) == key else ""
            assets["large_image"] = (_asset_match(key, known)
                                     or _asset_match(legacy, known)
                                     or "mistery")
        return payload

    def set_watching(
        self,
        title: str,
        subtitle: str = "",
        remaining: float | None = None,
        paused: bool = False,
        image_text: str = "",
        image: str = "",
    ) -> None:
        """Show a title. `remaining` drives Discord's countdown.

        `image` is a public https URL or the name of an uploaded image;
        _resolve_art picks between them and falls back to the icon.
        """
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
            # Passed on as written. This used to be lowercased, which is fine
            # for an asset name (asset_key builds those lowercase anyway) but
            # destroys a URL: TMDB and Wikimedia paths are case-sensitive, so
            # a flattened one fetches nothing at all.
            "large_image": (image or "").strip() or "mistery",
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
        image: str = "",
    ) -> None:
        """'Listening to' — Discord's type 2, shown with the song and a countdown.

        `image` is an uploaded image's name today; it goes through the same
        ladder as a film's, so a URL would work here too when something starts
        passing one.
        """
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
            # Written through unchanged, for the reason in set_watching.
            "large_image": (image or "").strip() or "mistery",
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
        shown_image = ""                # the picture that went with it
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

                if latest is None and shown is not None and pipe is not None:
                    # Nothing new to say, so check whether what is already on
                    # the profile would resolve to a different picture now:
                    # a poster uploaded during the film (known_assets re-reads
                    # every ten minutes), an art list that has become readable,
                    # or a URL Discord has just told us it could not use. Only
                    # a real change puts a frame on the pipe.
                    if _image_of(self._resolve_art(shown)) != shown_image:
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
                image = _image_of(frame)
                # Which SET_ACTIVITY to listen for an error about. Only a frame
                # carrying a URL is worth watching: that is the part Discord
                # has never documented for this transport.
                self._url_nonce = (str(frame.get("nonce") or "")
                                   if image[:8].lower() == "https://" else "")
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
                shown_image = image if has_activity else ""
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

        Discord answers every SET_ACTIVITY, and left unread the answers fill
        the pipe's buffer. One of them is worth reading: see _read_replies.
        """
        try:
            waiting = pipe.waiting()
            if waiting:
                pipe.spare += pipe.read(min(waiting, 65536), run.stop,
                                        time.monotonic() + _REPLY_TIMEOUT)
                pipe.spare = self._read_replies(pipe.spare)
            return True
        except OSError:
            return False

    def _read_replies(self, buffer: bytes) -> bytes:
        """Take whole reply frames off the buffer; return the unfinished tail.

        Only one answer changes anything: an ERROR against the SET_ACTIVITY
        that carried a picture URL. Putting a URL in the activity is
        documented, but not over this pipe — the documented example goes
        through Discord's embedded SDK — so if it ever comes back refused,
        URLs are given up and _resolve_art drops to uploaded art instead of
        leaving the card blank. Matched on the nonce, so an error about some
        other frame is not mistaken for this one.

        Discord accepting the activity is no proof the picture drew, of
        course; nothing on this pipe can tell us that. The export button is
        still there for a title that never comes out right.
        """
        while len(buffer) >= 8:
            opcode, length = struct.unpack("<II", buffer[:8])
            if length > 1 << 20:
                return b""              # not Discord talking; drop the lot
            if len(buffer) < 8 + length:
                return buffer           # the rest is still on its way
            body, buffer = buffer[8:8 + length], buffer[8 + length:]
            if opcode != OP_FRAME or not self._url_nonce:
                continue
            try:
                reply = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                continue
            if (isinstance(reply, dict) and reply.get("evt") == "ERROR"
                    and reply.get("nonce") == self._url_nonce):
                self._url_art = False
                self._url_nonce = ""
                _log.warning(
                    "Discord would not take a picture URL (%s); using uploaded "
                    "art from now on",
                    (reply.get("data") or {}).get("message") or "no reason given",
                )
        return buffer

    def _connect(self, run: _Run) -> _Pipe | None:
        now = time.monotonic()
        if _winapi is None or now < self._next_attempt:
            return None
        self._next_attempt = now + _RECONNECT_DELAY

        paths = self.pipe_paths
        if paths is None:
            paths = [_pipe_path(index) for index in range(_MAX_PIPES)]
        for path in paths:
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
