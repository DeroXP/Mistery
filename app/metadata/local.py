"""What sits beside the videos: .nfo files and pictures.

Kodi, Jellyfin, Emby and Plex all read (and most of them write) the same few
files next to a film or a series: a .nfo holding its title and story as XML, a
poster, a fanart. Someone who has looked after a library for years has these
already, with the titles and pictures they chose, and a lookup by file name can
only do worse. So they are read first, and the internet is asked only for what
they leave out.

They are only ever read. Nothing here opens one of them for writing, renames
one or removes one: other programs own these files. A picture is copied into
the art folder at the size it is shown at (fit.py), under a name made from where
it came from and when it last changed, so a picture that is replaced is a new
file there and the old one is never shown in its place.

    A film in a folder of its own       A film in a folder with others
      Low Tide (2019).mkv                 Low Tide (2019).mkv
      Low Tide (2019).nfo  or movie.nfo   Low Tide (2019).nfo
      poster.jpg  folder.jpg  cover.jpg   Low Tide (2019)-poster.jpg
      fanart.jpg  backdrop.jpg            Low Tide (2019)-fanart.jpg

    A series                            An episode
      Harbor Lights/tvshow.nfo            Season 01/Harbor Lights S01E01.mkv
      Harbor Lights/poster.jpg            Season 01/Harbor Lights S01E01.nfo
      Harbor Lights/fanart.jpg            Season 01/Harbor Lights S01E01-thumb.jpg

A name that speaks for a whole folder (poster.jpg, movie.nfo, tvshow.nfo) is
believed only where the folder holds that one thing: in a folder of twelve films
poster.jpg is nobody's, and tvshow.nfo above two series is neither's.
"""

from __future__ import annotations

import hashlib
import html
import io
import logging
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .. import parser
from ..config import art_dir, settings
from . import categories, fit

_log = logging.getLogger("local")

NFO = ".nfo"
# .tbn is Kodi's old name for a thumbnail: a JPEG or PNG by another suffix.
PICTURES = (".jpg", ".jpeg", ".png", ".webp", ".tbn")
_BESIDE = (NFO, *PICTURES)
# The copy's name in the art folder. What follows the prefix is made from the
# picture's own path, size and time: see _copy.
COPY_PREFIX = "local-"

# What follows the video's own name, most telling first. A picture named exactly
# as the video is (Plex's way) comes last, and is only believed when it has the
# shape of what it would be used as (_copy).
_OWN_POSTER = "-poster"
_OWN_BACKDROP = ("-fanart", "-backdrop", "-background", "-landscape", "-thumb")
_OWN_STILL = "-thumb"
_FOLDER_POSTER = ("poster", "folder", "cover", "movie", "show", "default")
_FOLDER_BACKDROP = ("fanart", "backdrop", "background", "art", "landscape")

# A .nfo is a few KB, forty with a long cast. Anything past this is something
# else that happens to end in .nfo, and a picture past its limit is not a poster.
_MAX_NFO = 2 * 1024 * 1024
_MAX_PICTURE = 40 * 1024 * 1024

_ROOT = re.compile(r"<(movie|tvshow|episodedetails)\b[^>]*>", re.IGNORECASE)
# An ampersand that starts no entity: "Fast & Loose" typed into a title by hand.
# Kodi's own reader lets those through, so they are everywhere.
_BARE_AMPERSAND = re.compile(r"&(?!(?:[A-Za-z][A-Za-z0-9]*|#[0-9]+|#x[0-9A-Fa-f]+);)")
_NAMED_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
# A "<" that opens no tag: "2 < 3", or the heart in "I <3 this".
_BARE_LESS_THAN = re.compile(r"<(?![A-Za-z/!?])")
_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_ARTICLE = re.compile(r"^(the|a|an)\s+")


def enabled() -> bool:
    return bool(settings.get("local_metadata", True))


def is_copy(path: object) -> bool:
    """Whether that artwork is a copy this module made of a picture beside a video."""
    return bool(path) and Path(str(path)).name.startswith(COPY_PREFIX)


# --- which files ------------------------------------------------------------

@dataclass(frozen=True)
class Beside:
    """The files that go with one film, episode or series."""

    nfo: Path | None = None
    poster: Path | None = None
    backdrop: Path | None = None
    # "poster" or "backdrop" when that picture is only named like the video
    # itself, and so has still to show it is the right shape.
    unsure: str = ""

    def signature(self, measure=None) -> str | None:
        """Changes when any of them is added, replaced, renamed or taken away;
        None when there are none. Nothing is opened: a size and a time each.

        It begins with a letter for each kind of file it was made from (n, p,
        b), so that what used to be there can be read off an old one: had_nfo.
        `measure` gives a path's (size, time); a stat when nobody brings one.
        """
        kinds, parts = "", []
        for kind, path in (("n", self.nfo), ("p", self.poster), ("b", self.backdrop)):
            if path is None:
                continue
            try:
                size, changed = measure(path) if measure is not None else _measure(path)
            except OSError:
                continue
            kinds += kind
            parts.append(f"{kind}|{path.name}|{size}|{changed}")
        if not parts:
            return None
        return kinds + "-" + hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


NOTHING = Beside()


def _measure(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def had_nfo(signature: object) -> bool:
    """Whether a signature was made from, among other things, a .nfo."""
    return "n" in str(signature or "").split("-", 1)[0]


class Layout:
    """What each folder of the library holds, from the library's own rows: the
    answer to whether a folder-wide name can be believed there."""

    def __init__(self, rows: Iterable) -> None:
        self._videos: Counter[str] = Counter()
        self._owners: dict[str, set] = {}           # folder -> show ids, None for a film
        self._below: dict[str, set[str]] = {}       # folder -> the folders of videos one level down
        self._episodes: dict[int, set[str]] = {}    # show id -> the folders its episodes are in
        for row in rows:
            folder = row["folder"] or str(Path(row["path"]).parent)
            key = os.path.normcase(folder)
            show = row["show_id"] if row["kind"] == "episode" else None
            self._videos[key] += 1
            self._owners.setdefault(key, set()).add(show)
            self._below.setdefault(os.path.normcase(str(Path(folder).parent)), set()).add(key)
            if show is not None:
                self._episodes.setdefault(int(show), set()).add(folder)

    def alone(self, folder: Path | str) -> bool:
        """Whether that folder holds one video and no more."""
        return self._videos[os.path.normcase(str(folder))] == 1

    def episode_folders(self, show_id: int) -> list[str]:
        return sorted(self._episodes.get(int(show_id), ()))

    def show_folders(self, show_id: int) -> list[Path]:
        """The folders that are this series' own, nearest its episodes first:
        where its tvshow.nfo and poster would be.

        An episode's folder, or the one above a "Season 2" folder, and the one
        above that for a series kept as "Harbor Lights/Harbor Lights S02/". A
        folder counts only if every video in it, and in the folders straight
        under it, is this series': the folder of a whole collection never does.
        """
        found: list[Path] = []
        for folder in self.episode_folders(show_id):
            here = Path(folder)
            season = bool(parser._SEASON_DIR_RE.match(here.name))
            for candidate in ((here.parent,) if season else (here, here.parent)):
                if candidate not in found and self._is_only(candidate, int(show_id)):
                    found.append(candidate)
        return found

    def _is_only(self, folder: Path, show_id: int) -> bool:
        key = os.path.normcase(str(folder))
        inside = [key, *self._below.get(key, ())]
        owners = [self._owners[name] for name in inside if name in self._owners]
        return bool(owners) and all(owner == {show_id} for owner in owners)


class Finder:
    """Finds the files beside a video by their names, and reads them.

    One for a scan or a metadata pass: it remembers each folder's listing, so
    a season of twenty episodes is one look at its folder and not twenty. The
    scan hands over the listings its own walk made (`listed`), which makes
    looking for these files cost a library without any nothing at all.
    """

    def __init__(self) -> None:
        self._folders: dict[str, dict[str, str]] = {}
        self._measures: dict[str, dict[str, tuple[int, int]]] = {}

    def signature(self, found: Beside) -> str | None:
        """A signature for those files, from one listing of each folder."""
        return found.signature(self._measure)

    def _measure(self, path: Path) -> tuple[int, int]:
        """A file's size and time, from a listing of its folder that is made
        once. Windows hands both over with the listing itself, so a folder is
        one question however many files beside its videos there are; a stat
        apiece is three round trips each when the library is on another PC.
        """
        key = os.path.normcase(str(path.parent))
        measures = self._measures.get(key)
        if measures is None:
            measures = self._measures[key] = {}
            try:
                with os.scandir(path.parent) as listing:
                    for entry in listing:
                        if entry.name.lower().endswith(_BESIDE):
                            try:
                                stat = entry.stat()
                            except OSError:
                                continue
                            measures[entry.name.lower()] = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                pass
        return measures.get(path.name.lower()) or _measure(path)

    def listed(self, folder: str, names: Iterable[str]) -> None:
        """A folder's files, from whoever has just listed it."""
        self._folders[os.path.normcase(str(folder))] = {
            name.lower(): name for name in names if name.lower().endswith(_BESIDE)}

    def walked(self, folder: object) -> bool:
        return os.path.normcase(str(folder)) in self._folders

    def _names(self, folder: Path) -> dict[str, str]:
        key = os.path.normcase(str(folder))
        if key not in self._folders:
            try:
                self.listed(str(folder), os.listdir(folder))
            except OSError:
                self._folders[key] = {}
        return self._folders[key]

    def _pick(self, folder: Path, stems: Iterable[str],
              suffixes: tuple[str, ...] = PICTURES) -> Path | None:
        names = self._names(folder)
        for stem in stems:
            for suffix in suffixes:
                actual = names.get((stem + suffix).lower())
                if actual:
                    return folder / actual
        return None

    def film(self, video: Path, alone: bool) -> Beside:
        folder, stem = video.parent, video.stem
        nfo = self._pick(folder, [stem], (NFO,))
        poster = self._pick(folder, [stem + _OWN_POSTER])
        backdrop = self._pick(folder, [stem + ending for ending in _OWN_BACKDROP])
        if alone:
            nfo = nfo or self._pick(folder, ["movie"], (NFO,))
            poster = poster or self._pick(folder, _FOLDER_POSTER)
            backdrop = backdrop or self._pick(folder, _FOLDER_BACKDROP)
        unsure = ""
        if poster is None:
            poster = self._pick(folder, [stem])
            unsure = "poster" if poster is not None else ""
        return Beside(nfo, poster, backdrop, unsure)

    def episode(self, video: Path) -> Beside:
        folder, stem = video.parent, video.stem
        still = self._pick(folder, [stem + _OWN_STILL])
        unsure = ""
        if still is None:
            still = self._pick(folder, [stem])
            unsure = "backdrop" if still is not None else ""
        return Beside(self._pick(folder, [stem], (NFO,)), None, still, unsure)

    def show(self, folders: Iterable[Path]) -> Beside:
        nfo = poster = backdrop = None
        for folder in folders:
            nfo = nfo or self._pick(folder, ["tvshow"], (NFO,))
            poster = poster or self._pick(folder, _FOLDER_POSTER)
            backdrop = backdrop or self._pick(folder, _FOLDER_BACKDROP)
        return Beside(nfo, poster, backdrop)

    def beside(self, row, layout: Layout) -> Beside:
        """The files for a row of the media table."""
        video = Path(row["path"])
        if row["kind"] == "episode":
            return self.episode(video)
        return self.film(video, layout.alone(video.parent))

    # --- what they say ------------------------------------------------------

    def for_media(self, row, layout: Layout) -> "Info":
        """What the files beside a film or an episode give for its row.

        Nothing, said in the log, if they cannot be read for a reason nobody
        foresaw: these are other programs' files, in whatever state they were
        left, and one of them must never stop a library pass.
        """
        try:
            return self._for_media(row, layout) if enabled() else Info()
        except Exception:               # noqa: BLE001
            _log.exception("local: could not read the files beside %s", row["path"])
            return Info()

    def for_show(self, show, layout: Layout) -> "Info":
        """What tvshow.nfo and the pictures in a series' own folder give for it."""
        try:
            return self._for_show(show, layout) if enabled() else Info()
        except Exception:               # noqa: BLE001
            _log.exception("local: could not read the files in the folder of %s", show["title"])
            return Info()

    def _for_media(self, row, layout: Layout) -> "Info":
        found = self.beside(row, layout)
        film = row["kind"] != "episode"
        info = Info()
        nfo = read_nfo(found.nfo, "movie" if film else "episodedetails", row["episode"]) \
            if found.nfo else None
        if nfo is not None:
            info.described = True
            fields = info.fields
            if nfo.title:
                fields["title"] = nfo.title
                fields["sort_title"] = (nfo.sort_title or _sortable(nfo.title)) if film \
                    else nfo.title.lower()
            if film and nfo.year:
                fields["year"] = nfo.year
            if nfo.plot:
                fields["overview"] = nfo.plot
            if film and nfo.tagline:
                fields["tagline"] = nfo.tagline
            if film and _known(nfo.genres):
                fields["genres"] = ", ".join(nfo.genres)
            if nfo.rating:
                fields["rating"] = nfo.rating
            if film and nfo.ids.get("tmdb", "").isdigit():
                fields["tmdb_id"] = int(nfo.ids["tmdb"])
        if film and found.poster:
            poster = _copy(found.poster, "poster", upright=True if found.unsure == "poster" else None)
            if poster:
                info.fields["poster"] = poster
        if found.backdrop:
            backdrop = _copy(found.backdrop, "backdrop",
                             upright=False if found.unsure == "backdrop" else None)
            if backdrop:
                info.fields["backdrop"] = backdrop
        # A film's wide picture can always be cut from the film; its story and
        # its poster cannot. An episode's still is the only picture it has.
        info.complete = bool(info.fields.get("overview")) and bool(
            info.fields.get("poster") if film else info.fields.get("backdrop"))
        return info

    def _for_show(self, show, layout: Layout) -> "Info":
        found = self.show(layout.show_folders(int(show["id"])))
        info = Info()
        nfo = read_nfo(found.nfo, "tvshow") if found.nfo else None
        if nfo is not None:
            info.described = True
            fields = info.fields
            if nfo.title:
                fields["title"] = nfo.title
                fields["sort_title"] = nfo.sort_title or _sortable(nfo.title)
            if nfo.year:
                fields["year"] = nfo.year
            if nfo.plot:
                fields["overview"] = nfo.plot
            if _known(nfo.genres):
                fields["genres"] = ", ".join(nfo.genres)
            if nfo.rating:
                fields["rating"] = nfo.rating
            # With these its episodes can be asked for by number, with no search.
            for source, column in (("tmdb", "tmdb_id"), ("tvmaze", "tvmaze_id")):
                if nfo.ids.get(source, "").isdigit():
                    fields[column] = int(nfo.ids[source])
        for path, column in ((found.poster, "poster"), (found.backdrop, "backdrop")):
            saved = _copy(path, column) if path else None
            if saved:
                info.fields[column] = saved
        info.complete = bool(info.fields.get("overview")) and bool(info.fields.get("poster"))
        return info


@dataclass
class Info:
    """What the files beside something say about it, as columns to write."""

    fields: dict = field(default_factory=dict)
    described: bool = False     # there is a .nfo, and it is about this
    complete: bool = False      # nothing is left that the internet would add


# --- pictures ---------------------------------------------------------------

def _kind(data: bytes) -> str | None:
    """The suffix a picture should have, going by its first bytes."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _copy(source: Path, role: str, upright: bool | None = None) -> str | None:
    """A copy of a picture in the art folder, at the size it is shown at.

    The copy is what the app shows: it is there when the drive the library is
    on is asleep or unplugged, and a 20 MB fanart is not decoded for a card.
    Its name carries the picture's path, size and time, so the same picture is
    copied once, and one that is replaced gets a copy of its own.

    `upright` is for a picture that is only named like the video: True if it
    has to be taller than wide to be believed (a poster), False for wider (a
    still). A screenshot left beside a film is not its poster.
    """
    try:
        stat = source.stat()
        if not 0 < stat.st_size <= _MAX_PICTURE:
            return None
        name = hashlib.sha1(f"{source}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")).hexdigest()[:16]
        stem = f"{COPY_PREFIX}{name}-{role}"
        for suffix in (".jpg", ".png", ".webp"):
            existing = art_dir() / (stem + suffix)
            if existing.is_file() and existing.stat().st_size > 0:
                return str(existing)
        data = source.read_bytes()
    except OSError:
        return None
    suffix = _kind(data)
    if suffix is None:
        return None
    if upright is not None:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as image:
                if (image.height > image.width) != upright:
                    return None
        except Exception:       # noqa: BLE001 - not a picture Pillow knows
            return None
    destination = art_dir() / (stem + suffix)
    return str(destination) if fit.write_fitted(data, destination) else None


def forget_copy(old: object, new: object = None) -> None:
    """Remove a copy that a row no longer points at. Only ever a file this
    module made, in the art folder: never a picture beside a video."""
    if not is_copy(old) or (new and os.path.normcase(str(old)) == os.path.normcase(str(new))):
        return
    path = Path(str(old))
    try:
        if path.parent.resolve() == art_dir().resolve():
            path.unlink()
    except OSError:
        pass


# --- .nfo -------------------------------------------------------------------

@dataclass
class Nfo:
    title: str | None = None
    sort_title: str | None = None
    year: int | None = None
    plot: str | None = None
    tagline: str | None = None
    genres: list[str] = field(default_factory=list)
    rating: float | None = None
    ids: dict[str, str] = field(default_factory=dict)       # "tmdb": "391", "imdb": "tt0058461"


def _sortable(title: str) -> str:
    return _ARTICLE.sub("", title.lower()).strip()


def _known(genres: list[str]) -> bool:
    """Whether any of those is a category this app shows. Genres in a language
    categories.py has no words for are left out, so that the categories pass
    still looks the film up, instead of leaving it with none for good."""
    return any(categories.expand(name) for name in genres)


def _text(raw: bytes) -> str:
    """A .nfo's bytes as text. UTF-8 nearly always; old ones in whatever the
    PC that wrote them used, which for these is close enough to Windows-1252."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _blocks(text: str, root: str) -> list[str]:
    """Every <root>...</root> in the text. A file can hold more than one (an
    episode file that is two episodes), and can have a web address after it,
    which is why the whole file is not handed to the XML reader as it is."""
    found = []
    lowered = text.lower()
    closing = f"</{root}>"
    for match in _ROOT.finditer(text):
        if match.group(1).lower() != root:
            continue
        end = lowered.find(closing, match.end())
        if end >= 0:
            found.append(text[match.start(): end + len(closing)])
    return found


def _entity(match: re.Match) -> str:
    """&nbsp; and the like are HTML's, and XML knows only five entities."""
    if match.group(1) in _XML_ENTITIES:
        return match.group(0)
    meant = html.unescape(match.group(0))
    if meant == match.group(0):
        return "&amp;" + match.group(0)[1:]         # no entity anyone knows: the letters themselves
    return html.escape(meant, quote=False)


def _mended(block: str) -> str:
    """The same XML with what a hand leaves in it put right: ampersands and
    less-than signs that are only themselves, and entities that are HTML's."""
    block = _BARE_LESS_THAN.sub("&lt;", _BARE_AMPERSAND.sub("&amp;", block))
    return _NAMED_ENTITY.sub(_entity, block)


def _parse(block: str) -> ET.Element | None:
    for attempt in (block, _mended(block)):
        try:
            return ET.fromstring(attempt)
        except ET.ParseError:
            continue
    return None


def _first(element: ET.Element, *names: str) -> str | None:
    """The text of the first child with one of those names that has any."""
    for name in names:
        for child in element:
            if isinstance(child.tag, str) and child.tag.lower() == name:
                text = "".join(child.itertext()).strip()
                if text:
                    return text
    return None


def _line(text: str | None) -> str | None:
    """A title or a tagline: one line, however it was wrapped in the file."""
    return " ".join(text.split()) or None if text else None


def _number(text: str | None) -> float | None:
    try:
        return float((text or "").strip().replace(",", "."))
    except ValueError:
        return None


def _whole(text: str | None) -> int | None:
    try:
        return int((text or "").strip())
    except ValueError:
        return None


def _year(element: ET.Element) -> int | None:
    year = _whole(_first(element, "year"))
    if year is None:
        dated = re.match(r"\s*(\d{4})", _first(element, "premiered", "releasedate", "aired") or "")
        year = int(dated.group(1)) if dated else None
    return year if year and 1880 <= year <= 2100 else None


def _rating(element: ET.Element) -> float | None:
    """Out of ten. Kodi writes <ratings> with one marked as the one to show;
    older files have a bare <rating>, sometimes with a decimal comma."""
    chosen: tuple[float, float] | None = None
    for group in element:
        if not isinstance(group.tag, str) or group.tag.lower() != "ratings":
            continue
        for rating in group:
            value = _number(_first(rating, "value"))
            if value is None:
                continue
            top = _number(rating.get("max")) or 10.0
            if chosen is None or (rating.get("default") or "").lower() == "true":
                chosen = (value, top)
    if chosen is None:
        value = _number(_first(element, "rating"))
        chosen = (value, 10.0) if value is not None else None
    if chosen is None or chosen[1] <= 0:
        return None
    out_of_ten = round(chosen[0] * 10.0 / chosen[1], 1)
    return out_of_ten if 0 < out_of_ten <= 10 else None


def _genres(element: ET.Element) -> list[str]:
    found: list[str] = []
    for child in element:
        if isinstance(child.tag, str) and child.tag.lower() == "genre":
            # One to a tag, or all in one: "Western / Drama".
            for name in re.split(r"[/|,;]", "".join(child.itertext())):
                name = name.strip()
                if name and name not in found:
                    found.append(name)
    return found


def _ids(element: ET.Element) -> dict[str, str]:
    found: dict[str, str] = {}
    for child in element:
        if not isinstance(child.tag, str):
            continue
        tag, text = child.tag.lower(), "".join(child.itertext()).strip()
        if not text:
            continue
        if tag == "uniqueid" and child.get("type"):
            found.setdefault(child.get("type").lower(), text)
        elif tag in ("tmdbid", "imdbid", "tvdbid", "tvmazeid"):
            found.setdefault(tag[:-2], text)
    return found


def read_nfo(path: Path, root: str, episode: int | None = None) -> Nfo | None:
    """What a .nfo says, if it is the XML kind and about the right sort of
    thing: `root` is "movie", "tvshow" or "episodedetails".

    None for everything else that ends in .nfo, which is most of what the name
    has meant over the years: the text file with a drawing made of letters that
    comes with a download says nothing a library can use.
    """
    try:
        if not 0 < path.stat().st_size <= _MAX_NFO:
            return None
        text = _text(path.read_bytes())
    except OSError:
        return None
    blocks = _blocks(text, root)
    parsed = [element for element in map(_parse, blocks) if element is not None]
    if not parsed:
        if blocks:
            _log.info("local: %s is not XML that can be read", path.name)
        return None
    chosen = parsed[0]
    if root == "episodedetails" and episode is not None:
        # A file that is two episodes has both in one .nfo: this row's, if it is there.
        chosen = next((element for element in parsed
                       if _whole(_first(element, "episode")) == int(episode)), chosen)
    nfo = Nfo(
        title=_line(_first(chosen, "title")),
        sort_title=(_line(_first(chosen, "sorttitle")) or "").lower() or None,
        year=_year(chosen),
        plot=_first(chosen, "plot", "outline"),
        tagline=_line(_first(chosen, "tagline")),
        genres=_genres(chosen),
        rating=_rating(chosen),
        ids=_ids(chosen),
    )
    if not (nfo.title or nfo.plot or nfo.genres or nfo.ids):
        return None             # the right kind of file, with nothing in it
    return nfo
