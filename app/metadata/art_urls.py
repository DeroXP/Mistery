"""Where artwork already in the library came from, worked out offline.

Posters and stills used to be downloaded and the address they came from thrown
away, so a library built before db.ART_URL_COLUMNS existed has the pictures but
not their web addresses — and Discord wants the address, not the file.

Most of them can be recovered without asking anyone. Every downloader in this
package names the file after the reply that described it (`tvmaze-4242-s01e04`,
`tmdb-movie-424242-p`), and those replies are still in the app's http_cache,
which nothing prunes — the month-long TTL only stops them being *read* as
fresh. So the name of the file on disk is enough to find its address again.

Wikipedia is the exception, and it is not recoverable: `wiki-low-tide-2019`
is named after the film, not the article the poster came from, and nothing in a
cached article says which film settled on it. Those rows get their address from
the next metadata pass instead.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from .tmdb import BACKDROP_SIZE, IMAGE_BASE, POSTER_SIZE

# db.cache_put keys look like "tmdb:<path>:<params json>" and
# "online:<url>:<params json>". A TMDB path never contains a colon; a URL does,
# so that one is read up to the "[" the params list always starts with.
_TMDB_KEY = re.compile(r"^tmdb:([^:]+):")
_ONLINE_KEY = re.compile(r"^online:(.+?):\[")

_TMDB_DETAIL = re.compile(r"^/(movie|tv)/(\d+)$")
_TMDB_EPISODE = re.compile(r"^/tv/(\d+)/season/(\d+)/episode/(\d+)$")
_TVMAZE_EPISODES = re.compile(r"^https://api\.tvmaze\.com/shows/(\d+)/episodes$")
_TVMAZE_SEARCH = "https://api.tvmaze.com/search/shows"
_TVMAZE_SINGLE = "https://api.tvmaze.com/singlesearch/shows"


def addresses_by_name(cached: Iterable[tuple[str, str]]) -> dict[str, str]:
    """{artwork file stem: public address} from replies already cached.

    The stem is the `name` its downloader was given, which is also the file's
    name on disk minus the suffix — that is the whole of the match. Anything
    unreadable is skipped: a damaged cache row must not stop the app starting.
    """
    found: dict[str, str] = {}
    for key, body in cached:
        try:
            tmdb = _TMDB_KEY.match(key or "")
            online = None if tmdb else _ONLINE_KEY.match(key or "")
            if not tmdb and not online:
                continue
            data = json.loads(body or "")
            if tmdb:
                _from_tmdb(tmdb.group(1), data, found)
            else:
                _from_online(online.group(1), data, found)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return found


def _remember(found: dict[str, str], name: str, address: str | None) -> None:
    if address:
        found[name] = address


def _from_tmdb(path: str, data, found: dict[str, str]) -> None:
    """Posters, backdrops and stills named by TmdbClient.

    Only the detail replies (/movie/42, /tv/42/season/1/episode/3) are read.
    A search reply carries poster paths too, but it is the details the pictures
    were downloaded from, and a poster TMDB has since changed would make the
    search reply disagree — a wrong address is worse than none.
    """
    if not isinstance(data, dict):
        return
    detail = _TMDB_DETAIL.match(path)
    if detail:
        kind, ident = detail.groups()
        stem = f"tmdb-movie-{ident}" if kind == "movie" else f"tmdb-tv-{ident}"
        _remember(found, f"{stem}-p", _tmdb_address(data.get("poster_path"), POSTER_SIZE))
        _remember(found, f"{stem}-b", _tmdb_address(data.get("backdrop_path"), BACKDROP_SIZE))
        return
    episode = _TMDB_EPISODE.match(path)
    if episode:
        show, season, number = (int(part) for part in episode.groups())
        _remember(found, f"tmdb-tv-{show}-s{season}e{number}",
                  _tmdb_address(data.get("still_path"), BACKDROP_SIZE))


def _tmdb_address(remote_path: str | None, size: str) -> str | None:
    return f"{IMAGE_BASE}/{size}{remote_path}" if remote_path else None


def _from_online(url: str, data, found: dict[str, str]) -> None:
    """Show posters and episode stills named by online._download_image."""
    if url == _TVMAZE_SINGLE:
        _tvmaze_show(data, found)
        return
    if url == _TVMAZE_SEARCH:
        for entry in data if isinstance(data, list) else []:
            _tvmaze_show((entry or {}).get("show"), found)
        return
    episodes = _TVMAZE_EPISODES.match(url)
    if not episodes:
        return                  # Wikipedia and Wikidata: nothing to match on
    show = int(episodes.group(1))
    for entry in data if isinstance(data, list) else []:
        season, number = (entry or {}).get("season"), entry.get("number")
        if season is None or number is None:
            continue
        _remember(found, f"tvmaze-{show}-s{int(season):02d}e{int(number):02d}",
                  (entry.get("image") or {}).get("original"))


def _tvmaze_show(data, found: dict[str, str]) -> None:
    if not isinstance(data, dict) or data.get("id") is None:
        return
    _remember(found, f"tvmaze-{int(data['id'])}-poster",
              (data.get("image") or {}).get("original"))
