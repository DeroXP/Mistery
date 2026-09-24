"""Browse pages: all movies, all shows.

A big title with a line saying what is here, a Find field and Shuffle (films);
under it the toolbar (watched or not, categories, sort) and the grid.
Search has a page of its own (search_view.py).
"""

from __future__ import annotations

from collections import Counter

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QActionGroup, QIcon
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .. import db
from ..config import settings
from ..metadata import categories as cat
from ..models import MediaItem, ShowItem
from .theme import C
from .widgets.chips import CategoryFilter
from .widgets.empty import EmptyState
from .widgets.icons import icon_pixmap
from .widgets.pill_field import PillField
from .widgets.rows import CardGrid
from .widgets.segments import SegmentTray

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
_NOUNS = {"movies": ("film", "Find in Movies"), "shows": ("show", "Find in Shows")}


class _SortButton(QPushButton):
    """The sort order, as a pill that opens a short menu. Answers like the
    combo box it replaced (currentIndex, setCurrentIndex, currentIndexChanged)."""

    currentIndexChanged = Signal(int)

    def __init__(self, names: list[str], parent=None) -> None:
        super().__init__(parent)
        self._names = names
        self._index = 0
        self.setObjectName("Pill")
        self.setFixedHeight(42)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)      # the chevron on the right
        self.setIcon(QIcon(icon_pixmap("chevron_down", 16, C.TEXT_DIM, self.devicePixelRatioF())))
        self.setIconSize(QSize(16, 16))
        self._menu = QMenu(self)
        group = QActionGroup(self._menu)
        group.setExclusive(True)
        self._actions = []
        for index, name in enumerate(names):
            action = self._menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(index == 0)
            action.triggered.connect(lambda _checked=False, which=index: self.setCurrentIndex(which))
            group.addAction(action)
            self._actions.append(action)
        self.clicked.connect(self._open)
        self._show()

    def _open(self) -> None:
        self._menu.setMinimumWidth(self.width())
        self._menu.popup(self.mapToGlobal(self.rect().bottomLeft()) + QPoint(0, 8))

    def _show(self) -> None:
        self.setText(f"Sort: {self._names[self._index]}")

    def currentIndex(self) -> int:  # noqa: N802 - the combo box's name
        return self._index

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 - the combo box's name
        if index == self._index or not 0 <= index < len(self._names):
            return
        self._index = index
        self._actions[index].setChecked(True)
        self._show()
        self.currentIndexChanged.emit(index)

    def count(self) -> int:
        return len(self._names)

    def itemText(self, index: int) -> str:  # noqa: N802 - the combo box's name
        return self._names[index]


class LibraryView(QWidget):
    play_requested = Signal(object)
    item_action = Signal(str, object)
    open_media = Signal(object)
    open_show = Signal(object)
    add_folder_requested = Signal()
    shuffle_requested = Signal(list)        # the films on screen, for a shuffled line-up

    def __init__(self, mode: str = "movies", parent=None) -> None:
        super().__init__(parent)
        self._mode = mode
        self._items: list = []
        self._shown: list = []
        self._categories: dict[int, frozenset[str]] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(52, 30, 52, 0)
        root.setSpacing(18)

        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        words = QVBoxLayout()
        words.setSpacing(2)
        self._heading = QLabel()
        self._heading.setObjectName("PageTitle")
        words.addWidget(self._heading)
        self._summary = QLabel()
        self._summary.setObjectName("PageSummary")
        words.addWidget(self._summary)
        title_row.addLayout(words, 1)

        self._find = PillField(_NOUNS.get(mode, ("", "Find"))[1])
        self._search = self._find.edit
        self._search.textChanged.connect(self._on_search_changed)
        title_row.addWidget(self._find, 0, Qt.AlignmentFlag.AlignBottom)

        self._shuffle = QPushButton("Shuffle")
        self._shuffle.setObjectName("Pill")
        self._shuffle.setFixedHeight(44)
        self._shuffle.setIcon(QIcon(icon_pixmap("shuffle", 18, C.TEXT, self.devicePixelRatioF())))
        self._shuffle.setIconSize(QSize(18, 18))
        self._shuffle.setToolTip("Play the films shown here, in a shuffled order")
        self._shuffle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._shuffle.clicked.connect(lambda: self.shuffle_requested.emit(list(self._shown)))
        title_row.addWidget(self._shuffle, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(title_row)

        self._controls = QWidget()
        controls = QHBoxLayout(self._controls)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(10)
        tray = SegmentTray()
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        for index, label in enumerate(_FILTERS):
            chip = QPushButton(label)
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setChecked(index == 0)
            self._filter_group.addButton(chip, index)
            tray.add(chip)
        self._filter_group.idToggled.connect(lambda _id, on: on and self._apply())
        controls.addWidget(tray)

        self._filter = CategoryFilter()
        self._filter.set_pill_look()
        # Through the debounce, not straight to _apply: ticking three categories
        # would otherwise rebuild every card in the grid three times.
        self._filter.changed.connect(self._on_categories_changed)
        controls.addWidget(self._filter)

        self._sort = _SortButton([name for name, _ in _SORTS])
        self._sort.currentIndexChanged.connect(self._apply)
        controls.addWidget(self._sort)

        controls.addStretch(1)
        self._count = QLabel()
        self._count.setObjectName("SectionAside")
        controls.addWidget(self._count)
        root.addWidget(self._controls)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 4, 0, 40)
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
        self._heading.setText({"movies": "Movies", "shows": "Shows"}.get(mode, "Library"))
        self._search.setPlaceholderText(_NOUNS.get(mode, ("", "Find"))[1])
        self._shuffle.setVisible(mode == "movies")
        # The category button hides itself as well when the library has no
        # categories at all — without a TMDB key that used to be every film, and
        # a button opening an empty popover is worse than no button.
        self._filter.setVisible(self._filter.has_categories())
        # "Any" is not remembered: it is the safe default and the one that
        # never hides a title you expected to see.
        self._filter.set_state(settings.get(self._settings_key) or [], "any")
        self._apply()

    def show_categories(self, names) -> None:
        """Open on these categories, any of them (Home's "See all"), kept as the
        page's choice like one made in its popover."""
        self._filter.set_state(list(names), "any")
        settings.set(self._settings_key, self._filter.selected())
        self._apply()

    def _on_categories_changed(self) -> None:
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
        else:
            rows = db.movies()
            self._items = [MediaItem.from_row(r) for r in rows]

        # Categories come off the database row, not the view model: `user_genres`
        # is a column of its own so a refetch can't wipe a hand-set category, and
        # the dataclasses in models.py don't carry it. Built from every row
        # before any filtering, so a chosen category never vanishes from the
        # popover just because it currently matches nothing.
        self._categories = {int(r["id"]): frozenset(cat.categories_of(r)) for r in rows}
        counts = Counter(name for names in self._categories.values() for name in names)
        self._filter.set_available([(name, counts[name])
                                    for name in cat.CATEGORIES if name in counts])
        self._filter.setVisible(self._filter.has_categories())
        self._summary.setText(self._summary_line())

        self._apply()

    def _summary_line(self) -> str:
        """What is here, under the title: "12 films · 5 in progress · 3 watched"."""
        items = self._items
        noun = _NOUNS.get(self._mode, ("title", ""))[0]
        bits = [f"{len(items)} {noun}{'' if len(items) == 1 else 's'}"]
        if self._mode == "shows":
            watching = sum(1 for show in items if 0 < show.watched_count < show.episode_count)
            done = sum(1 for show in items if show.episode_count and show.watched_count >= show.episode_count)
            if watching:
                bits.append(f"{watching} you're part way through")
            if done:
                bits.append(f"{done} finished")
        else:
            started = sum(1 for film in items if not film.watched and film.resume_position > 0)
            watched = sum(1 for film in items if film.watched)
            if started:
                bits.append(f"{started} in progress")
            if watched:
                bits.append(f"{watched} watched")
        return "  ·  ".join(bits)

    def _apply(self) -> None:
        term = self._search.text().strip()

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

        self._shown = items
        self._shuffle.setEnabled(bool(items))
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
