"""The Sound panel: volume matching, bass, clarity and 3D, in one popover.

It opens over whatever is playing and every choice is heard immediately — the
song does not stop, restart or lose its place — because the settings are a
filter chain mpv can swap mid-song (see music/audio_fx.py).

The panel is deliberately a short list of named steps rather than a graphic
equaliser. Named steps can be measured and levelled honestly: each one here was
checked on real songs so that switching it on changes the sound and not the
loudness, which is the only way to tell whether you actually prefer it.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ..config import settings
from ..music import audio_fx, loudness
from .theme import C
from .widgets.icons import paint_icon

_CHIP_STYLE = f"""
QPushButton {{
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 13px;
    color: {C.TEXT_DIM};
    font-size: 9pt;
    font-weight: 600;
    padding: 5px 12px;
}}
QPushButton:hover {{ color: {C.TEXT}; border-color: rgba(255,255,255,0.28); }}
QPushButton:checked {{
    background: {C.TEXT};
    border-color: {C.TEXT};
    color: #0B0B0B;
}}
"""


class Segmented(QWidget):
    """A labelled row of chips where exactly one is chosen."""

    changed = Signal(str)

    def __init__(self, title: str, options: list[tuple[str, str]], value: str,
                 note: str = "", parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

        heading = QLabel(title)
        heading.setStyleSheet(f"color: {C.TEXT}; font-size: 9.5pt; font-weight: 700;")
        layout.addWidget(heading)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._keys: list[str] = []
        for index, (key, label) in enumerate(options):
            chip = QPushButton(label)
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setStyleSheet(_CHIP_STYLE)
            chip.setChecked(key == value)
            self._group.addButton(chip, index)
            self._keys.append(key)
            row.addWidget(chip)
        row.addStretch(1)
        layout.addLayout(row)

        self._note = QLabel(note)
        self._note.setStyleSheet("color: rgba(255,255,255,0.56); font-size: 8.5pt;")
        self._note.setWordWrap(True)
        # Into the layout first: showing a widget that has no parent yet makes it
        # a window of its own for a moment, and a popup opened afterwards is
        # swallowed by Qt's popup handling — the panel simply never appeared.
        layout.addWidget(self._note)
        self._note.setVisible(bool(note))

        self._group.idClicked.connect(lambda index: self.changed.emit(self._keys[index]))

    def set_note(self, text: str) -> None:
        self._note.setText(text)
        self._note.setVisible(bool(text))

    def set_value(self, value: str) -> None:
        if value in self._keys:
            button = self._group.button(self._keys.index(value))
            if button:
                button.setChecked(True)


_COMBO_STYLE = f"""
QComboBox {{
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 8px;
    color: {C.TEXT};
    font-size: 9pt;
    padding: 5px 30px 5px 10px;
    min-width: 0px;
}}
QComboBox:hover {{ border-color: rgba(255,255,255,0.28); }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{ image: none; width: 0px; height: 0px; }}
"""

# The last device list, shared by every panel, so a panel opens already showing
# names while a fresh list is fetched behind it; and every description seen, so
# a device that has been unplugged since is still called by its name.
_known_devices: list[tuple[str, str]] = []
_descriptions: dict[str, str] = {}


class DeviceCombo(QComboBox):
    """A compact selector with a drawn chevron (the style sheet hides Qt's arrow)."""

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        glyph = QRectF(self.width() - 24, (self.height() - 14) / 2, 14, 14)
        paint_icon(painter, "chevron_down", glyph, QColor(C.TEXT_DIM), stroke=2.2)


class _DeviceLister(QObject):
    """Lists output devices on a thread and hands the answer back on the GUI thread.

    Asking mpv takes ~15 ms when it is running but ~120 ms (up to a second)
    through a throwaway mpv when it isn't, which is too long to hold the panel
    open on. The signal is emitted from the thread and delivered queued to the
    panel, which lives on the GUI thread.
    """

    listed = Signal(list)

    def __init__(self, player, parent=None) -> None:
        super().__init__(parent)
        self._player = player
        self._busy = False

    def fetch(self) -> bool:
        if self._busy:
            return False
        self._busy = True
        threading.Thread(target=self._work, name="mistery-audio-devices", daemon=True).start()
        return True

    def _work(self) -> None:
        try:
            devices = list(self._player.audio_devices())
        except Exception:
            devices = []
        self._busy = False
        self.listed.emit(devices)


class SoundPanel(QWidget):
    """The popover itself. Pass the music player; it applies changes live."""

    def __init__(self, player, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self._player = player
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(360)
        self._lister = _DeviceLister(player, self)
        self._lister.listed.connect(self._on_devices)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(13)

        title = QLabel("Sound")
        title.setStyleSheet(f"color: {C.TEXT}; font-size: 13pt; font-weight: 800;")
        root.addWidget(title)

        self._match = Segmented(
            "Match volume",
            [(key, loudness.TARGET_LABELS[key].split(" — ")[0]) for key in
             ("off", "quiet", "normal", "loud")],
            str(settings.get("music_normalize", "loud")),
        )
        self._match.changed.connect(self._on_match)
        root.addWidget(self._match)

        self._boost = Segmented(
            "Extra loudness",
            [(key, audio_fx.BOOST_LABELS[key]) for key in ("off", "low", "high")],
            audio_fx.current()["boost"],
            "On top of the matched level, with a limiter so nothing clips.",
        )
        self._boost.changed.connect(lambda value: self._set("music_boost", value))
        root.addWidget(self._boost)

        self._bass = Segmented(
            "Bass",
            [(key, audio_fx.BASS_LABELS[key]) for key in ("off", "warm", "deep", "massive")],
            audio_fx.current()["bass"],
            "A low shelf at 95 Hz. Deeper settings play slightly quieter to make room.",
        )
        self._bass.changed.connect(lambda value: self._set("music_bass", value))
        root.addWidget(self._bass)

        self._spatial = Segmented(
            "3D audio",
            [(key, audio_fx.SPATIAL_LABELS[key]) for key in ("off", "subtle", "normal", "wide")],
            audio_fx.current()["spatial"],
            "Headphones. Widens the picture, then puts one channel a few "
            "milliseconds behind the other — which the ear reads as space in "
            "front of you instead of sound inside your head.",
        )
        self._spatial.changed.connect(lambda value: self._set("music_spatial", value))
        root.addWidget(self._spatial)

        self._clarity = Segmented(
            "Clarity",
            [("off", "Off"), ("on", "On")],
            "on" if audio_fx.current()["clarity"] else "off",
            "Lifts the top end 3 dB: cymbals, consonants, air.",
        )
        self._clarity.changed.connect(
            lambda value: self._set("music_clarity", value == "on"))
        root.addWidget(self._clarity)

        rule = QFrame()
        rule.setFixedHeight(1)
        rule.setStyleSheet("background: rgba(255,255,255,0.08);")
        root.addWidget(rule)

        # Output device: where the music goes, switched live.
        output = QHBoxLayout()
        output.setSpacing(12)
        heading = QLabel("Output")
        heading.setStyleSheet(f"color: {C.TEXT}; font-size: 9.5pt; font-weight: 700;")
        output.addWidget(heading)
        self._device = DeviceCombo()
        self._device.setStyleSheet(_COMBO_STYLE)
        self._device.setCursor(Qt.CursorShape.PointingHandCursor)
        self._device.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._device.setMinimumContentsLength(12)
        self._device.activated.connect(self._on_device_chosen)
        output.addWidget(self._device, 1)
        root.addLayout(output)
        self._device_note = QLabel()
        self._device_note.setWordWrap(True)
        self._device_note.setStyleSheet("color: rgba(255,255,255,0.56); font-size: 8.5pt;")
        root.addWidget(self._device_note)
        self._device_note.setVisible(False)
        self._show_devices(_known_devices)

        self._summary = QLabel()
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(f"color: {C.TEXT_DIM}; font-size: 8.5pt;")
        root.addWidget(self._summary)
        self._refresh_summary()

    # --- behaviour ----------------------------------------------------------

    def _set(self, key: str, value) -> None:
        settings.set(key, value)
        self._player.apply_sound()
        self._refresh_summary()

    def _on_match(self, value: str) -> None:
        self._player.set_normalize(value)
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        target = loudness.target_db(settings.get("music_normalize", "loud"))
        if target is None:
            level = "Files play at whatever level they were mastered at."
        else:
            level = f"Every album is played at {target:g} LUFS."
        self._summary.setText(f"{level}  Effects: {audio_fx.describe()}.")

    def reload(self) -> None:
        state = audio_fx.current()
        self._match.set_value(str(settings.get("music_normalize", "loud")))
        self._boost.set_value(state["boost"])
        self._bass.set_value(state["bass"])
        self._spatial.set_value(state["spatial"])
        self._clarity.set_value("on" if state["clarity"] else "off")
        self._refresh_summary()
        if not self._device.view().isVisible():
            self._show_devices(_known_devices)

    # --- output device ------------------------------------------------------

    def refresh_devices(self) -> bool:
        """Ask for the device list again, off the GUI thread. False if already asking."""
        return self._lister.fetch()

    def _on_devices(self, devices: list) -> None:
        global _known_devices
        if devices:
            _known_devices = [(str(name), str(description)) for name, description in devices]
            _descriptions.update(_known_devices)
        if not self._device.view().isVisible():     # never rebuild a list the user has open
            self._show_devices(_known_devices)

    def _show_devices(self, devices: list[tuple[str, str]]) -> None:
        chosen = self._player.audio_device
        entries = list(devices) or [("auto", "System default")]
        names = [name for name, _ in entries]
        missing = chosen not in names
        if missing:
            # Chosen before and unplugged now: say so rather than pretend it's the default.
            # (Before the first list arrives it is simply not known yet.)
            described = _descriptions.get(chosen, "Chosen device")
            entries.append((chosen, f"{described} (not connected)" if devices else described))
        self._device.blockSignals(True)
        self._device.clear()
        for name, description in entries:
            self._device.addItem(description, name)
        self._device.setCurrentIndex([name for name, _ in entries].index(chosen))
        self._device.blockSignals(False)
        self._device.setToolTip(self._device.currentText())
        self._device_note.setText("That device isn't connected, so the music plays on the system "
                                  "default until it is back." if missing and devices else "")
        self._device_note.setVisible(bool(missing and devices))

    def _on_device_chosen(self, index: int) -> None:
        name = self._device.itemData(index)
        if name is None:
            return
        self._player.set_audio_device(str(name))
        self._show_devices(_known_devices)
        self.adjustSize()

    @property
    def device_combo(self) -> QComboBox:
        return self._device

    def popup_at(self, anchor: QWidget) -> None:
        """Open above the button that asked for it, kept inside the screen."""
        self.reload()
        self.refresh_devices()
        self.adjustSize()
        corner = anchor.mapToGlobal(anchor.rect().topLeft())
        x = corner.x() + anchor.width() // 2 - self.width() // 2
        y = corner.y() - self.height() - 10
        screen = anchor.screen() or self.screen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left() + 8, min(x, area.right() - self.width() - 8))
            if y < area.top() + 8:
                y = corner.y() + anchor.height() + 10
        self.move(x, y)
        self.show()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(self.rect().adjusted(0, 0, -1, -1), 16, 16)
        painter.fillPath(path, QColor(22, 22, 24, 250))
        painter.setPen(QColor(255, 255, 255, 26))
        painter.drawPath(path)
