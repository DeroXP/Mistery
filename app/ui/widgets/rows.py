"""Section containers: a horizontally scrolling row and a wrapping grid."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from ..theme import C
from .cards import make_card
from .flow import FlowLayout
from .icons import IconButton


class _SectionBase(QWidget):
    item_clicked = Signal(object)
    item_play_requested = Signal(object)
    item_action = Signal(str, object)

    def __init__(self, title: str, hint: str = "", parent=None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(14)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(10)

        self._title = QLabel(title)
        self._title.setObjectName("SectionTitle")
        header.addWidget(self._title)

        # How many, as a small pill after the title.
        self._count = QLabel()
        self._count.setObjectName("SectionCount")
        self._count.setVisible(False)
        header.addWidget(self._count, 0, Qt.AlignmentFlag.AlignVCenter)

        self._hint = QLabel(hint)
        self._hint.setObjectName("SectionHint")
        self._hint.setVisible(bool(hint))
        header.addWidget(self._hint)
        header.addStretch(1)

        # A faint note on the right ("Hover one for a moment"), and a link
        # ("See all"): both hidden until asked for.
        self._aside = QLabel()
        self._aside.setObjectName("SectionAside")
        self._aside.setVisible(False)
        header.addWidget(self._aside)
        self.link = QPushButton()
        self.link.setObjectName("SectionLink")
        self.link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.link.setVisible(False)
        header.addWidget(self.link)
        self._header = header
        self._layout.addLayout(header)

    def set_title(self, text: str) -> None:
        self._title.setText(text)

    def set_hint(self, text: str) -> None:
        self._hint.setText(text)
        self._hint.setVisible(bool(text))

    def set_count(self, count: int | None) -> None:
        self._count.setText(str(count) if count else "")
        self._count.setVisible(bool(count))

    def set_aside(self, text: str) -> None:
        self._aside.setText(text)
        self._aside.setVisible(bool(text))

    def set_link(self, text: str) -> None:
        self.link.setText(text)
        self.link.setVisible(bool(text))

    preview_host = None         # Home's hover preview, for the cards made from now on

    def set_preview_host(self, host) -> None:
        """Hand every card's hover to `host` (widgets/hover_preview.py), and
        switch its dwell on: Home's rows and grids play in the host's card."""
        self.preview_host = host
        for card in self.findChildren(QWidget):
            if hasattr(card, "preview_host"):
                card.preview_host = host
                card.set_preview_enabled(host is not None)

    def _wire(self, card) -> None:
        card.clicked.connect(self.item_clicked.emit)
        card.play_requested.connect(self.item_play_requested.emit)
        card.action_requested.connect(self.item_action.emit)
        if self.preview_host is not None:
            card.preview_host = self.preview_host
            card.set_preview_enabled(True)


class CardRow(_SectionBase):
    """A single horizontal strip of cards with arrow buttons."""

    def __init__(self, title: str, hint: str = "", wide: bool = True,
                 preview: bool = False, parent=None) -> None:
        super().__init__(title, hint, parent)
        self._wide = wide
        self._preview = preview

        self._left = IconButton("chevron_left", size=30, icon_size=17, tooltip="Scroll left")
        self._right = IconButton("chevron_right", size=30, icon_size=17, tooltip="Scroll right")
        self._left.clicked.connect(lambda: self._scroll(-1))
        self._right.clicked.connect(lambda: self._scroll(1))
        self._header.addWidget(self._left)
        self._header.addWidget(self._right)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._strip = QWidget()
        self._strip_layout = QHBoxLayout(self._strip)
        self._strip_layout.setContentsMargins(2, 2, 2, 12)
        self._strip_layout.setSpacing(16)
        self._strip_layout.addStretch(1)
        self._scroll_area.setWidget(self._strip)
        self._layout.addWidget(self._scroll_area)

    def _scroll(self, direction: int) -> None:
        bar = self._scroll_area.horizontalScrollBar()
        bar.setValue(bar.value() + direction * max(320, self._scroll_area.width() - 160))

    def set_items(self, items: list) -> None:
        while self._strip_layout.count() > 1:
            widget = self._strip_layout.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        for item in items:
            card = make_card(item, wide=self._wide)
            card.set_preview_enabled(self._preview)
            self._wire(card)
            self._strip_layout.insertWidget(self._strip_layout.count() - 1, card)

        has_items = bool(items)
        self.setVisible(has_items)
        if has_items:
            sample = self._strip_layout.itemAt(0).widget()
            self._scroll_area.setFixedHeight(sample.height() + 18)
        arrows = len(items) > 3
        self._left.setVisible(arrows)
        self._right.setVisible(arrows)


class CardGrid(_SectionBase):
    """A wrapping grid of poster cards."""

    def __init__(self, title: str = "", hint: str = "", wide: bool = False, parent=None) -> None:
        super().__init__(title, hint, parent)
        self._wide = wide
        self._title.setVisible(bool(title))

        container = QWidget()
        self._flow = FlowLayout(container, margin=2, h_spacing=18, v_spacing=22)
        container.setLayout(self._flow)
        self._layout.addWidget(container)

        self._empty = QLabel()
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet(f"color: {C.TEXT_FAINT}; padding: 40px; font-size: 10.5pt;")
        self._empty.setVisible(False)
        self._layout.addWidget(self._empty)

    def set_items(self, items: list, empty_text: str = "Nothing here yet") -> None:
        self._flow.clear()
        for item in items:
            card = make_card(item, wide=self._wide)
            self._wire(card)
            self._flow.addWidget(card)
        self._empty.setText(empty_text)
        self._empty.setVisible(not items)
