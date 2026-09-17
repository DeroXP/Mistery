"""A wrapping layout, so poster grids reflow with the window width."""

from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QSizePolicy, QWidgetItem


class FlowLayout(QLayout):
    def __init__(self, parent=None, margin: int = 0, h_spacing: int = 18, v_spacing: int = 24):
        super().__init__(parent)
        self._items: list[QWidgetItem] = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self.setContentsMargins(QMargins(margin, margin, margin, margin))

    def __del__(self):
        while self._items:
            self._items.pop()

    def addItem(self, item):  # noqa: N802 - Qt API
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):  # noqa: N802 - Qt API
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):  # noqa: N802 - Qt API
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def clear(self) -> None:
        while self._items:
            item = self._items.pop()
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def swap(self, first: int, second: int) -> None:
        """Exchange two widgets' places, keeping both.

        For a page that edits its own grid one step at a time: a playlist's
        Move up. Rebuilding the grid instead cost 1582 ms on a visible page of
        500 cards (measured, median of 5), against 8 ms through here — the cost
        is building and first-painting 500 new cards, not the layout pass.
        """
        if first == second:
            return
        if not (0 <= first < len(self._items) and 0 <= second < len(self._items)):
            return
        self._items[first], self._items[second] = self._items[second], self._items[first]
        self._relayout()

    def remove_at(self, index: int) -> None:
        """Take one widget out and destroy it, the way clear() does the lot."""
        item = self.takeAt(index)
        if item is None:
            return
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        self._relayout()

    def _relayout(self) -> None:
        # The height of a wrapping layout depends on its width, so the widget
        # holding it has to be asked for its size hint again; invalidate() on
        # its own left the grid the height it had before.
        self.invalidate()
        parent = self.parentWidget()
        if parent is not None:
            parent.updateGeometry()

    def expandingDirections(self):  # noqa: N802 - Qt API
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt API
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt API
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):  # noqa: N802 - Qt API
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt API
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _layout(self, rect: QRect, apply: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(),
                                  -margins.right(), -margins.bottom())
        x, y = effective.x(), effective.y()
        row_height = 0

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h_spacing
            if next_x - self._h_spacing > effective.right() and row_height > 0:
                x = effective.x()
                y = y + row_height + self._v_spacing
                next_x = x + hint.width() + self._h_spacing
                row_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            row_height = max(row_height, hint.height())

        return y + row_height - rect.y() + margins.bottom()
