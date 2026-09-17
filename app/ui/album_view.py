"""Album and artist pages, coloured by the record they show."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QRadialGradient
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from .. import db
from ..music import library
from ..music.tags import quality_label
from ..util import fmt_duration, fmt_size
from .theme import C
from .widgets.artview import ArtView
from .widgets.flow import FlowLayout
from .widgets.icons import IconButton, icon_pixmap
from .widgets.music_cards import AlbumCard, album_tile, play_tile
from .widgets.playlist_menu import ask_name, confirm_delete, display_name, move_entry
from .widgets.tracklist import TrackList, apply_liked, track_menu

_HEADER = 380


class _TintedPage(QWidget):
    """Scroll content that paints the record's colours behind the header."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.palette_colours = library.parse_palette(None)
        self.setAutoFillBackground(False)

    def set_colours(self, colours: dict) -> None:
        self.palette_colours = colours
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        rect = QRectF(self.rect())
        painter.fillRect(rect, QColor(C.BG))
        height = min(rect.height(), _HEADER + 220)
        vertical = QLinearGradient(0, 0, 0, height)
        vertical.setColorAt(0.0, QColor(self.palette_colours["mid"]))
        vertical.setColorAt(0.55, QColor(self.palette_colours["dark"]))
        vertical.setColorAt(1.0, QColor(C.BG))
        painter.fillRect(QRectF(0, 0, rect.width(), height), vertical)

        # A soft glow of the accent from the top corner, like light off the sleeve.
        # Its radius stays inside the tinted band — a glow still visible where
        # the band ends leaves a hard horizontal line across the page.
        glow_colour = QColor(self.palette_colours["accent"])
        glow = QRadialGradient(rect.width() * 0.15, 0, min(rect.width() * 0.55, height * 0.95))
        glow_colour.setAlpha(46)
        glow.setColorAt(0.0, glow_colour)
        glow_colour.setAlpha(0)
        glow.setColorAt(1.0, glow_colour)
        painter.fillRect(QRectF(0, 0, rect.width(), height), glow)


def _pill(text: str, accent: str) -> str:
    colour = QColor(accent)
    return (f"background: rgba({colour.red()},{colour.green()},{colour.blue()},0.16);"
            f"border: 1px solid rgba({colour.red()},{colour.green()},{colour.blue()},0.55);"
            f"border-radius: 4px; padding: 3px 10px; color: {accent};"
            f"font-size: 8.5pt; font-weight: 700;")


class _MusicPage(QWidget):
    """Shared scaffolding: tinted scroll page, back button, play/shuffle."""

    back_requested = Signal()
    # "Added to Road trip." for the top bar. A playlist is added to from a song's
    # menu on every one of these pages, and until this existed the click had
    # nothing to show for itself: the list is not on screen, so the page looks
    # exactly as it did.
    status = Signal(str)

    def __init__(self, player, parent=None) -> None:
        super().__init__(parent)
        self._player = player
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self._scroll)

        self._page = _TintedPage()
        self._scroll.setWidget(self._page)
        self.body = QVBoxLayout(self._page)
        self.body.setContentsMargins(52, 22, 52, 48)
        self.body.setSpacing(0)

        back = IconButton("back", size=40, icon_size=22, tooltip="Back")
        back.clicked.connect(self.back_requested.emit)
        self.body.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeft)
        self.body.addSpacing(18)

        player.track_changed.connect(lambda _t: self._sync_current())
        player.state_changed.connect(self._sync_current)
        player.track_updated.connect(self._on_track_updated)

    def _connect_list(self, tracks: TrackList) -> None:
        """The menu and the heart, the same on every list these pages build."""
        tracks.context_requested.connect(
            lambda track, pos: track_menu(self, track, self._player,
                                          on_playlist_change=self.status.emit).exec(pos))
        tracks.like_requested.connect(self._player.set_liked)

    def _own_rows(self) -> list[dict]:
        """The rows this page queues when Play is pressed."""
        return []

    def _on_track_updated(self, track) -> None:
        # The page's own rows as well as the list's copies: they are what Play
        # queues, and a stale one put a liked song in the queue unliked.
        apply_liked(self._own_rows(), track)
        for tracks in self.findChildren(TrackList):
            tracks.update_track(track)

    def _buttons(self, layout: QHBoxLayout) -> tuple[QPushButton, QPushButton]:
        ratio = self.devicePixelRatioF()
        play = QPushButton("Play")
        play.setObjectName("Primary")
        play.setIcon(QIcon(icon_pixmap("play", 20, C.PLAY_FG, ratio)))
        play.setIconSize(QSize(20, 20))
        play.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(play)
        shuffle = QPushButton("Shuffle")
        shuffle.setObjectName("Ghost")
        shuffle.setIcon(QIcon(icon_pixmap("shuffle", 19, C.TEXT, ratio)))
        shuffle.setIconSize(QSize(19, 19))
        shuffle.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(shuffle)
        layout.addStretch(1)
        return play, shuffle

    def _sync_current(self) -> None:
        current = self._player.current
        for tracks in self.findChildren(TrackList):
            tracks.set_current(current["id"] if current else None, self._player.is_playing)


class AlbumView(_MusicPage):
    artist_requested = Signal(str)

    def __init__(self, player, parent=None) -> None:
        super().__init__(player, parent)
        self._album_id: int | None = None
        self._album_title = ""
        self._rows: list[dict] = []

        header = QHBoxLayout()
        header.setSpacing(30)
        self._cover = ArtView(240, 240, radius=8)
        header.addWidget(self._cover, alignment=Qt.AlignmentFlag.AlignBottom)

        info = QVBoxLayout()
        info.setSpacing(6)
        info.addStretch(1)
        self._eyebrow = QLabel("ALBUM")
        info.addWidget(self._eyebrow)
        self._title = QLabel()
        self._title.setWordWrap(True)
        self._title.setStyleSheet(f"color: {C.TEXT}; font-size: 32pt; font-weight: 800;")
        info.addWidget(self._title)
        self._artist = QPushButton()
        self._artist.setCursor(Qt.CursorShape.PointingHandCursor)
        self._artist.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; padding: 0; text-align: left;"
            f" color: {C.TEXT}; font-size: 13pt; font-weight: 700; }}"
            f"QPushButton:hover {{ text-decoration: underline; }}")
        self._artist.clicked.connect(lambda: self.artist_requested.emit(self._artist.text()))
        info.addWidget(self._artist, alignment=Qt.AlignmentFlag.AlignLeft)
        self._meta = QLabel()
        self._meta.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10pt;")
        info.addWidget(self._meta)

        badges = QHBoxLayout()
        badges.setSpacing(8)
        self._quality = QLabel()
        badges.addWidget(self._quality)
        self._progress = QLabel()
        self._progress.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 9pt;")
        badges.addWidget(self._progress)
        badges.addStretch(1)
        info.addSpacing(4)
        info.addLayout(badges)

        actions = QHBoxLayout()
        actions.setSpacing(11)
        self._play, self._shuffle = self._buttons(actions)
        self._play.clicked.connect(self._play_album)
        self._shuffle.clicked.connect(self._shuffle_album)
        info.addSpacing(14)
        info.addLayout(actions)
        header.addLayout(info, 1)
        self.body.addLayout(header)
        self.body.addSpacing(30)

        self._tracks = TrackList(["number", "title", "like", "time"], auto_height=True)
        self._tracks.play_requested.connect(self._play_from)
        self._connect_list(self._tracks)
        self.body.addWidget(self._tracks)

        self._footer = QLabel()
        self._footer.setStyleSheet(f"color: {C.TEXT_FAINT}; font-size: 9pt;")
        self.body.addSpacing(18)
        self.body.addWidget(self._footer)
        self.body.addStretch(1)

    @property
    def album_id(self) -> int | None:
        return self._album_id

    def set_album(self, album_id: int) -> None:
        self._album_id = int(album_id)
        self.reload()
        self._scroll.verticalScrollBar().setValue(0)

    def reload(self) -> None:
        if self._album_id is None:
            return
        album = library.album(self._album_id)
        if album is None:
            self.back_requested.emit()
            return
        colours = library.parse_palette(album["palette"])
        self._page.set_colours(colours)
        self._tracks.set_accent(colours["accent"])
        self._eyebrow.setStyleSheet(
            f"color: {colours['accent']}; font-size: 9pt; font-weight: 800; letter-spacing: 1.5px;")

        self._cover.set_art(album["cover"], album["title"])
        self._title.setText(album["title"])
        self._album_title = album["title"] or ""
        self._artist.setText(album["artist"])

        self._rows = [dict(r) for r in library.album_tracks(self._album_id)]
        # A compilation shows who performs each song; a normal album doesn't.
        performers = {r.get("artist") for r in self._rows if r.get("artist")}
        columns = (["number", "title", "artist", "like", "time"] if len(performers) > 1
                   else ["number", "title", "like", "time"])
        if columns != self._tracks.model_.columns:
            index = self.body.indexOf(self._tracks)
            self._tracks.setParent(None)
            self._tracks.deleteLater()
            self._tracks = TrackList(columns, auto_height=True)
            self._tracks.set_accent(colours["accent"])
            self._tracks.play_requested.connect(self._play_from)
            self._connect_list(self._tracks)
            self.body.insertWidget(index, self._tracks)
        self._tracks.set_tracks(self._rows)

        ready = int(album["ready_count"] or 0)
        total = int(album["track_count"] or 0)
        bits = [b for b in (album["genre"], str(album["year"]) if album["year"] else "") if b]
        bits.append(f"{ready} song{'s' if ready != 1 else ''}, {fmt_duration(album['duration'] or 0)}")
        self._meta.setText("  ·  ".join(bits))

        label, detail = quality_label(album["codec"], album["sample_rate"], album["bit_depth"],
                                      album["bitrate"])
        self._quality.setText(f"{label}  ·  {detail}" if detail else label)
        self._quality.setStyleSheet(_pill(label, colours["accent"]))
        self._quality.setVisible(bool(album["codec"]))
        self._progress.setText(
            f"{ready} of {total} downloaded — the rest appear as they finish"
            if ready < total else "")
        self._play.setEnabled(ready > 0)
        self._shuffle.setEnabled(ready > 1)

        size = sum(int(r.get("size") or 0) for r in self._rows if r.get("state") == "ready")
        self._footer.setText(
            "  ·  ".join(b for b in (album["title"], str(album["year"] or ""),
                                     fmt_size(size) if size else "", album["codec"] or "") if b))
        self._sync_current()

    def _ready_rows(self) -> list[dict]:
        return [r for r in self._rows if r.get("state") == "ready"]

    def _own_rows(self) -> list[dict]:
        return self._rows

    def _context(self) -> dict:
        return {"kind": "album", "title": self._album_title, "id": self._album_id}

    def _play_album(self) -> None:
        ready = self._ready_rows()
        if ready:
            self._player.play_tracks(ready, 0, in_order=True, context=self._context())

    def _shuffle_album(self) -> None:
        ready = self._ready_rows()
        if ready:
            self._player.shuffle_tracks(ready, context=self._context())

    def _play_from(self, row: int) -> None:
        self._player.play_tracks(self._rows, row, in_order=True, context=self._context())


class ArtistView(_MusicPage):
    album_requested = Signal(int)

    def __init__(self, player, parent=None) -> None:
        super().__init__(player, parent)
        self._name = ""
        self._songs_rows: list[dict] = []

        header = QHBoxLayout()
        header.setSpacing(30)
        self._avatar = ArtView(200, 200, radius=100)
        header.addWidget(self._avatar, alignment=Qt.AlignmentFlag.AlignBottom)
        info = QVBoxLayout()
        info.setSpacing(6)
        info.addStretch(1)
        self._eyebrow = QLabel("ARTIST")
        info.addWidget(self._eyebrow)
        self._title = QLabel()
        self._title.setStyleSheet(f"color: {C.TEXT}; font-size: 38pt; font-weight: 800;")
        info.addWidget(self._title)
        self._meta = QLabel()
        self._meta.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10.5pt;")
        info.addWidget(self._meta)
        actions = QHBoxLayout()
        actions.setSpacing(11)
        self._play, self._shuffle = self._buttons(actions)
        self._play.clicked.connect(lambda: self._songs_rows and self._player.play_tracks(
            [r for r in self._songs_rows if r["state"] == "ready"], 0, in_order=True,
            context=self._context()))
        self._shuffle.clicked.connect(lambda: self._songs_rows and self._player.shuffle_tracks(
            [r for r in self._songs_rows if r["state"] == "ready"], context=self._context()))
        info.addSpacing(14)
        info.addLayout(actions)
        header.addLayout(info, 1)
        self.body.addLayout(header)
        self.body.addSpacing(34)

        albums_title = QLabel("Albums")
        albums_title.setObjectName("SectionTitle")
        self.body.addWidget(albums_title)
        self.body.addSpacing(10)
        holder = QWidget()
        holder_policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        holder_policy.setHeightForWidth(True)
        holder.setSizePolicy(holder_policy)
        self._albums = FlowLayout(holder, margin=0, h_spacing=14, v_spacing=18)
        self.body.addWidget(holder)
        self.body.addSpacing(26)

        songs_title = QLabel("Songs")
        songs_title.setObjectName("SectionTitle")
        self.body.addWidget(songs_title)
        self.body.addSpacing(8)
        self._tracks = TrackList(["number", "title", "album", "like", "time"], auto_height=True,
                                 numbering="index")
        self._tracks.play_requested.connect(
            lambda row: self._player.play_tracks(self._songs_rows, row, in_order=True,
                                                 context=self._context()))
        self._connect_list(self._tracks)
        self.body.addWidget(self._tracks)
        self.body.addStretch(1)

    @property
    def artist(self) -> str:
        return self._name

    def _own_rows(self) -> list[dict]:
        return self._songs_rows

    def _context(self) -> dict:
        # Artist pages are keyed by the name itself (see MainWindow.open_artist).
        return {"kind": "artist", "title": self._name, "id": self._name}

    def _on_card_action(self, action: str, tile) -> None:
        if action == "open":
            self.album_requested.emit(int(tile.key))
        else:
            play_tile(self._player, action, tile)

    def set_artist(self, name: str) -> None:
        self._name = name
        self.reload()
        self._scroll.verticalScrollBar().setValue(0)

    def reload(self) -> None:
        if not self._name:
            return
        albums = [dict(a) for a in library.artist_albums(self._name)]
        if not albums:
            self.back_requested.emit()
            return
        first = next((a for a in albums if a.get("cover")), albums[0])
        colours = library.parse_palette(first.get("palette"))
        self._page.set_colours(colours)
        self._tracks.set_accent(colours["accent"])
        self._eyebrow.setStyleSheet(
            f"color: {colours['accent']}; font-size: 9pt; font-weight: 800; letter-spacing: 1.5px;")
        self._avatar.set_art(first.get("cover"), self._name)
        self._title.setText(self._name)

        self._albums.clear()
        for album in albums:
            card = AlbumCard(album_tile(album))
            card.clicked.connect(lambda tile: self.album_requested.emit(int(tile.key)))
            card.play_requested.connect(lambda tile: play_tile(self._player, "play", tile))
            # The card's right-click menu (Play, Shuffle, Play next...) went
            # nowhere on this page: only the Music page listened to it.
            card.action_requested.connect(self._on_card_action)
            self._albums.addWidget(card)

        rows: list[dict] = []
        for album in albums:
            rows.extend(dict(t) for t in library.album_tracks(int(album["id"])))
        self._songs_rows = rows
        self._tracks.set_tracks(rows)
        ready = sum(1 for r in rows if r["state"] == "ready")
        self._meta.setText(f"{len(albums)} album{'s' if len(albums) != 1 else ''}  ·  "
                           f"{ready} song{'s' if ready != 1 else ''}")
        self._play.setEnabled(ready > 0)
        self._shuffle.setEnabled(ready > 1)
        self._sync_current()


class PlaylistView(_MusicPage):
    """One music playlist, built on the same page as an album.

    Everything an album page does it needs too — the tinted scroll, the back
    button, Play and Shuffle over a track list, the hearts staying in step — so
    it is ~110 lines rather than 300. What is different is that the order is the
    user's, so each row's menu can move it, and the page reloads itself
    afterwards: db.data_version deliberately does not report this process's own
    writes, so nothing else is going to tell it.
    """

    def __init__(self, player, parent=None) -> None:
        super().__init__(player, parent)
        self._playlist_id: int | None = None
        self._name = ""
        self._rows: list[dict] = []

        header = QHBoxLayout()
        header.setSpacing(30)
        self._cover = ArtView(240, 240, radius=8)
        header.addWidget(self._cover, alignment=Qt.AlignmentFlag.AlignBottom)

        info = QVBoxLayout()
        info.setSpacing(6)
        info.addStretch(1)
        self._eyebrow = QLabel("PLAYLIST")
        info.addWidget(self._eyebrow)
        self._title = QLabel()
        self._title.setWordWrap(True)
        self._title.setStyleSheet(f"color: {C.TEXT}; font-size: 32pt; font-weight: 800;")
        info.addWidget(self._title)
        self._meta = QLabel()
        self._meta.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 10pt;")
        info.addWidget(self._meta)

        actions = QHBoxLayout()
        actions.setSpacing(11)
        self._play, self._shuffle = self._buttons(actions)
        self._play.clicked.connect(lambda: self._play_from(0))
        self._shuffle.clicked.connect(self._shuffle_playlist)
        # Inserted before the stretch _buttons leaves behind, so the four sit
        # together: right-aligned they were 1400 px from Play on a wide window
        # and read as belonging to something else.
        for index, (label, slot) in enumerate((("Rename", self._rename),
                                               ("Delete", self._delete))):
            button = QPushButton(label)
            button.setObjectName("Ghost")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(slot)
            actions.insertWidget(2 + index, button)
        info.addSpacing(14)
        info.addLayout(actions)
        header.addLayout(info, 1)
        self.body.addLayout(header)
        self.body.addSpacing(30)

        self._tracks = TrackList(["number", "title", "artist", "album", "like", "time"],
                                 auto_height=True, numbering="index")
        self._tracks.play_requested.connect(self._play_from)
        self._connect_list(self._tracks)
        self.body.addWidget(self._tracks)

        self._empty = QLabel(
            "Nothing in this playlist yet.\n\nRight-click a song anywhere it is listed — "
            "an album, an artist, Songs, Liked — and choose Add to playlist."
        )
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 11pt; padding: 40px;")
        self.body.addWidget(self._empty)
        self.body.addStretch(1)

    @property
    def playlist_id(self) -> int | None:
        return self._playlist_id

    def set_playlist(self, playlist_id: int) -> None:
        self._playlist_id = int(playlist_id)
        self.reload()
        self._scroll.verticalScrollBar().setValue(0)

    def _connect_list(self, tracks: TrackList) -> None:
        # The shared song menu, plus the three things only a playlist page can
        # offer: it is the only place that knows which entry a row is.
        tracks.context_requested.connect(
            lambda track, pos: track_menu(self, track, self._player, self._row_actions(track),
                                          self._on_playlist_change).exec(pos))
        tracks.like_requested.connect(self._player.set_liked)

    def _on_playlist_change(self, message: str) -> None:
        # This page is one of the lists that was just written to, so it reloads;
        # the line still goes up, because the playlist added to may well be a
        # different one and then nothing on screen moves.
        self.reload()
        self.status.emit(message)

    def _row_actions(self, track: dict) -> list:
        entry = track.get("entry_id")
        if entry is None:
            return []
        return [("Move up", lambda: self._move(int(entry), -1)),
                ("Move down", lambda: self._move(int(entry), 1)),
                ("Remove from this playlist", lambda: self._remove(int(entry)))]

    def _move(self, entry_id: int, step: int) -> None:
        if self._playlist_id is not None and move_entry(
                self._playlist_id, entry_id, step, [r["entry_id"] for r in self._rows]):
            self.reload()

    def _remove(self, entry_id: int) -> None:
        if self._playlist_id is None:
            return
        db.remove_playlist_entries(self._playlist_id, [entry_id])
        self.reload()

    def _rename(self) -> None:
        if self._playlist_id is None:
            return
        name = ask_name(self, "Rename playlist", "Name", self._name)
        if name is None:
            return
        db.rename_playlist(self._playlist_id, name)
        self.reload()

    def _delete(self) -> None:
        if self._playlist_id is None:
            return
        if not confirm_delete(self, self._name):
            return
        db.delete_playlist(self._playlist_id)
        self._playlist_id = None
        self.back_requested.emit()

    def reload(self) -> None:
        if self._playlist_id is None:
            return
        row = db.playlist(self._playlist_id)
        if row is None:
            # Deleted here or in another window: leave, the way an album page
            # does when its album has gone. The id goes first, so whoever asked
            # for the page can see it did not open — set_playlist's caller shows
            # the page after it returns, and a page still holding the dead id
            # looked like a live one.
            self._playlist_id = None
            self.back_requested.emit()
            return
        self._name = row["name"] or "Untitled"
        self._rows = library.playlist_tracks(self._playlist_id)

        first = next((r for r in self._rows if r.get("cover")), None)
        colours = library.parse_palette(first.get("palette") if first else None)
        self._page.set_colours(colours)
        self._tracks.set_accent(colours["accent"])
        self._eyebrow.setStyleSheet(
            f"color: {colours['accent']}; font-size: 9pt; font-weight: 800; letter-spacing: 1.5px;")
        self._cover.set_art(first.get("cover") if first else None, self._name)
        self._title.setText(display_name(self._name))
        self._title.setToolTip(self._name)

        count = len(self._rows)
        held = int(row["item_count"] or 0)
        bits = [f"{count} song{'s' if count != 1 else ''}"]
        length = fmt_duration(sum(float(r.get("duration") or 0) for r in self._rows))
        if length:
            bits.append(length)
        if held > count:
            # Said out loud rather than shown as a shorter list: an unplugged
            # drive is back tomorrow, and the entries are still in the playlist.
            bits.append(f"{held - count} not available right now")
        self._meta.setText("  ·  ".join(bits))

        self._tracks.set_tracks(self._rows)
        self._tracks.setVisible(bool(self._rows))
        self._empty.setVisible(not self._rows)
        self._play.setEnabled(count > 0)
        self._shuffle.setEnabled(count > 1)
        self._sync_current()

    def _own_rows(self) -> list[dict]:
        return self._rows

    def _context(self) -> dict:
        return {"kind": "playlist", "title": self._name, "id": self._playlist_id}

    def _play_from(self, row: int) -> None:
        if self._rows:
            self._player.play_tracks(self._rows, row, in_order=True, context=self._context())

    def _shuffle_playlist(self) -> None:
        if self._rows:
            self._player.shuffle_tracks(self._rows, context=self._context())
