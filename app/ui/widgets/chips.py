"""Category chips: the filter on the browse pages, and the editor on a title's page.

Both are a popover of checkable #Chip buttons rather than a list of checkboxes,
because every other multi-choice surface in this app is chips and #Chip:checked
already reads as "on" (theme.py). The filter keeps its chips behind a button
instead of in the control bar: a library with a TMDB key produces up to 19
categories, which wraps to three rows above the grid and pushes the posters off
a 1080p screen — the opposite of browsing.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ...metadata import categories as cat
from ..theme import C
from .flow import FlowLayout
from .icons import icon_pixmap


class _Popover(QWidget):
    """A rounded panel that closes when you click away, like the sound panel."""

    closed = Signal()

    def __init__(self, parent=None, width: int = 340) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(width)

    def popup_below(self, anchor: QWidget) -> None:
        """Open under the button that asked for it, kept inside the screen."""
        self.adjustSize()
        corner = anchor.mapToGlobal(anchor.rect().bottomLeft())
        x, y = corner.x(), corner.y() + 8
        screen = anchor.screen() or self.screen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left() + 8, min(x, area.right() - self.width() - 8))
            if y + self.height() > area.bottom() - 8:
                # No room below: open above the button rather than off-screen.
                y = anchor.mapToGlobal(anchor.rect().topLeft()).y() - self.height() - 8
        self.move(x, y)
        self.show()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(self.rect().adjusted(0, 0, -1, -1), 22, 22)
        painter.fillPath(path, QColor(C.SURFACE))
        painter.setPen(QPen(QColor("#2B2520"), 1))
        painter.drawPath(path)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().hideEvent(event)
        self.closed.emit()


def _chip(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("Chip")
    button.setCheckable(True)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


class CategoryFilter(QWidget):
    """The "All categories" button on Movies and Shows, and its popover.

    `changed` fires on every toggle; the caller decides whether to debounce it.
    """

    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._available: list[tuple[str, int]] = []
        self._selected: set[str] = set()
        self._mode = "any"
        self._quiet = False

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self._button = _chip("All categories")
        self._button.clicked.connect(self._open)
        row.addWidget(self._button)

        self._panel = _Popover(self)
        panel = QVBoxLayout(self._panel)
        panel.setContentsMargins(18, 15, 18, 15)
        panel.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("Categories")
        title.setObjectName("SectionTitle")
        head.addWidget(title)
        head.addStretch(1)

        # Any, not All, by default: genre data is sparse (this library has four
        # shows and 11 films with categories at all), and three chips under All
        # lands on an empty grid often enough that it has to be chosen on
        # purpose rather than walked into.
        self._match = QButtonGroup(self)
        self._match.setExclusive(True)
        for index, label in enumerate(("Any", "All")):
            button = _chip(label)
            button.setChecked(index == 0)
            button.setToolTip("Titles in any of the chosen categories" if index == 0
                              else "Only titles in every chosen category")
            self._match.addButton(button, index)
            head.addWidget(button)
        self._match.idToggled.connect(self._on_match)
        panel.addLayout(head)

        holder = QWidget()
        self._flow = FlowLayout(holder, h_spacing=7, v_spacing=8)
        panel.addWidget(holder)

        self._clear = QPushButton("Clear")
        self._clear.setObjectName("Ghost")
        self._clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear.clicked.connect(self._on_clear)
        panel.addWidget(self._clear, alignment=Qt.AlignmentFlag.AlignLeft)

        self._note = QLabel()
        self._note.setObjectName("Faint")
        self._note.setWordWrap(True)
        panel.addWidget(self._note)

        # _open lights the button while the popover is up, and nothing put it
        # back: opening it and closing it again without ticking anything left
        # the control bar claiming a category filter was on until the next
        # reload, because #Chip:checked is the theme's "this filter is on".
        # Connected last, so _sync always has the widgets it reads.
        self._panel.closed.connect(self._sync)

    # --- state ---------------------------------------------------------------

    def selected(self) -> list[str]:
        return [name for name in cat.CATEGORIES if name in self._selected]

    def mode(self) -> str:
        return self._mode

    def set_state(self, names, mode: str) -> None:
        """Restore a saved selection. Anything not in the library is dropped."""
        have = {name for name, _ in self._available}
        self._selected = {n for n in names if n in have} if have else set(names)
        self._mode = "all" if mode == "all" else "any"
        button = self._match.button(1 if self._mode == "all" else 0)
        if button is not None and not button.isChecked():
            self._match.blockSignals(True)
            button.setChecked(True)
            self._match.blockSignals(False)
        self._sync()

    def set_available(self, counted: list[tuple[str, int]]) -> None:
        """The categories this library actually has, with how many titles each has.

        Built from every row before any filtering, so a chosen category never
        disappears from the popover just because it currently matches nothing.
        A category that left the library with its files is dropped quietly.
        """
        # Pruned before the early return, or a category that left with its files
        # would keep filtering from a button that is no longer on screen.
        self._selected &= {name for name, _ in counted}
        if counted == self._available:
            self._sync()
            return
        self._available = list(counted)

        self._flow.clear()
        for name, count in counted:
            chip = _chip(f"{name}  {count}")
            chip.setChecked(name in self._selected)
            chip.toggled.connect(lambda on, n=name: self._on_toggle(n, on))
            self._flow.addWidget(chip)
        self._sync()

    def has_categories(self) -> bool:
        return bool(self._available)

    def set_pill_look(self) -> None:
        """The button as a toolbar pill with a chevron (the Movies page's),
        rather than a chip."""
        self._button.setObjectName("Pill")
        self._button.setFixedHeight(42)
        self._button.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._button.setIcon(QIcon(icon_pixmap("chevron_down", 16, C.TEXT_DIM, self.devicePixelRatioF())))
        self._button.setIconSize(QSize(16, 16))

    # --- reactions -----------------------------------------------------------

    def _open(self) -> None:
        self._button.setChecked(True)       # pressed while the popover is open
        self._panel.popup_below(self._button)

    def _on_toggle(self, name: str, on: bool) -> None:
        if on:
            self._selected.add(name)
        else:
            self._selected.discard(name)
        if self._quiet:
            return
        self._sync()
        self.changed.emit()

    def _on_match(self, index: int, on: bool) -> None:
        if not on:
            return
        self._mode = "all" if index == 1 else "any"
        self._sync()
        if self._selected:
            self.changed.emit()

    def _on_clear(self) -> None:
        self._quiet = True              # one rebuild of the grid, not one per chip
        for index in range(self._flow.count()):
            widget = self._flow.itemAt(index).widget()
            if widget is not None:
                widget.setChecked(False)
        self._quiet = False
        self._selected.clear()
        self._sync()
        self.changed.emit()

    def _sync(self) -> None:
        chosen = self.selected()
        if not chosen:
            label = "All categories"
        elif len(chosen) <= 2:
            label = ", ".join(chosen)
        else:
            # A fixed width past two names, so the control bar doesn't grow a
            # row when someone ticks six categories.
            label = f"{len(chosen)} categories"
        self._button.setText(label)
        self._button.setChecked(bool(chosen) or self._panel.isVisible())
        self._clear.setVisible(bool(chosen))
        if len(chosen) > 1 and self._mode == "all":
            self._note.setText("All: a title has to be in every one of them.")
        else:
            self._note.setText("")
        self._note.setVisible(bool(self._note.text()))

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        # The browse page hides the whole control bar when a section is empty;
        # a popover left open would float over the guidance panel.
        self._panel.hide()
        self._button.setChecked(bool(self._selected))
        super().hideEvent(event)


class CategoryEditor(QWidget):
    """The categories line on a film's or a series' page, click to change.

    What a person picks here is stored in `user_genres`, a column of its own, so
    the next metadata pass — which rewrites `genres` wholesale — cannot undo it.
    """

    changed = Signal(list)
    closed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._chosen: set[str] = set()
        self._from_service: set[str] = set()

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._line = QPushButton()
        self._line.setObjectName("Ghost")
        self._line.setCursor(Qt.CursorShape.PointingHandCursor)
        self._line.setToolTip("Choose the categories this appears under")
        # A pill: 30 high with a radius under half of it (the Ghost style's 22 on
        # a 28-high button came out square, Qt's rule past half the height).
        self._line.setFixedHeight(30)
        self._line.setStyleSheet(
            f"color: {C.TEXT_DIM}; font-size: 10pt; font-weight: 500;"
            "text-align: left; padding: 0 14px; border-radius: 14px;"
        )
        self._line.clicked.connect(self._open)
        row.addWidget(self._line)
        row.addStretch(1)

        self._panel = _Popover(self, width=360)
        self._panel.closed.connect(self.closed.emit)
        panel = QVBoxLayout(self._panel)
        panel.setContentsMargins(18, 15, 18, 15)
        panel.setSpacing(11)
        title = QLabel("Categories")
        title.setObjectName("SectionTitle")
        panel.addWidget(title)

        holder = QWidget()
        self._flow = FlowLayout(holder, h_spacing=7, v_spacing=8)
        self._buttons: dict[str, QPushButton] = {}
        for name in cat.CATEGORIES:
            chip = _chip(name)
            chip.toggled.connect(lambda on, n=name: self._on_toggle(n, on))
            self._buttons[name] = chip
            self._flow.addWidget(chip)
        panel.addWidget(holder)

        note = QLabel("Your choices are kept when Mistery looks the title up again.")
        note.setObjectName("Faint")
        note.setWordWrap(True)
        panel.addWidget(note)

    def set_row(self, genres: str | None, user_genres: str | None) -> None:
        """What the service said and what the person chose; both are shown."""
        self._from_service = set(cat.split(genres))
        self._chosen = set(cat.split(user_genres))
        for name, chip in self._buttons.items():
            chip.blockSignals(True)
            chip.setChecked(name in self._chosen)
            chip.blockSignals(False)
            # A category the service already gave is dimmed rather than ticked:
            # ticking it too would write a duplicate into user_genres. But one
            # the person set by hand stays live even after the service starts
            # saying the same word, or there is no way left to take it off —
            # and that is the ordinary path, not a corner. The categories stage
            # queues exactly the films whose `genres` is empty, which is exactly
            # the film someone would have tagged by hand; disabled-and-ticked,
            # the choice was stuck for good, and came back on the film the day
            # a TMDB key rewrote `genres` without it.
            chip.setEnabled(name not in self._from_service or name in self._chosen)
        self._sync()

    def user_categories(self) -> list[str]:
        return [name for name in cat.CATEGORIES if name in self._chosen]

    def _open(self) -> None:
        self._panel.popup_below(self._line)

    def _on_toggle(self, name: str, on: bool) -> None:
        if on:
            self._chosen.add(name)
        else:
            self._chosen.discard(name)
        self._sync()
        self.changed.emit(self.user_categories())

    def _sync(self) -> None:
        shown = [name for name in cat.CATEGORIES
                 if name in self._from_service or name in self._chosen]
        self._line.setText("   ·   ".join(shown) if shown else "Add categories")

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._panel.hide()
        super().hideEvent(event)
