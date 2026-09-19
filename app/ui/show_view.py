"""Show page: series header, season switcher, episode list."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .. import db
from ..metadata import categories as cat
from ..models import MediaItem, ShowItem
from ..util import elide, fmt_duration
from .detail_view import _Backdrop
from .theme import C
from .widgets.artview import ArtView
from .widgets.cards import set_extra_actions
from .widgets.chips import CategoryEditor
from .widgets.icons import IconButton
from .widgets.rows import CardGrid


class ShowView(QWidget):
    play_requested = Signal(object, float)
    movie_night_requested = Signal(object)      # an episode to watch with friends
    item_action = Signal(str, object)
    open_media = Signal(object)
    back_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._show = ShowItem()
        self._episodes: list[MediaItem] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(scroll)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        scroll.setWidget(content)

        self._backdrop = _Backdrop(height=350)
        header = QHBoxLayout(self._backdrop)
        header.setContentsMargins(52, 22, 52, 30)
        header.setSpacing(28)
        header.setAlignment(Qt.AlignmentFlag.AlignBottom)

        left = QVBoxLayout()
        self._back = IconButton("back", size=38, icon_size=21, tooltip="Back")
        self._back.clicked.connect(self.back_requested.emit)
        left.addWidget(self._back, alignment=Qt.AlignmentFlag.AlignTop)
        left.addStretch(1)
        self._poster = ArtView(170, 255, radius=12)
        left.addWidget(self._poster)
        header.addLayout(left)

        info = QVBoxLayout()
        info.setSpacing(0)
        info.setAlignment(Qt.AlignmentFlag.AlignBottom)
        header.addLayout(info, 1)

        eyebrow = QLabel("SERIES")
        eyebrow.setStyleSheet(
            f"color: {C.ACCENT}; font-size: 9.5pt; font-weight: 700; letter-spacing: 1.2px;"
        )
        info.addWidget(eyebrow)

        self._title = QLabel()
        self._title.setWordWrap(True)
        self._title.setStyleSheet(f"color: {C.TEXT}; font-size: 26pt; font-weight: 700;")
        info.addSpacing(4)
        info.addWidget(self._title)

        self._meta = QLabel()
        self._meta.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10.5pt;")
        info.addSpacing(8)
        info.addWidget(self._meta)

        self._categories = CategoryEditor()
        self._categories.changed.connect(self._on_categories_changed)
        info.addSpacing(2)
        info.addWidget(self._categories)

        self._overview = QLabel()
        self._overview.setWordWrap(True)
        self._overview.setFixedWidth(760)
        # The header is a fixed height, so the summary must not be allowed to
        # push the buttons out of it — three lines maximum.
        self._overview.setMaximumHeight(66)
        self._overview.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10pt;")
        info.addSpacing(10)
        info.addWidget(self._overview)

        buttons = QHBoxLayout()
        buttons.setSpacing(11)
        info.addSpacing(18)
        info.addLayout(buttons)
        self._play = QPushButton("Play")
        self._play.setObjectName("Primary")
        self._play.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play.clicked.connect(self._play_next_up)
        buttons.addWidget(self._play)
        # Opens on your next episode; the dialog picks any other, because the
        # friends' place in a show is often not yours. Each episode's card menu
        # has it too.
        self._party = QPushButton("Start movie night")
        self._party.setObjectName("Ghost")
        self._party.setCursor(Qt.CursorShape.PointingHandCursor)
        self._party.setToolTip("Watch an episode with friends who have Mistery, in sync. Your "
                               "own place in the show stays where it is.")
        self._party.clicked.connect(self._movie_night)
        buttons.addWidget(self._party)
        buttons.addStretch(1)

        content_layout.addWidget(self._backdrop)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(52, 24, 52, 52)
        body_layout.setSpacing(18)
        content_layout.addWidget(body)

        self._seasons_row = QHBoxLayout()
        self._seasons_row.setSpacing(8)
        self._season_group = QButtonGroup(self)
        self._season_group.setExclusive(True)
        self._season_group.idToggled.connect(lambda _id, on: on and self._render_episodes())
        body_layout.addLayout(self._seasons_row)

        self._grid = CardGrid(wide=True)
        self._grid.item_clicked.connect(self.open_media.emit)
        self._grid.item_play_requested.connect(
            lambda item: self.play_requested.emit(item, item.resume_position)
        )
        self._grid.item_action.connect(self.item_action.emit)
        body_layout.addWidget(self._grid)
        body_layout.addStretch(1)

    # --- data ---------------------------------------------------------------

    def set_show(self, show: ShowItem) -> None:
        # The same show again is a refresh — closing the player, marking an
        # episode watched, the artwork retry — not a fresh visit, and it used to
        # put you back on Season 1 every time: after each episode of Season 3
        # you had to find Season 3 again.
        keep_season = self._season_group.checkedId() if show.id == self._show.id else -1
        fresh = db.get_show(show.id)
        self._show = ShowItem.from_row(fresh) if fresh else show
        show = self._show

        self._backdrop.set_art(show.backdrop or show.poster)
        self._poster.set_art(show.art, show.title)
        self._title.setText(show.title)

        self._episodes = [MediaItem.from_row(r) for r in db.episodes_for_show(show.id)]

        meta_bits = []
        if show.year:
            meta_bits.append(str(show.year))
        seasons = sorted({e.season for e in self._episodes if e.season is not None})
        if seasons:
            meta_bits.append(f"{len(seasons)} season{'s' if len(seasons) != 1 else ''}")
        meta_bits.append(f"{len(self._episodes)} episode{'s' if len(self._episodes) != 1 else ''}")
        total = sum(e.duration or 0 for e in self._episodes)
        if total:
            meta_bits.append(fmt_duration(total) + " total")
        self._meta.setText("   ·   ".join(meta_bits))

        # Genres were plain text on this line; they are now the editor below it.
        self._categories.set_row(show.genres, fresh["user_genres"] if fresh else None)

        self._overview.setText(elide(show.overview or "", 260))
        self._overview.setVisible(bool(show.overview))

        for button in list(self._season_group.buttons()):
            self._season_group.removeButton(button)
            button.setParent(None)
            button.deleteLater()
        while self._seasons_row.count():
            self._seasons_row.takeAt(0)

        if keep_season not in seasons:
            keep_season = seasons[0] if seasons else -1
        for season in seasons:
            chip = QPushButton(f"Season {season}")
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setChecked(season == keep_season)
            self._season_group.addButton(chip, season)
            self._seasons_row.addWidget(chip)
        self._seasons_row.addStretch(1)

        next_up = self._next_up()
        if next_up is not None:
            label = "Resume" if next_up.resume_position > 0 else "Play"
            self._play.setText(f"{label}  {next_up.code}".strip())
        self._play.setVisible(next_up is not None)
        self._party.setVisible(next_up is not None)

        self._render_episodes()

    def _on_categories_changed(self, names: list) -> None:
        """Written to user_genres, which the metadata pass never touches."""
        if self._show.id:
            db.update_show(self._show.id, user_genres=cat.join(names))

    def show_season(self, season: int | None) -> None:
        """Turn to a season if the show has it: the one the player was last in,
        which autoplay can have carried past the season you picked."""
        button = self._season_group.button(season) if season is not None else None
        if button is not None and not button.isChecked():
            button.setChecked(True)             # idToggled renders it

    def _next_up(self) -> MediaItem | None:
        """First unfinished episode, else the first episode."""
        for episode in self._episodes:
            if not episode.watched:
                return episode
        return self._episodes[0] if self._episodes else None

    def _play_next_up(self) -> None:
        episode = self._next_up()
        if episode is not None:
            self.play_requested.emit(episode, episode.resume_position)

    def _movie_night(self) -> None:
        episode = self._next_up()
        if episode is not None:
            self.movie_night_requested.emit(episode)

    def _render_episodes(self) -> None:
        season = self._season_group.checkedId()
        episodes = [e for e in self._episodes if e.season == season] or self._episodes
        self._grid.set_items(episodes, "No episodes in this season")
        # item_action("party", episode) reaches MainWindow like every other entry.
        set_extra_actions(self._grid, [("Start movie night", "party")])
