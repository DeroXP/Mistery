"""Which friends' PCs answer right now, for the library pages.

Friends' films, shows and music sit on this library's own pages, as if they
were yours, and a friend's whose PC does not answer is drawn dimmed there:
still listed, so the page does not change size every time somebody turns their
PC off, but plainly not something that will play this minute.

So every few minutes (and when asked, say when Movies opens) each friend you
share with is asked what has changed in their library, which is also how their
copy here stays current. A PC that answers is "here"; one that does not, or that
has sharing switched off or paused for you, is "away". Nothing is asked while
Mistery sits in the tray, since that is when a game is most likely running.

Until the first round has finished, a friend counts as here if a connection
with them worked in the last few minutes (friends.last_seen), so the pages do
not open with everything dimmed and then light up.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .. import db

_log = logging.getLogger("share")

FRESH_FOR = 10 * 60.0           # last_seen this recent counts as here, before the first check
EVERY = 4 * 60.0                # how often the window asks, in the background

_lock = threading.Lock()
_states: dict[int, bool] = {}   # friend id -> answered at the last check
_running = False


def here(friend) -> bool:
    """Whether this friend's PC is answering, as far as is known."""
    with _lock:
        known = _states.get(int(friend["id"]))
    if known is not None:
        return known
    seen = friend["last_seen"]
    return bool(seen) and time.time() - float(seen) < FRESH_FOR


def away_ids() -> set[int]:
    """Friends you share with whose PCs are not answering."""
    return {int(friend["id"]) for friend in db.friends(include_paused=False) if not here(friend)}


def check(done: Callable[[bool], None] | None = None) -> bool:
    """Ask every friend's PC, on a thread. `done(changed)` afterwards, from that
    thread, where changed says whether anyone came or went or any library changed.
    False when a round is already running (its own `done` will say)."""
    global _running
    with _lock:
        if _running:
            return False
        _running = True
    threading.Thread(target=_round, args=(done,), name="share-reach", daemon=True).start()
    return True


def _round(done) -> None:
    global _running
    from . import client

    changed = False
    try:
        for friend in db.friends(include_paused=False):
            friend_id = int(friend["id"])
            try:
                result = client.refresh(friend)
                answered = True
                changed = changed or bool(result.get("kinds"))
            except client.ShareError as problem:
                # Off, asleep, unreachable from here, or sharing switched off or
                # paused for us: in every case nothing of theirs will play now.
                answered = False
                _log.info("share: %s is away: %s", friend["name"], problem)
            except Exception:                           # noqa: BLE001 - one friend, not the round
                answered = False
                _log.exception("share: checking %s failed", friend["name"])
            with _lock:
                before = _states.get(friend_id)
                _states[friend_id] = answered
            changed = changed or before != answered
    finally:
        db.close_thread_connection()
        with _lock:
            _running = False
    if done is not None:
        done(changed)


def forget(friend_id: int | None = None) -> None:
    """Drop what is known: one friend removed, or everything (tests)."""
    with _lock:
        if friend_id is None:
            _states.clear()
        else:
            _states.pop(int(friend_id), None)


def set_state(friend_id: int, answered: bool) -> None:
    """What another part of the app just learned: a film of theirs played (here),
    or their PC could not be reached (away)."""
    with _lock:
        _states[int(friend_id)] = bool(answered)
