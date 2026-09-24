"""What a movie night says it is watching: the room's description of its film.

In its own module, apart from session.py, because a friend's movie night on
this PC's film is held by the Mistery with no window too (app/share/nights.py),
which has no Qt to import.
"""

from __future__ import annotations

from pathlib import Path

from .. import db
from ..models import MediaItem
from .people import Person


def media_for(item: MediaItem, me: Person, default: str, transcodes: bool) -> dict:
    """What the room says it is watching. Plain values only (sync.clean_media).

    "mbps" is the original's own average rate, so a guest whose stream keeps
    stalling can be offered a stream that is really lighter than it."""
    from . import transcode

    show_title = ""
    if item.is_episode and item.show_id:
        row = db.get_show(item.show_id)
        show_title = str(row["title"] or "") if row is not None else ""
    if item.is_episode:
        name = f"{item.code} · {item.title}".strip(" ·") if item.code else item.title
        title = f"{show_title} — {name}" if show_title else name
    else:
        title = item.title or Path(item.path).stem
    return {"key": f"{me.id}:{item.id}", "title": title, "duration": float(item.duration or 0) or None,
            "kind": item.kind, "quality": default, "transcode": bool(transcodes),
            "fps": float(item.fps) if item.fps else None, "show": show_title, "code": item.code,
            "name": item.title, "year": int(item.year) if item.year else None,
            "mbps": transcode.per_friend_mbps(item.size, item.duration)}
