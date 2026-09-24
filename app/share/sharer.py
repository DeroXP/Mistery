"""Serving friends: inside Mistery, and while Mistery is closed.

A friend can ask for something at any hour, so the listener cannot belong to a
window. This module owns it in both places it lives:

  - **Inside Mistery**, from the moment sharing is on with at least one friend.
  - **Without Mistery**, as `Mistery.exe --share`: the same program with no
    window, started when Windows signs the owner in. The same program on
    purpose — Windows Firewall asks about a program, once, and a separate one
    would ask again with nobody at the screen to answer.

Only one of the two holds the port at a time, and no message passes between
them. The background one watches for a Mistery starting (config.app_is_running,
the lock the app writes) and lets go within a second; when Mistery closes it
takes the port back. Neither has to know the other exists, and a crash on
either side leaves nothing to clean up but a port nobody is listening on.

A movie night needs that same port, and a port has one listener. So while
sharing holds it, a movie night borrows the listener (PartyServer.host_party)
instead of opening its own, and sharing does not let go of it until the movie
night is over, even if it is switched off in the meantime.

While a friend is actually fetching something the PC is kept awake — only then,
and only if the owner left that on: a PC that never sleeps costs somebody real
money, and a film stopping half way because the host's PC dozed off is the
thing this avoids.
"""

from __future__ import annotations

import logging
import threading
import time

from .. import db
from ..config import app_is_running, settings
from ..party import upnp
from ..party.server import PartyServer, ServerError
from . import identity as ident
from . import pairing
from . import server as share_server

_log = logging.getLogger("share")

PORT_POLL = 1.0                 # how often the background one looks for Mistery
AWAKE_POLL = 20.0               # how often it asks whether anybody is fetching
MAPPING_LEASE = 12 * 3600       # the router forward, renewed while sharing runs
PORT_RETRY = 0.25               # while the background one lets go of the port


class Sharer:
    """The listener friends reach, and everything that keeps it reachable."""

    def __init__(self, *, bind: str = "0.0.0.0", background: bool = False) -> None:
        self.bind = bind
        self.background = background
        self.listener: PartyServer | None = None
        self.mapping = None
        self.error: str | None = None
        self._lock = threading.Lock()
        self._keeper: threading.Thread | None = None
        self._stopping = threading.Event()
        self._awake = False
        self._stop_after_party = False
        self.lan_ip: str | None = None
        self.wan_ip: str | None = None
        # Told, from the listener's thread, when somebody used this PC's code:
        # callables taking the new friend's id. The Friends page is one.
        self.on_paired: list = []

    # --- starting and stopping ----------------------------------------------

    @property
    def running(self) -> bool:
        return self.listener is not None and self.listener.running

    @staticmethod
    def wanted() -> bool:
        """Whether there is anything to serve: sharing on with a friend to serve,
        or a friend code out in somebody's chat, which only works while this
        PC is listening for it."""
        if pairing.pending_secret() is not None:
            return True
        return bool(settings.get("sharing_enabled")) and bool(db.friends())

    def start(self, wait: float = 0.0) -> bool:
        """Open the port and start answering friends. False if nothing to do,
        or the port could not be had (self.error says why).

        `wait` is how long to keep trying for a port that is taken: the app
        passes a few seconds at launch, which is how long the background one
        can take to notice it and let go. Safe to call again: a running sharer
        simply says yes.
        """
        give_up = time.monotonic() + max(0.0, wait)
        while True:
            with self._lock:
                if self.running:
                    return True
                if not self.wanted():
                    return False
                port = int(settings.get("party_port", 42170) or 42170)
                me = ident.identity()
                listener = PartyServer(me, b"", port, bind=self.bind,
                                       context=me.server_context(db.friend_certificates()))
                listener.share_handler = share_server.make_handler(listener)
                try:
                    listener.start()
                except ServerError as problem:
                    self.error = str(problem)
                    listener = None
                else:
                    self._begin(listener)
                    return True
            if time.monotonic() >= give_up:
                _log.warning("share: cannot listen on %d: %s", port, self.error)
                return False
            time.sleep(PORT_RETRY)

    def _begin(self, listener: PartyServer) -> None:
        """A listener that has the port. With the lock held."""
        self.listener = listener
        self.error = None
        self._stop_after_party = False
        if self.lan_ip is None:
            self.lan_ip = upnp.lan_ip()
        # What pairing tells a new friend about where to find this PC.
        listener.lan_ip, listener.wan_ip = self.lan_ip, self.wan_ip
        listener.on_paired = self._paired
        self._stopping.clear()
        self._keeper = threading.Thread(target=self._keep, name="share-keeper", daemon=True)
        self._keeper.start()
        _log.info("share: listening on %d for %d friend(s)%s",
                  listener.port, len(db.friends()), " (no window)" if self.background else "")

    def stop(self, *, force: bool = False) -> None:
        """Close the port and stop answering. The friends stay friends.

        Not while a movie night is using the listener: its guests would be cut
        off mid-film. The stop happens when that movie night ends instead.
        `force` closes it anyway, which only quitting does, after the movie
        night has been ended.
        """
        with self._lock:
            listener = self.listener
            if listener is not None and listener.hosting and not force:
                if not self._stop_after_party:
                    _log.info("share: will stop listening when the movie night ends")
                self._stop_after_party = True
                listener.after_party = self._party_over
                return
            self.listener = None
            self._stop_after_party = False
            self._stopping.set()
        if listener is not None:
            listener.stop()
            _log.info("share: stopped listening")
        self._hold_awake(False)
        mapping, self.mapping = self.mapping, None
        if mapping is not None:
            upnp.close_port(mapping)

    def _party_over(self) -> None:
        """A movie night on this listener ended: a stop held back for it now
        happens, unless sharing is wanted again by now."""
        if self._stop_after_party and not self.wanted():
            self.stop()
        self._stop_after_party = False

    def _paired(self, friend_id: int) -> None:
        """Somebody used this PC's code and is a friend now: the listener has
        to know their certificate before they next call."""
        self.friends_changed()
        for told in list(self.on_paired):
            try:
                told(friend_id)
            except Exception:                   # noqa: BLE001 - a page, not the listener
                _log.exception("share: telling about a new friend failed")

    def lend(self) -> PartyServer | None:
        """The listener, for a movie night to borrow; None when not listening.

        The movie night calls host_party on it, and end_party when it is over.
        """
        with self._lock:
            listener = self.listener
            return listener if listener is not None and listener.running else None

    def note_addresses(self, lan_ip: str | None, wan_ip: str | None) -> None:
        """Where friends can reach this PC, as the Friends page last found out.
        Pairing sends them to a new friend with the welcome."""
        self.lan_ip = lan_ip or self.lan_ip
        self.wan_ip = wan_ip or self.wan_ip
        listener = self.listener
        if listener is not None:
            listener.lan_ip, listener.wan_ip = self.lan_ip, self.wan_ip

    def friends_changed(self) -> None:
        """A friend was added, removed or paused: rebuild the trust store.

        The certificates a listener accepts are settled when its context is
        built, so this makes a new one. Connections already open are left to
        finish; a removed friend's are cut off by withdraw_all and the refusal
        their next request gets. Also where a code being made or used up, or
        sharing being switched on or off, starts or stops the listener; and
        where the sign-in entry that serves friends while Mistery is closed
        follows along (background.sync).
        """
        from . import background

        self._follow_friends()
        background.sync()

    def _follow_friends(self) -> None:
        with self._lock:
            listener = self.listener
        if listener is None:
            if self.wanted():
                self.start()
            return
        # First, whatever happens next: a stop held back for a movie night
        # leaves the listener up, and a friend removed meanwhile must already
        # be a stranger to it.
        listener._context = ident.identity().server_context(db.friend_certificates())
        known = {row["id"] for row in db.friends()}
        for offer in list(getattr(listener, "_offers", {}).values()):
            if offer.friend_id not in known:
                listener.withdraw_all(offer.friend_id)
        if not self.wanted():
            self.stop()
        else:
            self._stop_after_party = False

    # --- keeping it reachable, and the PC awake ------------------------------

    def _keep(self) -> None:
        """One thread: the router forward, and the PC awake while friends watch."""
        next_mapping = 0.0
        while not self._stopping.wait(AWAKE_POLL if self.mapping else 1.0):
            listener = self.listener
            if listener is None:
                break
            now = time.monotonic()
            if settings.get("party_upnp", True) and now >= next_mapping:
                next_mapping = now + MAPPING_LEASE / 2
                self._ask_router(listener.port)
            # A friend fetching a film, or a movie night on this listener (a
            # friend's on this PC's film, app/share/nights.py, or the owner's).
            self._hold_awake(bool(getattr(listener, "_offers", {}) or listener.hosting)
                             and bool(settings.get("sharing_keep_awake", True)))

    def _ask_router(self, port: int | None) -> None:
        if not port:
            return
        try:
            self.mapping = upnp.open_port(port, lease=MAPPING_LEASE)
            _log.info("share: the router is forwarding %d to this PC", port)
        except upnp.UpnpError as problem:
            # Most routers here have UPnP off, and a forward made by hand works
            # just as well: this is a try, not a requirement.
            _log.debug("share: the router did not open %d: %s", port, problem)
        except Exception as problem:                # noqa: BLE001
            _log.debug("share: asking the router failed: %s", problem)

    def _hold_awake(self, wanted: bool) -> None:
        """Keep the PC awake while somebody is fetching, and only then."""
        if wanted == self._awake:
            return
        if _set_awake(wanted):
            self._awake = wanted
            _log.info("share: %s the PC awake for a friend",
                      "keeping" if wanted else "no longer keeping")


def _set_awake(wanted: bool) -> bool:
    """Ask Windows to stay awake (or stop asking). True when it was told."""
    try:
        import ctypes

        # ES_CONTINUOUS keeps the request until it is cleared; ES_SYSTEM_REQUIRED
        # stops the PC sleeping. The display is left alone on purpose: nobody is
        # watching this screen, and a monitor kept on all night for a friend's
        # film is exactly the thing an OLED owner does not want.
        state = 0x80000000 | (0x00000001 if wanted else 0)
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(state))
    except (AttributeError, OSError):
        return False                                # not Windows, or not allowed


# --- the one in the app -------------------------------------------------------

_sharer: Sharer | None = None


def sharer(*, bind: str = "0.0.0.0", background: bool = False) -> Sharer:
    global _sharer
    if _sharer is None:
        _sharer = Sharer(bind=bind, background=background)
    return _sharer


def current() -> Sharer | None:
    """The one in this process, if anything has made it. Never makes one."""
    return _sharer


def start_if_wanted(wait: float = 0.0) -> bool:
    """Called when Mistery starts, when a movie night gives the port back, and
    whenever sharing or friends change. Blocks for up to `wait` seconds while
    the port is taken, so not on Qt's thread with a wait."""
    return sharer().start(wait) if Sharer.wanted() else False


def stop(*, force: bool = False) -> None:
    if _sharer is not None:
        _sharer.stop(force=force)


def forget() -> None:
    """Drop the one in this process. Tests."""
    global _sharer
    stop(force=True)
    _sharer = None


# --- the one without the app --------------------------------------------------

def run_background(*, poll: float = PORT_POLL, until: float | None = None,
                   bind: str = "0.0.0.0", stop=None) -> int:
    """Serve friends while Mistery is closed. Returns when told to stop.

    Started at sign-in (app/share/background.py), and it spends almost all of
    its life asleep in this loop. It holds the port only while Mistery itself
    does not: the app is the one with a window, a library being scanned and a
    person in front of it, so it wins every time.

    `stop(seconds)` is its sleep: it waits that long to be asked to leave (the
    updater asks, to put new files in) and says whether it was. And with
    Mistery closed and nothing left to serve in the background (sharing off,
    no friends, or "keep sharing while Mistery is closed" unticked) it leaves
    by itself: the app starts it again when that changes.
    """
    from ..party import upnp as _upnp
    from . import background

    _upnp.cleanup_stale()                   # a forward a crash left open
    # bind is 0.0.0.0 in real life and 127.0.0.1 in tests, where listening on
    # every interface would make Windows Firewall ask the owner a question.
    mine = Sharer(bind=bind, background=True)
    deadline = None if until is None else time.monotonic() + until
    yielded = False
    try:
        while deadline is None or time.monotonic() < deadline:
            app = app_is_running()
            if not app and not background.wanted() and pairing.pending_secret() is None:
                _log.info("share: nothing to serve with Mistery closed; leaving")
                break
            if app and mine.running:
                _log.info("share: Mistery is open (pid %s); it takes the port from here", app)
                mine.stop()
                yielded = True
            elif not app:
                if yielded:
                    _log.info("share: Mistery has closed; taking the port back")
                    yielded = False
                if Sharer.wanted() and not mine.running:
                    mine.start()
                elif mine.running and not Sharer.wanted():
                    mine.stop()             # the owner switched sharing off
            if stop is None:
                time.sleep(poll)
            elif stop(poll):
                _log.info("share: asked to step aside (the updater); leaving")
                break
    except KeyboardInterrupt:
        pass
    finally:
        mine.stop()
    return 0
