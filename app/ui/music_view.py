"""Music: the library page — Albums, Artists, Songs, Liked songs and Playlists."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QStackedWidget, QVBoxLayout, QWidget,
)

from .. import db
from ..music import library
from ..util import fmt_duration
from .album_view import PlaylistView
from .theme import C
from .widgets.flow import FlowLayout
from .widgets.icons import icon_pixmap
from .widgets.music_cards import (
    AlbumCard, ArtistCard, album_tile, artist_tile, play_tile, playlist_tile,
)
from .widgets.playlist_menu import ask_name, confirm_delete, new_playlist_with
from .widgets.tracklist import TrackList, track_menu

# The two must stay the same length and in the same order as the chips built
# from _TAB_LABELS: current_tab indexes one by the button id, show_tab the other
# by name.
TABS = ("albums", "artists", "songs", "liked", "playlists")
_TAB_LABELS = ("Albums", "Artists", "Songs", "Liked", "Playlists")

LIKED_CONTEXT = {"kind": "liked", "title": "Liked Songs", "id": None}
SONGS_CONTEXT = {"kind": "songs", "title": "All songs", "id": None}


def liked_context(text: str = "") -> dict:
    """"Playing from" for Liked Songs. Played while the search box narrowed the
    list, it says so, and keeps the search as its id so opening it brings the
    same few songs back rather than every liked song."""
    if text:
        return {"kind": "liked", "title": f"Liked Songs matching “{text}”", "id": text}
    return dict(LIKED_CONTEXT)


def search_context(text: str) -> dict:
    """"Playing from" for search results. The id is the search itself, so the
    results can be brought back the way an album is by its id."""
    return {"kind": "search", "title": f"Search: {text}", "id": text}


def _scroll(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(inner)
    return area


class MusicView(QWidget):
    album_opened = Signal(int)
    artist_opened = Signal(str)
    # A line for the top bar — "Added to Road trip." Adding a song to a playlist
    # from Songs or Liked changes nothing you can see on this page, so without
    # this the click looked like it had missed.
    status = Signal(str)

    def __init__(self, player, parent=None) -> None:
        super().__init__(parent)
        self._player = player
        self._open_playlist: int | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        # Two levels in one page: the tabs, and one playlist opened from them.
        # The playlist page paints a tint from edge to edge, so the page margins
        # live on the browse side rather than out here — nested inside them the
        # band was inset by 52 px and read as a floating card.
        self._level = QStackedWidget()
        outer.addWidget(self._level)

        self._browse = QWidget()
        self._level.addWidget(self._browse)
        root = QVBoxLayout(self._browse)
        root.setContentsMargins(52, 30, 52, 0)
        root.setSpacing(16)

        top = QHBoxLayout()
        title = QLabel("Music")
        title.setObjectName("PageTitle")
        top.addWidget(title)
        top.addStretch(1)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search songs, artists, albums…")
        self._search.setFixedWidth(340)
        self._search.textChanged.connect(self.reload)
        top.addWidget(self._search)
        root.addLayout(top)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._tabs = QButtonGroup(self)
        self._tabs.setExclusive(True)
        for index, label in enumerate(_TAB_LABELS):
            chip = QPushButton(label)
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.setChecked(index == 0)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            self._tabs.addButton(chip, index)
            bar.addWidget(chip)
        self._tabs.idClicked.connect(self._on_tab)
        bar.addSpacing(14)
        self._sort = QComboBox()
        self._sort.addItem("By artist", "artist")
        self._sort.addItem("By title", "title")
        self._sort.addItem("Recently added", "recent")
        self._sort.addItem("Recently played", "played")
        self._sort.currentIndexChanged.connect(self.reload)
        bar.addWidget(self._sort)
        bar.addStretch(1)
        # Downloads are a first-class state here, not an error: say so.
        self._downloading = QLabel()
        self._downloading.setObjectName("Faint")
        bar.addWidget(self._downloading)
        root.addLayout(bar)

        self._pages = QStackedWidget()
        root.addWidget(self._pages, 1)

        albums_inner = QWidget()
        albums_layout = QVBoxLayout(albums_inner)
        albums_layout.setContentsMargins(0, 6, 0, 40)
        albums_layout.setSpacing(0)
        albums_holder = QWidget()
        self._album_flow = FlowLayout(albums_holder, margin=0, h_spacing=14, v_spacing=18)
        albums_layout.addWidget(albums_holder)
        albums_layout.addStretch(1)
        self._albums_page = _scroll(albums_inner)
        self._pages.addWidget(self._albums_page)

        artists_inner = QWidget()
        artists_layout = QVBoxLayout(artists_inner)
        artists_layout.setContentsMargins(0, 6, 0, 40)
        artists_holder = QWidget()
        self._artist_flow = FlowLayout(artists_holder, margin=0, h_spacing=14, v_spacing=18)
        artists_layout.addWidget(artists_holder)
        artists_layout.addStretch(1)
        self._artists_page = _scroll(artists_inner)
        self._pages.addWidget(self._artists_page)

        self._songs = TrackList(["number", "title", "artist", "album", "like", "time"],
                                numbering="index")
        self._songs.play_requested.connect(self._play_song)
        self._connect_list(self._songs)
        self._pages.addWidget(self._songs)

        self._liked_page = self._build_liked()
        self._pages.addWidget(self._liked_page)

        self._playlists_tab = self._build_playlists()
        self._pages.addWidget(self._playlists_tab)

        self._empty = QLabel(
            "No music yet.\n\nAlbums in your library folders appear here. Anything still "
            "downloading shows up the moment it finishes — no rescan needed."
        )
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 11pt;")
        self._pages.addWidget(self._empty)

        # The playlist a tile opens, over the whole page. It keeps itself in
        # step with the player (it is a _MusicPage), so nothing here does.
        self._playlist_page = PlaylistView(player)
        self._playlist_page.back_requested.connect(self._close_playlist)
        self._playlist_page.status.connect(self.status)
        self._level.addWidget(self._playlist_page)

        player.track_changed.connect(lambda _t: self._sync_current())
        player.state_changed.connect(self._sync_current)
        player.track_updated.connect(self._on_track_updated)

    def _build_playlists(self) -> QWidget:
        """The Playlists tab: album-shaped tiles, and one button to start a list."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(0)

        header = QHBoxLayout()
        new = QPushButton("New playlist")
        new.setObjectName("Ghost")
        new.setCursor(Qt.CursorShape.PointingHandCursor)
        new.clicked.connect(self._new_playlist)
        header.addWidget(new)
        header.addSpacing(8)
        self._playlists_meta = QLabel()
        self._playlists_meta.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10pt;")
        header.addWidget(self._playlists_meta)
        header.addStretch(1)
        layout.addLayout(header)
        layout.addSpacing(14)

        self._playlists_stack = QStackedWidget()
        holder = QWidget()
        self._playlist_flow = FlowLayout(holder, margin=0, h_spacing=14, v_spacing=18)
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 40)
        inner_layout.addWidget(holder)
        inner_layout.addStretch(1)
        self._playlists_stack.addWidget(_scroll(inner))

        # Not "in Up next": the queue's rows have a menu of their own (Play now,
        # Remove from queue) and never build track_menu. Save as playlist, over
        # the whole queue, is what that page offers instead — so it is named.
        empty = QLabel(
            "No playlists yet.\n\nRight-click any song — on an album, in Songs or in "
            "Liked — and choose Add to playlist › New playlist.\n\n"
            "Or keep what you are listening to: Up next › Save as playlist."
        )
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setWordWrap(True)
        empty.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 11pt;")
        self._playlists_stack.addWidget(empty)
        self._playlists_empty = empty
        layout.addWidget(self._playlists_stack, 1)
        return page

    def _build_liked(self) -> QWidget:
        """Liked songs: Play and Shuffle over the list, newest like first."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.setSpacing(11)
        ratio = self.devicePixelRatioF()
        self._liked_play = QPushButton("Play")
        self._liked_play.setObjectName("Primary")
        self._liked_play.setIcon(QIcon(icon_pixmap("play", 20, C.PLAY_FG, ratio)))
        self._liked_play.setIconSize(QSize(20, 20))
        self._liked_play.setCursor(Qt.CursorShape.PointingHandCursor)
        self._liked_play.clicked.connect(lambda: self._play_liked(0))
        header.addWidget(self._liked_play)
        self._liked_shuffle = QPushButton("Shuffle")
        self._liked_shuffle.setObjectName("Ghost")
        self._liked_shuffle.setIcon(QIcon(icon_pixmap("shuffle", 19, C.TEXT, ratio)))
        self._liked_shuffle.setIconSize(QSize(19, 19))
        self._liked_shuffle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._liked_shuffle.clicked.connect(self._shuffle_liked)
        header.addWidget(self._liked_shuffle)
        header.addSpacing(8)
        self._liked_meta = QLabel()
        self._liked_meta.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10pt;")
        header.addWidget(self._liked_meta)
        header.addStretch(1)
        layout.addLayout(header)
        layout.addSpacing(14)

        self._liked_stack = QStackedWidget()
        self._liked = TrackList(["number", "title", "artist", "album", "like", "time"],
                                numbering="index")
        self._liked.play_requested.connect(self._play_liked)
        self._connect_list(self._liked)
        self._liked_stack.addWidget(self._liked)

        # Nothing liked yet: say where the hearts are, rather than show a blank.
        empty = QWidget()
        box = QVBoxLayout(empty)
        box.setSpacing(10)
        box.addStretch(1)
        glyph = QLabel()
        glyph.setPixmap(icon_pixmap("heart", 52, C.TEXT_FAINT, ratio))
        box.addWidget(glyph, alignment=Qt.AlignmentFlag.AlignHCenter)
        box.addSpacing(6)
        self._liked_empty_title = QLabel()
        self._liked_empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._liked_empty_title.setStyleSheet(f"color: {C.TEXT}; font-size: 14pt; font-weight: 700;")
        box.addWidget(self._liked_empty_title)
        self._liked_empty_text = QLabel()
        self._liked_empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._liked_empty_text.setWordWrap(True)
        self._liked_empty_text.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10.5pt;")
        box.addWidget(self._liked_empty_text)
        box.addStretch(2)
        self._liked_empty = empty
        self._liked_stack.addWidget(empty)
        layout.addWidget(self._liked_stack, 1)
        return page

    def _connect_list(self, tracks: TrackList) -> None:
        tracks.context_requested.connect(
            lambda track, pos: track_menu(self, track, self._player,
                                          on_playlist_change=self._on_added).exec(pos))
        tracks.like_requested.connect(self._player.set_liked)

    def _on_added(self, message: str) -> None:
        """A song went into (or came out of) a playlist from one of these lists.

        The Playlists tab behind is stale now — db.data_version does not report
        this process's own writes — but it is not the tab you are on, and _on_tab
        reloads it when you get there. So: say so, and leave the tiles.
        """
        self.status.emit(message)

    # --- data ---------------------------------------------------------------

    @property
    def current_tab(self) -> str:
        return TABS[max(0, self._tabs.checkedId())]

    def show_tab(self, name: str, search: str | None = None) -> None:
        """Point the page at a tab, and a search, before it is shown.

        For "Playing from": the caller shows the page, which reloads it, so
        this only moves the chips and the search box, quietly.
        """
        index = TABS.index(name) if name in TABS else 0
        self._tabs.button(index).setChecked(True)
        # Asking for a tab is asking for the tabs: "Playing from Liked Songs"
        # clicked while a playlist page was open used to move the chip behind it
        # and leave the playlist on screen.
        self._open_playlist = None
        self._level.setCurrentWidget(self._browse)
        if search is not None and search != self._search.text():
            self._search.blockSignals(True)
            self._search.setText(search)
            self._search.blockSignals(False)

    def show_playlist(self, playlist_id: int) -> None:
        """Open one music playlist over the whole page.

        Called before the page is shown, so the window's _go reloads it into
        place — that is what "Playing from PLAYLIST" does.
        """
        self._open_playlist = int(playlist_id)
        self._tabs.button(TABS.index("playlists")).setChecked(True)
        self._playlist_page.set_playlist(self._open_playlist)
        # set_playlist reloads, and a playlist that has gone since asks to go
        # back from inside that reload — which has already put the tabs on
        # screen and cleared _open_playlist. Showing the page anyway left a
        # working page for a list that no longer exists: right title, right
        # rows, Play enabled, and nothing afterwards to take it down.
        if self._open_playlist is None or self._playlist_page.playlist_id is None:
            return
        self._level.setCurrentWidget(self._playlist_page)

    @property
    def open_playlist(self) -> int | None:
        return self._open_playlist

    def _close_playlist(self) -> None:
        # The reload is the point: the list may have been renamed, reordered,
        # emptied or deleted from the page above, and nothing else will tell the
        # tiles — db.data_version deliberately does not report this process's
        # own writes.
        self._open_playlist = None
        self._level.setCurrentWidget(self._browse)
        self.reload()

    def reload(self) -> None:
        if self._open_playlist is not None:
            self._playlist_page.reload()
            return
        text = self._search.text().strip().lower()
        tab = self._tabs.checkedId()
        self._sort.setVisible(tab == 0)

        downloading = library.incomplete_count()
        self._downloading.setText(
            f"{downloading} track{'s' if downloading != 1 else ''} still downloading — "
            "they'll appear as they finish" if downloading else "")

        if not library.has_music() and not downloading:
            self._pages.setCurrentWidget(self._empty)
            return

        if tab == 0:
            rows = library.albums(self._sort.currentData())
            if text:
                rows = [r for r in rows if text in (r["title"] or "").lower()
                        or text in (r["artist"] or "").lower()]
            self._fill(self._album_flow, [album_tile(r) for r in rows], AlbumCard)
            self._pages.setCurrentWidget(self._albums_page)
        elif tab == 1:
            rows = library.artists()
            if text:
                rows = [r for r in rows if text in (r["name"] or "").lower()]
            self._fill(self._artist_flow, [artist_tile(r) for r in rows], ArtistCard)
            self._pages.setCurrentWidget(self._artists_page)
        elif tab == 2:
            rows = library.search(text) if text else library.tracks()
            self._songs.set_tracks(rows)
            self._pages.setCurrentWidget(self._songs)
            self._sync_current()
        elif tab == 4:
            self._reload_playlists()
            self._pages.setCurrentWidget(self._playlists_tab)
        else:
            self._reload_liked()
            self._pages.setCurrentWidget(self._liked_page)

    def _reload_playlists(self) -> None:
        text = self._search.text().strip().lower()
        rows = [dict(r) for r in db.playlists("music")]
        total = len(rows)
        if text:
            rows = [r for r in rows if text in (r.get("name") or "").lower()]
        covers = library.playlist_covers(int(r["id"]) for r in rows)
        self._fill(self._playlist_flow,
                   [playlist_tile(r, covers.get(int(r["id"]))) for r in rows], AlbumCard)
        songs = sum(int(r.get("item_count") or 0) for r in rows)
        self._playlists_meta.setText(
            f"{len(rows)} of {total} playlists" if text and rows else
            f"{len(rows)} playlist{'s' if len(rows) != 1 else ''}  ·  "
            f"{songs} song{'s' if songs != 1 else ''}" if rows else "")
        if rows:
            self._playlists_stack.setCurrentIndex(0)
        else:
            self._playlists_empty.setText(
                f"No playlists match “{self._search.text().strip()}”" if total else
                "No playlists yet.\n\nRight-click any song — on an album, in Songs or in "
                "Liked — and choose Add to playlist › New playlist.\n\n"
                "Or keep what you are listening to: Up next › Save as playlist.")
            self._playlists_stack.setCurrentWidget(self._playlists_empty)

    def _new_playlist(self) -> None:
        playlist_id = new_playlist_with(self, "music", [])
        if playlist_id is not None:
            self.show_playlist(playlist_id)

    def _reload_liked(self, keep_scroll: bool = False) -> None:
        text = self._search.text().strip()
        needle = text.lower()
        rows = [dict(r) for r in library.liked_tracks()]
        total = len(rows)
        if needle:
            rows = [r for r in rows if any(needle in (r.get(key) or "").lower()
                                           for key in ("title", "artist", "album_title"))]
        scroll = self._liked.verticalScrollBar().value()
        self._liked.set_tracks(rows)
        if keep_scroll:
            # A heart clicked halfway down the list reloads it; the list stays
            # where it was rather than jumping back to the top.
            self._liked.verticalScrollBar().setValue(scroll)

        seconds = sum(float(r.get("duration") or 0) for r in rows)
        if rows and needle:
            self._liked_meta.setText(f"{len(rows)} of {total} liked songs")
        elif rows:
            count = f"{len(rows)} song{'s' if len(rows) != 1 else ''}"
            length = fmt_duration(seconds)
            self._liked_meta.setText(f"{count}, {length}" if length else count)
        else:
            self._liked_meta.setText("")
        # Hidden rather than disabled with nothing to play: the white Play plate
        # has no disabled look, and over the empty state it read as a button
        # that did nothing.
        self._liked_play.setVisible(bool(rows))
        self._liked_shuffle.setVisible(bool(rows))
        self._liked_shuffle.setEnabled(len(rows) > 1)

        if rows:
            self._liked_stack.setCurrentWidget(self._liked)
        elif total:
            self._liked_empty_title.setText(f"No liked songs match “{text}”")
            self._liked_empty_text.setText("")
            self._liked_stack.setCurrentWidget(self._liked_empty)
        else:
            self._liked_empty_title.setText("Songs you like appear here")
            self._liked_empty_text.setText(
                "Tap the heart next to a song — on an album, in Songs, or on Now Playing — "
                "and it's kept here, newest first.")
            self._liked_stack.setCurrentWidget(self._liked_empty)
        self._sync_current()

    def _fill(self, flow: FlowLayout, tiles: list, card_type) -> None:
        flow.clear()
        for tile in tiles:
            card = card_type(tile)
            card.clicked.connect(self._open)
            card.play_requested.connect(lambda item: self._act("play", item))
            card.action_requested.connect(self._act)
            flow.addWidget(card)

    def _on_tab(self, _index: int) -> None:
        self.reload()

    def _sync_current(self) -> None:
        current = self._player.current
        current_id = current["id"] if current else None
        self._songs.set_current(current_id, self._player.is_playing)
        self._liked.set_current(current_id, self._player.is_playing)

    def _on_track_updated(self, track) -> None:
        """A song was liked or unliked, here or anywhere else."""
        self._songs.update_track(track)
        if self._pages.currentWidget() is self._liked_page and self.isVisible():
            # On show, the list is the answer to "what have I liked", so it
            # changes at once: an unliked song leaves, a newly liked one arrives.
            self._reload_liked(keep_scroll=True)
        else:
            self._liked.update_track(track)     # the whole list is reloaded when next shown

    # --- actions ----------------------------------------------------------------

    def _open(self, tile) -> None:
        if tile.kind == "album":
            self.album_opened.emit(int(tile.key))
        elif tile.kind == "playlist":
            self.show_playlist(int(tile.key))
        else:
            self.artist_opened.emit(str(tile.key))

    def _act(self, action: str, tile) -> None:
        if action == "open":
            self._open(tile)
            return
        if action == "rename":
            name = ask_name(self, "Rename playlist", "Name", tile.title)
            if name is not None:
                db.rename_playlist(int(tile.key), name)
                self.reload()
            return
        if action == "delete":
            if confirm_delete(self, tile.title):
                db.delete_playlist(int(tile.key))
                self.reload()
            return
        play_tile(self._player, action, tile)

    def _play_song(self, row: int) -> None:
        tracks = self._songs.tracks
        text = self._search.text().strip()
        # From the Songs list, "in order" means the list as you see it.
        self._player.play_tracks(tracks, row, in_order=False,
                                 context=search_context(text) if text else dict(SONGS_CONTEXT))

    def _play_liked(self, row: int) -> None:
        tracks = self._liked.tracks
        if tracks:
            # A mix of records, like Songs: each song levelled on its own.
            self._player.play_tracks(tracks, row, in_order=False,
                                     context=liked_context(self._search.text().strip()))

    def _shuffle_liked(self) -> None:
        tracks = self._liked.tracks
        if tracks:
            self._player.shuffle_tracks(tracks, context=liked_context(self._search.text().strip()))
