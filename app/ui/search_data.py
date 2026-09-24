"""What Search finds, with no widgets in it.

A few letters find films, shows, episodes, music (artists, albums, songs) and
the films, shows and albums your friends share, each as a Hit: what a result
row shows and what its buttons act on. Also the six moods Search offers before
you type, made from your own library, what you searched for lately, and a
surprise: a film you haven't started that fits the hour.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .. import db
from ..config import settings
from ..metadata import categories as cat
from ..models import MediaItem, ShowItem
from ..util import fmt_duration, fmt_remaining

_log = logging.getLogger("ui")

# The groups results come in, in this order, and the chips that pick one.
GROUPS = ("film", "show", "episode", "music", "friend")
GROUP_TITLES = {"film": "Films", "show": "Shows", "episode": "Episodes", "music": "Music",
                "friend": "From friends' libraries"}
CHIPS = (("all", "All"), ("film", "Films"), ("show", "Shows"), ("episode", "Episodes"),
         ("music", "Music"), ("friend", "Friends' libraries"))
MIN_LETTERS = 2
RECENT_KEY = "recent_searches"
RECENT_MAX = 8


@dataclass
class Hit:
    """One thing found: what its row shows, and what it opens or plays."""

    group: str                      # film | show | episode | music | friend
    kind: str                       # film | show | episode | song | album | artist | friend
    title: str
    sub: str
    item: object                    # MediaItem, ShowItem, a track row, an album id, an artist, a FriendItem
    art: str | None = None          # the row's picture
    wide_art: str | None = None     # the top result's
    score: int = 0                  # how well the title matches (score())
    started: bool = False
    rating: float = 0.0
    who: str = ""                   # whose, for a friend's thing

    @property
    def playable(self) -> bool:
        if self.kind == "friend":
            return getattr(self.item, "kind", "") in ("movie", "episode", "album", "track")
        return self.kind in ("film", "episode", "song", "album")


def score(title: str, needle: str) -> int:
    """3 for the whole title, 2 for how it starts, 1 for how a word in it starts."""
    text = (title or "").lower()
    if not needle or not text:
        return 0
    if text == needle:
        return 3
    if text.startswith(needle):
        return 2
    words = text.replace("-", " ").replace(":", " ").replace(".", " ").split()
    return 1 if any(word.startswith(needle) for word in words) else 0


def _like(term: str) -> str:
    # Escaped, or "100%" would match everything.
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _left_or_length(item: MediaItem) -> str:
    if item.watched:
        return "Watched"
    if item.resume_position > 0 and item.duration:
        return fmt_remaining(item.position, item.duration)
    return fmt_duration(item.duration) if item.duration else ""


def film_hit(item: MediaItem, row=None, needle: str = "") -> Hit:
    first = cat.categories_of(row if row is not None else item)[:1]
    sub = " · ".join(bit for bit in (str(item.year or ""), first[0] if first else "", _left_or_length(item)) if bit)
    return Hit("film", "film", item.title, sub, item, art=item.art, wide_art=item.wide_art,
               score=score(item.title, needle), started=item.resume_position > 0, rating=item.rating or 0.0)


def episode_hit(item: MediaItem, show_name: str, needle: str = "") -> Hit:
    sub = " · ".join(bit for bit in (show_name, item.code, _left_or_length(item)) if bit)
    return Hit("episode", "episode", item.title or item.code, sub, item, art=item.wide_art, wide_art=item.wide_art,
               score=score(item.title, needle), started=item.resume_position > 0, rating=item.rating or 0.0)


def show_hit(show: ShowItem, needle: str = "") -> Hit:
    bits = [show.subtitle]
    left = show.episode_count - show.watched_count
    if show.episode_count and left <= 0:
        bits.append("all watched")
    elif show.watched_count:
        bits.append(f"{left} to go")
    return Hit("show", "show", show.title, " · ".join(bit for bit in bits if bit), show,
               art=show.poster or show.backdrop, wide_art=show.backdrop or show.poster,
               score=score(show.title, needle), started=0 < show.watched_count < show.episode_count,
               rating=show.rating or 0.0)


def _music_hits(term: str, needle: str, limit: int = 40) -> list[Hit]:
    from ..music import library as music_library

    try:
        tracks = music_library.search(term)
    except Exception:                               # noqa: BLE001 - the rest of the results
        _log.warning("search: music search failed", exc_info=True)
        return []
    artists: dict[str, Hit] = {}
    albums: dict[int, Hit] = {}
    songs: list[Hit] = []
    for track in tracks:
        keys = set(track.keys())
        album_title = track["album_title"] or ""
        album_artist = (track["album_artist_name"] if "album_artist_name" in keys else "") or track["artist"] or ""
        if needle in (track["title"] or "").lower():
            songs.append(Hit("music", "song", track["title"] or "Untitled",
                             " · ".join(bit for bit in ("Song", track["artist"] or "", album_title) if bit),
                             track, art=track["cover"], score=score(track["title"], needle)))
        album_id = track["album_id"]
        if album_id is not None and int(album_id) not in albums and needle in album_title.lower():
            albums[int(album_id)] = Hit("music", "album", album_title,
                                        " · ".join(bit for bit in ("Album", album_artist) if bit),
                                        int(album_id), art=track["cover"], score=score(album_title, needle))
        for name in {track["artist"] or "", album_artist}:
            if not name or needle not in name.lower():
                continue
            key = music_library.artist_key(name)
            if key in artists:
                continue
            # Only an artist with an album of their own has a page to open.
            count = len(music_library.artist_albums(name))
            if count:
                artists[key] = Hit("music", "artist", music_library.artist_name(name),
                                   f"Artist · {count} album" + ("" if count == 1 else "s"),
                                   music_library.artist_name(name), art=track["cover"], score=score(name, needle))
    by_score = lambda hit: (-hit.score, hit.title.lower())     # noqa: E731
    return (sorted(artists.values(), key=by_score) + sorted(albums.values(), key=by_score)
            + sorted(songs, key=by_score))[:limit]


def _friend_hits(term: str, needle: str, limit: int = 40) -> list[Hit]:
    from .friend_library_view import friend_item

    try:
        rows = db.query(
            "SELECT m.friend_id, m.kind, m.remote_id, f.name AS who FROM friend_media m "
            "JOIN friends f ON f.id = m.friend_id "
            "WHERE m.kind IN ('movie', 'show', 'album') "
            "AND (m.title LIKE ? ESCAPE '\\' OR m.artist LIKE ? ESCAPE '\\') "
            "ORDER BY m.sort_title COLLATE NOCASE LIMIT ?", (_like(term), _like(term), limit))
    except Exception:                               # noqa: BLE001 - the rest of the results
        _log.warning("search: friends' libraries search failed", exc_info=True)
        return []
    words = {"movie": "Film", "show": "Show", "album": "Album"}
    hits = []
    for row in rows:
        item = friend_item(int(row["friend_id"]), row["kind"], int(row["remote_id"]))
        if item is None:
            continue
        who = row["who"] or "a friend"
        hits.append(Hit("friend", "friend", item.title, f"{words[row['kind']]} · in {who}'s library", item,
                        art=item.art, wide_art=item.wide_art, score=score(item.title, needle), who=who,
                        started=item.progress > 0))
    return hits


def find(term: str) -> list[Hit]:
    """Everything `term` finds, grouped by GROUPS order, best matches first."""
    term = term.strip()
    needle = term.lower()
    if len(needle) < MIN_LETTERS:
        return []
    shows = [ShowItem.from_row(row) for row in db.all_shows()]
    names = {show.id: show.title for show in shows}
    films: list[Hit] = []
    episodes: list[Hit] = []
    for row in db.search(term, limit=300):
        item = MediaItem.from_row(row)
        if item.is_episode:
            # Found only by its show's name, an episode is that show's result.
            if needle in (item.title or "").lower() or needle in (item.overview or "").lower():
                episodes.append(episode_hit(item, names.get(item.show_id, ""), needle))
        else:
            films.append(film_hit(item, row, needle))
    found_shows = [show_hit(show, needle) for show in shows
                   if needle in show.title.lower() or any(needle in name.lower() for name in cat.split(show.genres))]
    ranked = lambda hit: (-hit.score, not hit.started, hit.title.lower())     # noqa: E731
    return (sorted(films, key=ranked) + sorted(found_shows, key=ranked) + sorted(episodes, key=ranked)
            + _music_hits(term, needle) + _friend_hits(term, needle))


def top_result(hits: list[Hit]) -> Hit | None:
    """The one for the big card: the best-matching title, a film or show before
    the rest when they match as well."""
    order = {"film": 0, "show": 1, "episode": 2, "friend": 3, "album": 4, "artist": 5, "song": 6}
    if not hits:
        return None
    return max(hits, key=lambda hit: (hit.score, hit.started, -order.get(hit.kind, 9), hit.rating))


# --- moods ------------------------------------------------------------------------------------


@dataclass
class Mood:
    name: str
    line: str
    colours: tuple[str, str]        # the tile's gradient, top-left to bottom-right
    icon: str
    pick: Callable[[], list[Hit]]


def _library() -> tuple[list[tuple[MediaItem, list[str]]], list[tuple[ShowItem, list[str]]]]:
    films = [(MediaItem.from_row(row), cat.categories_of(row)) for row in db.movies()]
    shows = [(ShowItem.from_row(row), cat.categories_of(row)) for row in db.all_shows()]
    return films, shows


def _in_categories(wanted: set[str], avoid: set[str] = frozenset()) -> Callable[[], list[Hit]]:
    def pick() -> list[Hit]:
        films, shows = _library()
        out = [film_hit(film) for film, names in films
               if not film.watched and set(names) & wanted and not set(names) & avoid]
        out += [show_hit(show) for show, names in shows if set(names) & wanted and not set(names) & avoid]
        return sorted(out, key=lambda hit: (-hit.rating, hit.title.lower()))
    return pick


def _long_films() -> list[Hit]:
    films, _shows = _library()
    long = [film for film, _names in films if not film.watched and film.duration >= 2.25 * 3600]
    return [film_hit(film) for film in sorted(long, key=lambda film: -film.duration)]


def _short() -> list[Hit]:
    """Under half an hour: what's left of something started, the next episode
    of a show you're watching, or a short film."""
    shows = {show.id: show.title for show in (ShowItem.from_row(row) for row in db.all_shows())}
    out: list[Hit] = []
    seen: set[int] = set()
    started = [MediaItem.from_row(row) for row in db.continue_watching(limit=40, min_seconds=30)]
    for item in started + [MediaItem.from_row(row) for row in db.next_up()]:
        left = item.duration - item.position if item.resume_position > 0 else item.duration
        if item.id in seen or not item.duration or left > 30 * 60:
            continue
        seen.add(item.id)
        out.append(episode_hit(item, shows.get(item.show_id, "")) if item.is_episode else film_hit(item))
    films, _shows = _library()
    out += [film_hit(film) for film, _names in films
            if film.id not in seen and not film.watched and 0 < film.duration <= 30 * 60]
    return out


def _unfinished() -> list[Hit]:
    shows = {show.id: show.title for show in (ShowItem.from_row(row) for row in db.all_shows())}
    items = [MediaItem.from_row(row) for row in
             db.continue_watching(limit=60, min_seconds=float(settings.get("resume_min_seconds", 30)))]
    return [episode_hit(item, shows.get(item.show_id, "")) if item.is_episode else film_hit(item)
            for item in items]


MOODS = (
    Mood("Cozy", "Warm, gentle, nothing too loud", ("#7A4A2A", "#2E1C12"), "mug",
         _in_categories({"Romance", "Family", "Animation", "Fantasy", "Music"},
                        {"Horror", "Thriller", "War", "Crime"})),
    Mood("Rainy day", "Long films for a long afternoon", ("#34506A", "#151F2B"), "rain", _long_films),
    Mood("Short on time", "Under half an hour", ("#6A5A2A", "#262012"), "clock", _short),
    Mood("Late night", "Mysteries and slow burns", ("#3B2D55", "#151126"), "moon",
         _in_categories({"Mystery", "Thriller", "Crime", "Drama"})),
    Mood("Feel-good", "Comedies and gentle ones", ("#7A3A4A", "#2A1219"), "smile",
         _in_categories({"Comedy", "Family", "Animation", "Music", "Sport"}, {"Horror", "War"})),
    Mood("Unfinished", "Everything you stopped part way", ("#2F5A4E", "#11211D"), "resume", _unfinished),
)


def surprise(now: datetime | None = None) -> MediaItem | None:
    """A film you haven't started, one that fits the hour when there is one."""
    from .home_view import mood_for

    _title, names = mood_for((now or datetime.now()).hour)
    films, _shows = _library()
    fresh = [(film, set(found)) for film, found in films if not film.watched and film.resume_position <= 0]
    fitting = [film for film, found in fresh if found & set(names)]
    pool = fitting or [film for film, _found in fresh]
    return random.choice(pool) if pool else None


# --- recent searches --------------------------------------------------------------------------


def recent() -> list[str]:
    value = settings.get(RECENT_KEY) or []
    return [str(term) for term in value if isinstance(term, str) and term.strip()][:RECENT_MAX]


def remember(term: str) -> None:
    term = term.strip()
    if len(term) < MIN_LETTERS:
        return
    kept = [old for old in recent() if old.lower() != term.lower()]
    settings.set(RECENT_KEY, [term] + kept[:RECENT_MAX - 1])


def forget_recent() -> None:
    settings.set(RECENT_KEY, [])
