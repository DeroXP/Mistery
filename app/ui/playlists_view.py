"""The Playlists page: films and episodes gathered into lists.

Two levels in one page, because the window gives it one nav entry and one stack
slot: the index of every video playlist, and one playlist's items. `show_playlist`
picks which, and the window records that choice in its history (_page_state), so
coming back from a film's details lands on the playlist you were in rather than
the index.

Music playlists are not here. They live on the Music page next to Albums and
Liked, where the tiles and the track list already are.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget,
)

from .. import db
from ..models import MediaItem
from ..util import fmt_duration
from .theme import C
from .widgets.cards import WideCard, card_flow, set_extra_actions
from .widgets.empty import EmptyState
from .widgets.flow import FlowLayout
from .widgets.icons import IconButton
from .widgets.playlist_menu import (
    ask_name, confirm_delete, display_name, move_entry, new_playlist_with,
)
from .widgets.rows import CardGrid

# What a playlist page adds to every card's menu. The card emits the string and
# this page does the work, so cards still know nothing about the database.
_ROW_ACTIONS = [("Move up", "up"), ("Move down", "down"),
                ("Remove from this playlist", "unlist")]


class _PlaylistTile:
    """What a WideCard needs to draw a playlist: a still, a name, a size.

    The still is the first item's, which is free and goes slightly stale when
    you reorder — a mosaic of four would look better and is the obvious follow-up.
    """

    def __init__(self, row, first: MediaItem | None, seconds: float, playable: int) -> None:
        self.id = int(row["id"])
        self.title = row["name"] or "Untitled"
        self.path = ""
        self.progress = 0.0
        self.watched = False
        self.position = 0.0
        self.duration = seconds
        self._first = first
        held = int(row["item_count"] or 0)
        bits = [f"{held} item{'s' if held != 1 else ''}"]
        length = fmt_duration(seconds)
        if length:
            bits.append(length)
        if held > playable:
            bits.append(f"{held - playable} unavailable")
        self.subtitle = "  ·  ".join(bits)

    @property
    def display_title(self) -> str:
        return self.title

    @property
    def art(self) -> str | None:
        return self._first.art if self._first else None

    @property
    def wide_art(self) -> str | None:
        return self._first.wide_art if self._first else None


class _PlaylistCard(WideCard):
    """A playlist on the index. Its menu is about the list, not about a film,
    so it does not go through the window's card menu at all."""

    def contextMenuEvent(self, event) -> None:
        item = self._item
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)      # see PosterCard's menu
        menu.addAction("Open", lambda: self.clicked.emit(item))
        menu.addAction("Play all", lambda: self.play_requested.emit(item))
        menu.addSeparator()
        menu.addAction("Rename…", lambda: self.action_requested.emit("rename", item))
        menu.addAction("Delete playlist", lambda: self.action_requested.emit("delete", item))
        menu.exec(event.globalPos())


class PlaylistsView(QWidget):
    play_requested = Signal(object)          # a media row, played now
    open_media = Signal(object)              # a media row, opened in its page
    item_action = Signal(str, object)        # the card menu, as the library pages emit it
    open_playlist = Signal(int)              # a tile was opened; the window routes it

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._playlist_id: int | None = None
        self._items: list[MediaItem] = []
        self._entries: list[int] = []        # entry id per item, same order
        self._name = ""
        self._played = 0                     # which item Play was last pressed on

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._level = QStackedWidget()
        root.addWidget(self._level)
        self._level.addWidget(self._build_index())
        self._level.addWidget(self._build_one())

    # --- the index ----------------------------------------------------------

    def _build_index(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(52, 30, 52, 0)
        layout.setSpacing(16)

        top = QHBoxLayout()
        title = QLabel("Playlists")
        title.setObjectName("PageTitle")
        top.addWidget(title)
        top.addSpacing(14)
        new = QPushButton("New playlist")
        new.setObjectName("Ghost")
        new.setCursor(Qt.CursorShape.PointingHandCursor)
        new.clicked.connect(self._new_playlist)
        top.addWidget(new)
        top.addStretch(1)
        layout.addLayout(top)

        self._index_stack = QStackedWidget()
        holder = QWidget()
        self._flow = FlowLayout(holder, margin=2, h_spacing=18, v_spacing=22)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 40)
        inner_layout.addWidget(holder)
        inner_layout.addStretch(1)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(inner)
        self._index_stack.addWidget(area)

        empty = EmptyState(
            "queue", "No playlists yet",
            "Right-click any film or episode — on Home, in Movies, in Shows or on a series "
            "page — and choose Add to playlist.",
            "New playlist",
        )
        empty.action_clicked.connect(self._new_playlist)
        self._index_empty = empty
        self._index_stack.addWidget(empty)
        layout.addWidget(self._index_stack, 1)
        return page

    # --- one playlist -------------------------------------------------------

    def _build_one(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(52, 22, 52, 0)
        layout.setSpacing(0)

        back = IconButton("back", size=40, icon_size=22, tooltip="All playlists")
        back.clicked.connect(self._show_index)
        layout.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(14)

        eyebrow = QLabel("PLAYLIST")
        eyebrow.setStyleSheet(
            f"color: {C.ACCENT}; font-size: 9pt; font-weight: 800; letter-spacing: 1.5px;")
        layout.addWidget(eyebrow)
        self._title = QLabel()
        self._title.setWordWrap(True)
        self._title.setStyleSheet(f"color: {C.TEXT}; font-size: 30pt; font-weight: 800;")
        layout.addSpacing(4)
        layout.addWidget(self._title)
        self._meta = QLabel()
        self._meta.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10pt;")
        layout.addSpacing(4)
        layout.addWidget(self._meta)

        actions = QHBoxLayout()
        actions.setSpacing(11)
        self._play_all = QPushButton("Play all")
        self._play_all.setObjectName("Primary")
        self._play_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play_all.clicked.connect(lambda: self._play_item(0))
        actions.addWidget(self._play_all)
        for label, slot in (("Rename", self._rename), ("Delete", self._delete)):
            button = QPushButton(label)
            button.setObjectName("Ghost")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(slot)
            actions.addWidget(button)
        actions.addStretch(1)
        layout.addSpacing(18)
        layout.addLayout(actions)
        layout.addSpacing(22)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 40)
        self._grid = CardGrid(wide=True)
        self._grid.item_clicked.connect(self.open_media.emit)
        self._grid.item_play_requested.connect(self._play_card)
        self._grid.item_action.connect(self._on_item_action)
        inner_layout.addWidget(self._grid)
        inner_layout.addStretch(1)
        area.setWidget(inner)
        layout.addWidget(area, 1)
        return page

    # --- what the window calls ----------------------------------------------

    def show_playlist(self, playlist_id: int | None) -> None:
        """Open one list, or None for the page of all of them."""
        self._playlist_id = int(playlist_id) if playlist_id is not None else None

    @property
    def playlist_id(self) -> int | None:
        return self._playlist_id

    @property
    def line_up(self) -> tuple[list[int], int]:
        """The ids to play in order, and where the last Play started.

        The window hands this to the player, which has no queue of its own and
        would otherwise ask the database for "the next episode" and find
        nothing after a film. The index comes from the card that was clicked,
        not from a search by id, so a film listed twice still advances from the
        copy you pressed.
        """
        return [item.id for item in self._items], self._played

    def reload(self) -> None:
        if self._playlist_id is None:
            self._reload_index()
            return
        row = db.playlist(self._playlist_id)
        if row is None:
            # Deleted here or elsewhere: the index is what is left to show.
            self._show_index()
            return
        self._level.setCurrentIndex(1)
        entries = db.playlist_entries(self._playlist_id)
        media = {int(m["id"]): m for m in db.playlist_media(self._playlist_id)}
        self._items, self._entries = [], []
        for entry in entries:
            found = media.get(int(entry["item_id"]))
            if found is not None:
                self._items.append(MediaItem.from_row(found))
                self._entries.append(int(entry["id"]))
        self._played = 0

        self._describe(row)
        self._grid.set_items(self._items, "Nothing in this playlist yet — right-click a film "
                                          "or an episode and choose Add to playlist.")
        set_extra_actions(self._grid, _ROW_ACTIONS)

    def _describe(self, row) -> None:
        """The title and the line under it — everything about the page but the
        cards, so an edit that moves one card can say the rest without a
        rebuild."""
        self._name = row["name"] or "Untitled"
        self._title.setText(display_name(self._name))
        self._title.setToolTip(self._name)
        held = int(row["item_count"] or 0)
        seconds = sum(float(item.duration or 0) for item in self._items)
        bits = [f"{len(self._items)} item{'s' if len(self._items) != 1 else ''}"]
        length = fmt_duration(seconds)
        if length:
            bits.append(length)
        if held > len(self._items):
            # Said rather than hidden: a drive unplugged today is back tomorrow,
            # and the entries are still in the playlist either way.
            bits.append(f"{held - len(self._items)} not available right now")
        self._meta.setText("  ·  ".join(bits))
        self._play_all.setEnabled(bool(self._items))

    def _reload_index(self) -> None:
        self._level.setCurrentIndex(0)
        self._flow.clear()
        rows = db.playlists("video")
        for row in rows:
            items = db.playlist_media(int(row["id"]))
            first = MediaItem.from_row(items[0]) if items else None
            seconds = sum(float(m["duration"] or 0) for m in items)
            card = _PlaylistCard(_PlaylistTile(row, first, seconds, len(items)),
                                 show_remaining=False)
            card.clicked.connect(self._open_tile)
            card.play_requested.connect(self._play_tile)
            card.action_requested.connect(self._on_tile_action)
            self._flow.addWidget(card)
        self._index_stack.setCurrentIndex(0 if rows else 1)

    # --- actions ------------------------------------------------------------

    def _show_index(self) -> None:
        self._playlist_id = None
        self.reload()

    def _open_tile(self, tile) -> None:
        # Through the window, the way an album tile is: open_playlist pushes the
        # history entry, so Back from inside a playlist comes back to the list of
        # them instead of going Home. It calls straight back into show_playlist.
        self.open_playlist.emit(int(tile.id))

    def _play_tile(self, tile) -> None:
        self._open_tile(tile)
        # Only if the window really opened it: a playlist deleted since the page
        # was drawn is refused there, and playing item 0 of the list you were
        # already looking at would be somebody else's playlist.
        if self._playlist_id == int(tile.id):
            self._play_item(0)

    def _on_tile_action(self, action: str, tile) -> None:
        if action == "rename":
            name = ask_name(self, "Rename playlist", "Name", tile.title)
            if name is not None:
                db.rename_playlist(tile.id, name)
                self.reload()
        elif action == "delete" and confirm_delete(self, tile.title):
            db.delete_playlist(tile.id)
            self.reload()

    def _new_playlist(self) -> None:
        playlist_id = new_playlist_with(self, "video", [])
        if playlist_id is not None:
            self.show_playlist(playlist_id)
            self.reload()

    def _rename(self) -> None:
        if self._playlist_id is None:
            return
        name = ask_name(self, "Rename playlist", "Name", self._name)
        if name is not None:
            db.rename_playlist(self._playlist_id, name)
            self.reload()

    def _delete(self) -> None:
        if self._playlist_id is None or not confirm_delete(self, self._name):
            return
        db.delete_playlist(self._playlist_id)
        self._show_index()

    def _play_card(self, item) -> None:
        index = next((i for i, m in enumerate(self._items) if m is item), 0)
        self._play_item(index)

    def _play_item(self, index: int) -> None:
        if not 0 <= index < len(self._items):
            return
        self._played = index
        self.play_requested.emit(self._items[index])

    def _on_item_action(self, action: str, item) -> None:
        """The card menu. The three this page added are handled here; everything
        else is the same menu as on any other page and goes to the window."""
        if action == "play":
            # Through this page, not straight to the window, so Play from the
            # menu carries the rest of the list the way the card's own play
            # circle does.
            self._play_card(item)
            return
        if action in ("up", "down", "unlist") and self._playlist_id is not None:
            index = next((i for i, m in enumerate(self._items) if m is item), None)
            if index is None:
                return
            entry = self._entries[index]
            if action == "unlist":
                db.remove_playlist_entries(self._playlist_id, [entry])
                self._drop_card(index)
            else:
                step = -1 if action == "up" else 1
                if not move_entry(self._playlist_id, entry, step, self._entries):
                    return      # already at the end it was pushed towards
                self._swap_cards(index, index + step)
            return
        self.item_action.emit(action, item)

    # --- editing the grid in place -------------------------------------------
    #
    # Both of these end in reload() when anything does not add up, and reload()
    # is the whole of what they replace, so the worst they can do is be slow.
    # They exist because rebuilding is not cheap on a long list: one Move down
    # on a visible 500-item page blocked the GUI thread for 1.6 s; measured
    # again through this path it is 8 ms, and Remove 14 ms. What costs the 1.6 s
    # is building and first painting 500 new cards — the Movies page pays the
    # same once when it opens — and neither of these needs a single new card.

    def _swap_cards(self, first: int, second: int) -> None:
        flow = card_flow(self._grid)
        if flow is None or flow.count() != len(self._items):
            self.reload()
            return
        self._items[first], self._items[second] = self._items[second], self._items[first]
        self._entries[first], self._entries[second] = (self._entries[second],
                                                       self._entries[first])
        flow.swap(first, second)
        self._played = 0        # as reload() does: Play decides again where it starts

    def _drop_card(self, index: int) -> None:
        flow = card_flow(self._grid)
        # The last one out leaves the grid's empty state behind, which only
        # set_items puts up — and rebuilding nothing is free.
        if flow is None or flow.count() != len(self._items) or len(self._items) <= 1:
            self.reload()
            return
        del self._items[index]
        del self._entries[index]
        flow.remove_at(index)
        self._played = 0
        row = db.playlist(self._playlist_id)
        if row is None:            # deleted under us between the two queries
            self._show_index()
            return
        self._describe(row)
