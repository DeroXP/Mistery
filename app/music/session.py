"""The music session file: what was playing, so the next start can pick it up.

It is a small JSON file next to the library (music-session.json), not rows in
the database. It is rewritten every 15 s while music plays, and a database
write is seen by the window's change watcher as the library changing; a file of
its own touches nothing else.

The file is only ever swapped in whole (see config.Settings._write, which it
shares): a power cut mid-save leaves the previous session, never half of one.
Reading never raises. A missing, empty, cut-short or hand-edited file is just
"nothing to restore", and every field is checked before the player trusts it.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

from ..config import Settings, data_dir

_log = logging.getLogger("music")

VERSION = 1
REPEAT_MODES = ("off", "all", "one")


def path() -> Path:
    return data_dir() / "music-session.json"


def write(state: dict) -> bool:
    """Swap the session file for `state`. False (logged, never raised) on failure."""
    try:
        text = json.dumps({"version": VERSION, **state}, ensure_ascii=False, separators=(",", ":"))
        if Settings._write(path(), text):
            return True
        _log.warning("could not save the music session")
    except Exception:
        _log.exception("could not save the music session")
    return False


def read() -> dict | None:
    """The saved session, cleaned up, or None when there is nothing usable.

    Returns {"queue": [ids], "original": [ids], "index", "position", "shuffle",
    "repeat", "in_order", "context", "counted", "ended"}; ids are ints in play
    order and may repeat (Play next can queue a song twice).
    """
    try:
        raw = path().read_bytes()
    except OSError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        _log.warning("music session file is damaged; starting without it")
        return None
    if not isinstance(data, dict):
        return None
    queue = _ids(data.get("queue"))
    if not queue:
        return None
    original = _ids(data.get("original")) or list(queue)
    index = data.get("index")
    index = index if isinstance(index, int) and not isinstance(index, bool) else 0
    position = data.get("position")
    position = float(position) if isinstance(position, (int, float)) \
        and not isinstance(position, bool) and math.isfinite(position) else 0.0
    repeat = data.get("repeat") if data.get("repeat") in REPEAT_MODES else "off"
    return {
        "queue": queue,
        "original": original,
        "index": index,
        "position": max(0.0, position),
        "shuffle": data.get("shuffle") is True,
        "repeat": repeat,
        "in_order": data.get("in_order") is not False,
        "context": clean_context(data.get("context")),
        "counted": data.get("counted") is True,
        "ended": data.get("ended") is True,
    }


def clean_context(context) -> dict | None:
    """A play context ("Playing from ...") in its documented shape, or None.

    The kind is whatever the view that started the queue said (album, artist,
    songs, liked, search, queue); any non-empty word is kept, so a new kind of
    page needs no change here.
    """
    if not isinstance(context, dict):
        return None
    kind = context.get("kind")
    if not isinstance(kind, str) or not kind:
        return None
    title = context.get("title")
    ident = context.get("id")
    if isinstance(ident, bool) or not isinstance(ident, (int, str, type(None))):
        ident = None
    return {"kind": kind, "title": title if isinstance(title, str) else "", "id": ident}


def _ids(value) -> list[int]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, int) and not isinstance(item, bool)]
