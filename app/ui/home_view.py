"""Home: hero banner, Continue Watching, Movie nights, then the library as a grid."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .. import db
from ..config import settings
from ..models import MediaItem, ShowItem
from ..util import fmt_clock, progress_fraction
from .friend_library_view import FriendItem, friend_continue_items, friend_night_item
from .party_dialog import and_list, when_text
from .theme import C
from .widgets.artview import ArtView
from .widgets.flow import FlowLayout
from .widgets.hero import HeroBanner
from .widgets.rows import CardGrid, CardRow

# How many movie nights Home lists: the most recent, one card each.
_MOVIE_NIGHTS = 8
_CARD_W = 540


class _PartyArt(ArtView):
    """The still of what a movie night watched, with how far it got along the
    bottom, the way Continue Watching shows it."""

    def __init__(self, parent=None) -> None:
        super().__init__(176, 99, radius=6, parent=parent)
        self.fraction = 0.0

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.fraction <= 0.001:
            return
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        track = QRectF(0, self.height() - 4.0, self.width(), 4.0)
        painter.setBrush(QColor(90, 90, 90, 220))
        painter.drawRect(track)
        painter.setBrush(QColor(C.ACCENT))
        painter.drawRect(QRectF(0, track.top(), track.width() * max(0.02, self.fraction), 4.0))


class PartyCard(QFrame):
    """One movie night on Home: what it watched, who came, where it got to.

    The host can Continue it: same party, same place, and a fresh code for the
    friends. A guest's card says who can, because the film is on their PC. One
    that a friend's PC held for its friends (Watch together, app/share/nights.py)
    can be asked of that PC again: Watch together again, from where it got to.
    """

    continue_requested = Signal(object)         # the party's newest party_progress row
    forget_requested = Signal(str)              # party_id
    together_requested = Signal(object, object)     # the friend's film (a FriendItem), the row

    def __init__(self, row, me: str, parent=None) -> None:
        super().__init__(parent)
        from ..party import people

        self.row = row
        self.setObjectName("Card")
        # Two to a row in the default 1440 px window, three from about 1760:
        # a movie night is known by what it watched, so the title gets the room.
        self.setFixedSize(QSize(_CARD_W, 131))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 14, 16, 14)
        layout.setSpacing(16)

        media_id = row["media_id"]
        fresh = db.get_media(int(media_id)) if row["role"] == "host" and media_id else None
        item = MediaItem.from_row(fresh) if fresh is not None and not fresh["missing"] else None
        self.item = item

        members = people.parse_members(row["members"])
        host_id = str(row["media_key"] or "").split(":", 1)[0]
        host = next((p.name for p in members if p.id == host_id), "")
        others = [p.name for p in members if p.id != me and p.id != host_id]
        # Held by a friend's PC for its friends: nobody of that PC's was in the
        # room, so its owner is not among who came. The film is theirs to lend
        # still, while their catalogue has it, so their PC can be asked again.
        self.friend = None
        self.friend_item = None
        if row["role"] != "host" and host_id and not host:
            self.friend = db.friend_by_person(host_id)
            if self.friend is not None:
                self.friend_item = friend_night_item(int(self.friend["id"]), row["media_key"])

        self.art = _PartyArt()
        if self.friend_item is not None:
            self.art.set_art(self.friend_item.wide_art, row["title"] or "Movie night", framed=True)
        else:
            self.art.set_art(item.wide_art if item else None, row["title"] or "Movie night")
        self.art.fraction = progress_fraction(row["position"], row["duration"])
        layout.addWidget(self.art, 0, Qt.AlignmentFlag.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(3)
        self.title = QLabel()
        self.title.setStyleSheet(f"color: {C.TEXT}; font-size: 10.5pt; font-weight: 700;")
        self.title.setToolTip(row["title"] or "")
        text.addWidget(self.title)

        if row["role"] == "host":
            who = f"With {and_list(others)}" if others else "Nobody else joined"
        elif self.friend is not None:
            who = f"From {self.friend['name']}'s library" + (
                f", with {and_list(others)}" if others else "")
        else:
            who = (f"{host}'s movie night" if host else "A friend's movie night") + (
                f", with {and_list(others)}" if others else "")
        self.who = QLabel()
        self.who.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 9.5pt;")
        self.who.setToolTip(who)
        self._words = {self.title: row["title"] or "Movie night", self.who: who}
        text.addWidget(self.who)

        where = f"Got to {fmt_clock(row['position'])}"
        if row["duration"]:
            where += f" of {fmt_clock(row['duration'])}"
        when = when_text(row["updated_at"])
        self.where = QLabel(where + (f"  ·  {when}" if when else ""))
        self.where.setStyleSheet(f"color: {C.TEXT_FAINT}; font-size: 9pt;")
        text.addWidget(self.where)
        text.addStretch(1)

        self.action = None
        if row["role"] == "host" and item is not None:
            self.action = QPushButton("Continue")
            self.action.setObjectName("Chip")
            self.action.setCursor(Qt.CursorShape.PointingHandCursor)
            self.action.setToolTip("Start this movie night again where it got to. Friends get "
                                   "a new code.")
            self.action.clicked.connect(lambda: self.continue_requested.emit(self.row))
            text.addWidget(self.action, 0, Qt.AlignmentFlag.AlignLeft)
        elif self.friend_item is not None:
            name = self.friend["name"]
            self.action = QPushButton("Watch together again")
            self.action.setObjectName("Chip")
            self.action.setCursor(Qt.CursorShape.PointingHandCursor)
            self.action.setToolTip(f"Ask {name}'s PC to start this movie night again where it got "
                                   f"to. There's a new code to pass on to {name}'s friends.")
            self.action.clicked.connect(self._together)
            text.addWidget(self.action, 0, Qt.AlignmentFlag.AlignLeft)
        else:
            if row["role"] == "host":
                words = "The file is no longer in your library"
            elif self.friend is not None:
                words = f"It is no longer in {self.friend['name']}'s library"
            else:
                words = f"{host or 'The host'} can continue it"
            note = QLabel(words)
            note.setStyleSheet(f"color: {C.TEXT_FAINT}; font-size: 8.5pt;")
            text.addWidget(note)
        layout.addLayout(text, 1)

    def _together(self) -> None:
        self.together_requested.emit(self.friend_item, self.row)

    def showEvent(self, event) -> None:
        """Titles are cut to fit here, where the labels have the font they
        paint with: cut when the card was made, the measure was the plain
        font's, and "Harbor Lights — S01E03 · ...and the Signal in the Dark"
        lost its last letters with no "…" to say so."""
        super().showEvent(event)
        width = _CARD_W - 14 - 176 - 16 - 16          # the text column: card less margins, art, gap
        for label, words in self._words.items():
            label.setText(label.fontMetrics().elidedText(words, Qt.TextElideMode.ElideRight, width))

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if self.friend_item is not None:
            menu.addAction("Watch together again", self._together)
            menu.addSeparator()
        elif self.action is not None:
            menu.addAction("Continue movie night", lambda: self.continue_requested.emit(self.row))
            menu.addSeparator()
        menu.addAction("Remove from Movie nights",
                       lambda: self.forget_requested.emit(str(self.row["party_id"])))
        menu.exec(event.globalPos())


class MovieNights(QWidget):
    """Home's Movie nights: the recent parties, and the way into a friend's.

    Always there, even before the first movie night, because Join has to be
    findable from Home: then it is one line and the button.
    """

    continue_requested = Signal(object)         # a party_progress row
    forget_requested = Signal(str)
    join_requested = Signal()
    together_requested = Signal(object, object)     # a friend's film (FriendItem), a party_progress row

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        header = QHBoxLayout()
        header.setSpacing(10)
        title = QLabel("Movie nights")
        title.setObjectName("SectionTitle")
        header.addWidget(title)
        self.hint = QLabel()
        self.hint.setObjectName("SectionHint")
        header.addWidget(self.hint)
        header.addStretch(1)
        self.join = QPushButton("Join a movie night")
        self.join.setObjectName("Chip")
        self.join.setCursor(Qt.CursorShape.PointingHandCursor)
        self.join.setToolTip("Watch a friend's film with them: paste the code they sent you")
        self.join.clicked.connect(self.join_requested.emit)
        header.addWidget(self.join)
        layout.addLayout(header)

        self.empty = QLabel("Watch something from your library with friends who have Mistery, "
                            "in sync. Start one from a film's page, or join a friend's with the "
                            "code they send you.")
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet(f"color: {C.TEXT_FAINT}; font-size: 9.5pt;")
        layout.addWidget(self.empty)

        grid = QWidget()
        self.flow = FlowLayout(grid, margin=0, h_spacing=16, v_spacing=16)
        layout.addWidget(grid)
        self.cards: list[PartyCard] = []

    def reload(self) -> None:
        from ..party import people

        newest: dict[str, object] = {}
        for row in db.recent_parties(_MOVIE_NIGHTS * 6):
            # One card a movie night, for the last thing it watched: a night of
            # three episodes is one night, not three.
            if row["party_id"] not in newest:
                newest[row["party_id"]] = row
            if len(newest) >= _MOVIE_NIGHTS:
                break
        self.flow.clear()
        self.cards = []
        me = people.person_id() if newest else ""
        for row in newest.values():
            card = PartyCard(row, me)
            card.continue_requested.connect(self.continue_requested.emit)
            card.forget_requested.connect(self.forget_requested.emit)
            card.together_requested.connect(self.together_requested.emit)
            self.flow.addWidget(card)
            self.cards.append(card)
        self.empty.setVisible(not newest)
        self.hint.setText(str(len(newest)) if newest else "")
        self.hint.setVisible(bool(newest))



class HomeView(QWidget):
    play_requested = Signal(object)
    play_in_vr_requested = Signal(object)
    item_action = Signal(str, object)
    open_media = Signal(object)
    open_show = Signal(object)
    add_folder_requested = Signal()
    # A friend's film or episode in Continue Watching (friend_library_view.
    # FriendItem): resumed from their PC, or opened in their library. Never
    # through the signals above, which all mean something of this library's.
    friend_play_requested = Signal(object)
    friend_open_requested = Signal(object)
    friend_together_requested = Signal(object)      # its menu's Watch together (app/share/nights.py)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self._scroll)

        content = QWidget()
        self._content = QVBoxLayout(content)
        self._content.setContentsMargins(0, 0, 0, 0)
        self._content.setSpacing(0)
        self._scroll.setWidget(content)

        self.hero = HeroBanner()
        self.hero.play_requested.connect(self.play_requested.emit)
        self.hero.play_in_vr_requested.connect(self.play_in_vr_requested.emit)
        self.hero.details_requested.connect(self.open_media.emit)
        self._content.addWidget(self.hero)

        body = QWidget()
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(52, 8, 52, 48)
        self._body.setSpacing(38)
        self._content.addWidget(body)

        # Only this row previews: these are files you have already started, so
        # the seek sprite exists and the resume point is the frame worth showing.
        self.continue_row = CardRow("Continue Watching", wide=True, preview=True)
        self._connect(self.continue_row)
        self._body.addWidget(self.continue_row)

        self.next_up_row = CardRow("Next Up", wide=True)
        self._connect(self.next_up_row)
        self._body.addWidget(self.next_up_row)

        # MainWindow connects its signals: Join, Continue and Remove.
        self.movie_nights = MovieNights()
        self._body.addWidget(self.movie_nights)

        self.movies_grid = CardGrid("Movies")
        self._connect(self.movies_grid)
        self._body.addWidget(self.movies_grid)

        self.shows_grid = CardGrid("Shows")
        self._connect(self.shows_grid)
        self._body.addWidget(self.shows_grid)

        self._empty = self._build_empty_state()
        self._body.addWidget(self._empty)
        self._body.addStretch(1)

    def _connect(self, section) -> None:
        section.item_clicked.connect(self._on_item_clicked)
        section.item_play_requested.connect(self._on_play)
        section.item_action.connect(self._on_action)

    def _on_item_clicked(self, item) -> None:
        if isinstance(item, FriendItem):
            self.friend_open_requested.emit(item)
        elif isinstance(item, ShowItem):
            self.open_show.emit(item)
        else:
            self.open_media.emit(item)

    def _on_play(self, item) -> None:
        if isinstance(item, FriendItem):
            self.friend_play_requested.emit(item)
        else:
            self.play_requested.emit(item)

    def _on_action(self, action: str, item) -> None:
        if isinstance(item, FriendItem):
            {"play": self.friend_play_requested,
             "together": self.friend_together_requested}.get(action, self.friend_open_requested).emit(item)
        else:
            self.item_action.emit(action, item)

    def _build_empty_state(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 60, 0, 0)
        layout.setSpacing(14)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        headline = QLabel("No videos found yet")
        headline.setStyleSheet(f"color: {C.TEXT}; font-size: 17pt; font-weight: 600;")
        headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(headline)

        self._empty_detail = QLabel()
        self._empty_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_detail.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10.5pt;")
        self._empty_detail.setWordWrap(True)
        self._empty_detail.setMaximumWidth(560)
        layout.addWidget(self._empty_detail, alignment=Qt.AlignmentFlag.AlignCenter)

        button = QPushButton("Add a library folder")
        button.setObjectName("Primary")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(self.add_folder_requested.emit)
        layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignCenter)
        panel.setVisible(False)
        return panel

    # --- refresh ------------------------------------------------------------

    def reload(self) -> None:
        movies = [MediaItem.from_row(r) for r in db.movies()]
        shows = [ShowItem.from_row(r) for r in db.all_shows()]
        # Yours and your friends' in one row, by when you last watched each:
        # a friend's film you stopped halfway is resumed from here like yours.
        min_seconds = float(settings.get("resume_min_seconds", 30))
        started = [(float(r["updated_at"] or 0.0), MediaItem.from_row(r))
                   for r in db.continue_watching(limit=16, min_seconds=min_seconds)]
        started += friend_continue_items(limit=16, min_seconds=min_seconds)
        started.sort(key=lambda pair: pair[0], reverse=True)
        resume = [item for _when, item in started[:16]]

        self.hero.set_item(MediaItem.from_row(db.hero_candidate()))
        self.continue_row.set_items(resume)
        self.continue_row.set_hint(f"{len(resume)} in progress" if resume else "")

        next_up = [MediaItem.from_row(r) for r in db.next_up()]
        self.next_up_row.set_items(next_up)
        self.movie_nights.reload()

        self.movies_grid.set_items(movies)
        self.movies_grid.setVisible(bool(movies))
        self.movies_grid.set_hint(f"{len(movies)}" if movies else "")

        self.shows_grid.set_items(shows)
        self.shows_grid.setVisible(bool(shows))
        self.shows_grid.set_hint(f"{len(shows)}" if shows else "")

        empty = not movies and not shows
        self._empty.setVisible(empty)
        if empty:
            folders = settings.get("library_folders", [])
            if folders:
                self._empty_detail.setText(
                    "Watching:\n" + "\n".join(folders)
                    + "\n\nNothing playable turned up in there. Add another folder, "
                      "or use Rescan in the top bar once you've copied files in."
                )
            else:
                self._empty_detail.setText(
                    "Point Mistery at a folder of movies or shows to get started."
                )
