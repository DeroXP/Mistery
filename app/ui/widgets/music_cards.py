"""Album and artist tiles — the same hover growth and play circle as the film
and show cards, so the whole app moves the same way."""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QMenu

from .cards import PosterCard, _REST_INSET


@dataclass
class MusicTile:
    """What a card needs to draw, for an album or an artist."""

    kind: str                    # album | artist | playlist
    key: object                  # album id, artist name, or playlist id
    title: str
    subtitle: str = ""
    art: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def display_title(self) -> str:
        return self.title

    # PosterCard reads these; music has no watch state.
    progress: float = 0.0
    watched: bool = False


class AlbumCard(PosterCard):
    ART_W = 200
    ART_H = 200

    def contextMenuEvent(self, event) -> None:
        item = self._item
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)     # see PosterCard's menu
        menu.addAction("Play",lambda: self.action_requested.emit("play", item))
        menu.addAction("Shuffle", lambda: self.action_requested.emit("shuffle", item))
        menu.addSeparator()
        menu.addAction("Play next", lambda: self.action_requested.emit("next", item))
        menu.addAction("Add to queue", lambda: self.action_requested.emit("queue", item))
        menu.addSeparator()
        menu.addAction({"album": "Open album", "artist": "Open artist"}.get(item.kind,
                                                                            "Open playlist"),
                       lambda: self.action_requested.emit("open", item))
        if item.kind == "playlist":
            menu.addSeparator()
            menu.addAction("Rename…", lambda: self.action_requested.emit("rename", item))
            menu.addAction("Delete playlist", lambda: self.action_requested.emit("delete", item))
        menu.exec(event.globalPos())


class ArtistCard(AlbumCard):
    """Round, the way every music app draws a person rather than a record."""

    def _paint_art(self, painter: QPainter, rect: QRectF) -> None:
        side = min(rect.width(), rect.height())
        circle = QRectF(0, 0, side, side)
        circle.moveCenter(rect.center())
        path = QPainterPath()
        path.addEllipse(circle)

        if self._hover > 0.01:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, int(150 * self._hover)))
            painter.drawEllipse(circle.adjusted(3, 5, 3, 7))

        source = self._preview_tile if self._preview_tile is not None else self._pixmap
        if source is not None:
            painter.save()
            painter.setClipPath(path)
            painter.drawPixmap(circle, source, QRectF(source.rect()))
            painter.restore()
        if self._hover > 0.01:
            painter.fillPath(path, QColor(0, 0, 0, int(72 * self._hover)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, int(26 + 130 * self._hover)), 1.2))
        painter.drawPath(path)

    def _paint_text(self, painter: QPainter, top: float, title: str, subtitle: str) -> None:
        # Centred under a circle reads better than flush-left.
        from PySide6.QtGui import QFont

        from ..theme import C

        width = self.width() - _REST_INSET * 2
        painter.setPen(QColor(C.TEXT))
        font = QFont(painter.font())
        font.setPointSizeF(9.8)
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        painter.drawText(QRectF(_REST_INSET, top, width, metrics.height()), Qt.AlignmentFlag.AlignCenter,
                         metrics.elidedText(title, Qt.TextElideMode.ElideRight, int(width)))
        if subtitle:
            font.setBold(False)
            font.setPointSizeF(8.6)
            painter.setFont(font)
            painter.setPen(QColor(C.TEXT_FAINT))
            sub = painter.fontMetrics()
            painter.drawText(QRectF(_REST_INSET, top + metrics.height() + 1, width, sub.height()),
                             Qt.AlignmentFlag.AlignCenter,
                             sub.elidedText(subtitle, Qt.TextElideMode.ElideRight, int(width)))


def album_tile(row) -> MusicTile:
    row = dict(row)
    bits = [row.get("artist") or ""]
    if row.get("year"):
        bits.append(str(row["year"]))
    art = row.get("cover")
    small = art.replace(".jpg", "-sm.jpg") if art else None
    return MusicTile("album", int(row["id"]), row.get("title") or "Unknown Album",
                     " · ".join(b for b in bits if b), small, row)


def artist_tile(row) -> MusicTile:
    row = dict(row)
    count = int(row.get("album_count") or 0)
    art = row.get("cover")
    small = art.replace(".jpg", "-sm.jpg") if art else None
    return MusicTile("artist", row["name"], row["name"],
                     f"{count} album{'s' if count != 1 else ''}", small, row)


def playlist_tile(row, cover: str | None = None) -> MusicTile:
    """A music playlist as a tile, sized and shaped like an album's.

    `item_count` is what the playlist holds; the subtitle says that rather than
    what resolves today, so a list whose songs are mid-download or on an
    unplugged drive does not silently shrink to "0 songs".
    """
    row = dict(row)
    count = int(row.get("item_count") or 0)
    small = cover.replace(".jpg", "-sm.jpg") if cover else None
    return MusicTile("playlist", int(row["id"]), row.get("name") or "Untitled",
                     f"{count} song{'s' if count != 1 else ''}", small, row)


def tile_context(tile: MusicTile) -> dict:
    """"Playing from" for a queue started from a card: the album, the artist or
    the playlist, keyed the way each of their pages is opened."""
    if tile.kind == "album":
        return {"kind": "album", "title": tile.title, "id": int(tile.key)}
    if tile.kind == "playlist":
        return {"kind": "playlist", "title": tile.title, "id": int(tile.key)}
    return {"kind": "artist", "title": str(tile.key), "id": str(tile.key)}


def tile_tracks(tile: MusicTile) -> list[dict]:
    """The playable songs behind a card, album by album for an artist."""
    from ...music import library

    if tile.kind == "album":
        rows = library.album_tracks(int(tile.key))
    elif tile.kind == "playlist":
        # Already filtered to playable songs, and already in playlist order.
        return [dict(t) for t in library.playlist_tracks(int(tile.key))]
    else:
        rows = [track for album in library.artist_albums(str(tile.key))
                for track in library.album_tracks(int(album["id"]))]
    return [dict(t) for t in rows if t["state"] == "ready"]


def play_tile(player, action: str, tile: MusicTile) -> None:
    """A card's Play, Shuffle, Play next or Add to queue — the same on every page."""
    ready = tile_tracks(tile)
    if not ready:
        return
    # A playlist is in_order for the same reason an album is: the order is the
    # point. Shuffling it is what the Shuffle action is for.
    in_order = tile.kind in ("album", "playlist")
    if action == "play":
        player.play_tracks(ready, 0, in_order=in_order, context=tile_context(tile))
    elif action == "shuffle":
        player.shuffle_tracks(ready, context=tile_context(tile))
    elif action == "next":
        if not player.has_queue:
            # Inserting one by one in reverse would start with the last song.
            player.play_tracks(ready, 0, in_order=in_order, context=tile_context(tile))
        else:
            for track in reversed(ready):
                player.play_next(track)
    elif action == "queue":
        for track in ready:
            player.add_to_queue(track)
