"""The one "Add to playlist" menu, for a song and for a film alike.

Every right-click that can put something in a playlist builds this: the song
menu in tracklist.py (which serves the album and artist pages, Songs, Liked and
a playlist's own page at once) and the card menu on films and episodes, by way
of the window. Written once because the picking is the same either way — only
`kind` differs, and it is the same two tables underneath.

Not the Up next list: its rows have a menu of their own — Play now, Remove from
queue — and the queue's gesture is Save as playlist, over the whole of it.

A playlist already holding the item is ticked, and clicking a ticked one takes
the item back out. That is what a tick in a menu means; the alternative was a
second copy of the song appearing silently, which reads as the click having
done nothing.

One click undoes one add, no more: a song deliberately in a list twice loses
the later copy and the tick stays on, because the earlier one is still there.
Everything else here makes a duplicate independently addressable — entry ids
rather than item ids, Move up on the second copy alone — and a tick that swept
out both copies at once was the one place that did not.
"""

from __future__ import annotations

from typing import Callable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog, QLineEdit, QMenu, QMessageBox

from ... import db

_KIND_WORD = {"music": "song", "video": "title"}

# How much of a typed name is kept. See ask_name.
NAME_LIMIT = 120


def confirm_delete(parent, name: str) -> bool:
    """Deleting a playlist is the one thing here that cannot be undone, so it
    is the one thing that asks first."""
    box = QMessageBox(parent)
    box.setWindowTitle("Delete playlist")
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(f"Delete “{name}”?")
    box.setInformativeText("Nothing leaves your library; only the list goes.")
    box.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
    box.setDefaultButton(QMessageBox.StandardButton.Cancel)
    return box.exec() == QMessageBox.StandardButton.Yes


def ask_name(parent, title: str, label: str, text: str = "") -> str | None:
    """A one-line prompt. Returns None when it was cancelled or left empty.

    QInputDialog rather than a hand-built dialog: there is no dialog furniture
    in the app yet, and its QLineEdit and buttons pick up the window stylesheet
    on their own (theme.py styles both globally).
    """
    name, accepted = QInputDialog.getText(parent, title, label, QLineEdit.EchoMode.Normal, text)
    # Capped where it is read, so every way in is capped at once. A playlist
    # title is 30 pt above the scroll area on the video page: 300 characters
    # wrapped to five lines and pushed the grid off the bottom of the window,
    # with nothing to scroll. 120 is two lines on a 1440-wide window and is
    # more name than anyone types.
    name = (name or "").strip()[:NAME_LIMIT].strip()
    return name if accepted and name else None


def display_name(name: str) -> str:
    """A name cut to what a page title can hold, for the two playlist pages.

    ask_name caps what is typed, but a row written before that cap — or by hand,
    or by another tool — can still be any length, and the title is the one label
    that has nowhere to go: it sits above the scroll area, so a long enough name
    pushes the list off the bottom of the window. The pages put the whole name
    in the tooltip.
    """
    name = name or "Untitled"
    return name if len(name) <= NAME_LIMIT else name[:NAME_LIMIT - 1] + "…"


def _entries_holding(playlist_id: int, item_ids: Sequence[int]) -> list[int]:
    """One entry per item — the last copy of each, or nothing if it isn't there.

    Non-empty is what ticks the playlist; the ids themselves are what a click on
    a ticked one removes. The last rather than every matching entry so that one
    click takes back one add (see the module docstring), and one per item so a
    menu built over several items still removes one copy of each.
    """
    wanted = {int(i) for i in item_ids}
    last: dict[int, int] = {}
    for row in db.playlist_entries(playlist_id):
        item_id = int(row["item_id"])
        if item_id in wanted:
            last[item_id] = int(row["id"])       # entries come back in playlist order
    return list(last.values())


def add_to_playlist_menu(parent, kind: str, item_ids: Sequence[int],
                         on_change: Callable[[str], None] | None = None,
                         title: str = "Add to playlist") -> QMenu:
    """The menu itself — exec() it, or hand it to QMenu.addMenu as a submenu.

    `parent` must be the menu it hangs off when it is a submenu, so it dies
    with it (track_menu deletes itself on close; a submenu parented elsewhere
    outlived it).
    """
    ids = [int(i) for i in item_ids]
    menu = QMenu(title, parent)
    menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    lists = db.playlists(kind)

    for row in lists:
        playlist_id = int(row["id"])
        held = _entries_holding(playlist_id, ids)
        count = int(row["item_count"] or 0)
        action = menu.addAction(f"{row['name']}   ({count})" if count else str(row["name"]))
        action.setCheckable(True)
        action.setChecked(bool(held))
        action.triggered.connect(
            lambda _checked=False, pid=playlist_id, name=str(row["name"]), entries=tuple(held):
            _toggle(pid, name, ids, entries, on_change))

    if lists:
        menu.addSeparator()
    menu.addAction("New playlist…", lambda: new_playlist_with(parent, kind, ids, on_change))
    return menu


def _toggle(playlist_id: int, name: str, item_ids: Sequence[int],
            entries: Sequence[int], on_change) -> None:
    if entries:
        db.remove_playlist_entries(playlist_id, entries)
        message = f"Removed from {name}."
    else:
        db.add_to_playlist(playlist_id, item_ids)
        message = f"Added to {name}."
    if on_change is not None:
        on_change(message)


def move_entry(playlist_id: int, entry_id: int, step: int, visible: Sequence[int]) -> bool:
    """Move one entry up (-1) or down (+1) past the next one you can see.

    `visible` is the entry ids the page is showing, in order. It is not always
    the whole playlist: a song whose file is missing today is still an entry,
    still holds its position, and is not on screen. Swapping the two *slots*
    rather than shifting by one position means Move up crosses a hidden entry
    in one go, instead of looking like the click did nothing.

    False when there is nothing on that side.
    """
    order = [int(row["id"]) for row in db.playlist_entries(playlist_id)]
    seen = [int(i) for i in visible if int(i) in order]
    try:
        here = seen.index(int(entry_id))
    except ValueError:
        return False
    there = here + step
    if not 0 <= there < len(seen):
        return False
    first, second = order.index(seen[here]), order.index(seen[there])
    order[first], order[second] = order[second], order[first]
    db.set_playlist_order(playlist_id, order)
    return True


def new_playlist_with(parent, kind: str, item_ids: Sequence[int],
                      on_change: Callable[[str], None] | None = None) -> int | None:
    """Make a playlist and put these in it. Returns its id, or None if cancelled."""
    word = _KIND_WORD.get(kind, "item")
    count = len(item_ids)
    label = (f"Name for a new playlist with {count} {word}{'s' if count != 1 else ''}"
             if count else "Name for the new playlist")
    name = ask_name(parent, "New playlist", label)
    if name is None:
        return None
    playlist_id = db.create_playlist(kind, name)
    if item_ids:
        db.add_to_playlist(playlist_id, item_ids)
    if on_change is not None:
        on_change(f"Created {name}.")
    return playlist_id
