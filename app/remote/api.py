"""What the phone may ask this PC, through the website (relay.py).

Two kinds of address:

  /api/...         the library and commands. Every one must carry a signature
                   made with the phone's key (link.py), over the method, the
                   path, the time, a fresh nonce and the body:
                       X-Mistery-Time, X-Mistery-Nonce, X-Mistery-Sig
                   = hex HMAC-SHA256(key, "METHOD\\n/path?query\\ntime\\nnonce\\nsha256(body)")
                   A wrong one gets 401 and nothing else; one more than two
                   minutes off this PC's clock gets 401 with "now", so a phone
                   whose clock is wrong can try once more; a nonce seen before is
                   refused, so a request copied off the wire does nothing twice.
  /m/<token>/...   pictures, songs and films, which an <img>, <audio> or <video>
                   asks for and cannot sign. The token comes from a signed
                   request, names exactly one thing (or the pictures), and lasts
                   TOKEN_HOURS.

    GET  /api/hello                 this PC's name, whether its window is open, the pictures' token
    GET  /api/library               albums, films, shows, playlists
    GET  /api/album/<id>            an album and its songs
    GET  /api/show/<id>             a show and its episodes
    GET  /api/playlist/<kind>/<id>  a playlist (music or video): your own things in it
    GET  /api/liked                 your liked songs
    GET  /api/songs?q=              songs matching a search
    POST /api/stream {kind, id}     a song or a film for this phone: {url, type, start, duration}
    POST /api/progress {kind, id, position, duration}   where the phone got to in a film
    GET  /api/now                   what the PC is playing (bridge.py)
    POST /api/do {target, action, value}                 play, pause, skip, seek, volume on the PC
    POST /api/play {kind, id}       play that on the PC

    GET  /m/<t>/art/<kind>/<id>     a picture (album, movie, show, episode)
    GET  /m/<t>/song/<id>           a song as it is, when a phone plays its kind (MP3, AAC)
    GET  /m/<t>/hls/index.m3u8, media.m3u8, <n>.ts, subs/<k>.m3u8, subs/<k>.vtt
                                    a film (720p), or a song that has to be made (AAC)

Friends' things are not offered here: they are on their PCs, not this one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

from .. import __version__, db
from . import link
from .relay import Reply

_log = logging.getLogger("remote")

SKEW = 120.0                    # seconds a phone's clock may be off this PC's
TOKEN_HOURS = 6.0
FILM_QUALITY = "720p"           # through the website: about 3 Mbit/s rather than 1080p's 6-8
FILM_IDLE = 180.0               # a film nobody has asked anything of for this long is closed
MAX_FILMS = 2                   # films (or made songs) being streamed at once
PLAYABLE_AS_IS = {".mp3": ("MP3", "audio/mpeg"), ".m4a": ("AAC", "audio/mp4"), ".aac": ("AAC", "audio/aac")}
_TOKEN = re.compile(r"[A-Za-z0-9_-]{22}")

_lock = threading.Lock()
_nonces: dict[str, float] = {}
_tokens: dict[str, dict] = {}   # token -> {"what": ..., "until": ..., ...}
_films: dict[str, object] = {}  # token -> phone._Film
_failures: list[float] = []


def forget() -> None:
    """Everything handed out goes: the remote was switched off."""
    with _lock:
        films = list(_films.values())
        _films.clear()
        _tokens.clear()
        _nonces.clear()
    for film in films:
        film.close()


# --- answering ---------------------------------------------------------------------------------

def _json(status: int, payload: dict) -> Reply:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return Reply(status, {"content-type": "application/json; charset=utf-8",
                          "content-length": str(len(body)), "cache-control": "no-store"}, [body])


def _problem(status: int, words: str) -> Reply:
    return _json(status, {"error": words})


def handle(method: str, path: str, query: str, headers: dict, body: bytes) -> Reply:
    try:
        if path.startswith("/m/"):
            return _media(method, path, headers)
        if path.startswith("/api/"):
            refused = _check(method, path, query, headers, body)
            if refused is not None:
                return refused
            return _api(method, path, parse_qs(query), body)
        return _problem(404, "Nothing here.")
    except LookupError as problem:
        return _problem(404 if "window" not in str(problem) else 409, str(problem))
    except (ValueError, TypeError) as problem:
        return _problem(400, str(problem) or "That request doesn't make sense.")
    except TimeoutError as problem:
        return _problem(503, str(problem))
    except Exception:                                # noqa: BLE001 - said, and logged
        _log.exception("remote: %s %s failed", method, path)
        return _problem(500, "Something went wrong on your PC.")
    finally:
        db.close_thread_connection()


def _check(method: str, path: str, query: str, headers: dict, body: bytes) -> Reply | None:
    """None when the request is the phone's own; otherwise the answer to give."""
    now = time.time()
    with _lock:
        recent = [moment for moment in _failures if now - moment < 60]
        _failures[:] = recent
        if len(recent) > 30:
            return _problem(429, "Too many wrong attempts. Wait a minute.")
    stamp, nonce, signature = (headers.get("x-mistery-time", ""), headers.get("x-mistery-nonce", ""),
                               headers.get("x-mistery-sig", ""))
    try:
        sent_at = int(stamp)
    except ValueError:
        sent_at = None
    if sent_at is None or not re.fullmatch(r"[0-9a-f]{16}", nonce) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        return _refused("This phone isn't linked to this PC.")
    target = path + (f"?{query}" if query else "")
    text = f"{method}\n{target}\n{stamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}"
    wanted = hmac.new(link.key_bytes(), text.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(wanted, signature):
        return _refused("This phone's link isn't valid for this PC any more.")
    if abs(now - sent_at) > SKEW:
        return _json(401, {"error": "This phone's clock is off.", "now": int(now)})
    with _lock:
        for seen, moment in list(_nonces.items()):
            if now - moment > SKEW * 2:
                del _nonces[seen]
        if nonce in _nonces:
            return _problem(401, "That request was already answered.")
        _nonces[nonce] = now
    return None


def _refused(words: str) -> Reply:
    with _lock:
        _failures.append(time.time())
    return _problem(401, words)


def _body(body: bytes) -> dict:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        raise ValueError("That request doesn't make sense.") from None
    if not isinstance(data, dict):
        raise ValueError("That request doesn't make sense.")
    return data


def _number(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("That isn't something in your library.")
    return value


def _api(method: str, path: str, query: dict, body: bytes) -> Reply:
    parts = path.strip("/").split("/")[1:]
    if method == "GET" and parts == ["hello"]:
        return _json(200, _hello())
    if method == "GET" and parts == ["library"]:
        return _json(200, _library())
    if method == "GET" and len(parts) == 2 and parts[0] == "album" and parts[1].isdigit():
        return _json(200, _album(int(parts[1])))
    if method == "GET" and len(parts) == 2 and parts[0] == "show" and parts[1].isdigit():
        return _json(200, _show(int(parts[1])))
    if method == "GET" and len(parts) == 3 and parts[0] == "playlist" and parts[1] in ("music", "video") \
            and parts[2].isdigit():
        return _json(200, _playlist(parts[1], int(parts[2])))
    if method == "GET" and parts == ["liked"]:
        from ..music import library

        return _json(200, {"songs": [_song(row) for row in library.liked_tracks()]})
    if method == "GET" and parts == ["songs"]:
        from ..music import library

        text = (query.get("q") or [""])[0].strip()[:100]
        rows = library.search(text)[:200] if text else []
        return _json(200, {"songs": [_song(row) for row in rows]})
    if method == "POST" and parts == ["stream"]:
        data = _body(body)
        return _json(200, _stream(str(data.get("kind")), _number(data.get("id"))))
    if method == "POST" and parts == ["progress"]:
        data = _body(body)
        _progress(str(data.get("kind")), _number(data.get("id")), data.get("position"), data.get("duration"))
        return _json(200, {"ok": True})
    if method == "GET" and parts == ["now"]:
        from . import bridge

        if not bridge.available():
            return _json(200, {"window": False, "music": None, "video": None})
        return _json(200, bridge.run(bridge.now))
    if method == "POST" and parts == ["do"]:
        from . import bridge

        data = _body(body)
        target, action = data.get("target"), data.get("action")
        if target not in ("music", "video") or not isinstance(action, str):
            raise ValueError("That isn't something a player does.")
        bridge.run(lambda window: bridge.do(window, target, action, data.get("value")))
        return _json(200, {"ok": True})
    if method == "POST" and parts == ["play"]:
        from . import bridge

        data = _body(body)
        kind = str(data.get("kind"))
        if kind == "playlist":
            playlist = db.playlist(_number(data.get("id")))
            if playlist is None:
                raise LookupError("That playlist isn't on your PC any more.")
            kind = f"playlist-{playlist['kind']}"
        number = _number(data.get("id"))
        said = bridge.run(lambda window: bridge.play(window, kind, number))
        return _json(200, {"said": said})
    return _problem(404, "Nothing here.")


# --- the library, as the phone sees it ------------------------------------------------------------

def _token(what: dict) -> str:
    token = secrets.token_urlsafe(16)
    with _lock:
        now = time.time()
        for old, entry in list(_tokens.items()):
            if entry["until"] < now:
                del _tokens[old]
                film = _films.pop(old, None)
                if film is not None:
                    film.close()
        _tokens[token] = {**what, "until": now + TOKEN_HOURS * 3600}
    return token


def _hello() -> dict:
    from ..party import people
    from . import bridge

    return {"name": people.display_name() or "Your PC", "version": __version__,
            "window": bridge.available(), "art": f"/m/{_token({'what': 'art'})}/art"}


def _library() -> dict:
    from ..models import MediaItem
    from ..music import library

    albums = [{"id": int(row["id"]), "title": row["title"] or "Untitled", "artist": row["artist"] or "",
               "year": row["year"], "art": bool(row["cover"])} for row in library.albums("artist")]
    movies = []
    for row in db.movies():
        film = MediaItem.from_row(row)
        movies.append({"id": film.id, "title": film.title, "year": film.year, "duration": film.duration,
                       "progress": round(film.progress, 4), "watched": bool(film.watched),
                       "overview": (row["overview"] or "")[:1200], "art": bool(row["poster"])})
    shows = [{"id": int(row["id"]), "title": row["title"] or "Untitled", "year": row["year"],
              "episodes": int(row["episode_count"] or 0), "art": bool(row["poster"])}
             for row in db.all_shows()]
    playlists = {kind: [{"id": int(row["id"]), "name": row["name"] or "Untitled",
                         "count": int(row["item_count"] or 0)} for row in db.playlists(kind)]
                 for kind in ("music", "video")}
    return {"albums": albums, "movies": movies, "shows": shows, "playlists": playlists}


def _song(row) -> dict:
    row = dict(row)
    return {"id": int(row["id"]), "title": row.get("title") or "Untitled",
            "artist": row.get("artist") or row.get("album_artist") or "",
            "album": row.get("album_title") or row.get("album") or "", "album_id": row.get("album_id"),
            "duration": float(row.get("duration") or 0.0), "track_no": row.get("track_no"),
            "disc_no": row.get("disc_no"), "art": bool(row.get("cover"))}


def _album(album_id: int) -> dict:
    from ..music import library

    album = library.album(album_id)
    if album is None:
        raise LookupError("That album isn't on your PC any more.")
    return {"album": {"id": album_id, "title": album["title"], "artist": album["artist"], "year": album["year"],
                      "art": bool(album["cover"])},
            "songs": [_song(row) for row in library.album_tracks(album_id) if row["state"] == "ready"]}


def _show(show_id: int) -> dict:
    from ..models import MediaItem

    show = db.get_show(show_id)
    if show is None:
        raise LookupError("That show isn't on your PC any more.")
    episodes = []
    for row in db.episodes_for_show(show_id):
        if row["missing"]:
            continue
        item = MediaItem.from_row(row)
        episodes.append({"id": item.id, "title": item.title, "season": item.season, "episode": item.episode,
                         "duration": item.duration, "progress": round(item.progress, 4),
                         "watched": bool(item.watched)})
    episodes.sort(key=lambda e: ((e["season"] if e["season"] is not None else 9999), e["episode"] or 0))
    return {"show": {"id": show_id, "title": show["title"], "year": show["year"], "art": bool(show["poster"])},
            "episodes": episodes}


def _playlist(kind: str, playlist_id: int) -> dict:
    row = db.playlist(playlist_id)
    if row is None or row["kind"] != kind:
        raise LookupError("That playlist isn't on your PC any more.")
    if kind == "music":
        from ..music import library

        songs = [_song(song) for song in library.playlist_tracks(playlist_id) if not song.get("friend")]
        return {"name": row["name"], "songs": songs}
    items = [{"kind": media["kind"], "id": int(media["id"]), "title": media["title"], "year": media["year"],
              "duration": media["duration"]} for media in db.playlist_media(playlist_id)]
    return {"name": row["name"], "items": items}


# --- songs and films for the phone ------------------------------------------------------------------

def _stream(kind: str, number: int) -> dict:
    if kind == "track":
        from ..music import library

        row = library.track(number)
        if row is None or not row["path"] or not os.path.isfile(row["path"]):
            raise LookupError("That song isn't on your PC any more.")
        suffix = Path(row["path"]).suffix.lower()
        if suffix in PLAYABLE_AS_IS and (row["codec"] or "") == PLAYABLE_AS_IS[suffix][0]:
            token = _token({"what": "song", "id": number})
            return {"url": f"/m/{token}/song/{number}{suffix}", "type": "file", "start": 0,
                    "duration": row["duration"]}
        return _made(row["path"], float(row["duration"] or 0), audio_only=True, start=0.0)
    if kind in ("movie", "episode"):
        row = db.get_media(number)
        if row is None or row["missing"] or row["kind"] != kind or not os.path.isfile(row["path"]):
            raise LookupError("That isn't on your PC any more.")
        from ..models import MediaItem

        item = MediaItem.from_row(row)
        if not item.duration:
            from ..party.session import _measured

            item = _measured(item)
        return _made(item.path, float(item.duration or 0), audio_only=False, start=item.resume_position)
    raise ValueError("That can't be played on a phone.")


def _made(path: str, duration: float, *, audio_only: bool, start: float) -> dict:
    """A film (720p) or a song (AAC) made for the phone as it is asked for, in
    the segments phone movie nights use (party/phone.py _Film)."""
    from ..party.phone import _Film

    if duration <= 0:
        raise LookupError("Your PC can't tell how long that is yet. Try again in a minute.")
    film = _Film(secrets.token_hex(4), path, duration, quality=FILM_QUALITY, audio_only=audio_only)
    token = _token({"what": "hls"})
    with _lock:
        _films[token] = film
        film.asked_at = time.monotonic()
        idle = sorted(_films.items(), key=lambda pair: getattr(pair[1], "asked_at", 0.0))
        while len(idle) > MAX_FILMS:
            old_token, old = idle.pop(0)
            _films.pop(old_token, None)
            _tokens.pop(old_token, None)
            old.close()
    _tidy_later()
    return {"url": f"/m/{token}/hls/index.m3u8", "type": "hls", "start": float(start or 0.0),
            "duration": duration}


_tidier: threading.Timer | None = None


def _tidy_later() -> None:
    """Close films nobody has asked anything of for FILM_IDLE (their ffmpeg and folder)."""
    global _tidier
    if _tidier is not None:
        return

    def tidy() -> None:
        global _tidier
        now = time.monotonic()
        with _lock:
            stale = [token for token, film in _films.items() if now - getattr(film, "asked_at", now) > FILM_IDLE]
            gone = [_films.pop(token) for token in stale]
            for token in stale:
                _tokens.pop(token, None)
            _tidier = None
            busy = bool(_films)
        for film in gone:
            film.close()
        if busy:
            _tidy_later()

    _tidier = threading.Timer(60.0, tidy)
    _tidier.daemon = True
    _tidier.start()


def _progress(kind: str, number: int, position, duration) -> None:
    if kind not in ("movie", "episode"):
        return
    if not isinstance(position, (int, float)) or not isinstance(duration, (int, float)) or duration <= 0:
        raise ValueError("That isn't a place in a film.")
    row = db.get_media(number)
    if row is None:
        raise LookupError("That isn't on your PC any more.")
    position = max(0.0, min(float(position), float(duration)))
    db.set_progress(number, position, float(duration), watched=True if position >= duration * 0.93 else None)


# --- pictures, songs and films by token ------------------------------------------------------------

def _media(method: str, path: str, headers: dict) -> Reply:
    parts = path.strip("/").split("/")
    if len(parts) < 3 or not _TOKEN.fullmatch(parts[1]):
        return _problem(404, "Nothing here.")
    with _lock:
        entry = _tokens.get(parts[1])
        if entry is not None and entry["until"] < time.time():
            entry = None
    if entry is None:
        return _problem(404, "That link has run out. Go back and choose it again.")
    rest = parts[2:]
    if entry["what"] == "art" and rest[0] == "art" and len(rest) == 3 and rest[2].isdigit():
        return _art(rest[1], int(rest[2]), headers)
    if entry["what"] == "song" and rest[0] == "song" and len(rest) == 2:
        from ..music import library

        row = library.track(entry["id"])
        if row is None or not rest[1].startswith(f"{entry['id']}."):
            return _problem(404, "That song isn't on your PC any more.")
        kind = PLAYABLE_AS_IS.get(Path(row["path"]).suffix.lower(), ("", "application/octet-stream"))[1]
        return _file(row["path"], kind, headers, "private, max-age=3600")
    if entry["what"] == "hls" and rest[0] == "hls":
        return _hls(parts[1], rest[1:], headers)
    return _problem(404, "Nothing here.")


def _art(kind: str, number: int, headers: dict) -> Reply:
    picture = None
    if kind == "album":
        row = db.query_one("SELECT cover FROM albums WHERE id = ?", (number,))
        if row is not None and row["cover"]:
            small = row["cover"].replace(".jpg", "-sm.jpg")
            picture = small if os.path.isfile(small) else row["cover"]
    elif kind in ("movie", "episode"):
        row = db.query_one("SELECT poster, backdrop FROM media WHERE id = ?", (number,))
        picture = row and (row["poster"] or row["backdrop"])
    elif kind == "show":
        row = db.query_one("SELECT poster FROM shows WHERE id = ?", (number,))
        picture = row and row["poster"]
    if not picture or not os.path.isfile(picture):
        return _problem(404, "No picture.")
    kind_of = mimetypes.guess_type(picture)[0] or "image/jpeg"
    return _file(picture, kind_of, headers, "private, max-age=86400")


def _hls(token: str, rest: list[str], headers: dict) -> Reply:
    with _lock:
        film = _films.get(token)
        if film is not None:
            film.asked_at = time.monotonic()
    if film is None:
        return _problem(404, "That has stopped. Go back and choose it again.")
    playlist = "application/vnd.apple.mpegurl"
    if rest == ["index.m3u8"]:
        return _bytes(film.master(), playlist)
    if rest == ["media.m3u8"]:
        return _bytes(film.media(), playlist)
    if len(rest) == 1 and rest[0].endswith(".ts") and rest[0][:-3].isdigit():
        segment = film.segment(int(rest[0][:-3]))
        if segment is None:
            return _problem(503, "Your PC couldn't make that part in time.")
        return _file(str(segment), "video/mp2t", headers, "private, max-age=3600")
    if len(rest) == 2 and rest[0] == "subs" and re.fullmatch(r"\d{1,3}\.(m3u8|vtt)", rest[1]):
        number, _, ext = rest[1].partition(".")
        made = film.subtitle_playlist(int(number)) if ext == "m3u8" else film.subtitle(int(number))
        if made is None:
            return _problem(404, "No such subtitles.")
        return _bytes(made, playlist if ext == "m3u8" else "text/vtt; charset=utf-8")
    return _problem(404, "Nothing here.")


def _bytes(data: bytes, kind: str) -> Reply:
    return Reply(200, {"content-type": kind, "content-length": str(len(data)), "cache-control": "no-store"}, [data])


def _file(path: str, kind: str, headers: dict, cache: str) -> Reply:
    """A file, whole or the one range asked for (a phone seeking in a song)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return _problem(404, "That isn't on your PC any more.")
    first, last, status = 0, size - 1, 200
    wanted = re.fullmatch(r"bytes=(\d*)-(\d*)", headers.get("range", "").strip())
    if wanted and (wanted.group(1) or wanted.group(2)):
        if wanted.group(1):
            first = int(wanted.group(1))
            last = min(size - 1, int(wanted.group(2))) if wanted.group(2) else size - 1
        else:
            first = max(0, size - int(wanted.group(2)))
        if first > last or first >= size:
            return Reply(416, {"content-range": f"bytes */{size}", "content-length": "0"}, [])
        status = 206
    length = last - first + 1
    answer = {"content-type": kind, "content-length": str(length), "accept-ranges": "bytes", "cache-control": cache}
    if status == 206:
        answer["content-range"] = f"bytes {first}-{last}/{size}"

    def pieces():
        with open(path, "rb") as handle:
            handle.seek(first)
            left = length
            while left > 0:
                block = handle.read(min(256 * 1024, left))
                if not block:
                    return
                left -= len(block)
                yield block

    return Reply(status, answer, pieces())
