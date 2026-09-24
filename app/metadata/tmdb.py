"""TheMovieDB client — posters, backdrops, overviews, genres and ratings.

Responses are cached in SQLite and images are written into the app's art folder,
so a second launch does no network at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import requests

from .. import __version__, db
from ..config import art_dir

API_BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p"

POSTER_SIZE = "w500"
BACKDROP_SIZE = "w1280"

_TIMEOUT = 12.0
# Settings' "Save and test" asks for one tiny document while the user waits
# for the answer, so a stalled network is given up on sooner than a lookup is.
_VERIFY_TIMEOUT = (4.0, 8.0)
_CACHE_TTL = 60 * 60 * 24 * 30      # a month is plenty for film metadata

_KEY_IN_URL = re.compile(r"(api_key=)[^&\s'\"()]+")


class TmdbError(RuntimeError):
    pass


def _without_key(text: str, api_key: str) -> str:
    """requests' error text with the API key taken out.

    A connection error quotes the URL it was fetching, query string and all,
    so the key typed into a password-masked field came back in plain text in
    the status line under it, for anyone watching the screen (a Discord
    screen share, say) to read.
    """
    text = _KEY_IN_URL.sub(r"\1…", text)
    return text.replace(api_key, "…") if api_key else text


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


class TmdbClient:
    def __init__(self, api_key: str, language: str = "en-US") -> None:
        self.api_key = (api_key or "").strip()
        self.language = language or "en-US"
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": f"Mistery/{__version__}"})

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # --- transport ----------------------------------------------------------

    def _get(self, path: str, **params) -> dict | None:
        if not self.enabled:
            return None
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params.update(api_key=self.api_key, language=self.language)

        cache_key = f"tmdb:{path}:{json.dumps(sorted(params.items()))}"
        cached = db.cache_get(cache_key, _CACHE_TTL)
        if cached:
            try:
                return json.loads(cached)
            except ValueError:
                pass

        try:
            response = self._session.get(
                f"{API_BASE}{path}", params=params, timeout=_TIMEOUT
            )
        except requests.RequestException as exc:
            # Not chained: the original exception carries the URL with the key
            # in it, and would print it with any traceback of this error.
            raise TmdbError(_without_key(str(exc), self.api_key)) from None

        if response.status_code == 401:
            raise TmdbError("TMDB rejected the API key")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise TmdbError(f"TMDB returned HTTP {response.status_code}")

        try:
            data = response.json()
        except ValueError as exc:
            raise TmdbError(f"unreadable TMDB response: {exc}") from exc

        db.cache_put(cache_key, json.dumps(data))
        return data

    def verify_key(self) -> tuple[bool, str]:
        """Used by the settings screen to give immediate feedback.

        Blocks for as long as the network takes, so call it off the UI thread.
        """
        if not self.enabled:
            return False, "No API key set"
        try:
            response = self._session.get(
                f"{API_BASE}/configuration", params={"api_key": self.api_key},
                timeout=_VERIFY_TIMEOUT,
            )
        except requests.RequestException as exc:
            return False, f"Could not reach TMDB: {_without_key(str(exc), self.api_key)}"
        if response.status_code == 200:
            return True, "API key works"
        if response.status_code == 401:
            return False, "That key was rejected by TMDB"
        return False, f"TMDB returned HTTP {response.status_code}"

    # --- matching -----------------------------------------------------------

    def _best_match(self, results: list[dict], title: str, year: int | None,
                    date_field: str, title_field: str) -> tuple[dict | None, bool]:
        """Pick a result and report whether the match is confident.

        Confident means the titles agree once punctuation is stripped and, when
        both are known, the years are within a year of each other. Only then do
        we let TMDB's title replace the one parsed from the filename.
        """
        if not results:
            return None, False
        wanted = _normalise(title)

        best, best_score = None, -1.0
        for item in results[:8]:
            candidate = _normalise(item.get(title_field) or "")
            score = 0.0
            if candidate == wanted:
                score += 3.0
            elif wanted and (wanted in candidate or candidate in wanted):
                score += 1.4
            released = (item.get(date_field) or "")[:4]
            if year and released.isdigit():
                delta = abs(int(released) - year)
                if delta == 0:
                    score += 2.5
                elif delta == 1:
                    score += 1.0
                else:
                    score -= 1.5
            score += min(float(item.get("popularity") or 0), 60) / 240.0
            if score > best_score:
                best, best_score = item, score

        return best, best_score >= 3.0

    def find_movie(self, title: str, year: int | None) -> tuple[dict | None, bool]:
        data = self._get("/search/movie", query=title, year=year)
        if not data or not data.get("results"):
            if year:      # the filename year is often the release, not production
                data = self._get("/search/movie", query=title)
        results = (data or {}).get("results") or []
        return self._best_match(results, title, year, "release_date", "title")

    def find_show(self, title: str, year: int | None) -> tuple[dict | None, bool]:
        data = self._get("/search/tv", query=title, first_air_date_year=year)
        if not data or not data.get("results"):
            data = self._get("/search/tv", query=title)
        results = (data or {}).get("results") or []
        return self._best_match(results, title, year, "first_air_date", "name")

    def movie_details(self, movie_id: int) -> dict | None:
        return self._get(f"/movie/{movie_id}")

    def show_details(self, show_id: int) -> dict | None:
        return self._get(f"/tv/{show_id}")

    def episode_details(self, show_id: int, season: int, episode: int) -> dict | None:
        return self._get(f"/tv/{show_id}/season/{season}/episode/{episode}")

    # --- images -------------------------------------------------------------

    def download_image(
        self, remote_path: str | None, size: str, name: str
    ) -> tuple[str | None, str | None]:
        """(the file saved in the art folder, the address it was fetched from).

        image.tmdb.org is public and needs no key, and Discord's rich presence
        will fetch a picture from a public address itself — so the address is
        worth as much as the file now. It is only ever returned beside the file
        it fetched, so the two cannot drift apart later.
        """
        if not remote_path:
            return None, None
        destination = art_dir() / f"{name}{Path(remote_path).suffix or '.jpg'}"
        url = f"{IMAGE_BASE}/{size}{remote_path}"
        if destination.is_file() and destination.stat().st_size > 0:
            return str(destination), url
        try:
            response = self._session.get(url, timeout=_TIMEOUT * 2)
            if response.status_code != 200 or not response.content:
                return None, None
            destination.write_bytes(response.content)
        except (requests.RequestException, OSError):
            return None, None
        return str(destination), url

    # --- high level ---------------------------------------------------------

    def movie_fields(self, title: str, year: int | None) -> dict | None:
        """Everything we store for a movie, or None when there's no match."""
        match, confident = self.find_movie(title, year)
        if not match:
            return None
        movie_id = int(match["id"])
        details = self.movie_details(movie_id) or match

        poster, poster_url = self.download_image(
            details.get("poster_path"), POSTER_SIZE, f"tmdb-movie-{movie_id}-p"
        )
        backdrop, backdrop_url = self.download_image(
            details.get("backdrop_path"), BACKDROP_SIZE, f"tmdb-movie-{movie_id}-b"
        )
        fields: dict = {
            "tmdb_id": movie_id,
            "overview": details.get("overview") or None,
            "tagline": details.get("tagline") or None,
            "rating": details.get("vote_average") or None,
            "genres": ", ".join(g["name"] for g in details.get("genres") or []) or None,
            "poster": poster,
            "poster_url": poster_url,
            "backdrop": backdrop,
            "backdrop_url": backdrop_url,
            "meta_state": "done",
            "meta_source": "tmdb",
        }
        if confident:
            official = details.get("title")
            released = (details.get("release_date") or "")[:4]
            if official:
                fields["title"] = official
                fields["sort_title"] = re.sub(
                    r"^(the|a|an)\s+", "", official.lower()
                ).strip()
            if released.isdigit():
                fields["year"] = int(released)
        return {k: v for k, v in fields.items() if v is not None}

    def show_fields(self, title: str, year: int | None) -> dict | None:
        match, confident = self.find_show(title, year)
        if not match:
            return None
        show_id = int(match["id"])
        details = self.show_details(show_id) or match

        poster, poster_url = self.download_image(
            details.get("poster_path"), POSTER_SIZE, f"tmdb-tv-{show_id}-p"
        )
        backdrop, backdrop_url = self.download_image(
            details.get("backdrop_path"), BACKDROP_SIZE, f"tmdb-tv-{show_id}-b"
        )
        fields: dict = {
            "tmdb_id": show_id,
            "overview": details.get("overview") or None,
            "rating": details.get("vote_average") or None,
            "genres": ", ".join(g["name"] for g in details.get("genres") or []) or None,
            "poster": poster,
            "poster_url": poster_url,
            "backdrop": backdrop,
            "backdrop_url": backdrop_url,
            "meta_state": "done",
        }
        if confident and details.get("name"):
            fields["title"] = details["name"]
            first_air = (details.get("first_air_date") or "")[:4]
            if first_air.isdigit():
                fields["year"] = int(first_air)
        return {k: v for k, v in fields.items() if v is not None}

    def episode_fields(self, show_tmdb_id: int, season: int, episode: int) -> dict | None:
        details = self.episode_details(show_tmdb_id, season, episode)
        if not details:
            return None
        still, still_url = self.download_image(
            details.get("still_path"), BACKDROP_SIZE,
            f"tmdb-tv-{show_tmdb_id}-s{season}e{episode}",
        )
        fields = {
            "overview": details.get("overview") or None,
            "rating": details.get("vote_average") or None,
            "backdrop": still,
            "backdrop_url": still_url,
            "meta_state": "done",
            "meta_source": "tmdb",
        }
        if details.get("name"):
            fields["title"] = details["name"]
        return {k: v for k, v in fields.items() if v is not None}
