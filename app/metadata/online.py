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

from .. import db
from ..config import art_dir

_TIMEOUT = 12.0
_CACHE_TTL = 60 * 60 * 24 * 30

# Be a good citizen: these are free, unauthenticated APIs. One request per host
# per interval, and honour 429 rather than hammering through it.
_MIN_INTERVAL = 0.8
_MAX_RETRIES = 3

_session = requests.Session()
_session.headers["User-Agent"] = "Mistery/1.0 (local media library)"

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
        previous = _last_request.get(host, 0.0)
        gap = time.monotonic() - previous
        if gap < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - gap)
        _last_request[host] = time.monotonic()

_TAG_RE = re.compile(r"<[^>]+>")


class OnlineError(RuntimeError):
    """Network trouble — the caller should stop hammering and fall back."""


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    return _TAG_RE.sub("", text).replace(" ", " ").strip() or None


def _get_json(url: str, params: dict | None = None) -> dict | list | None:
    """GET with month-long caching. 404s cache as misses so they aren't retried."""
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


def _art(url: str | None, name: str) -> tuple[str | None, str]:
    """(saved image or None, the meta_state a lookup with it should get).

    'pending' when the image exists but has not arrived: 'done' is never looked
    at again, so a show or episode saved as done during a CDN hiccup kept a
    blank poster or still for good.
    """
    try:
        return _download_image(url, name), "done"
    except _ImageUnavailable:
        return None, "pending"


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
    poster, state = _art(image, f"tvmaze-{show_id}-poster")
    fields = {
        "tvmaze_id": show_id,
        "title": data.get("name") or title,
        "overview": _strip_html(data.get("summary")),
        "genres": ", ".join(data.get("genres") or []) or None,
        "rating": (data.get("rating") or {}).get("average"),
        "poster": poster,
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
    backdrop, state = _art(still, f"tvmaze-{int(tvmaze_id)}-s{season:02d}e{episode:02d}")
    fields = {
        "title": entry.get("name") or None,
        "overview": _strip_html(entry.get("summary")),
        "rating": (entry.get("rating") or {}).get("average"),
        "backdrop": backdrop,
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


def wikipedia_movie(title: str, year: int | None) -> dict | None:
    """Poster and plot summary from the film's Wikipedia article.

    Search first, then verify. Guessing article titles took up to ten requests
    for a film with no article and got us rate-limited; searching costs one
    request and handles punctuation a filename could not represent (a colon in
    "Spider-Man: Across the Spider-Verse" becomes " - " on disk).
    """
    variants = _title_variants(title)
    query = f"{variants[-1]} film"
    if year:
        query = f"{variants[-1]} {year} film"

    data = None
    for page_title in _wikipedia_search(query)[:3]:
        if not _title_close_enough(page_title, title):
            continue
        found = _wikipedia_summary(page_title)
        if found and _is_film_article(found, year):
            data = found
            break

    if data is None:
        # Exact article name as a last resort, for titles search ranks poorly.
        for candidate in ([f"{variants[0]} ({year} film)"] if year else []) + [variants[0]]:
            found = _wikipedia_summary(candidate)
            if found and _is_film_article(found, year):
                data = found
                break

    if data is None:
        return None

    image = ((data.get("originalimage") or {}).get("source")
             or (data.get("thumbnail") or {}).get("source"))
    poster, state = _art(image, f"wiki-{_normalise(title)}-{year or 'na'}")
    fields = {
        "overview": (data.get("extract") or "").strip() or None,
        "poster": poster,
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
    """Guard against a search result that is merely related to the film."""
    found_key = _normalise(re.sub(r"\(.*?\)", "", found))
    wanted_key = _normalise(wanted)
    return found_key == wanted_key or wanted_key in found_key or found_key in wanted_key
