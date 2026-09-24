"""A friend's library: what they share, browsed from the copy this PC keeps.

Opened from the Friends page. Everything on it comes from friend_media, the
copy of their catalogue (app/share/catalog.py), so it opens at once and still
works while their PC is off. Opening it also asks their PC what has changed,
and their pictures arrive as they come on screen (app/share/art.py). Nothing
here names a file of theirs: a title is a kind and a number, which their
Mistery looks up for itself.

Three levels, the way the rest of the app goes: their films, shows and music in
tabs; then a show's episodes, an album's songs, or a film's own page. Every
Play here goes out through play_requested with the FriendItem to play.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea, QStackedWidget,
    QVBoxLayout, QWidget,
)

from .. import db
from ..util import fmt_clock, fmt_duration, fmt_remaining
from .party_dialog import _Spinner, _text
from .theme import C
from .widgets.artview import ArtView
from .widgets.cards import PosterCard, WideCard
from .widgets.flow import FlowLayout
from .widgets.icons import IconButton
from .widgets.music_cards import AlbumCard

_log = logging.getLogger("share")

CHECK_AGAIN_AFTER = 60.0        # opened twice in a minute: their PC is asked once
TABS = (("movie", "Films"), ("show", "Shows"), ("album", "Music"))


@dataclass
class FriendItem:
    """Something of a friend's, as a card draws it and Play asks for it."""

    friend_id: int
    kind: str                   # movie | show | episode | album | track
    remote_id: int              # its id on their PC
    title: str
    subtitle: str = ""
    art: str | None = None      # our copy of their picture, once it has come
    art_mark: str | None = None
    year: int | None = None
    duration: float | None = None
    overview: str | None = None
    genres: str | None = None
    artist: str | None = None
    parent_id: int | None = None
    season: int | None = None
    episode: int | None = None
    position: float = 0.0       # where you got to in it (friend_progress), never theirs
    watched: bool = False
    heading: str | None = None  # a card's title when not its own: an episode's show, on Home
    backdrop: str | None = None         # our copy of their wide picture (a film's backdrop), once fetched
    backdrop_mark: str | None = None    # their mark for it (art.WIDE)

    @property
    def display_title(self) -> str:
        return self.heading or self.title

    @property
    def progress(self) -> float:
        if self.watched or not self.duration or self.position <= 0:
            return 0.0
        return max(0.0, min(1.0, self.position / self.duration))

    @property
    def wide_art(self) -> str | None:
        return self.backdrop or self.art


def _subtitle(row, episodes: int = 0) -> str:
    kind = row["kind"]
    if kind == "movie":
        return " · ".join(bit for bit in (str(row["year"] or ""), fmt_duration(row["duration"])) if bit)
    if kind == "show":
        return " · ".join(bit for bit in (str(row["year"] or ""),
                                          f"{episodes} episode" + ("" if episodes == 1 else "s")
                                          if episodes else "") if bit)
    if kind == "episode":
        number = (f"S{row['season']} · E{row['episode']}" if row["season"] is not None
                  and row["episode"] is not None else "")
        return " · ".join(bit for bit in (number, fmt_duration(row["duration"])) if bit)
    if kind == "album":
        return " · ".join(bit for bit in (row["artist"] or "", str(row["year"] or "")) if bit)
    return row["artist"] or ""


def _item(friend_id: int, row, place, episodes: int, cached) -> FriendItem:
    """A friend_media row as a FriendItem, with where you got to in it (a
    friend_progress row, or None) and our copy of its picture if there is one."""
    from ..share.art import WIDE

    kind = row["kind"]
    wide = WIDE.get(kind)
    backdrop_mark = row["backdrop_mark"] if wide else None
    return FriendItem(
        friend_id=friend_id, kind=kind, remote_id=int(row["remote_id"]),
        title=row["title"] or "Untitled",
        subtitle=_subtitle(row, episodes),
        art=cached(friend_id, kind, int(row["remote_id"]), row["art_mark"]),
        backdrop=cached(friend_id, wide, int(row["remote_id"]), backdrop_mark) if wide else None,
        backdrop_mark=backdrop_mark,
        art_mark=row["art_mark"], year=row["year"], duration=row["duration"],
        overview=row["overview"], genres=row["genres"], artist=row["artist"],
        parent_id=row["parent_id"], season=row["season"], episode=row["episode"],
        position=float(place["position"] or 0) if place is not None else 0.0,
        watched=bool(place["watched"]) if place is not None else False)


def friend_item(friend_id: int, kind: str, remote_id: int) -> FriendItem | None:
    """One thing of a friend's, as friend_items() would give it, or None when
    their catalogue no longer has it."""
    from ..share import art

    row = db.friend_media_one(friend_id, kind, remote_id)
    if row is None:
        return None
    episodes = 0
    if kind == "show":
        episodes = int(db.query_one("SELECT COUNT(*) AS n FROM friend_media WHERE friend_id = ? "
                                    "AND kind = 'episode' AND parent_id = ?",
                                    (friend_id, remote_id))["n"])
    return _item(friend_id, row, db.friend_progress(friend_id, kind, remote_id), episodes,
                 art.cached)


def friend_continue_items(limit: int = 16, min_seconds: float = 30.0) -> list[tuple[float, FriendItem]]:
    """Friends' films and episodes you are part way through, for Home's
    Continue Watching: (when you last watched, the item), most recent first.

    The card says whose it is and how long is left, since a friend's title
    looks like one of yours otherwise; an episode's card is headed with its
    show and falls back to the show's picture, as your own do.
    """
    from ..share import art

    names = {row["id"]: row["name"] for row in db.friends()}
    out = []
    for row in db.friend_continue_watching(limit=limit, min_seconds=min_seconds):
        item = friend_item(int(row["friend_id"]), row["kind"], int(row["remote_id"]))
        if item is None:
            continue
        saved = db.friend_progress(item.friend_id, item.kind, item.remote_id)
        if saved is not None and saved["duration"]:
            item.duration = float(saved["duration"])      # the stream's own, when it has said
        whose = f"from {names.get(item.friend_id, 'a friend')}"
        left = fmt_remaining(item.position, item.duration) if item.duration else ""
        if item.kind == "episode":
            show = db.friend_media_one(item.friend_id, "show", item.parent_id) if item.parent_id else None
            if show is not None:
                item.heading = show["title"]
                if item.art is None:
                    item.art = art.cached(item.friend_id, "show", int(show["remote_id"]),
                                          show["art_mark"])
            numbered = (f"S{item.season} · E{item.episode}" if item.season is not None
                        and item.episode is not None else "")
            item.subtitle = " · ".join(bit for bit in (numbered, whose, left) if bit)
        else:
            item.subtitle = " · ".join(bit for bit in (whose, left) if bit)
        out.append((float(row["updated_at"] or 0.0), item))
    return out


def friend_night_item(friend_id: int, media_key: str) -> FriendItem | None:
    """The film or episode of a friend's that a movie night on their PC watched,
    from its party_progress key ("<their person id>:<their media id>"), or None
    when their catalogue no longer has it. An episode with no picture of its
    own shows its show's, as on Continue Watching."""
    from ..share import art

    number = str(media_key or "").partition(":")[2]
    # Their room's text: only digits are an id, and a dozen is plenty.
    if not (number.isascii() and number.isdigit() and len(number) <= 12):
        return None
    remote_id = int(number)
    item = friend_item(friend_id, "movie", remote_id) or friend_item(friend_id, "episode", remote_id)
    if item is not None and item.kind == "episode" and item.art is None and item.parent_id:
        show = db.friend_media_one(friend_id, "show", item.parent_id)
        if show is not None:
            item.art = art.cached(friend_id, "show", int(show["remote_id"]), show["art_mark"])
    return item


def friend_items(friend_id: int, kind: str, parent_id: int | None = None) -> list[FriendItem]:
    """A friend's things of one kind (an album's songs, a show's episodes), with
    our copy of each picture if we have it and where you got to in each."""
    from ..share import art

    rows = db.friend_media(friend_id, kind, parent_id=parent_id)
    places = {(row["kind"], row["remote_id"]): row for row in db.query(
        "SELECT kind, remote_id, position, watched FROM friend_progress WHERE friend_id = ?",
        (friend_id,))}
    episodes: dict[int, int] = {}
    if kind == "show":
        for row in db.query("SELECT parent_id, COUNT(*) AS n FROM friend_media WHERE friend_id = ? "
                            "AND kind = 'episode' GROUP BY parent_id", (friend_id,)):
            episodes[row["parent_id"]] = int(row["n"])
    items = [_item(friend_id, row, places.get((kind, row["remote_id"])),
                   episodes.get(row["remote_id"], 0), art.cached) for row in rows]
    if kind == "episode":
        items.sort(key=lambda item: (item.season or 0, item.episode or 0))
    elif kind == "track":
        items.sort(key=lambda item: (item.season or 0, item.episode or 0, item.title.lower()))
    return items


class _FriendMenu:
    """What a right-click offers on something of a friend's."""

    offers_show = False         # an episode's card away from its show's own page

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt API
        item = self._item
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if item.kind in ("movie", "episode", "album"):
            menu.addAction("Resume" if item.progress > 0 else "Play",
                           lambda: self.action_requested.emit("play", item))
        if item.kind in ("movie", "episode"):
            menu.addAction("Watch together", lambda: self.action_requested.emit("together", item))
        opens = {"movie": "Show details", "show": "Open show", "album": "Open album"}
        if self.offers_show:
            opens["episode"] = "Open show"
        if item.kind in opens:
            menu.addAction(opens[item.kind], lambda: self.action_requested.emit("details", item))
        menu.exec(event.globalPos())


class FriendCard(_FriendMenu, PosterCard):
    """A friend's film or show."""


class FriendAlbumCard(_FriendMenu, AlbumCard):
    """A friend's album: square, like yours."""


class FriendEpisodeCard(_FriendMenu, WideCard):
    """One of a friend's episodes."""

    def __init__(self, item, parent=None) -> None:
        super().__init__(item, show_remaining=False, parent=parent)


class FriendContinueCard(FriendEpisodeCard):
    """A friend's film or episode in Home's Continue Watching. Its line under
    the title (friend_continue_items) already says whose it is and what is left.

    A friend's film comes with its poster only, never the wide backdrop yours
    have there, so the poster is shown whole in the 16:9 frame rather than
    cropped to its middle third. An episode's still is wide already."""

    offers_show = True
    FRAMED_ART = True


def friend_card(item: FriendItem, wide: bool = False):
    """The card for something of a friend's in one of the app's own rows
    (widgets/cards.make_card): its own menu, never one that would touch this
    library, such as Mark watched or Open folder."""
    if wide:
        return FriendContinueCard(item)
    return FriendAlbumCard(item) if item.kind == "album" else FriendCard(item)


def _scroll(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(inner)
    return area


def _eyebrow(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {C.ACCENT}; font-size: 9.5pt; font-weight: 700; "
                        "letter-spacing: 1.2px;")
    return label


class _Grid(QWidget):
    """One tab: a wrapping grid of a friend's cards, or a line saying why not."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 40)
        layout.setSpacing(0)
        holder = QWidget()
        self.flow = FlowLayout(holder, margin=2, h_spacing=18, v_spacing=22)
        layout.addWidget(holder)
        self.empty = _text("", 10.5, C.TEXT_FAINT, rich=False)
        self.empty.setVisible(False)
        layout.addWidget(self.empty)
        layout.addStretch(1)


class _Track(QWidget):
    """One of an album's songs: its number, title and length."""

    play = Signal(object)

    def __init__(self, item: FriendItem, place: int, parent=None) -> None:
        super().__init__(parent)
        self.item = item
        self.setObjectName("FriendTrack")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 9, 14, 9)
        row.setSpacing(16)
        # Its number on the album; where the tags had none, its place in the list.
        number = QLabel(str(item.episode or place))
        number.setFixedWidth(26)
        number.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        number.setStyleSheet(f"color: {C.TEXT_FAINT}; font-size: 10pt;")
        row.addWidget(number)
        title = QLabel(item.title)
        title.setStyleSheet(f"color: {C.TEXT}; font-size: 10.5pt;")
        row.addWidget(title, 1)
        length = QLabel(fmt_clock(item.duration) if item.duration else "")
        length.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10pt;")
        row.addWidget(length)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            self.play.emit(self.item)


_STYLE = f"""
#FriendTrack {{ border-radius: 6px; }}
#FriendTrack:hover {{ background: {C.SURFACE_HOVER}; }}
"""


class FriendLibraryView(QWidget):
    """Browse one friend's films, shows and music."""

    back_requested = Signal()
    play_requested = Signal(object)         # a FriendItem: a film, an episode, an album or a song
    together_requested = Signal(object)     # a FriendItem: a movie night of a film or episode, on their PC
    # From threads, back on Qt's thread.
    _art_arrived = Signal(str, int, str)
    _refreshed = Signal(int, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(_STYLE)
        self._art_arrived.connect(self._on_art)
        self._refreshed.connect(self._on_refreshed)
        self.friend_id: int | None = None
        self._fetcher = None
        self._cards: dict[tuple[str, int], list] = {}           # the tabs' cards
        self._episode_cards: dict[tuple[str, int], list] = {}   # the open show's
        self._episode_items: list[FriendItem] = []
        self._checked_at: dict[int, float] = {}
        self._checking = False
        self._state = "unknown"
        self._trouble = ""
        self._show: FriendItem | None = None
        self._album: FriendItem | None = None
        self._film: FriendItem | None = None
        self._notes: list = []                  # each level's line for what went wrong

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._levels = QStackedWidget()
        root.addWidget(self._levels)
        self._levels.addWidget(self._build_browse())
        self._levels.addWidget(self._build_show())
        self._levels.addWidget(self._build_album())
        self._levels.addWidget(self._build_film())

    # --- building ---------------------------------------------------------------

    def _build_browse(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(52, 26, 52, 0)
        layout.setSpacing(14)
        top = QHBoxLayout()
        top.setSpacing(14)
        back = IconButton("back", size=38, icon_size=21, tooltip="Back")
        back.clicked.connect(self.back_requested.emit)
        top.addWidget(back, 0, Qt.AlignmentFlag.AlignVCenter)
        self.title = QLabel()
        self.title.setObjectName("PageTitle")
        top.addWidget(self.title)
        top.addStretch(1)
        self.spinner = _Spinner(16)
        self.spinner.setVisible(False)
        top.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        self.status = QLabel()
        self.status.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 9.5pt;")
        top.addWidget(self.status)
        self.refresh_button = QPushButton("Check again")
        self.refresh_button.setObjectName("Chip")
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_button.clicked.connect(lambda: self.refresh(force=True))
        top.addWidget(self.refresh_button)
        layout.addLayout(top)

        self.note = _text("", 9.8, C.TEXT_DIM, rich=False)
        self.note.setVisible(False)
        layout.addWidget(self.note)
        self._notes.append(self.note)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.tabs = QButtonGroup(self)
        self.tabs.setExclusive(True)
        self._tab_buttons = []
        for index, (_kind, label) in enumerate(TABS):
            chip = QPushButton(label)
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.setChecked(index == 0)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            self.tabs.addButton(chip, index)
            self._tab_buttons.append(chip)
            bar.addWidget(chip)
        bar.addStretch(1)
        self.tabs.idClicked.connect(self._on_tab)
        layout.addLayout(bar)

        self.pages = QStackedWidget()
        self.grids: dict[str, _Grid] = {}
        for kind, _label in TABS:
            grid = _Grid()
            self.grids[kind] = grid
            self.pages.addWidget(_scroll(grid))
        layout.addWidget(self.pages, 1)
        return page

    def _header(self, layout: QVBoxLayout, eyebrow: str,
                art: ArtView) -> tuple[QLabel, QLabel, QVBoxLayout]:
        """Back, the picture, an eyebrow, a title and a line under it, on every
        level; and the column they are in, for what that level adds."""
        back = IconButton("back", size=38, icon_size=21, tooltip="Back")
        back.clicked.connect(self.go_up)
        layout.addWidget(back, 0, Qt.AlignmentFlag.AlignLeft)
        head = QHBoxLayout()
        head.setSpacing(28)
        head.addWidget(art, 0, Qt.AlignmentFlag.AlignTop)
        info = QVBoxLayout()
        info.setSpacing(6)
        info.addWidget(_eyebrow(eyebrow))
        title = QLabel()
        title.setWordWrap(True)
        title.setStyleSheet(f"color: {C.TEXT}; font-size: 24pt; font-weight: 700;")
        info.addWidget(title)
        meta = QLabel()
        meta.setWordWrap(True)
        meta.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10.5pt;")
        info.addWidget(meta)
        head.addLayout(info, 1)
        layout.addLayout(head)
        note = _text("", 9.8, C.TEXT_DIM, rich=False)
        note.setVisible(False)
        layout.addWidget(note)
        self._notes.append(note)
        return title, meta, info

    def _build_show(self) -> QWidget:
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(52, 26, 52, 40)
        layout.setSpacing(18)
        self.show_art = ArtView(150, 225, radius=10)
        self.show_title, self.show_meta, info = self._header(layout, "SERIES", self.show_art)
        self.show_overview = _text("", 10.0, C.TEXT_DIM, rich=False)
        info.addWidget(self.show_overview)
        info.addStretch(1)
        holder = QWidget()
        self.episode_flow = FlowLayout(holder, margin=2, h_spacing=18, v_spacing=22)
        layout.addWidget(holder)
        layout.addStretch(1)
        return _scroll(inner)

    def _build_album(self) -> QWidget:
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(52, 26, 52, 40)
        layout.setSpacing(18)
        self.album_art = ArtView(200, 200, radius=10)
        self.album_title, self.album_meta, info = self._header(layout, "ALBUM", self.album_art)
        self.album_play = QPushButton("Play")
        self.album_play.setObjectName("Primary")
        self.album_play.setCursor(Qt.CursorShape.PointingHandCursor)
        self.album_play.clicked.connect(self._play_album)
        info.addSpacing(8)
        info.addWidget(self.album_play, 0, Qt.AlignmentFlag.AlignLeft)
        info.addStretch(1)
        self.track_list = QVBoxLayout()
        self.track_list.setSpacing(2)
        layout.addLayout(self.track_list)
        layout.addStretch(1)
        return _scroll(inner)

    def _build_film(self) -> QWidget:
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(52, 26, 52, 40)
        layout.setSpacing(18)
        self.film_art = ArtView(200, 300, radius=10)
        self.film_title, self.film_meta, info = self._header(layout, "FILM", self.film_art)
        self.film_overview = _text("", 10.5, C.TEXT_DIM, rich=False)
        info.addSpacing(6)
        info.addWidget(self.film_overview)
        self.film_play = QPushButton("Play")
        self.film_play.setObjectName("Primary")
        self.film_play.setCursor(Qt.CursorShape.PointingHandCursor)
        self.film_play.clicked.connect(self._play_film)
        # A movie night of it, held by their PC for their friends
        # (app/share/nights.py): the one who presses joins at once and gets the
        # code to pass on.
        self.film_together = QPushButton("Watch together")
        self.film_together.setObjectName("Ghost")
        self.film_together.setCursor(Qt.CursorShape.PointingHandCursor)
        self.film_together.setToolTip("A movie night of this on their PC. Only their friends "
                                      "can join, with the code you'll get.")
        self.film_together.clicked.connect(self._together_film)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addWidget(self.film_play)
        buttons.addWidget(self.film_together)
        buttons.addStretch(1)
        info.addSpacing(12)
        info.addLayout(buttons)
        info.addStretch(1)
        layout.addStretch(1)
        return _scroll(inner)

    # --- whose, and what -------------------------------------------------------------

    def set_friend(self, friend_id: int) -> None:
        """Point the page at a friend, at the top level, on their films."""
        friend_id = int(friend_id)
        if friend_id != self.friend_id:
            self._stop_fetching()
            self.friend_id = friend_id
            self._state = "unknown"
            self.tabs.button(0).setChecked(True)
            self.pages.setCurrentIndex(0)
        self._levels.setCurrentIndex(0)
        self.reload()
        self.refresh()

    def reload(self) -> None:
        """Everything on the top level, again from the copy of their library."""
        friend = db.friend(self.friend_id) if self.friend_id is not None else None
        if friend is None:
            return
        self.title.setText(f"{friend['name']}'s library")
        self._cards.clear()
        for index, (kind, label) in enumerate(TABS):
            items = friend_items(self.friend_id, kind)
            self._tab_buttons[index].setText(f"{label}  {len(items)}" if items else label)
            grid = self.grids[kind]
            grid.flow.clear()
            for item in items:
                card = (FriendAlbumCard if kind == "album" else FriendCard)(item)
                card.clicked.connect(self._open)
                card.play_requested.connect(self._play_or_open)
                card.action_requested.connect(self._on_action)
                grid.flow.addWidget(card)
                self._cards.setdefault((kind, item.remote_id), []).append(card)
            grid.empty.setText(self._empty_words(friend, kind))
            grid.empty.setVisible(not items)
        self._say_state()
        self._want_art()

    def _empty_words(self, friend, kind: str) -> str:
        name = friend["name"]
        if not friend["catalog_at"]:
            return (f"Nothing from {name} yet: their library shows here once their PC has "
                    "answered.")
        return {"movie": f"{name} isn't sharing any films.",
                "show": f"{name} isn't sharing any shows.",
                "album": f"{name} isn't sharing any music."}[kind]

    def _on_tab(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        if self._fetcher is not None:
            self._fetcher.clear()
        self._want_art()

    # --- their PC ------------------------------------------------------------------------

    def refresh(self, force: bool = False) -> None:
        """Ask their PC what has changed, on a thread."""
        friend_id = self.friend_id
        if friend_id is None or self._checking:
            return
        if not force and time.monotonic() - self._checked_at.get(friend_id, -1e9) < CHECK_AGAIN_AFTER:
            return
        self._checking = True
        self.spinner.setVisible(True)
        self.refresh_button.setEnabled(False)
        self.status.setText("Checking…")

        def run() -> None:
            from ..share import client

            result: dict = {"state": "trouble"}
            try:
                friend = db.friend(friend_id)
                if friend is None:
                    result = {"state": "gone"}
                    return
                with client.Channel(friend) as channel:
                    if channel.hello().get("sharing") is False:
                        result = {"state": "closed"}
                    else:
                        result = {"state": "online", "changed": channel.refresh()}
            except client.Unreachable:
                result = {"state": "offline"}
            except client.ShareError as problem:
                result = {"state": "trouble", "text": str(problem)}
            except Exception:                   # noqa: BLE001 - one friend, not the app
                _log.exception("share: refreshing a friend's library failed")
            finally:
                db.close_thread_connection()
                self._refreshed.emit(friend_id, result)

        threading.Thread(target=run, name="share-browse", daemon=True).start()

    def _on_refreshed(self, friend_id: int, result: dict) -> None:
        self._checking = False
        self.spinner.setVisible(False)
        self.refresh_button.setEnabled(True)
        self._checked_at[friend_id] = time.monotonic()
        if friend_id != self.friend_id:
            return
        self._state = result.get("state", "trouble")
        self._trouble = result.get("text", "")
        changed = result.get("changed") or {}
        if self._fetcher is not None:
            if self._state == "online":
                self._fetcher.retry()
            elif self._state in ("offline", "closed"):
                # Their PC would refuse every picture, one connection each.
                self._fetcher.offline = True
                self._fetcher.clear()
        if changed.get("stored") or changed.get("dropped") or self._state == "online":
            self.reload()
        else:
            self._say_state()

    def _say_state(self) -> None:
        friend = db.friend(self.friend_id) if self.friend_id is not None else None
        if friend is None:
            return
        name = friend["name"]
        when = friend["catalog_at"]
        as_of = f"as of {_ago(when)}" if when else ""
        if self._checking:
            return
        state = self._state
        if state == "online":
            self.status.setText("Online")
            self._set_note("")
        elif state == "offline":
            self.status.setText("Not reachable" + (f" · {as_of}" if as_of else ""))
            self._set_note(f"{name}'s PC isn't answering, so this is their library {as_of or 'as last seen'}. "
                           "Pictures you haven't seen yet, and playing, come back when it's on.")
        elif state == "closed":
            self.status.setText("Not sharing with you right now")
            self._set_note(f"{name} has paused sharing with you, or switched sharing off. "
                           f"This is their library {as_of or 'as last seen'}.")
        elif state == "trouble":
            self.status.setText("Didn't work" + (f" · {as_of}" if as_of else ""))
            self._set_note(getattr(self, "_trouble", "") or "Something went wrong talking to "
                           "their Mistery.")
        else:
            self.status.setText(as_of.capitalize() if as_of else "")
            self._set_note("")

    def _set_note(self, text: str) -> None:
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def say(self, text: str) -> None:
        """A sentence on whichever level is showing: why a Play did not work."""
        note = self._notes[self._levels.currentIndex()]
        note.setText(text)
        note.setVisible(bool(text))

    def shown_note(self) -> str:
        """What the level on screen is saying, if anything."""
        note = self._notes[self._levels.currentIndex()]
        return note.text() if note.isVisible() else ""

    def _to_level(self, level: int) -> None:
        """Another level on screen, without the last one's complaint on it."""
        if level:
            self._notes[level].setVisible(False)
        self._levels.setCurrentIndex(level)

    # --- pictures --------------------------------------------------------------------------

    def _want_art(self) -> None:
        """Ask for the pictures of what is on screen: this tab, or this level."""
        if self.friend_id is None:
            return
        fetcher = self._ensure_fetcher()
        level = self._levels.currentIndex()
        if level == 1 and self._show is not None:
            wanted = [self._show, *self._episode_items]
        elif level == 2 and self._album is not None:
            wanted = [self._album]
        elif level == 3 and self._film is not None:
            wanted = [self._film]
        else:
            kind = TABS[self.pages.currentIndex()][0]
            wanted = [card.item for card in _cards_in(self.grids[kind].flow)]
        for item in wanted:
            if not item.art and item.art_mark:
                fetcher.want(item.kind, item.remote_id, item.art_mark)
        # A film's own page: its wide picture too, for Continue Watching's card.
        # Only here, not for every poster in a grid: a backdrop is a megabyte.
        film = self._film if level == 3 else None
        if film is not None and not film.backdrop and film.backdrop_mark:
            from ..share.art import WIDE

            fetcher.want(WIDE[film.kind], film.remote_id, film.backdrop_mark)

    def _ensure_fetcher(self):
        from ..share import art

        if self._fetcher is None or self._fetcher.friend_id != self.friend_id:
            self._stop_fetching()
            self._fetcher = art.Fetcher(self.friend_id, self._art_arrived.emit)
            if self._state in ("offline", "closed"):
                self._fetcher.offline = True
        return self._fetcher

    def _stop_fetching(self) -> None:
        if self._fetcher is not None:
            self._fetcher.stop()
            self._fetcher = None

    def _on_art(self, kind: str, remote_id: int, path: str) -> None:
        key = (kind, remote_id)
        for card in self._cards.get(key, []) + self._episode_cards.get(key, []):
            card.set_item(replace(card.item, art=path))
        for view, item in ((self.show_art, self._show), (self.album_art, self._album),
                           (self.film_art, self._film)):
            if item is not None and (item.kind, item.remote_id) == (kind, remote_id):
                item.art = path
                view.set_art(path, item.title)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().hideEvent(event)
        # Leaving the page lets their PC go: the connection closes with the fetcher.
        self._stop_fetching()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        # Back from the tray, or from the player: what is on screen still wants
        # its pictures, and hiding stopped the fetcher.
        self._want_art()

    # --- going in and out --------------------------------------------------------------------

    def _open(self, item: FriendItem) -> None:
        if item.kind == "show":
            self.open_show(item)
        elif item.kind == "album":
            self.open_album(item)
        elif item.kind == "movie":
            self.open_film(item)
        elif item.kind == "episode":
            self.play_requested.emit(item)

    def _play_or_open(self, item: FriendItem) -> None:
        if item.kind == "show":
            self.open_show(item)
        else:
            self.play_requested.emit(item)

    def _on_action(self, action: str, item: FriendItem) -> None:
        if action == "play":
            self.play_requested.emit(item)
        elif action == "together":
            self.together_requested.emit(item)
        elif item.kind != "episode":
            self._open(item)

    def _play_album(self) -> None:
        if self._album is not None:
            self.play_requested.emit(self._album)

    def _play_film(self) -> None:
        if self._film is not None:
            self.play_requested.emit(self._film)

    def _together_film(self) -> None:
        if self._film is not None:
            self.together_requested.emit(self._film)

    def open_show(self, item: FriendItem) -> None:
        self._show = item
        self.show_title.setText(item.title)
        self.show_meta.setText(item.subtitle)
        self.show_overview.setText(item.overview or "")
        self.show_art.set_art(item.art, item.title)
        self._episode_items = friend_items(self.friend_id, "episode", parent_id=item.remote_id)
        self.episode_flow.clear()
        self._episode_cards.clear()
        for episode in self._episode_items:
            card = FriendEpisodeCard(episode)
            card.clicked.connect(self._open)
            card.play_requested.connect(self.play_requested.emit)
            card.action_requested.connect(self._on_action)
            self.episode_flow.addWidget(card)
            self._episode_cards.setdefault(("episode", episode.remote_id), []).append(card)
        self._to_level(1)
        self._want_art()

    def open_album(self, item: FriendItem) -> None:
        self._album = item
        self.album_title.setText(item.title)
        songs = friend_items(self.friend_id, "track", parent_id=item.remote_id)
        total = sum(song.duration or 0 for song in songs)
        self.album_meta.setText(" · ".join(bit for bit in (
            item.artist or "", str(item.year or ""),
            f"{len(songs)} song" + ("" if len(songs) == 1 else "s"),
            fmt_duration(total)) if bit))
        self.album_art.set_art(item.art, item.title)
        while self.track_list.count():
            widget = self.track_list.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for place, song in enumerate(songs, start=1):
            row = _Track(song, place)
            row.play.connect(self.play_requested.emit)
            self.track_list.addWidget(row)
        self.album_play.setEnabled(bool(songs))
        self._to_level(2)
        self._want_art()

    def open_film(self, item: FriendItem) -> None:
        self._film = item
        self.film_title.setText(item.title)
        self.film_meta.setText(" · ".join(bit for bit in (
            str(item.year or ""), fmt_duration(item.duration), item.genres or "") if bit))
        self.film_overview.setText(item.overview or "")
        self.film_art.set_art(item.art, item.title)
        self.film_play.setText("Resume" if item.progress > 0 else "Play")
        self._to_level(3)
        self._want_art()

    def go_up(self) -> bool:
        """One level back up inside the page. False at the top: the window's to go."""
        if self._levels.currentIndex() == 0:
            self.back_requested.emit()
            return False
        self._levels.setCurrentIndex(0)
        self._want_art()
        return True

    @property
    def level(self) -> int:
        return self._levels.currentIndex()


def _cards_in(flow: FlowLayout) -> list:
    cards = []
    for index in range(flow.count()):
        widget = flow.itemAt(index).widget()
        if widget is not None:
            cards.append(widget)
    return cards


def _ago(stamp: float) -> str:
    from .friends_view import ago

    return ago(stamp)
