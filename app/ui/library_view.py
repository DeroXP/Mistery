"""Browse pages: all movies, all shows, and search results."""

from __future__ import annotations

from collections import Counter

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from .. import db
from ..config import settings
from ..metadata import categories as cat
from ..models import MediaItem, ShowItem
from .theme import C
from .widgets.chips import CategoryFilter
from .widgets.empty import EmptyState
from .widgets.rows import CardGrid

# Shown when a whole section of the library has nothing in it yet.
_EMPTY_LIBRARY = {
    "movies": (
        "film",
        "No movies yet",
        "Mistery reads titles straight from your filenames — "
        "<span style='color:#EDF1F6'>Blade.Runner.2049.2160p.HDR.mkv</span> becomes "
        "<b>Blade Runner 2049</b>, 2049, 4K, HDR.<br><br>"
        "Add a folder containing your films and they'll show up here.",
        "Add a library folder",
    ),
    "shows": (
        "tv",
        "No shows yet",
        "Episodes group into Show → Season → Episode on their own. Mistery reads "
        "<span style='color:#EDF1F6'>Show.Name.S01E04.mkv</span>, "
        "<span style='color:#EDF1F6'>Show Name - 1x04.mkv</span>, and files inside a "
        "<span style='color:#EDF1F6'>Season 01</span> folder."
        "<br><br>Add a folder of TV files and your series land here, with resume "
        "and auto-play-next across episodes.",
        "Add a library folder",
    ),
    "search": (
        "search",
        "Search your library",
        "Look up anything by title, genre, or plot. Two characters is enough to start.",
        "",
    ),
}

_SORTS = [
    ("Title", lambda i: (getattr(i, "title", "") or "").lower()),
    # Newest first. Not the database order reversed: movies and shows both come
    # back sorted by title, so reversing them only ever gave Z to A.
    ("Recently added", lambda i: -(getattr(i, "added_at", 0) or 0)),
    ("Year", lambda i: -(getattr(i, "year", 0) or 0)),
    ("Runtime", lambda i: -(getattr(i, "duration", 0) or 0)),
]

_FILTERS = ["All", "Unwatched", "In progress", "Watched"]


class LibraryView(QWidget):
    play_requested = Signal(object)
    item_action = Signal(str, object)
    open_media = Signal(object)
    open_show = Signal(object)
    add_folder_requested = Signal()

    def __init__(self, mode: str = "movies", parent=None) -> None:
        super().__init__(parent)
        self._mode = mode
        self._items: list = []
        self._categories: dict[int, frozenset[str]] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(52, 34, 52, 0)
        root.setSpacing(18)

        title_row = QHBoxLayout()
        self._heading = QLabel()
        self._heading.setObjectName("PageTitle")
        title_row.addWidget(self._heading)
        title_row.addStretch(1)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search titles, genres, plots…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(320)
        self._search.textChanged.connect(self._on_search_changed)
        title_row.addWidget(self._search)
        root.addLayout(title_row)

        self._controls = QWidget()
        controls = QHBoxLayout(self._controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        for index, label in enumerate(_FILTERS):
            chip = QPushButton(label)
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setChecked(index == 0)
            self._filter_group.addButton(chip, index)
            controls.addWidget(chip)
        self._filter_group.idToggled.connect(lambda _id, on: on and self._apply())

        controls.addSpacing(16)
        self._filter = CategoryFilter()
        # Through the debounce, not straight to _apply: ticking three categories
        # would otherwise rebuild every card in the grid three times.
        self._filter.changed.connect(self._on_categories_changed)
        controls.addWidget(self._filter)

        self._sort = QComboBox()
        self._sort.addItems([name for name, _ in _SORTS])
        self._sort.currentIndexChanged.connect(self._apply)
        controls.addWidget(self._sort)

        controls.addStretch(1)
        self._count = QLabel()
        self._count.setObjectName("Faint")
        controls.addWidget(self._count)
        root.addWidget(self._controls)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 40)
        self._grid = CardGrid()
        self._grid.item_clicked.connect(self._on_clicked)
        self._grid.item_play_requested.connect(self.play_requested.emit)
        self._grid.item_action.connect(self.item_action.emit)
        holder_layout.addWidget(self._grid)

        self._empty = EmptyState()
        self._empty.action_clicked.connect(self.add_folder_requested.emit)
        self._empty.setVisible(False)
        holder_layout.addWidget(self._empty)
        holder_layout.addStretch(1)
        scroll.setWidget(holder)
        root.addWidget(scroll)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)
        self._debounce.timeout.connect(self._apply)

        self.set_mode(mode)

    # --- state --------------------------------------------------------------

    @property
    def _settings_key(self) -> str:
        return "show_categories" if self._mode == "shows" else "movie_categories"

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._heading.setText(
            {"movies": "Movies", "shows": "Shows", "search": "Search"}.get(mode, "Library")
        )
        show_controls = mode != "search"
        # The category button hides itself as well when the library has no
        # categories at all — without a TMDB key that used to be every film, and
        # a button opening an empty popover is worse than no button.
        self._filter.setVisible(show_controls and self._filter.has_categories())
        self._sort.setVisible(show_controls)
        for button in self._filter_group.buttons():
            button.setVisible(show_controls)
        if show_controls:
            # "Any" is not remembered: it is the safe default and the one that
            # never hides a title you expected to see.
            self._filter.set_state(settings.get(self._settings_key) or [], "any")
        if mode == "search":
            self._search.setFocus()
        self._apply()

    def _on_categories_changed(self) -> None:
        if self._mode != "search":
            settings.set(self._settings_key, self._filter.selected())
        self._debounce.start()

    def _show_empty(self, empty: bool) -> None:
        """Swap the grid for a guidance panel when there is nothing to browse."""
        if empty:
            self._empty.configure(*_EMPTY_LIBRARY[self._mode])
        self._empty.setVisible(empty)
        self._grid.setVisible(not empty)
        self._controls.setVisible(not empty)

    def focus_search(self) -> None:
        self._search.setFocus()
        self._search.selectAll()

    def _on_search_changed(self) -> None:
        self._debounce.start()

    def _on_clicked(self, item) -> None:
        if isinstance(item, ShowItem):
            self.open_show.emit(item)
        else:
            self.open_media.emit(item)

    # --- data ---------------------------------------------------------------

    def reload(self) -> None:
        if self._mode == "shows":
            rows = db.all_shows()
            self._items = [ShowItem.from_row(r) for r in rows]
        elif self._mode == "movies":
            rows = db.movies()
            self._items = [MediaItem.from_row(r) for r in rows]
        else:
            rows, self._items = [], []

        # Categories come off the database row, not the view model: `user_genres`
        # is a column of its own so a refetch can't wipe a hand-set category, and
        # the dataclasses in models.py don't carry it. Built from every row
        # before any filtering, so a chosen category never vanishes from the
        # popover just because it currently matches nothing.
        self._categories = {int(r["id"]): frozenset(cat.categories_of(r)) for r in rows}
        counts = Counter(name for names in self._categories.values() for name in names)
        self._filter.set_available([(name, counts[name])
                                    for name in cat.CATEGORIES if name in counts])
        self._filter.setVisible(self._mode != "search" and self._filter.has_categories())

        self._apply()

    def _apply(self) -> None:
        term = self._search.text().strip()

        if self._mode == "search":
            if len(term) < 2:
                self._show_empty(True)
                self._count.setText("")
                return
            items = [MediaItem.from_row(r) for r in db.search(term)]
            self._show_empty(False)
            self._grid.set_items(items, f"Nothing matches “{term}”")
            self._count.setText(f"{len(items)} result(s)")
            return

        if not self._items:
            # Nothing of this kind in the library at all — explain how to add some.
            self._show_empty(True)
            self._count.setText("")
            return
        self._show_empty(False)

        items = list(self._items)
        if term:
            lowered = term.lower()
            items = [i for i in items if lowered in (getattr(i, "title", "") or "").lower()]

        # Whole names, compared against the row's categories. The old filter
        # asked whether the chosen word appeared anywhere in the joined genres
        # string, so Fantasy matched every "Sci-Fi & Fantasy" series and Action
        # matched every "Action & Adventure" one.
        chosen = self._filter.selected()
        mode = self._filter.mode()
        if chosen:
            wanted = set(chosen)
            empty = frozenset()
            items = [i for i in items
                     if cat.matches(wanted, self._categories.get(int(i.id), empty), mode)]

        state = self._filter_group.checkedId()
        if state == 1:
            items = [i for i in items if not getattr(i, "watched", False)
                     and getattr(i, "progress", 0) <= 0.001]
        elif state == 2:
            items = [i for i in items if 0.001 < getattr(i, "progress", 0) < 0.97
                     and not getattr(i, "watched", False)]
        elif state == 3:
            items = [i for i in items if getattr(i, "watched", False)]

        items.sort(key=_SORTS[self._sort.currentIndex()][1])

        self._grid.set_items(items, self._empty_message(term, chosen, mode, state))
        self._count.setText(f"{len(items)} of {len(self._items)}")

    def _empty_message(self, term: str, chosen: list, mode: str, state: int) -> str:
        """Name every filter that is on, so it is clear what to undo."""
        names = ""
        if chosen:
            # "and" under All, "or" under Any: with two chips ticked under Any,
            # "Nothing in Anime and Comedy" describes the other mode.
            joiner = " and " if mode == "all" else " or "
            names = (", ".join(chosen[:-1]) + joiner + chosen[-1]
                     if len(chosen) > 1 else chosen[0])
        if term and names:
            return f"Nothing in {names} matches “{term}”"
        if term:
            return f"Nothing matches “{term}”"
        if names:
            if len(chosen) > 1 and mode == "all":
                # The common way to land here: three categories under All is
                # usually empty, and the way out is one fewer, not a rescan.
                return f"Nothing is in {names} at once — try Any, or one fewer"
            return f"Nothing in {names}"
        if state == 1:
            return "You've started everything here"
        if state == 2:
            return "Nothing in progress"
        if state == 3:
            return "Nothing marked watched yet"
        return "Nothing here yet"
