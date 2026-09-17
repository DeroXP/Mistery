"""Home: hero banner, Continue Watching, then the library as a grid."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .. import db
from ..config import settings
from ..models import MediaItem, ShowItem
from .theme import C
from .widgets.hero import HeroBanner
from .widgets.rows import CardGrid, CardRow


class HomeView(QWidget):
    play_requested = Signal(object)
    play_in_vr_requested = Signal(object)
    item_action = Signal(str, object)
    open_media = Signal(object)
    open_show = Signal(object)
    add_folder_requested = Signal()

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
        section.item_play_requested.connect(self.play_requested.emit)
        section.item_action.connect(self.item_action.emit)

    def _on_item_clicked(self, item) -> None:
        if isinstance(item, ShowItem):
            self.open_show.emit(item)
        else:
            self.open_media.emit(item)

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
        resume = [
            MediaItem.from_row(r)
            for r in db.continue_watching(
                limit=16, min_seconds=float(settings.get("resume_min_seconds", 30))
            )
        ]

        self.hero.set_item(MediaItem.from_row(db.hero_candidate()))
        self.continue_row.set_items(resume)
        self.continue_row.set_hint(f"{len(resume)} in progress" if resume else "")

        next_up = [MediaItem.from_row(r) for r in db.next_up()]
        self.next_up_row.set_items(next_up)

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
