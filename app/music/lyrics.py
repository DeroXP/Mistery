"""Synced lyrics: find them, parse them, and know which line is being sung.

Sources, most trustworthy first:

    a .lrc file next to the track         you put it there
    lyrics embedded in the file's tags    the release put it there
    LRCLIB (lrclib.net)                   a free, keyless public database

The middle one needs watching. Albums downloaded from blogs often carry a
lyrics tag containing the blog's own address and nothing else — every song on
one album here carries that address and no words — and a tag like that
would otherwise win over a real, synced set of lyrics and never be asked about
again. So anything claiming to be lyrics has to read like lyrics first.

Only the last goes online, only for a track that is actually being played (and
the one queued after it), and every answer — including "none found" — is cached,
so a song is looked up once. Negative results are retried after a week, since
LRCLIB grows.
"""

from __future__ import annotations

import bisect
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .. import db

_log = logging.getLogger("music")

_STAMP_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_OFFSET_RE = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)
_RETRY_NONE_AFTER = 7 * 24 * 3600
from .. import __version__

_USER_AGENT = f"Mistery/{__version__} (local music player)"

# LRCLIB answers in well under a second when it is up. On a network that takes
# connections and never replies (a captive portal, stalled Wi-Fi, an outage)
# each request used to wait 8 s, three times over with pauses between: a lookup
# took 28 s to give up, and the song on screen could be waiting behind another
# song's lookup first, close to a minute of "Finding lyrics…". (connect, read)
# in seconds; a timeout is never retried, since waiting again only doubles it.
_TIMEOUT = (3.05, 6.0)
# After LRCLIB could not answer, songs are not looked up online for this long.
# They come back as 'unavailable', which is not cached, so each one is asked
# for again the next time it plays.
_OFFLINE_FOR = 60.0
_offline_until = 0.0


@dataclass
class Lyrics:
    lines: list[tuple[float, str]]   # (seconds, text); empty when not synced
    plain: str
    source: str                      # lrc | embedded | lrclib | instrumental | none | unavailable

    @property
    def synced(self) -> bool:
        return bool(self.lines)

    @property
    def available(self) -> bool:
        return bool(self.lines or self.plain.strip())

    def line_at(self, position: float) -> int:
        """Index of the line being sung at `position` seconds, or -1 before the first."""
        if not self.lines:
            return -1
        times = [stamp for stamp, _ in self.lines]
        return bisect.bisect_right(times, position + 0.15) - 1


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """LRC to (seconds, line), sorted — handling repeated stamps and [offset:]."""
    if not text:
        return []
    offset_match = _OFFSET_RE.search(text)
    # A positive offset means the lyrics should appear *earlier*.
    shift = -int(offset_match.group(1)) / 1000 if offset_match else 0.0
    lines: list[tuple[float, str]] = []
    for raw in text.splitlines():
        stamps = list(_STAMP_RE.finditer(raw))
        if not stamps:
            continue
        words = raw[stamps[-1].end():].strip()
        for stamp in stamps:
            minutes, seconds = int(stamp.group(1)), int(stamp.group(2))
            fraction = stamp.group(3) or "0"
            value = minutes * 60 + seconds + int(fraction) / (10 ** len(fraction))
            lines.append((max(0.0, value + shift), words))
    lines.sort(key=lambda item: item[0])
    return lines


_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
_MIN_WORDS = 12


def looks_like_lyrics(text: str) -> bool:
    """Is this someone's words, or the uploader's advertising?

    Web addresses are stripped before counting, so a real set of lyrics with a
    credit line at the bottom still passes, while a tag that is only a link to
    a blog has nothing left to count.
    """
    if not text or not text.strip():
        return False
    body = _URL_RE.sub(" ", text)
    for stamp in _STAMP_RE.finditer(body):    # timings are not words
        body = body.replace(stamp.group(0), " ")
    return len(_WORD_RE.findall(body)) >= _MIN_WORDS


def _embedded(path: str) -> str:
    try:
        import mutagen
        audio = mutagen.File(path)
    except Exception:
        return ""
    tags = getattr(audio, "tags", None)
    if not tags:
        return ""
    try:
        for key in ("LYRICS", "lyrics", "UNSYNCEDLYRICS", "unsyncedlyrics", "\xa9lyr"):
            value = tags.get(key) if hasattr(tags, "get") else None
            if value:
                return str(value[0] if isinstance(value, list) else value)
        for key, frame in tags.items():
            if str(key).startswith("USLT"):
                return str(getattr(frame, "text", "") or "")
    except Exception:
        return ""
    return ""


def _from_row(row) -> Lyrics:
    source = row["source"] or "none"
    return Lyrics(parse_lrc(row["synced"] or ""), row["plain"] or "", source)


def cached(track_id: int) -> Lyrics | None:
    row = db.query_one("SELECT * FROM lyrics WHERE track_id = ?", (track_id,))
    if row is None:
        return None
    if row["source"] == "none" and time.time() - (row["fetched_at"] or 0) > _RETRY_NONE_AFTER:
        return None
    # Kept lyrics are re-judged on the way out, so the junk stored by earlier
    # versions is dropped and looked up properly instead of living forever.
    if row["source"] not in ("none", "instrumental"):
        if not looks_like_lyrics(f"{row['synced'] or ''}\n{row['plain'] or ''}"):
            return None
    return _from_row(row)


def _store(track_id: int, synced: str, plain: str, source: str) -> Lyrics:
    db.execute(
        "INSERT OR REPLACE INTO lyrics (track_id, synced, plain, source, fetched_at) VALUES (?,?,?,?,?)",
        (track_id, synced, plain, source, time.time()),
    )
    return Lyrics(parse_lrc(synced), plain, source)


def find(track: dict, allow_online: bool = True) -> Lyrics:
    """Lyrics for a track row, from the best source that has them. Never raises."""
    track_id = int(track["id"])
    hit = cached(track_id)
    if hit is not None:
        return hit

    path = Path(track["path"])
    sidecar = path.with_suffix(".lrc")
    if sidecar.is_file():
        try:
            text = sidecar.read_text(encoding="utf-8-sig", errors="replace")
            if looks_like_lyrics(text):
                if parse_lrc(text):
                    return _store(track_id, text, "", "lrc")
                return _store(track_id, "", text, "lrc")
        except OSError:
            pass

    embedded = _embedded(str(path))
    if looks_like_lyrics(embedded):
        if parse_lrc(embedded):
            return _store(track_id, embedded, "", "embedded")
        return _store(track_id, "", embedded, "embedded")
    if embedded.strip():
        _log.info("ignoring the lyrics tag on %s (%d characters, no words): %s",
                  path.name, len(embedded.strip()), embedded.strip()[:60])

    if not allow_online:
        return Lyrics([], "", "none")
    return _lrclib(track)


class _Unavailable(Exception):
    """LRCLIB couldn't answer right now — which is not the same as 'no lyrics'."""


def _get(url: str, params: dict):
    """GET with one retry for a refused connection, rate limiting and server
    trouble, and none for a timeout.

    Returns the parsed body for 200, None for 404, and raises _Unavailable for
    anything else, so a transient failure is never cached as "has no lyrics".
    """
    import requests

    last = "no response"
    attempts = 2
    for attempt in range(attempts):
        retry = attempt + 1 < attempts
        try:
            response = requests.get(url, params=params, timeout=_TIMEOUT,
                                    headers={"User-Agent": _USER_AGENT})
        except requests.Timeout as exc:
            raise _Unavailable(f"{type(exc).__name__}: {exc}") from exc
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            if retry:
                time.sleep(0.6)
            continue
        if response.status_code == 200:
            return response.json()
        if response.status_code == 404:
            return None
        last = f"HTTP {response.status_code}"
        if response.status_code in (429, 500, 502, 503, 504):
            if retry:
                try:
                    wait = float(response.headers.get("Retry-After", 0))
                except ValueError:
                    wait = 0.0
                time.sleep(min(3.0, max(wait, 0.6)))
            continue
        break
    raise _Unavailable(last)


_FEAT_RE = re.compile(r"\s*[\(\[]?(?:feat|ft|featuring|with)\.?\s[^)\]]*[\)\]]?\s*$",
                      re.IGNORECASE)
_BRACKET_RE = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*$")
_ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|&|;|/|x|vs\.?|and)\s*", re.IGNORECASE)
_TIDY_RE = re.compile(r"[^a-z0-9]+")
# Past this much difference in running time it is a different recording — a
# remix, a live take, or the deluxe edition's twelve-minute version.
_DURATION_SLACK = 12.0


def _tidy(text: str) -> str:
    return _TIDY_RE.sub(" ", (text or "").lower()).strip()


def _title_forms(title: str) -> list[str]:
    """The same song as different releases write it.

    A file called "Long Time (Intro)" is "Long Time - Intro" on LRCLIB, and
    "Fell In Luv (feat. Bryson Tiller)" is usually just "Fell In Luv".
    """
    forms = [title]
    without_feat = _FEAT_RE.sub("", title).strip()
    bare = _BRACKET_RE.sub("", without_feat).strip()
    suffix = title[len(bare):].strip(" ()[]-") if title.startswith(bare) else ""
    if suffix and bare:
        forms.append(f"{bare} - {suffix}")
    for form in (without_feat, bare):
        if form and form not in forms:
            forms.append(form)
    return forms


def _artist_forms(track: dict) -> list[str]:
    """The credited artist, then just the first one named."""
    forms = []
    for value in (track.get("artist"), track.get("album_artist"),
                  track.get("album_artist_name")):
        value = (value or "").strip()
        if value and value not in forms:
            forms.append(value)
        if value:
            first = _ARTIST_SPLIT_RE.split(value)[0].strip()
            if first and first not in forms:
                forms.append(first)
    return forms


def _score(candidate: dict, title: str, artist: str, duration: float) -> float | None:
    """How much this result looks like the song in hand. None means: not it."""
    from difflib import SequenceMatcher

    name = _tidy(candidate.get("trackName"))
    wanted = _tidy(title)
    if not name:
        return None
    title_match = SequenceMatcher(None, name, wanted).ratio()
    # "Long Time - Intro" contains "long time"; treat that as a near match.
    if wanted and (wanted in name or name in wanted):
        title_match = max(title_match, 0.9)
    if title_match < 0.6:
        return None
    artist_match = SequenceMatcher(None, _tidy(candidate.get("artistName")),
                                   _tidy(artist)).ratio()
    if artist_match < 0.45:
        return None

    length = candidate.get("duration")
    if length and duration:
        difference = abs(float(length) - duration)
        if difference > _DURATION_SLACK:
            return None
        length_match = 1.0 - difference / _DURATION_SLACK
    else:
        length_match = 0.4          # unknown length: possible, but not proof

    return (title_match * 3 + artist_match * 2 + length_match * 3
            + (1.5 if candidate.get("syncedLyrics") else 0.0))


def _lrclib(track: dict) -> Lyrics:
    """Ask LRCLIB, trying the ways other people write the same song.

    The exact lookup is tried first because it is one cheap request and it hits
    most of the time. When it misses, a search is scored on title, artist and
    running time, and the best match wins — which is what finds a song whose
    release wrote "(Intro)" where LRCLIB wrote "- Intro".
    """
    try:
        import requests  # noqa: F401
    except ImportError:
        return Lyrics([], "", "none")

    track_id = int(track["id"])
    title = (track.get("title") or "").strip()
    album = track.get("album") or track.get("album_title") or ""
    duration = float(track.get("duration") or 0)
    artists = _artist_forms(track)
    if not title or not artists:
        return _store(track_id, "", "", "none")

    global _offline_until
    if time.monotonic() < _offline_until:
        return Lyrics([], "", "unavailable")

    titles = _title_forms(title)
    data = None
    try:
        data = _get("https://lrclib.net/api/get", {
            "track_name": title, "artist_name": artists[0],
            "album_name": album, "duration": round(duration),
        })
        if data is None:
            seen: list[dict] = []
            for form in titles[:3]:
                for artist in artists[:2]:
                    found = _get("https://lrclib.net/api/search",
                                 {"track_name": form, "artist_name": artist})
                    seen.extend(found or [])
                if seen:
                    break        # the plainest form that returns anything is enough
            scored = []
            for candidate in seen:
                mark = _score(candidate, title, artists[0], duration)
                if mark is not None:
                    scored.append((mark, candidate))
            if scored:
                scored.sort(key=lambda pair: pair[0], reverse=True)
                data = scored[0][1]
                _log.info("LRCLIB matched %s — %s to %r by %r (%.1f)", artists[0], title,
                          data.get("trackName"), data.get("artistName"), scored[0][0])
    except _Unavailable as exc:
        # Not cached, so the next play tries again; and nothing goes online for
        # a minute, so the song after this one is not kept waiting as well.
        _offline_until = time.monotonic() + _OFFLINE_FOR
        _log.warning("LRCLIB unavailable for %s — %s: %s; not asking again for %.0f s",
                     artists[0], title, exc, _OFFLINE_FOR)
        return Lyrics([], "", "unavailable")
    except Exception as exc:
        _log.warning("LRCLIB lookup failed for %s — %s: %s", artists[0], title, exc)
        return Lyrics([], "", "unavailable")

    if not data:
        return _store(track_id, "", "", "none")
    if data.get("instrumental"):
        return _store(track_id, "", "", "instrumental")
    synced, plain = data.get("syncedLyrics") or "", data.get("plainLyrics") or ""
    if not looks_like_lyrics(synced + "\n" + plain):
        return _store(track_id, "", "", "none")
    return _store(track_id, synced, plain, "lrclib")
