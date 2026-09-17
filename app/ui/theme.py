"""Dark cinematic palette and the application stylesheet."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase


class C:
    """Colour tokens: red accent on neutral near-black, the streaming look.

    The accent is deliberately reserved — it marks the active nav item, watch
    progress and selection, and nothing else. The primary action button is
    white, which is what makes a dark page read as a streaming app rather than
    a dark app with a coloured button.
    """

    BG = "#141414"
    BG_ELEV = "#181818"
    SURFACE = "#232323"
    SURFACE_HOVER = "#2C2C2C"
    SURFACE_ACTIVE = "#333333"
    BORDER = "#2A2A2A"
    BORDER_STRONG = "#404040"

    TEXT = "#FFFFFF"
    TEXT_DIM = "#B3B3B3"
    TEXT_FAINT = "#777777"

    ACCENT = "#E50914"
    ACCENT_HOVER = "#F6121D"
    ACCENT_PRESSED = "#B20710"
    ON_ACCENT = "#FFFFFF"

    # The primary action: white plate, black glyph.
    PLAY_BG = "#FFFFFF"
    PLAY_BG_HOVER = "#D9D9D9"
    PLAY_FG = "#000000"

    DANGER = "#FF5A5A"
    SUCCESS = "#4ADE80"
    INFO = "#63A4FF"

    SCRIM = "#000000"


# These are the tile's *widget* size. The artwork sits inset inside it at rest
# and grows to fill it on hover, so the visible art is ~184x276 / 316x171.
POSTER_W, POSTER_H = 200, 300
WIDE_W, WIDE_H = 332, 187
HERO_H = 470
TOPBAR_H = 68
RADIUS = 6


def qcolor(hex_value: str, alpha: int = 255) -> QColor:
    color = QColor(hex_value)
    color.setAlpha(alpha)
    return color


def ui_font(size: int = 10, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    families = QFontDatabase.families()
    for candidate in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Arial"):
        if candidate in families:
            font = QFont(candidate, size)
            font.setWeight(weight)
            return font
    font = QFont()
    font.setPointSize(size)
    font.setWeight(weight)
    return font


def display_font(size: int = 26, weight: QFont.Weight = QFont.Weight.DemiBold) -> QFont:
    families = QFontDatabase.families()
    for candidate in ("Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI", "Arial"):
        if candidate in families:
            font = QFont(candidate, size)
            font.setWeight(weight)
            return font
    return ui_font(size, weight)


STYLESHEET = f"""
QWidget {{
    background: transparent;
    color: {C.TEXT};
    font-family: "Segoe UI Variable Text", "Segoe UI", Arial, sans-serif;
    font-size: 10pt;
}}

QMainWindow, #RootPane {{ background: {C.BG}; }}

/* ---------- top navigation ---------- */
#TopBar {{ background: {C.BG}; border-bottom: 1px solid rgba(255, 255, 255, 0.06); }}
#Brand {{
    color: {C.ACCENT};
    font-size: 19pt;
    font-weight: 800;
    letter-spacing: 2px;
    padding: 0 6px 0 0;
}}
QPushButton#NavItem {{
    background: transparent;
    border: none;
    color: {C.TEXT_DIM};
    padding: 8px 14px;
    font-size: 10.5pt;
}}
QPushButton#NavItem:hover {{ color: {C.TEXT}; }}
QPushButton#NavItem:checked {{ color: {C.TEXT}; font-weight: 700; }}
#NavStats {{ color: {C.TEXT_FAINT}; font-size: 8.5pt; }}

/* ---------- headings ---------- */
#SectionTitle {{
    font-size: 14pt;
    font-weight: 600;
    color: {C.TEXT};
    padding: 2px 0 2px 0;
}}
#SectionHint {{ color: {C.TEXT_FAINT}; font-size: 9pt; }}
#PageTitle {{ font-size: 22pt; font-weight: 700; }}
#Muted {{ color: {C.TEXT_DIM}; }}
#Faint {{ color: {C.TEXT_FAINT}; font-size: 9pt; }}

/* ---------- buttons ---------- */
QPushButton {{
    background: {C.SURFACE};
    border: 1px solid {C.BORDER};
    border-radius: 7px;
    padding: 8px 16px;
    color: {C.TEXT};
}}
QPushButton:hover {{ background: {C.SURFACE_HOVER}; border-color: {C.BORDER_STRONG}; }}
QPushButton:pressed {{ background: {C.SURFACE_ACTIVE}; }}
QPushButton:disabled {{ color: {C.TEXT_FAINT}; border-color: {C.BORDER}; }}

QPushButton#Primary {{
    background: {C.PLAY_BG};
    color: {C.PLAY_FG};
    border: none;
    font-weight: 700;
    padding: 12px 30px;
    border-radius: 4px;
    font-size: 11.5pt;
}}
QPushButton#Primary:hover {{ background: {C.PLAY_BG_HOVER}; }}
QPushButton#Primary:pressed {{ background: #C4C4C4; }}

QPushButton#Ghost {{
    background: rgba(109, 109, 110, 0.7);
    border: none;
    color: {C.TEXT};
    padding: 12px 26px;
    border-radius: 4px;
    font-size: 11pt;
    font-weight: 600;
}}
QPushButton#Ghost:hover {{ background: rgba(109, 109, 110, 0.45); }}

QPushButton#Chip {{
    background: transparent;
    border: 1px solid {C.BORDER_STRONG};
    border-radius: 4px;
    padding: 6px 15px;
    color: {C.TEXT_DIM};
    font-size: 9pt;
    /* One weight for both states: a chip is sized for its unchecked text, so a
       bolder checked label would be wider than the button and get clipped. */
    font-weight: 600;
}}
QPushButton#Chip:hover {{ color: {C.TEXT}; border-color: {C.TEXT_DIM}; }}
QPushButton#Chip:checked {{
    background: {C.TEXT};
    color: #000000;
    border-color: {C.TEXT};
}}

/* ---------- inputs ---------- */
QLineEdit {{
    background: {C.SURFACE};
    border: 1px solid {C.BORDER};
    border-radius: 8px;
    padding: 9px 13px;
    selection-background-color: {C.ACCENT};
    selection-color: {C.ON_ACCENT};
}}
QLineEdit:focus {{ border-color: {C.ACCENT}; }}

QComboBox {{
    background: {C.SURFACE};
    border: 1px solid {C.BORDER};
    border-radius: 7px;
    padding: 7px 12px;
    min-width: 120px;
}}
QComboBox:hover {{ border-color: {C.BORDER_STRONG}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {C.BG_ELEV};
    border: 1px solid {C.BORDER_STRONG};
    selection-background-color: {C.SURFACE_ACTIVE};
    outline: none;
    padding: 4px;
}}

QCheckBox {{ spacing: 9px; color: {C.TEXT}; }}
QCheckBox::indicator {{
    width: 17px; height: 17px;
    border-radius: 5px;
    border: 1px solid {C.BORDER_STRONG};
    background: {C.SURFACE};
}}
QCheckBox::indicator:checked {{ background: {C.ACCENT}; border-color: {C.ACCENT}; }}
QCheckBox::indicator:hover {{ border-color: {C.ACCENT}; }}

/* ---------- menus ---------- */
QMenu {{
    background: {C.BG_ELEV};
    border: 1px solid {C.BORDER_STRONG};
    border-radius: 9px;
    padding: 6px;
}}
QMenu::item {{ padding: 8px 26px 8px 14px; border-radius: 6px; color: {C.TEXT}; }}
QMenu::item:selected {{ background: {C.SURFACE_ACTIVE}; }}
QMenu::item:checked {{ color: {C.ACCENT}; }}
QMenu::separator {{ height: 1px; background: {C.BORDER}; margin: 5px 8px; }}

/* ---------- scroll ---------- */
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {C.BORDER_STRONG};
    border-radius: 5px;
    min-height: 40px;
}}
QScrollBar::handle:vertical:hover {{ background: #4B5565; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: {C.BORDER_STRONG};
    border-radius: 5px;
    min-width: 40px;
}}
QScrollBar::handle:horizontal:hover {{ background: #4B5565; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- cards ---------- */
#Card {{
    background: {C.BG_ELEV};
    border: 1px solid {C.BORDER};
    border-radius: 6px;
}}
#Badge {{
    background: rgba(255, 255, 255, 0.09);
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 3px;
    padding: 3px 9px;
    color: {C.TEXT_DIM};
    font-size: 8.5pt;
    font-weight: 600;
}}
#BadgeAccent {{
    background: rgba(229, 9, 20, 0.18);
    border: 1px solid rgba(229, 9, 20, 0.55);
    border-radius: 3px;
    padding: 3px 9px;
    color: #FF5A63;
    font-size: 8.5pt;
    font-weight: 700;
}}

QToolTip {{
    background: {C.BG_ELEV};
    color: {C.TEXT};
    border: 1px solid {C.BORDER_STRONG};
    padding: 6px 9px;
    border-radius: 6px;
}}

QSplitter::handle {{ background: {C.BORDER}; }}
"""
