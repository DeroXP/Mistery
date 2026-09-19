"""Who you are at a movie night: an id that stays, and the name friends see.

The id is how a movie night recognises people: in party_progress's members, in
"Continue movie night", and on the wire. It is made once per install — a random
uuid4, saved in settings — and never derived from anything about the PC or the
person, so all it tells a host is "the same Mistery as last time". The name can
change whenever you like (Settings → Movie night); the id does not, so Sam
renaming himself "Samuel" is still the same friend.

Names come from other people's computers, so every name — yours from settings,
theirs off the wire — goes through clean_name before anything shows it.
"""

from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
import uuid
from dataclasses import dataclass

# What fits beside a face on the overlay's people list without eliding, and
# Discord's own limit, which is where most people's name will be pasted from.
NAME_MAX = 32

# The name when there is nothing better: no name in settings and a Windows
# user name that cleans away to nothing. Never empty — "Waiting for …" with a
# blank where the name should be reads like a bug.
FALLBACK_NAME = "Friend"

# An id on the wire: what uuid4().hex makes (32 hex digits), or anything else
# of the same alphabet a later version might choose, within bounds. No colon,
# because a media key is "<host person id>:<host media id>".
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# Zalgo text stacks dozens of combining marks on one letter to paint over the
# rows above and below it. Two in a row covers every real script's needs
# (Vietnamese uses two).
_MAX_MARKS_IN_A_ROW = 2

_lock = threading.Lock()


@dataclass(frozen=True)
class Person:
    """Somebody at a movie night, as everyone else knows them."""
    id: str
    name: str


def is_person_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def person_id() -> str:
    """This install's id: the saved one, or a new one saved for next time.

    Made under a lock: two threads asking at the first start (the hub and the
    Qt side, say) would otherwise each make one, and whoever saved last would
    leave the other using an id that no longer exists anywhere.
    """
    from ..config import settings

    with _lock:
        saved = settings.get("party_person_id")
        if is_person_id(saved):
            return saved
        made = uuid.uuid4().hex
        settings.set("party_person_id", made)
        return made


def display_name() -> str:
    """The name friends see: yours from settings, else your Windows user name."""
    from ..config import settings

    return (clean_name(settings.get("party_name"))
            or clean_name(_windows_user_name())
            or FALLBACK_NAME)


def me() -> Person:
    return Person(person_id(), display_name())


def clean_name(value: object, limit: int = NAME_MAX) -> str:
    """A name that is safe to show, or "" when nothing usable is left.

    Removed: control and formatting characters (a right-to-left override would
    let "Sam" arrive looking like "maS", and a zero-width space makes two
    identical-looking names), private-use and unassigned code points (they
    draw as boxes), and combining marks past two in a row (Zalgo). Every run
    of whitespace, including line breaks, becomes one space; the result is
    trimmed and cut to `limit` characters. sync uses the same rules, with a
    longer limit, for the title the host sends.
    """
    if not isinstance(value, str):
        return ""
    kept: list[str] = []
    marks = 0
    for char in unicodedata.normalize("NFC", value[:limit * 8]):
        category = unicodedata.category(char)
        if category in ("Zs", "Zl", "Zp") or char in "\t\n\r\v\f":
            kept.append(" ")
            marks = 0
            continue
        if category[0] == "C":                  # Cc Cf Cs Co Cn
            continue
        if category[0] == "M":
            marks += 1
            if marks > _MAX_MARKS_IN_A_ROW:
                continue
        else:
            marks = 0
        kept.append(char)
    name = " ".join("".join(kept).split())
    return name[:limit].rstrip()


def unique_name(name: str, taken: set[str]) -> str:
    """`name`, or `name` with the lowest number after it that nobody has yet.

    `taken` holds the names already in the room, casefolded. Two friends both
    called Sam would otherwise be indistinguishable in "Waiting for Sam…"; the
    first to arrive keeps the plain name, the second is "Sam 2".
    """
    if name.casefold() not in taken:
        return name
    number = 2
    while True:
        suffix = f" {number}"
        candidate = name[:NAME_MAX - len(suffix)].rstrip() + suffix
        if candidate.casefold() not in taken:
            return candidate
        number += 1


def members_json(people: list[Person]) -> str:
    """Everyone who came, for party_progress.members: JSON [{"id", "name"}].

    One entry per id, in the order they arrived, with the latest name each used.
    """
    by_id: dict[str, str] = {}
    for person in people:
        if is_person_id(person.id):
            by_id[person.id] = clean_name(person.name) or FALLBACK_NAME
    return json.dumps([{"id": pid, "name": name} for pid, name in by_id.items()],
                      ensure_ascii=False)


def parse_members(text: object) -> list[Person]:
    """party_progress.members back into people. Whatever is unreadable is left out."""
    if not isinstance(text, str) or not text:
        return []
    try:
        raw = json.loads(text)
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    people = []
    for entry in raw[:64]:
        if isinstance(entry, dict) and is_person_id(entry.get("id")):
            people.append(Person(entry["id"], clean_name(entry.get("name")) or FALLBACK_NAME))
    return people


def _windows_user_name() -> str:
    """The signed-in user's name, "" if Windows will not say.

    USERNAME is the account name ("Sam", "sam.jones"). getpass reads the
    same variable plus a few Unix ones, and can raise when none are set.
    """
    name = os.environ.get("USERNAME", "")
    if name:
        return name
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return ""
