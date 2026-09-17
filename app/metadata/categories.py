"""Categories: the genres from four different services, spoken as one language.

TMDB calls it "Science Fiction" for a film and "Sci-Fi & Fantasy" for a series;
TVmaze calls it "Science-Fiction"; Wikidata calls it "science fiction film".
Left alone, a library filled from more than one source ends up with four buttons
that mean the same thing. Everything that arrives is mapped onto one list of
names, and the pages only ever show that list.

The mapping is deliberate in both directions. "Action & Adventure" becomes
Action because TMDB's television list has no plain Action to pick instead — but
"Sci-Fi & Fantasy" becomes Science Fiction and *not* Fantasy, so the Fantasy
chip keeps meaning something. The old filter compared the chosen name against
the whole comma-joined string, which made Fantasy match every Sci-Fi & Fantasy
series and Action match every Action & Adventure one; nothing here does that.

"Anime" is the reason this file has to think rather than just rename: no TMDB
genre says it. It comes from TVmaze directly, from Wikidata's "anime film", and
from a person setting it by hand.
"""
from __future__ import annotations

# The names shown on screen, in the order they appear in the filter.
CATEGORIES: tuple[str, ...] = (
    "Action", "Adventure", "Animation", "Anime", "Comedy", "Crime", "Documentary",
    "Drama", "Family", "Fantasy", "History", "Horror", "Kids", "Music", "Mystery",
    "Reality", "Romance", "Science Fiction", "Sport", "Superhero", "Supernatural",
    "Thriller", "War", "Western",
)

# What the services say -> what we call it. Keys are lowercased on lookup and a
# trailing "film"/"movie"/"series" is stripped first, so only real differences
# need a line here. A word can mean two categories: Wikidata's "romantic drama"
# is both, and a row that says it belongs under either chip.
_ALIASES: dict[str, tuple[str, ...]] = {
    # --- one service's spelling for another's ---
    "action & adventure": ("Action",),      # TMDB television has no plain Action
    "sci-fi & fantasy": ("Science Fiction",),
    "science-fiction": ("Science Fiction",),
    "scifi": ("Science Fiction",),
    "sci-fi": ("Science Fiction",),
    "war & politics": ("War",),
    "kids & family": ("Kids", "Family"),    # iTunes
    "children": ("Kids",),
    "children's": ("Kids",),
    "sports": ("Sport",),
    "musicals": ("Music",),
    "musical": ("Music",),
    "soap": ("Drama",),
    "soap opera": ("Drama",),
    "espionage": ("Thriller",),
    "spy": ("Thriller",),
    "legal": ("Drama",),
    "medical": ("Drama",),
    "food": ("Documentary",),
    "travel": ("Documentary",),
    "nature": ("Documentary",),
    "diy": ("Documentary",),
    "special interest": ("Documentary",),   # iTunes' bucket for how-to and lectures
    "biographical": ("History",),           # a guess: TMDB files biopics under History
    "historical drama": ("History", "Drama"),
    "period drama": ("History", "Drama"),
    "anime": ("Anime", "Animation"),
    "animated": ("Animation",),
    "animation": ("Animation",),
    # --- genres we do not show ---
    "tv movie": (),                 # a TMDB bookkeeping genre, not a category
    "news": (),
    "talk": (),
    "talk show": (),
    "adult": (),
    "short": (),
    "silent": (),
    # --- Wikidata's compounds and sub-genres, which are finer than the chips ---
    "action comedy": ("Action", "Comedy"),
    "romantic comedy": ("Romance", "Comedy"),
    "romantic drama": ("Romance", "Drama"),
    "romance": ("Romance",),
    "comedy-drama": ("Comedy", "Drama"),
    "dramedy": ("Comedy", "Drama"),
    "drama fiction": ("Drama",),
    "crime fiction": ("Crime",),
    "coming-of-age": ("Drama",),
    "teen": ("Drama",),
    "dark fantasy": ("Fantasy",),
    "high fantasy": ("Fantasy",),
    "urban fantasy": ("Fantasy",),
    "technofantasy": ("Science Fiction",),
    "speculative fiction": ("Science Fiction",),
    "biopunk": ("Science Fiction",),
    "cyberpunk": ("Science Fiction",),
    "steampunk": ("Science Fiction",),
    "dystopian": ("Science Fiction",),
    "space opera": ("Science Fiction",),
    "superhero": ("Superhero",),    # not also Action: Wikidata lists that itself
    "psychological thriller": ("Thriller",),
    "slasher": ("Horror",),
    "zombie": ("Horror",),
    "heist": ("Crime",),
    "gangster": ("Crime",),
    "disaster": ("Action",),
    "martial arts": ("Action",),
    "wuxia": ("Action",),
    "swashbuckler": ("Adventure",),
    "supernatural": ("Supernatural",),
    "paranormal": ("Supernatural",),
    "docudrama": ("Documentary", "Drama"),
    "mockumentary": ("Documentary", "Comedy"),
}

# Wikidata says "action film", iTunes says "Action & Adventure", TVmaze says
# "Action". Stripping the noun leaves one word to look up instead of three
# hundred aliases; the compounds above are what is left after that.
_SUFFIXES = (" film", " films", " movie", " movies", " series", " genre", " fiction")

_BY_LOWER = {name.lower(): name for name in CATEGORIES}


def expand(name: str) -> list[str]:
    """One service's word for a genre, as the categories this app shows.

    Empty when it is not a category we show (TMDB's "TV Movie", TVmaze's
    "Adult") or when nothing recognises it — an unknown word is dropped rather
    than becoming a chip nobody can explain.
    """
    key = (name or "").strip().lower()
    if not key:
        return []
    for candidate in _shrink(key):
        if candidate in _ALIASES:
            return list(_ALIASES[candidate])
        if candidate in _BY_LOWER:
            return [_BY_LOWER[candidate]]
    return []


def _shrink(key: str):
    """The key itself, then the same without a trailing noun, longest first."""
    yield key
    for suffix in _SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix):
            trimmed = key[: -len(suffix)].strip()
            if trimmed:
                yield trimmed


def canonical(name: str) -> str | None:
    """The single category a word maps to, or None. `expand` when it can be two."""
    found = expand(name)
    return found[0] if found else None


def split(value: str | None) -> list[str]:
    """A stored comma-joined genres string, as a list of category names."""
    out: list[str] = []
    for part in (value or "").split(","):
        for name in expand(part):
            if name not in out:
                out.append(name)
    return out


def join(names) -> str:
    """Category names as they are stored in user_genres."""
    return ", ".join(name for name in CATEGORIES if name in set(names))


def categories_of(row) -> list[str]:
    """Every category for a film, episode or series, in CATEGORIES order.

    Both what a service said (`genres`) and what a person chose by hand
    (`user_genres`) count, and a hand-made choice is never lost to a refetch
    because the two are stored in different columns.
    """
    def field(name: str):
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            return getattr(row, name, None)

    found = set(split(field("genres"))) | set(split(field("user_genres")))
    return [name for name in CATEGORIES if name in found]


def matches(selected, categories, mode: str = "any") -> bool:
    """Does a row pass the filter? `mode` is "any" (default) or "all".

    "any" is the default because genre data is sparse — most libraries have a
    few rows with four genres and a long tail with none — and "all" over three
    chips lands on nothing often enough that it has to be a deliberate choice.
    """
    if not selected:
        return True
    have = categories if isinstance(categories, (set, frozenset)) else set(categories)
    if mode == "all":
        return set(selected) <= have
    return not have.isdisjoint(selected)
