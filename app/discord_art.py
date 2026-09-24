"""Which pictures Discord still needs uploading by hand, and in what order.

Most titles need nothing now. A film or show whose artwork came from TMDB,
TVmaze or Wikipedia has a public address, and Discord fetches that itself
(app/discord_presence.py). What is left for the export is what has no address:

  - films and shows whose poster Mistery cut from the film's own frames, or
    that nothing online matched;
  - album covers, which come out of the music files themselves and have never
    been anywhere with a web address.

And of those, only what Discord does not already have: its list of uploaded
images is public, so the export asks before writing, and a second export after
adding three albums is three files, not the whole library again.

Discord allows 300 images per application. The owner had 14 on 2026-09-21 and a
music library that grew by a hundred songs in an afternoon, so the cap is a real
edge, and what goes first is what gets played: films and shows (there are few
without an address), then albums by how often their songs have been played.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import db
from .discord_presence import _asset_match, album_asset_key, asset_key

MAX_ASSETS = 300


@dataclass
class ArtPlan:
    """What the export will write, and everything it decided not to."""

    wanted: dict[str, tuple[str, str]] = field(default_factory=dict)  # key -> (image, backdrop)
    films: int = 0          # films and shows to upload
    albums: int = 0         # album covers to upload
    online: int = 0         # need nothing: Discord fetches their poster from the web
    already: int = 0        # uploaded before
    left_out: int = 0       # would not fit under Discord's 300
    known: int | None = None    # how many images the application has; None if unknown

    @property
    def total(self) -> int:
        return len(self.wanted)


def plan(known: set[str] | None, capacity: int = MAX_ASSETS) -> ArtPlan:
    """Decide the export. `known` is Discord's list, or None when it could not
    be read — then nothing is skipped as uploaded, and the cap counts from zero.
    """
    result = ArtPlan(known=None if known is None else len(known))
    have = known or set()

    def uploaded(key: str) -> bool:
        return bool(have) and bool(_asset_match(key, have))

    films: list[tuple[str, tuple[str, str]]] = []
    for kind, rows in (("show", db.all_shows()), ("movie", db.movies())):
        for row in rows:
            columns = row.keys()
            backdrop = (row["backdrop"] if "backdrop" in columns else "") or ""
            poster = row["poster"] or backdrop
            if not poster:
                continue
            if db.art_url(kind, row["id"]):
                result.online += 1          # Discord fetches this one itself
                continue
            key = asset_key(row["title"])
            if not key or any(key == seen for seen, _ in films):
                continue                    # the first title with a name keeps it
            if uploaded(key):
                result.already += 1
                continue
            films.append((key, (poster, backdrop)))

    albums: list[tuple[int, float, str, str]] = []
    for row in db.query(
            "SELECT a.id, a.title, a.artist, a.cover, a.added_at, "
            "COALESCE(SUM(t.play_count), 0) AS plays FROM albums a "
            "LEFT JOIN tracks t ON t.album_id = a.id "
            "GROUP BY a.id"):
        cover = row["cover"]
        if not cover:
            continue
        key = album_asset_key(row["artist"], row["title"])
        if not key:
            continue
        if uploaded(key):
            result.already += 1
            continue
        albums.append((int(row["plays"] or 0), float(row["added_at"] or 0), key, cover))
    # Most played first, then newest: a record just downloaded and not played
    # yet still beats one nobody has touched in a year.
    albums.sort(key=lambda entry: (-entry[0], -entry[1]))

    room = max(0, capacity - len(have))
    for key, sources in films:
        if len(result.wanted) >= room:
            result.left_out += 1
            continue
        result.wanted[key] = sources
        result.films += 1
    for _plays, _added, key, cover in albums:
        if key in result.wanted:
            continue
        if len(result.wanted) >= room:
            result.left_out += 1
            continue
        # A square cover: compose_wide_art puts it whole in the middle of the
        # 1024x576 card over a blurred copy of itself, the way a poster goes.
        result.wanted[key] = (cover, "")
        result.albums += 1
    return result


def describe(plan_: ArtPlan) -> str:
    """One honest paragraph about what the export did and did not do."""
    parts = []
    if plan_.online:
        parts.append(f"{plan_.online} film(s) and show(s) need nothing — Discord fetches their "
                     "poster from the web")
    if plan_.already:
        parts.append(f"{plan_.already} are already uploaded")
    if plan_.left_out:
        parts.append(f"{plan_.left_out} did not fit: Discord allows {MAX_ASSETS} images and the "
                     "most played went first")
    if plan_.known is None:
        parts.append("Discord's list of what you have already uploaded could not be read, so "
                     "everything without a web address was written")
    return ("; ".join(parts) + ".") if parts else ""
