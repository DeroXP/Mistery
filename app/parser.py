"""Turn scene-release filenames into clean titles, years and episode numbers.

    Night.Train.2.2019.2160p.BluRayRip.EAC3.5.1.HDR.x265-NOGRP[A1B2]
        -> title="Night Train 2", year=2019, tags={4K, BluRay, HEVC, EAC3, 5.1, HDR}
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path

_MAX_YEAR = datetime.date.today().year + 2

# Bump when parsing rules change, so existing libraries re-derive their titles
# and show grouping on next launch instead of staying wrong forever.
# 4: anime revisions ("- 05v2"), four-digit absolute episodes in release-style
#    names, "Show.E05" in a season folder, and "Film - 10 Years Later (2019)",
#    "Borat - 1492" and "Film - 2160 HDR" no longer episodes.
PARSER_VERSION = 4

# Canonical tag vocabularies. Order matters: the most specific pattern wins.
_RESOLUTION = [
    (r"2160p|4k|uhd", "4K"),
    (r"1440p", "1440p"),
    (r"1080p|fhd", "1080p"),
    (r"720p|hd", "720p"),
    (r"576p", "576p"),
    (r"480p", "480p"),
]
_SOURCE = [
    (r"bdremux|remux", "REMUX"),
    (r"blu-?ray-?rip|bd-?rip|br-?rip", "BluRay"),
    (r"blu-?ray", "BluRay"),
    (r"web-?dl", "WEB-DL"),
    (r"web-?rip|webrip", "WEBRip"),
    (r"hdtv", "HDTV"),
    (r"dvd-?rip|dvd-?r", "DVD"),
    (r"hd-?rip", "HDRip"),
    (r"cam-?rip|hdcam", "CAM"),
]
_CODEC = [
    (r"x265|h\.?265|hevc", "HEVC"),
    (r"x264|h\.?264|avc", "H.264"),
    (r"av1", "AV1"),
    (r"xvid", "XviD"),
    (r"divx", "DivX"),
    (r"mpeg-?2", "MPEG-2"),
]
_AUDIO = [
    (r"truehd", "TrueHD"),
    (r"atmos", "Atmos"),
    (r"dts-?hd-?ma|dts-?hd|dtshd", "DTS-HD"),
    (r"dts-?x", "DTS:X"),
    (r"dts", "DTS"),
    (r"e-?ac-?3|eac3|ddp|dd\+", "EAC3"),
    (r"ac-?3|dd(?![a-z0-9])", "AC3"),
    (r"aac", "AAC"),
    (r"flac", "FLAC"),
    (r"opus", "Opus"),
    (r"mp3", "MP3"),
]
_CHANNELS = [
    (r"7\.1", "7.1"),
    (r"5\.1", "5.1"),
    (r"2\.0", "2.0"),
]
_HDR = [
    (r"hdr10\+|hdr10plus", "HDR10+"),
    (r"dolby-?vision|dovi|dv(?![a-z0-9])", "Dolby Vision"),
    (r"hdr10", "HDR10"),
    (r"hlg", "HLG"),
    (r"hdr", "HDR"),
]
_EDITION = [
    (r"director'?s?-?cut", "Director's Cut"),
    (r"extended-?(cut|edition)?", "Extended"),
    (r"unrated", "Unrated"),
    (r"uncut", "Uncut"),
    (r"theatrical-?(cut|edition)?", "Theatrical"),
    (r"final-?cut", "Final Cut"),
    (r"ultimate-?edition", "Ultimate Edition"),
    (r"special-?edition", "Special Edition"),
    (r"imax", "IMAX"),
    (r"re-?master(ed)?", "Remastered"),
]

# Anything here marks the end of the title even when no year is present.
_MISC_JUNK = [
    r"proper", r"repack", r"internal", r"limited", r"complete", r"multi",
    r"dual-?audio", r"subbed", r"dubbed", r"retail", r"custom", r"read-?nfo",
    r"nfo-?fix", r"10-?bit", r"8-?bit", r"hi10p", r"sdr", r"3d", r"hsbs",
    r"bt2020", r"ddp?5", r"6ch", r"2ch", r"8ch",
]

_TAG_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("resolution", _RESOLUTION),
    ("source", _SOURCE),
    ("codec", _CODEC),
    ("audio", _AUDIO),
    ("channels", _CHANNELS),
    ("hdr", _HDR),
]

_BOUND_L = r"(?<![A-Za-z0-9])"
_BOUND_R = r"(?![A-Za-z0-9])"


def _bounded(pattern: str) -> re.Pattern:
    """Compile a tag pattern, treating `-?` as "any optional separator".

    The vocabularies above were written for scene names, where words are joined
    by dots or dashes. Bracketed anime releases separate them with spaces —
    `[Dual Audio]`, `[Multi Sub]` — and `dual-?audio` does not match
    `dual.audio`, so those tags leaked into titles instead of ending them.
    """
    pattern = pattern.replace("-?", r"[.\-_\s]?")
    return re.compile(_BOUND_L + "(?:" + pattern + ")" + _BOUND_R, re.IGNORECASE)


_COMPILED_GROUPS = [
    (name, [(_bounded(pat), label) for pat, label in items])
    for name, items in _TAG_GROUPS
]
_COMPILED_RESOLUTION = [regex for regex, _ in _COMPILED_GROUPS[0][1]]
_COMPILED_EDITION = [(_bounded(pat), label) for pat, label in _EDITION]
_COMPILED_JUNK = [_bounded(pat) for pat in _MISC_JUNK]

_YEAR_RE = re.compile(r"(?<![0-9])((?:19|20)\d{2})(?![0-9A-Za-z])")
_BRACKET_YEAR_RE = re.compile(r"[\(\[\{]\s*((?:19|20)\d{2})\s*[\)\]\}]")

_EPISODE_PATTERNS = [
    re.compile(_BOUND_L + r"s(\d{1,2})[.\-\s]?e(\d{1,3})", re.IGNORECASE),
    re.compile(_BOUND_L + r"season[.\-\s]?(\d{1,2})[.\-\s]?episode[.\-\s]?(\d{1,3})", re.IGNORECASE),
    re.compile(_BOUND_L + r"(\d{1,2})x(\d{2,3})" + _BOUND_R),
]
# "E05 - The Pager.mkv" — an episode number with no season, only valid at the
# very start of the name so it cannot match a stray token mid-filename. The
# season then comes from the folder.
_BARE_EPISODE_RE = re.compile(r"^e(\d{1,3})(?:v\d)?(?![a-z0-9])", re.IGNORECASE)
# The same bare number after a separator: "Show.Name.E05.1080p". Mid-name it
# could be a stray token, so parse() only accepts it when a folder above the
# file supplies the season ("Season 02", "Show Name S02").
_MID_EPISODE_RE = re.compile(r"(?<=[.\s_\-])e(\d{1,3})(?:v\d)?(?![a-z0-9])", re.IGNORECASE)
# Anime numbering: "Starbound - 01", with no season anywhere in the name. Two or
# more digits is deliberate — releases zero-pad, and accepting a single digit
# would turn "Kill Bill - 2" into an episode. The trailing guard keeps it off
# "- 1080p"; a re-released episode carries a revision, "- 12v2". Four digits
# are real (One Piece and Detective Conan are past 1000) but so is "Toy Story
# 4 - 2019", so a four-digit number shaped like a year is never an episode.
_ABSOLUTE_EPISODE_RE = re.compile(
    r"(?<=[.\s_])-[.\s_]*(?!(?:19|20)\d{2}(?![0-9]))(\d{2,4})(v\d)?(?![0-9a-z])",
    re.IGNORECASE,
)
_WORD_AFTER_RE = re.compile(r"[.\s_]*[A-Za-z]")
# How a release goes on after its episode number: tags in brackets, or " - "
# and the episode's title. A film's name just ends, or runs on in words.
_RELEASE_TAIL_RE = re.compile(r"[.\s_]*(?:[\[\(]|-[.\s_])")
# "Show - 12 END (2019)": the last episode, not a film titled "12 End". The mark
# has to stand alone, so "Film - 10 Final Days (2019)" is still a film.
_END_MARK_RE = re.compile(r"[.\s_]*(?:end|final|fin)[.\s_]*(?:[\[\(]|$)", re.IGNORECASE)
# A resolution with its "p" dropped: "Some Film - 1080 [x265]", "- 2160 HDR".
_RESOLUTION_NUMBERS = frozenset({480, 576, 720, 1080, 2160, 4320})
_SEASON_ONLY_RE = re.compile(_BOUND_L + r"(?:s|season[.\-\s]?)(\d{1,2})" + _BOUND_R, re.IGNORECASE)
# A folder that is nothing but a season marker: "Season 02", "S02", "2nd Season".
_SEASON_DIR_RE = re.compile(
    r"^(?:(?:season|series|s)[.\-\s_]*(\d{1,2})"
    r"|(\d{1,2})(?:st|nd|rd|th)[.\-\s_]*(?:season|series))$",
    re.IGNORECASE,
)
# A season marker tacked onto the end of a series folder: "Harbor Lights S01",
# and the form anime uses: "Starbound 1st Season".
_SEASON_SUFFIX_RE = re.compile(
    r"[.\s_-]+(?:(?:season|series|s)[.\s_-]*(\d{1,2})"
    r"|(\d{1,2})(?:st|nd|rd|th)[.\s_-]*(?:season|series))\s*$",
    re.IGNORECASE,
)
# A leading release group: "[Anime Time] Starbound - 01", "[SubsPlease] Show - 12".
_LEADING_GROUP_RE = re.compile(r"^\s*(?:\[[^\]]*\]|\([^)]*\))\s*")

_SAMPLE_RE = re.compile(r"(?<![a-z])sample(?![a-z])", re.IGNORECASE)
_TRAILER_RE = re.compile(r"(?<![a-z])trailer(?![a-z])", re.IGNORECASE)

_ARTICLES = ("the ", "a ", "an ")


@dataclass
class ParsedName:
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_title: str | None = None
    edition: str | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def is_episode(self) -> bool:
        return self.season is not None and self.episode is not None

    @property
    def sort_title(self) -> str:
        low = self.title.lower()
        for article in _ARTICLES:
            if low.startswith(article):
                return low[len(article):]
        return low


_SEPARATORS = str.maketrans({c: "." for c in "_ \t[]{}()"})


def _normalise(stem: str) -> str:
    """Collapse a filename to a uniform dot-separated form for tokenising.

    Strictly character-for-character so offsets stay aligned with the original
    stem — that lets us match bracketed years against the raw text and still use
    the resulting index against the normalised form.
    """
    return stem.translate(_SEPARATORS)


def clean_title(raw: str) -> str:
    """Render a dot-separated fragment as human-readable text."""
    text = raw.replace(".", " ").replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(" -–—([{,")
    # "Movie Name, The" -> "The Movie Name"
    match = re.match(r"^(.*),\s*(the|a|an)$", text, re.IGNORECASE)
    if match:
        text = f"{match.group(2)} {match.group(1)}"
    return text.strip()


def _extract_tags(work: str) -> tuple[dict[str, str], list[int]]:
    """Collect canonical quality tags plus the index of every junk token."""
    tags: dict[str, str] = {}
    cuts: list[int] = []
    for name, patterns in _COMPILED_GROUPS:
        for regex, label in patterns:
            match = regex.search(work)
            if match:
                cuts.append(match.start())
                if name not in tags:
                    tags[name] = label
                break
    for regex in _COMPILED_JUNK:
        match = regex.search(work)
        if match:
            cuts.append(match.start())
    return tags, cuts


def _extract_edition(work: str) -> tuple[str | None, int | None]:
    for regex, label in _COMPILED_EDITION:
        match = regex.search(work)
        if match:
            return label, match.start()
    return None, None


def _pick_year(work: str, raw: str | None = None) -> tuple[int | None, int | None]:
    """Return (year, index). Prefers a bracketed year, else the last plausible one.

    Handles titles that are themselves years: in "2012.2009.1080p" the title is
    "2012" and the year is 2009, because a year at index 0 is part of the title
    whenever another candidate follows it.
    """
    bracketed = _BRACKET_YEAR_RE.search(raw if raw is not None else work)
    if bracketed:
        value = int(bracketed.group(1))
        if value <= _MAX_YEAR:
            return value, bracketed.start()

    candidates = [m for m in _YEAR_RE.finditer(work) if int(m.group(1)) <= _MAX_YEAR]
    if not candidates:
        return None, None
    later = [m for m in candidates if m.start() > 0]
    match = later[-1] if later else candidates[-1]
    return int(match.group(1)), match.start()


def _season_number(match: re.Match | None) -> int | None:
    """The season out of a pattern that accepts either word order."""
    if match is None:
        return None
    for value in match.groups():
        if value:
            return int(value)
    return None


_TITLE_TAIL_RE = re.compile(r"[.\s_]*-[.\s_]")
_BRACKET_YEAR_AT_RE = re.compile(r"[.\s_]*[\(\[\{]\s*(?:19|20)\d{2}\s*[\)\]\}]")
_RES_VALUE_RE = re.compile(r"(?<![0-9])(480|576|720|1080|1440|2160|4320)p", re.IGNORECASE)


def _states_other_resolution(work: str, number: int) -> bool:
    """The name gives a resolution that is not this number: "- 480 [720p]"."""
    return any(int(m.group(1)) != number for m in _RES_VALUE_RE.finditer(work))


def _find_episode(work: str, raw: str | None = None, grouped: bool = False,
                  in_season_folder: bool = False, folder_title: str = ""
                  ) -> tuple[int | None, int | None, int | None, int | None]:
    """(season, episode, start, end) of the episode marker in `work`.

    `raw` is the unnormalised name (offsets match `work`), because only it still
    tells "10 Years" from "01 [Multi Sub]". `grouped` means the name began with
    a release group in brackets and `in_season_folder` that a folder above the
    file names a season: both say the file belongs to a show. `folder_title` is
    the folder holding the file, cleaned: "One Piece\\One Piece - 720.mkv" sits in
    a folder named exactly like the show, which films in their own folders
    ("Rocky (1976)\\Rocky - 1080 [x265].mkv") don't.
    """
    for regex in _EPISODE_PATTERNS:
        match = regex.search(work)
        if match:
            return int(match.group(1)), int(match.group(2)), match.start(), match.end()
    bare = _BARE_EPISODE_RE.match(work)
    if bare:
        return None, int(bare.group(1)), bare.start(), bare.end()
    # Last resort, because it is the loosest rule: absolute numbering with no
    # season marker at all. The season comes from the folder, or defaults to 1.
    for absolute in _ABSOLUTE_EPISODE_RE.finditer(work):
        digits, revised, end = absolute.group(1), absolute.group(2), absolute.end()
        show_like = grouped or in_season_folder or bool(
            folder_title and clean_title(work[:absolute.start()]).casefold() == folder_title)
        # "Some Film - 2160 HDR" is a resolution without its "p". It is an
        # episode only in a show's context (a release group, a season folder, a
        # folder named after the show), when an episode title follows
        # ("- 720 - Title"), or when the name gives a different resolution
        # ("- 1080 [720p]"). Stating the same one ("Film - 2160 [4K]") proves nothing.
        if (int(digits) in _RESOLUTION_NUMBERS and not show_like
                and not (raw is not None and _TITLE_TAIL_RE.match(raw, end))
                and not _states_other_resolution(work, int(digits))):
            continue
        # Four digits reach past any series' length but a few long-running ones,
        # and a film can be "Borat - 1492". Those episodes come named like a
        # release: a group in front, a revision, tags or a title after the number.
        # A bracketed year straight after the number is how films are named
        # ("Columbus - 1492 (1992)"), not a release tail.
        if (len(digits) == 4 and not (show_like or revised)
                and not (raw is not None and _RELEASE_TAIL_RE.match(raw, end)
                         and not _BRACKET_YEAR_AT_RE.match(raw, end))):
            continue
        # "The Movie - 10 Years Later (2019)": a number running straight on into
        # words, and then a bracketed year, is how films are named. Not in a
        # show's own naming, though, where "- 12 END (2019)" is its last episode.
        if (raw is not None and not show_like and _WORD_AFTER_RE.match(raw, end)
                and not _END_MARK_RE.match(raw, end)
                and _BRACKET_YEAR_RE.search(raw, end)):
            continue
        return None, int(digits), absolute.start(), end
    return None, None, None, None


def parse(path: str | Path) -> ParsedName:
    """Parse a video file path into structured library metadata."""
    path = Path(path)
    # Drop a leading release group so it can't become the title. If what's left
    # has no usable name — "[REC] 2007 1080p" is a real film — the folder
    # fallback below recovers it, so this is safe to do unconditionally.
    stem = _LEADING_GROUP_RE.sub("", path.stem, count=1) or path.stem
    work = _normalise(stem)
    grouped = stem != path.stem and path.stem.lstrip().startswith("[")
    in_season_folder = _title_from_folders(path)[2] is not None
    folder_title = clean_title(_normalise(path.parent.name)).casefold() if path.parent.name else ""

    season, episode, ep_index, ep_end = _find_episode(work, stem, grouped, in_season_folder,
                                                      folder_title)
    if episode is None:
        mid = _MID_EPISODE_RE.search(work)
        if mid and in_season_folder:
            episode, ep_index, ep_end = int(mid.group(1)), mid.start(), mid.end()
    year, year_index = _pick_year(work, stem)
    tags, junk_cuts = _extract_tags(work)
    edition, edition_index = _extract_edition(work)
    if edition_index is not None:
        junk_cuts.append(edition_index)

    # The title ends at the first structural marker we found.
    markers = [i for i in (ep_index, year_index, *junk_cuts) if i is not None and i > 0]
    cut = min(markers) if markers else len(work)
    title = clean_title(work[:cut])

    episode_title = None
    if ep_index is not None:
        after_markers = [i for i in junk_cuts if i is not None and i > ep_index]
        tail_end = max(min(after_markers) if after_markers else len(work), ep_end)
        # Everything between the SxxExx token and the first quality tag.
        candidate = clean_title(work[ep_end:tail_end])
        if candidate and not candidate.isdigit():
            episode_title = candidate
        # A year that sits after the episode marker belongs to the show, not a movie.
        if year_index is not None and year_index > ep_index:
            year = None

    # "S01E01 - Pilot.mkv" carries no series name at all — the episode marker is
    # the very first thing in the name, so there is nothing before it to use.
    if ep_index == 0:
        title = ""

    # Fall back to the folders above the file. Note "2012", "1917" and "300" are
    # real titles, so only bare 1-2 digit stems count as missing.
    if len(title) < 2 or (title.isdigit() and len(title) <= 2):
        folder_title, folder_year, folder_season = _title_from_folders(path)
        if folder_title:
            title = folder_title
            year = year or folder_year
            if season is None:
                season = folder_season

    # A season folder fills in a season the filename omitted.
    if season is None:
        _, _, folder_season = _title_from_folders(path)
        season = folder_season

    # "Starbound 2nd Season - 01" carries the season in the title itself, which
    # would otherwise split one show into one per season. Episodes only, so a
    # film whose name happens to end in a number is left alone.
    if episode is not None:
        suffix = _SEASON_SUFFIX_RE.search(title)
        if suffix:
            if season is None:
                season = _season_number(suffix)
            title = _SEASON_SUFFIX_RE.sub("", title).strip()

    # A bare "E05" with nothing else to go on is season 1.
    if season is None and episode is not None:
        season = 1

    return ParsedName(
        title=title,
        year=year,
        season=season,
        episode=episode,
        episode_title=episode_title,
        edition=edition,
        tags=tags,
    )


def _title_from_folders(path: Path) -> tuple[str | None, int | None, int | None]:
    """Find the series/film name in the folders above a file.

    Handles the three layouts that actually turn up on disk:

        Harbor Lights S01/S01E01 - Pilot.mkv    season suffix on the folder
        Harbor Lights/Season 01/S01E01.mkv      a pure season folder to skip
        Some.Movie.2019/movie.mkv               plain release folder

    Returns (title, year, season); any of them may be None.
    """
    season: int | None = None
    for parent in list(path.parents)[:3]:
        name = parent.name
        if not name or len(name) < 2:
            continue

        # A folder that is only a season marker: remember it and keep climbing.
        only_season = _SEASON_DIR_RE.match(name)
        if only_season:
            if season is None:
                season = _season_number(only_season)
            continue

        suffix = _SEASON_SUFFIX_RE.search(name)
        if suffix and season is None:
            season = _season_number(suffix)
        cleaned = _SEASON_SUFFIX_RE.sub("", name) if suffix else name

        parsed = parse_folder_title(cleaned)
        if parsed.title and len(parsed.title) >= 2:
            return parsed.title, parsed.year, season

    return None, None, season


def parse_folder_title(name: str) -> ParsedName:
    """Parse a release-directory name (same rules, minus the folder fallback)."""
    work = _normalise(name)
    year, year_index = _pick_year(work, name)
    tags, junk_cuts = _extract_tags(work)
    edition, edition_index = _extract_edition(work)
    if edition_index is not None:
        junk_cuts.append(edition_index)
    markers = [i for i in (year_index, *junk_cuts) if i is not None and i > 0]
    cut = min(markers) if markers else len(work)
    return ParsedName(title=clean_title(work[:cut]), year=year, edition=edition, tags=tags)


def looks_like_extra(path: str | Path, size: int = 0) -> bool:
    """True for sample clips, trailers and other non-feature files."""
    path = Path(path)
    name = path.stem
    if _TRAILER_RE.search(name):
        return True
    if _SAMPLE_RE.search(name) and size < 400 * 1024 * 1024:
        return True
    if path.parent.name.lower() in {"sample", "samples", "extras", "featurettes", "trailers"}:
        return True
    return False


def show_key(title: str, year: int | None) -> str:
    """Stable identity for grouping episodes into a show."""
    return re.sub(r"[^a-z0-9]+", "", title.lower())
