"""Keyless online metadata: TVmaze for shows and episodes, Wikipedia for movies.

Used when no TMDB key is configured (TMDB stays the best source when present).
Everything is cached in the http_cache table and images land in the art folder,
so repeat lookups cost nothing and the app keeps working offline afterwards.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import requests

from .. import __version__, db
from ..config import art_dir
from . import categories

_TIMEOUT = 12.0
_CACHE_TTL = 60 * 60 * 24 * 30

# Be a good citizen: these are free, unauthenticated APIs. One request per host
# per interval, and honour 429 rather than hammering through it.
_MIN_INTERVAL = 0.8
_MAX_RETRIES = 3

_session = requests.Session()
_session.headers["User-Agent"] = f"Mistery/{__version__} (local media library)"

# Hosts that ask for more room than that. Apple documents about 20 calls a
# minute for the Search API, which is 3 s.
#
# Wikidata is a guess, not a measurement. An earlier comment here claimed 429
# after 12 requests 1.4 s apart; re-running 16 different films' Q-ids through
# this code at 1.5 s gave 16 answers, 0 failures, 22.7 s in total, so whatever
# that was, it was not the steady-state limit. 1.5 s is kept because the query
# service runs arbitrary SPARQL for everyone and asks callers to go easy, not
# because 0.8 s was seen to fail. _get_json honours a 429 either way.
_HOST_INTERVAL = {
    "itunes.apple.com": 3.0,
    "query.wikidata.org": 1.5,
}

_last_request: dict[str, float] = {}
_throttle_lock = threading.Lock()

# Set once the app is quitting. The pipeline thread keeps a closed Mistery
# running until it returns, and on a network that never answers the two retries
# after a failed request were most of that time: up to half a minute.
_closing = threading.Event()


def close() -> None:
    """Stop retrying lookups: the app is quitting. A request under way ends on its own."""
    _closing.set()


def _back_off(seconds: float) -> None:
    """Wait before a retry, but not past the moment the app starts quitting.

    A rate-limited host can ask for ten seconds. Slept through, that wait held
    a quitting app for all of it before the loop could see there would be no
    retry; waiting on the closing event ends it as soon as close() is called.
    """
    _closing.wait(seconds)


def _wait_turn(host: str) -> None:
    with _throttle_lock:
        interval = _HOST_INTERVAL.get(host, _MIN_INTERVAL)
        previous = _last_request.get(host, 0.0)
        gap = time.monotonic() - previous
        if gap < interval:
            time.sleep(interval - gap)
        _last_request[host] = time.monotonic()

_TAG_RE = re.compile(r"<[^>]+>")


class OnlineError(RuntimeError):
    """Network trouble — the caller should stop hammering and fall back."""


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    return _TAG_RE.sub("", text).replace(" ", " ").strip() or None


def _get_json(url: str, params: dict | None = None, trim=None) -> dict | list | None:
    """GET with month-long caching. 404s cache as misses so they aren't retried.

    `trim`, when given, is applied to the parsed body before it is both cached
    and returned, so the cache and a live reply are always the same shape. It
    is for a reply that is mostly fields nothing here reads — see _itunes_trim.
    """
    key = f"online:{url}:{json.dumps(sorted((params or {}).items()))}"
    cached = db.cache_get(key, _CACHE_TTL)
    if cached is not None:
        try:
            payload = json.loads(cached)
        except ValueError:
            payload = None
        return None if payload == {"__miss__": True} else payload

    host = url.split("/")[2]
    response = None
    for attempt in range(_MAX_RETRIES):
        if attempt and _closing.is_set():
            break
        _wait_turn(host)
        try:
            response = _session.get(url, params=params, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            if attempt == _MAX_RETRIES - 1:
                raise OnlineError(str(exc)) from exc
            _back_off(1.0 * (attempt + 1))
            continue

        if response.status_code not in (429, 503):
            break
        # Rate limited: wait as told, or back off, then try again.
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else 0.0
        except ValueError:
            delay = 0.0
        _back_off(min(max(delay, 1.5 * (attempt + 1)), 10.0))

    if response is None:
        raise OnlineError(f"no response from {host}")
    if response.status_code == 404:
        db.cache_put(key, json.dumps({"__miss__": True}))
        return None
    if response.status_code != 200:
        raise OnlineError(f"HTTP {response.status_code} from {host}")

    try:
        data = response.json()
    except ValueError as exc:
        raise OnlineError(f"unreadable response: {exc}") from exc
    if trim is not None:
        data = trim(data)
    db.cache_put(key, json.dumps(data))
    return data


# Posters and stills each come from one image host, separate from the API. When
# it stalls, the next image will stall too, and every try costs the full
# timeout: one timed-out still per episode held the pipeline for ten minutes
# on a season. So a host that failed is left alone for a while.
_IMAGE_HOST_BACKOFF = 10 * 60
_image_host_down: dict[str, float] = {}


class _ImageUnavailable(Exception):
    """There is an image, but it could not be fetched right now."""


def _download_image(url: str | None, name: str) -> str | None:
    """The image saved into the art folder, or None when there is no image.

    Raises _ImageUnavailable when the image host could not be reached or was
    failing, which is not the same as "no image": the caller keeps the row
    queued so the art arrives on a later pass instead of never.
    """
    if not url:
        return None
    suffix = Path(url.split("?")[0]).suffix or ".jpg"
    destination = art_dir() / f"{name}{suffix}"
    if destination.is_file() and destination.stat().st_size > 0:
        return str(destination)
    host = url.split("/")[2]
    if time.monotonic() < _image_host_down.get(host, 0.0):
        raise _ImageUnavailable(f"{host} failed a moment ago")
    try:
        response = _session.get(url, timeout=_TIMEOUT * 2)
    except requests.RequestException as exc:
        _image_host_down[host] = time.monotonic() + _IMAGE_HOST_BACKOFF
        raise _ImageUnavailable(str(exc)) from exc
    if response.status_code == 429 or response.status_code >= 500:
        _image_host_down[host] = time.monotonic() + _IMAGE_HOST_BACKOFF
        raise _ImageUnavailable(f"HTTP {response.status_code} from {host}")
    if response.status_code != 200 or not response.content:
        return None
    try:
        destination.write_bytes(response.content)
    except OSError:
        return None
    return str(destination)


def _art(url: str | None, name: str) -> tuple[str | None, str | None, str]:
    """(saved image or None, the address it came from, the meta_state to use).

    'pending' when the image exists but has not arrived: 'done' is never looked
    at again, so a show or episode saved as done during a CDN hiccup kept a
    blank poster or still for good.

    The address comes back only alongside a file that was actually saved, so
    the pair always means "this picture is at this address" — Discord is shown
    the address instead of the file, and the two must not describe different
    pictures. See db.ART_URL_COLUMNS.
    """
    try:
        saved = _download_image(url, name)
    except _ImageUnavailable:
        return None, None, "pending"
    return saved, (url if saved else None), "done"


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


# --- TVmaze: shows and episodes ---------------------------------------------

def tvmaze_show(title: str, year: int | None) -> dict | None:
    """Show-level fields, or None when there's no plausible match."""
    data = _get_json("https://api.tvmaze.com/singlesearch/shows", {"q": title})
    if not isinstance(data, dict):
        return None

    # singlesearch returns its best guess; make sure it's actually our show.
    if _normalise(data.get("name") or "") != _normalise(title):
        results = _get_json("https://api.tvmaze.com/search/shows", {"q": title}) or []
        data = None
        for entry in results[:5]:
            candidate = entry.get("show") or {}
            if _normalise(candidate.get("name") or "") == _normalise(title):
                data = candidate
                break
        if data is None:
            return None

    premiered = (data.get("premiered") or "")[:4]
    if year and premiered.isdigit() and abs(int(premiered) - year) > 1:
        return None

    show_id = int(data["id"])
    image = (data.get("image") or {}).get("original")
    poster, poster_url, state = _art(image, f"tvmaze-{show_id}-poster")
    fields = {
        "tvmaze_id": show_id,
        "title": data.get("name") or title,
        "overview": _strip_html(data.get("summary")),
        "genres": ", ".join(data.get("genres") or []) or None,
        "rating": (data.get("rating") or {}).get("average"),
        "poster": poster,
        "poster_url": poster_url,
        "meta_state": state,
    }
    if premiered.isdigit():
        fields["year"] = int(premiered)
    return {k: v for k, v in fields.items() if v is not None}


def tvmaze_episodes(tvmaze_id: int) -> dict[tuple[int, int], dict]:
    """Every episode of a show, keyed by (season, episode)."""
    data = _get_json(f"https://api.tvmaze.com/shows/{int(tvmaze_id)}/episodes")
    episodes: dict[tuple[int, int], dict] = {}
    for entry in data if isinstance(data, list) else []:
        season, number = entry.get("season"), entry.get("number")
        if season is None or number is None:
            continue
        episodes[(int(season), int(number))] = entry
    return episodes


def tvmaze_episode_fields(tvmaze_id: int, season: int, episode: int) -> dict | None:
    entry = tvmaze_episodes(tvmaze_id).get((season, episode))
    if entry is None:
        return None
    still = (entry.get("image") or {}).get("original")
    backdrop, backdrop_url, state = _art(
        still, f"tvmaze-{int(tvmaze_id)}-s{season:02d}e{episode:02d}"
    )
    fields = {
        "title": entry.get("name") or None,
        "overview": _strip_html(entry.get("summary")),
        "rating": (entry.get("rating") or {}).get("average"),
        "backdrop": backdrop,
        "backdrop_url": backdrop_url,
        "meta_state": state,
        "meta_source": "tvmaze",
    }
    return {k: v for k, v in fields.items() if v is not None}


# --- Wikipedia: movies -------------------------------------------------------

def _wikipedia_summary(page_title: str) -> dict | None:
    slug = requests.utils.quote(page_title.replace(" ", "_"), safe="")
    data = _get_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}")
    if not isinstance(data, dict) or data.get("type") == "disambiguation":
        return None
    return data


def _wikipedia_search(query: str) -> list[str]:
    """Article titles matching a free-text query, best first."""
    data = _get_json(
        "https://en.wikipedia.org/w/rest.php/v1/search/page",
        {"q": query, "limit": 6},
    )
    pages = (data or {}).get("pages") if isinstance(data, dict) else None
    return [p.get("title") for p in (pages or []) if p.get("title")]


def _title_variants(title: str) -> list[str]:
    """Filenames can't contain ':', so ' - ' usually stands in for it."""
    variants = [title]
    if " - " in title:
        variants.append(title.replace(" - ", ": ", 1))
        variants.append(title.replace(" - ", " ", 1))
    return list(dict.fromkeys(variants))


def wikipedia_article(title: str, year: int | None) -> dict | None:
    """The film's Wikipedia article summary, or None when there isn't one.

    Search first, then verify. Guessing article titles took up to ten requests
    for a film with no article and got us rate-limited; searching costs one
    request and handles punctuation a filename could not represent (a colon in
    "Night Train: First Light" becomes " - " on disk).

    Separate from wikipedia_movie because the categories lookup wants the same
    article for its `wikibase_item` and the month-long cache makes the second
    caller free: all 11 of this library's matched films answered in 0.02 s each
    on the second pass.
    """
    variants = _title_variants(title)
    query = f"{variants[-1]} film"
    if year:
        query = f"{variants[-1]} {year} film"

    for page_title in _wikipedia_search(query)[:3]:
        if not _title_close_enough(page_title, title):
            continue
        found = _wikipedia_summary(page_title)
        if found and _is_film_article(found, year):
            return found

    # Exact article name as a last resort, for titles search ranks poorly.
    for candidate in ([f"{variants[0]} ({year} film)"] if year else []) + [variants[0]]:
        found = _wikipedia_summary(candidate)
        if found and _is_film_article(found, year):
            return found
    return None


def wikipedia_movie(title: str, year: int | None) -> dict | None:
    """Poster and plot summary from the film's Wikipedia article."""
    data = wikipedia_article(title, year)
    if data is None:
        return None

    image = ((data.get("originalimage") or {}).get("source")
             or (data.get("thumbnail") or {}).get("source"))
    poster, poster_url, state = _art(image, f"wiki-{_normalise(title)}-{year or 'na'}")
    fields = {
        "overview": (data.get("extract") or "").strip() or None,
        "poster": poster,
        "poster_url": poster_url,
        "meta_state": state,
        "meta_source": "wikipedia",
    }
    return {k: v for k, v in fields.items() if v is not None}


def _is_film_article(data: dict, year: int | None) -> bool:
    extract = (data.get("extract") or "").lower()
    if "film" not in extract and "movie" not in extract:
        return False
    if year:
        # A film article that never mentions our year is probably a different
        # production sharing the name.
        return str(year) in extract or str(year) in (data.get("description") or "")
    return True


def _title_close_enough(found: str, wanted: str) -> bool:
    """Guard against a search result that is merely related to the film.

    `_normalise` keeps only a-z and 0-9, so a title written in Japanese,
    Chinese or Cyrillic — or one that is all punctuation, like "!!!" — comes
    out as "". "" is a substring of every article title there is, so the
    substring test below waved through whatever Wikipedia's search happened to
    rank first, and movie_categories then wrote that article's Wikidata genres
    onto the film. Those titles get a plain case-folded compare instead: it is
    strict, but a film called 君の名は has an article called 君の名は.
    """
    found_key = _normalise(re.sub(r"\(.*?\)", "", found))
    wanted_key = _normalise(wanted)
    if not found_key or not wanted_key:
        bare = re.sub(r"\s*\(.*?\)", "", found or "").strip().casefold()
        return bare == (wanted or "").strip().casefold()
    return found_key == wanted_key or wanted_key in found_key or found_key in wanted_key


# --- Categories for films, with no API key -----------------------------------
#
# Films are the gap: TVmaze gives shows their genres (including "Anime"), but
# the keyless film source is Wikipedia, whose summary endpoint carries no genre
# at all. Measured on this library: 0 of 12 films had a single genre, so the
# categories filter would have been an empty row of chips on the Movies page.
#
# Two sources, in order of how well they answered those 12 films:
#  * Wikidata, reached through the `wikibase_item` already sitting in the
#    Wikipedia summary this app fetched anyway. 11 of 12 (the twelfth is an
#    episode filed as a film and has no article). One request per film.
#  * iTunes, for films Wikipedia never matched. It answered 2 of 12 on a strict
#    title-and-year match — worth having, not worth relying on.

_QID_RE = re.compile(r"^Q[1-9][0-9]*$")

# genre, what the thing *is*, and where it is from — enough for "anime", which
# no genre vocabulary outside TVmaze has a word for. One request instead of the
# three the wbgetclaims API needs, and 25 KB instead of 180 KB.
_WIKIDATA_SPARQL = """SELECT ?gLabel ?iLabel ?cLabel WHERE {
  VALUES ?work { wd:%s }
  OPTIONAL { ?work wdt:P136 ?g }
  OPTIONAL { ?work wdt:P31 ?i }
  OPTIONAL { ?work wdt:P495 ?c }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}"""


def wikidata_categories(qid: str) -> list[str]:
    """Category names for one Wikidata item, e.g. Q29588607 -> Action, Comedy…"""
    if not _QID_RE.match(qid or ""):
        return []           # the id goes straight into the query text
    data = _get_json("https://query.wikidata.org/sparql",
                     {"query": _WIKIDATA_SPARQL % qid, "format": "json"})
    if not isinstance(data, dict):
        # The same guard the TVmaze and Wikipedia readers above use. A 200 with
        # a JSON array or string in it — a proxy or a captive portal — used to
        # raise AttributeError past _categories_stage's `except OnlineError`
        # into the pipeline's one catch-all, which then skipped the thumbnail
        # and intro stages for that whole pass.
        return []
    results = data.get("results")
    rows = results.get("bindings") if isinstance(results, dict) else None
    if not isinstance(rows, list):
        return []

    found: list[str] = []
    kinds: set[str] = set()
    countries: set[str] = set()
    for row in rows:
        for name in categories.expand((row.get("gLabel") or {}).get("value") or ""):
            if name not in found:
                found.append(name)
        kinds.add(((row.get("iLabel") or {}).get("value") or "").lower())
        countries.add(((row.get("cLabel") or {}).get("value") or "").lower())

    # "Anime film" is its own thing to Wikidata; "animated film" plus Japan is
    # how everything older than that entry is described. Wikidata's genre list
    # never says either — Chainsaw Man's reads action/romantic drama/
    # supernatural/dark fantasy — so this is where Anime comes from for films.
    animated = any("anime" in kind or "animat" in kind for kind in kinds)
    if any("anime" in kind for kind in kinds) or (animated and "japan" in countries):
        found += ["Anime", "Animation"]
    elif animated:
        found.append("Animation")
    return [name for name in categories.CATEGORIES if name in set(found)]


_ITUNES_FIELDS = ("kind", "trackName", "releaseDate", "primaryGenreName")


def _itunes_trim(data):
    """The four fields itunes_movie_categories reads, and nothing else.

    A limit=50 reply is mostly artwork URLs, prices, long descriptions and store
    ids that nothing here looks at: one blockbuster sequel came back as
    102,371 bytes, against 2.5-4.6 KB for a Wikipedia row and 1.2-3.2 KB for a
    Wikidata one. http_cache is written for a month and never pruned — nothing
    in app/ deletes an expired row and there is no VACUUM — so every film
    Wikidata could not answer for was leaving 100 KB in the library database for
    good. Trimmed, the same 57 results are 7,895 bytes, 13x smaller.

    Every result is kept, not just the feature-movie ones, so that the day Apple
    renames `kind` the cached reply still shows what it now says.

    The limit of 50 stays: without `entity=movie` a film can rank well down a
    page of songs and albums of the same name, so the results have to be deep.
    """
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        return data
    return {"results": [{k: row.get(k) for k in _ITUNES_FIELDS}
                        for row in data["results"] if isinstance(row, dict)]}


def itunes_movie_categories(title: str, year: int | None) -> list[str]:
    """Category names from the iTunes Search API. No key, one request.

    Searched without `entity=movie`: that filter has stopped returning anything
    at all (0 results for every term tried, in both the US and GB stores, on
    2026-09-17), while the same search unfiltered still comes back with
    `kind: feature-movie` rows. Films are picked out of the mixed results here
    instead, and only an exact title with a matching year counts — the store's
    ranking happily offers "Night Train: First Light" for "Night Train 3".
    """
    data = _get_json("https://itunes.apple.com/search", {"term": title, "limit": 50},
                     trim=_itunes_trim)
    if not isinstance(data, dict):
        return []               # see wikidata_categories: a 200 that isn't JSON
    wanted = _normalise(title)
    entries = data.get("results")
    for entry in entries if isinstance(entries, list) else []:
        if entry.get("kind") != "feature-movie":
            continue
        name = entry.get("trackName") or ""
        if _normalise(re.sub(r"\(.*?\)", "", name)) != wanted:
            continue
        released = (entry.get("releaseDate") or "")[:4]
        if year and released.isdigit() and abs(int(released) - year) > 1:
            continue
        found = categories.expand(entry.get("primaryGenreName") or "")
        if found:
            return found
    return []


def movie_categories(title: str, year: int | None) -> str | None:
    """Categories for a film with no TMDB key, ready for the `genres` column.

    Both sources are cached for a month like everything else here, so a second
    pass over a library costs nothing and a film that genuinely has no article
    is not searched for again until the cache ages out.
    """
    article = wikipedia_article(title, year)
    found = wikidata_categories((article or {}).get("wikibase_item") or "")
    if not found:
        found = itunes_movie_categories(title, year)
    return categories.join(found) or None
