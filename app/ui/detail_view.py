"""Detail page for a single movie or episode."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QImage, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from .. import db, vr
from ..images import load_async
from ..metadata import categories as cat
from ..models import MediaItem
from ..util import elide, fmt_clock, fmt_duration, fmt_remaining, fmt_size, reveal_in_explorer
from .theme import C, display_family
from .widgets.artview import ArtView
from .widgets.chips import CategoryEditor
from .widgets.flow import FlowLayout
from .widgets.hero import ProgressBar
from .widgets.icons import IconButton, icon_pixmap

_TITLE_W = 760                  # the title's room before it steps down a size
_TITLE_SIZES = (38, 32, 27)     # pt, largest first


class _Backdrop(QWidget):
    """Painted backdrop with scrims; children lay out on top of it."""

    def __init__(self, height: int = 430, parent=None) -> None:
        super().__init__(parent)
        self._image = QImage()
        self._path: str | None = None
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_art(self, path: str | None) -> None:
        # Asked again while there's no picture, as the home hero is: a read that
        # failed once (a file held by a backup tool) is otherwise never retried,
        # because the artwork retry reopens the page with the same path.
        if path == self._path and not self._image.isNull():
            return
        self._path = path
        self._image = QImage()
        if path:
            load_async(path, lambda image, wanted=path: self._on_image(image, wanted))
        self.update()

    def _on_image(self, image: QImage, wanted: str | None = None) -> None:
        if wanted != self._path:
            return                  # a read for the title shown before this one
        self._image = image
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        rect = QRectF(self.rect())
        painter.fillRect(rect, QColor(C.BG))

        if not self._image.isNull():
            scale = max(rect.width() / self._image.width(),
                        rect.height() / self._image.height())
            width = self._image.width() * scale
            height = self._image.height() * scale
            painter.setOpacity(0.85)
            painter.drawImage(
                QRectF((rect.width() - width) / 2, (rect.height() - height) / 3, width, height),
                self._image,
            )
            painter.setOpacity(1.0)

        base = QColor(C.BG)
        red, green, blue = base.red(), base.green(), base.blue()
        horizontal = QLinearGradient(0, 0, rect.width(), 0)
        horizontal.setColorAt(0.0, QColor(red, green, blue, 245))
        horizontal.setColorAt(0.55, QColor(red, green, blue, 170))
        horizontal.setColorAt(1.0, QColor(red, green, blue, 80))
        painter.fillRect(rect, horizontal)

        vertical = QLinearGradient(0, rect.height() * 0.35, 0, rect.height())
        vertical.setColorAt(0.0, QColor(red, green, blue, 0))
        vertical.setColorAt(1.0, base)
        painter.fillRect(rect, vertical)


class DetailView(QWidget):
    play_requested = Signal(object, float)
    play_in_vr_requested = Signal(object)
    movie_night_requested = Signal(object)      # watch this with friends (app/ui/party_dialog.py)
    back_requested = Signal()
    open_show = Signal(int)
    media_changed = Signal()
    refresh_art_requested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._item = MediaItem()

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

        # --- header -------------------------------------------------------
        self._backdrop = _Backdrop()
        header = QHBoxLayout(self._backdrop)
        header.setContentsMargins(52, 22, 52, 34)
        header.setSpacing(30)
        header.setAlignment(Qt.AlignmentFlag.AlignBottom)

        back_column = QVBoxLayout()
        self._back = IconButton("back", size=38, icon_size=21, tooltip="Back")
        self._back.clicked.connect(self.back_requested.emit)
        back_column.addWidget(self._back, alignment=Qt.AlignmentFlag.AlignTop)
        back_column.addStretch(1)

        self._poster = ArtView(206, 309, radius=18)
        back_column.addWidget(self._poster)
        header.addLayout(back_column)

        info = QVBoxLayout()
        info.setSpacing(0)
        info.setAlignment(Qt.AlignmentFlag.AlignBottom)
        header.addLayout(info, 1)

        self._eyebrow = QLabel()
        self._eyebrow.setTextFormat(Qt.TextFormat.RichText)
        info.addWidget(self._eyebrow)

        self._title = QLabel()
        self._title.setWordWrap(True)
        self._title.setMaximumWidth(_TITLE_W)
        info.addSpacing(6)
        info.addWidget(self._title)

        self._tagline = QLabel()
        self._tagline.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 11pt; font-style: italic;")
        info.addSpacing(2)
        info.addWidget(self._tagline)

        self._meta = QLabel()
        self._meta.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 11pt;")
        info.addSpacing(8)
        info.addWidget(self._meta)

        # How far along, as on Home's hero: the bar, and the time left.
        self._progress_row = QWidget()
        progress = QHBoxLayout(self._progress_row)
        progress.setContentsMargins(0, 12, 0, 0)
        progress.setSpacing(14)
        self._progress = ProgressBar()
        progress.addWidget(self._progress, 0, Qt.AlignmentFlag.AlignVCenter)
        self._left = QLabel()
        self._left.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10.5pt;")
        progress.addWidget(self._left)
        progress.addStretch(1)
        info.addWidget(self._progress_row)

        self._categories = CategoryEditor()
        self._categories.changed.connect(self._on_categories_changed)
        # Only once the popover closes: media_changed reloads this very page
        # (main_window.reload_all), which would rebuild the editor under the
        # hand that is still ticking chips.
        self._categories.closed.connect(self.media_changed.emit)
        info.addSpacing(4)
        info.addWidget(self._categories)

        self._badges = QHBoxLayout()
        self._badges.setSpacing(7)
        self._badges.setContentsMargins(0, 0, 0, 0)
        info.addSpacing(12)
        info.addLayout(self._badges)

        # Six buttons at streaming-app proportions overflow a narrow window,
        # so let the row wrap instead of running off the edge.
        button_row = QWidget()
        # Without this the parent layout ignores heightForWidth and the wrapped
        # second row gets clipped.
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        policy.setHeightForWidth(True)
        button_row.setSizePolicy(policy)
        buttons = FlowLayout(button_row, h_spacing=11, v_spacing=10)
        info.addSpacing(20)
        info.addWidget(button_row)

        ratio = self.devicePixelRatioF()
        self._play = QPushButton("Play")
        self._play.setObjectName("Primary")
        self._play.setIcon(QIcon(icon_pixmap("play", 20, C.PLAY_FG, ratio)))
        self._play.setIconSize(QSize(20, 20))
        self._play.setFixedHeight(52)
        # Half the height at most: past half, Qt draws the corners square.
        self._play.setStyleSheet("padding: 0 26px 0 20px; border-radius: 25px; font-size: 12.5pt;")
        self._play.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play.clicked.connect(self._on_play)
        buttons.addWidget(self._play)

        self._restart = self._pill("Start over", "refresh")
        self._restart.clicked.connect(lambda: self.play_requested.emit(self._item, 0.0))
        buttons.addWidget(self._restart)

        self._party = self._pill("Start movie night", "people")
        self._party.setToolTip("Watch this with friends who have Mistery, in sync. Your own "
                               "place in it stays where it is.")
        self._party.clicked.connect(lambda: self.movie_night_requested.emit(self._item))
        buttons.addWidget(self._party)

        self._vr = self._pill("Play in VR", "vr")
        self._vr.clicked.connect(lambda: self.play_in_vr_requested.emit(self._item))
        buttons.addWidget(self._vr)

        self._watched = self._pill("", "check")
        self._watched.clicked.connect(self._toggle_watched)
        buttons.addWidget(self._watched)

        self._folder = self._pill("Open folder", "folder")
        self._folder.clicked.connect(self._open_folder)
        buttons.addWidget(self._folder)

        content_layout.addWidget(self._backdrop)

        # --- body ---------------------------------------------------------
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(52, 26, 52, 52)
        body_layout.setSpacing(26)
        content_layout.addWidget(body)

        self._overview = QLabel()
        self._overview.setWordWrap(True)
        self._overview.setMaximumWidth(820)
        self._overview.setStyleSheet("color: #E8E0D4; font-size: 12pt;")
        body_layout.addWidget(self._overview)

        details_title = QLabel("File details")
        details_title.setObjectName("SectionTitle")
        body_layout.addWidget(details_title)

        details_card = QWidget()
        details_card.setObjectName("Card")
        details_card.setStyleSheet("#Card { background: #161311; border: 1px solid #241F1B; border-radius: 22px; }")
        self._details = QGridLayout(details_card)
        self._details.setContentsMargins(26, 22, 26, 22)
        self._details.setHorizontalSpacing(34)
        self._details.setVerticalSpacing(11)
        self._details.setColumnStretch(1, 1)
        body_layout.addWidget(details_card)

        self._art_note = QLabel()
        self._art_note.setObjectName("Faint")
        self._art_note.setWordWrap(True)
        body_layout.addWidget(self._art_note)
        body_layout.addStretch(1)

    def _pill(self, text: str, icon: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("Ghost")
        button.setIcon(QIcon(icon_pixmap(icon, 19, C.TEXT, self.devicePixelRatioF())))
        button.setIconSize(QSize(19, 19))
        button.setFixedHeight(52)
        button.setStyleSheet("padding: 0 22px 0 18px; border-radius: 25px; font-size: 11.5pt;")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    # --- population ---------------------------------------------------------

    def set_media(self, item: MediaItem) -> None:
        fresh = db.get_media(item.id)
        self._item = MediaItem.from_row(fresh) if fresh else item
        item = self._item

        self._backdrop.set_art(item.wide_art)
        self._poster.set_art(item.art, item.title)

        if item.is_episode:
            show = db.get_show(item.show_id) if item.show_id else None
            eyebrow = (show["title"] if show else "").upper()
            words = f"{eyebrow}   ·   {item.code}" if eyebrow else item.code
        else:
            words = "FILM"
        self._eyebrow.setText(
            f'<span style="color: {C.ACCENT}; font-size: 9.5pt; font-weight: 700; letter-spacing: 1.4px;">'
            f"{words}</span>" if words else "")
        self._eyebrow.setVisible(bool(words))

        # The biggest size the title fits on one line at, down to a floor.
        title = item.title or Path(item.path).stem
        size = _TITLE_SIZES[-1]
        for size in _TITLE_SIZES:
            if QFontMetrics(QFont(display_family(), size, QFont.Weight.Bold)).horizontalAdvance(title) <= _TITLE_W:
                break
        self._title.setStyleSheet(
            f'color: {C.TEXT}; font-family: "{display_family()}"; font-size: {size}pt; font-weight: 700;')
        self._title.setText(title)

        self._tagline.setText(item.tagline or "")
        self._tagline.setVisible(bool(item.tagline))

        meta_bits = []
        if item.year:
            meta_bits.append(str(item.year))
        if item.duration:
            meta_bits.append(fmt_duration(item.duration))
        if item.rating:
            meta_bits.append(f"★ {item.rating:.1f}")
        if item.edition:
            meta_bits.append(item.edition)
        self._meta.setText("   ·   ".join(meta_bits))

        # Genres used to sit in the line above as plain text. Episodes never
        # have any — the series owns them — so the editor is only for films.
        self._categories.setVisible(not item.is_episode)
        if not item.is_episode:
            self._categories.set_row(
                fresh["genres"] if fresh else item.genres,
                fresh["user_genres"] if fresh else None,
            )

        while self._badges.count():
            widget = self._badges.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        for index, text in enumerate(item.badges):
            label = QLabel(text)
            label.setObjectName("BadgeAccent" if index < 2 else "Badge")
            self._badges.addWidget(label)
        self._badges.addStretch(1)

        resume = item.resume_position
        if resume > 0:
            self._play.setText(f"Resume · {fmt_clock(resume)}")
            self._restart.setVisible(True)
        else:
            self._play.setText("Play")
            self._restart.setVisible(False)
        started = resume > 0 and bool(item.duration)
        self._progress_row.setVisible(started)
        if started:
            self._progress.set_fraction(item.position / item.duration)
            self._left.setText(fmt_remaining(item.position, item.duration))

        self._watched.setText("Mark unwatched" if item.watched else "Mark watched")

        target = vr.preferred_target()
        if target is None:
            self._vr.setToolTip("No VR player found — click to see the options for your headset")
        elif target.kind == vr.MIRROR:
            self._vr.setToolTip(f"{target.name} mirrors your desktop — click for details")
        else:
            self._vr.setToolTip(f"Open this file in {target.name}")

        self._overview.setText(item.overview or "")
        self._overview.setVisible(bool(item.overview))

        self._fill_details(item)

        if item.meta_source == "ffmpeg":
            self._art_note.setText(
                "Artwork was generated from the video itself. Add a TMDB API key in "
                "Settings to pull real posters, plots and cast data."
            )
        else:
            self._art_note.setText("")
        self._art_note.setVisible(bool(self._art_note.text()))

    def _fill_details(self, item: MediaItem) -> None:
        while self._details.count():
            widget = self._details.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()

        rows = []
        if item.width and item.height:
            rows.append(("Resolution", f"{item.width} × {item.height}  ({item.resolution_label})"))
        if item.video_codec:
            video = item.video_codec.upper()
            if item.bit_depth:
                video += f" · {item.bit_depth}-bit"
            if item.hdr:
                video += f" · {item.hdr}"
            if item.fps:
                video += f" · {item.fps:g} fps"
            rows.append(("Video", video))
        if item.audio_codec:
            audio = item.audio_codec.upper()
            if item.audio_channels:
                audio += f" · {item.audio_channels} channels"
            rows.append(("Audio", audio))
        rows.append(("Subtitles", f"{item.sub_count} embedded" if item.sub_count else "None embedded"))
        if item.chapters:
            rows.append(("Chapters", str(item.chapters)))
        if item.duration:
            rows.append(("Runtime", fmt_duration(item.duration)))
        if item.size:
            rows.append(("Size", fmt_size(item.size)))
        if item.position and not item.watched:
            rows.append(("Last position", fmt_clock(item.position)))
        rows.append(("File", Path(item.path).name))
        rows.append(("Folder", str(Path(item.path).parent)))

        for row, (label_text, value_text) in enumerate(rows):
            label = QLabel(label_text)
            label.setStyleSheet(f"color: {C.TEXT_FAINT}; font-size: 9.5pt;")
            value = QLabel(value_text)
            value.setStyleSheet(f"color: {C.TEXT}; font-size: 9.5pt;")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._details.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)
            self._details.addWidget(value, row, 1)

    # --- actions ------------------------------------------------------------

    def _on_play(self) -> None:
        self.play_requested.emit(self._item, self._item.resume_position)

    def _on_categories_changed(self, names: list) -> None:
        """Written to user_genres, which the metadata pass never touches."""
        if self._item.id:
            db.update_media(self._item.id, user_genres=cat.join(names))

    def _toggle_watched(self) -> None:
        db.set_watched(self._item.id, not self._item.watched)
        self.set_media(self._item)
        self.media_changed.emit()

    def _open_folder(self) -> None:
        reveal_in_explorer(self._item.path)
