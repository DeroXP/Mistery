"""Hearth: sunflower light on a deep black, and the application stylesheet."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtGui import QColor, QFont, QFontDatabase


class C:
    """Colour tokens: sunflower yellow on a deep, faintly warm black.

    The yellow means "you are here" and "press this": the page in the sidebar,
    the main button of a page (Play, Resume), watch progress and selection.
    Everything else is warm grey, so the yellow always says something. Text
    on the yellow is near-black, never white: white on #FFD23F is 1.4:1.
    """

    BG = "#0B0A09"
    BG_ELEV = "#151210"         # menus, popovers, cards
    SURFACE = "#1A1714"         # fields and plain buttons
    SURFACE_HOVER = "#231F1B"
    SURFACE_ACTIVE = "#2C2621"
    BORDER = "#26211C"
    BORDER_STRONG = "#3A322B"
    RAIL = "#0F0D0B"            # the sidebar

    TEXT = "#F6F0E6"
    TEXT_DIM = "#CDC3B6"
    TEXT_FAINT = "#9A8F82"      # 6.9:1 on BG, 5.3:1 on SURFACE

    ACCENT = "#FFD23F"
    ACCENT_HOVER = "#FFDC66"
    ACCENT_PRESSED = "#E9BC2C"
    ON_ACCENT = "#1B1406"

    # The primary action: the yellow plate, a dark glyph.
    PLAY_BG = ACCENT
    PLAY_BG_HOVER = ACCENT_HOVER
    PLAY_FG = ON_ACCENT

    DANGER = "#FF6B5E"
    SUCCESS = "#7BD88F"
    INFO = "#7FB0E0"

    SCRIM = "#000000"


# These are the tile's *widget* size. The artwork sits inset inside it at rest
# and grows to fill it on hover, so the visible art is ~184x276 / 316x171.
POSTER_W, POSTER_H = 200, 300
WIDE_W, WIDE_H = 332, 187
HERO_H = 380            # Home's hero card, inset in the page
TOPBAR_H = 68           # the old top bar's; nothing lays out by it now
RAIL_W = 84             # the sidebar, closed
RAIL_OPEN_W = 236       # and open
RADIUS = 14

# The two faces, each with what Windows has for when they are not bundled.
# Fraunces (titles) and Figtree (everything else) are free (SIL Open Font
# License) and load from assets/fonts when they are there (load_fonts).
DISPLAY_FAMILIES = ("Fraunces", "Sitka Display", "Georgia")
UI_FAMILIES = ("Figtree", "Segoe UI Variable Text", "Segoe UI", "Arial")

_log = logging.getLogger("startup")
_fonts_loaded = False


def load_fonts() -> None:
    """Register the bundled faces once, before any window is styled: a family
    the style sheet names must exist when the widgets are first polished."""
    global _fonts_loaded
    if _fonts_loaded:
        return
    _fonts_loaded = True
    from ..config import assets_dir

    folder = Path(assets_dir()) / "fonts"
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.ttf")):
        if QFontDatabase.addApplicationFont(str(path)) < 0:
            _log.warning("could not load the font %s", path.name)


def _first(families: tuple[str, ...]) -> str:
    known = set(QFontDatabase.families())
    return next((family for family in families if family in known), families[-1])


def display_family() -> str:
    return _first(DISPLAY_FAMILIES)


def qcolor(hex_value: str, alpha: int = 255) -> QColor:
    color = QColor(hex_value)
    color.setAlpha(alpha)
    return color


def ui_font(size: float = 10, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont(_first(UI_FAMILIES))
    font.setPointSizeF(float(size))         # 10.5 as well as 10: QFont(family, size) takes whole points
    font.setWeight(weight)
    return font


def display_font(size: float = 26, weight: QFont.Weight = QFont.Weight.Bold) -> QFont:
    font = QFont(display_family())
    font.setPointSizeF(float(size))
    font.setWeight(weight)
    return font


_UI_STACK = ", ".join(f'"{family}"' for family in UI_FAMILIES) + ", sans-serif"
_DISPLAY_STACK = ", ".join(f'"{family}"' for family in DISPLAY_FAMILIES) + ", serif"

STYLESHEET = f"""
QWidget {{
    background: transparent;
    color: {C.TEXT};
    font-family: {_UI_STACK};
    font-size: 10pt;
}}

QMainWindow, #RootPane {{ background: {C.BG}; }}

/* ---------- the old top bar's names, kept for anything still asking ---------- */
#Brand {{
    color: {C.ACCENT};
    font-family: {_DISPLAY_STACK};
    font-size: 19pt;
    font-weight: 700;
}}
#NavStats {{ color: {C.TEXT_FAINT}; font-size: 9pt; }}

/* ---------- headings ---------- */
#SectionTitle {{
    font-family: {_DISPLAY_STACK};
    font-size: 17pt;
    font-weight: 700;
    color: {C.TEXT};
    padding: 2px 0 2px 0;
}}
#SectionHint {{ color: {C.TEXT_FAINT}; font-size: 9pt; }}
#SectionCount {{
    background: #221E1A;
    color: {C.TEXT_DIM};
    /* Under half its ~19 px height: past half, Qt draws the corners square. */
    border-radius: 9px;
    padding: 1px 9px;
    font-size: 9.5pt;
    font-weight: 600;
}}
#SectionAside {{ color: {C.TEXT_FAINT}; font-size: 9.5pt; }}
QPushButton#SectionLink {{
    background: transparent;
    border: none;
    padding: 4px 6px;
    color: {C.ACCENT};
    font-size: 10.5pt;
    font-weight: 600;
}}
QPushButton#SectionLink:hover {{ color: {C.ACCENT_HOVER}; text-decoration: underline; }}
#PageTitle {{ font-family: {_DISPLAY_STACK}; font-size: 30pt; font-weight: 700; }}
#PageSummary {{ color: {C.TEXT_FAINT}; font-size: 11pt; }}
#Muted {{ color: {C.TEXT_DIM}; }}
#Faint {{ color: {C.TEXT_FAINT}; font-size: 9pt; }}

/* ---------- buttons ---------- */
QPushButton {{
    background: {C.SURFACE};
    border: 1px solid {C.BORDER};
    border-radius: 12px;
    padding: 9px 17px;
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
    padding: 12px 28px;
    border-radius: 22px;
    font-size: 11.5pt;
}}
QPushButton#Primary:hover {{ background: {C.PLAY_BG_HOVER}; }}
QPushButton#Primary:pressed {{ background: {C.ACCENT_PRESSED}; }}
QPushButton#Primary:disabled {{ background: {C.SURFACE_ACTIVE}; color: {C.TEXT_FAINT}; }}

QPushButton#Ghost {{
    background: rgba(246, 240, 230, 0.12);
    border: none;
    color: {C.TEXT};
    padding: 12px 24px;
    border-radius: 22px;
    font-size: 11pt;
    font-weight: 600;
}}
QPushButton#Ghost:hover {{ background: rgba(246, 240, 230, 0.2); }}
QPushButton#Ghost:pressed {{ background: rgba(246, 240, 230, 0.08); }}

QPushButton#Chip {{
    background: transparent;
    border: 1px solid {C.BORDER_STRONG};
    /* Under half its ~30 px height: past half, Qt draws the corners square. */
    border-radius: 13px;
    padding: 6px 15px;
    color: {C.TEXT_DIM};
    font-size: 9pt;
    /* One weight for both states: a chip is sized for its unchecked text, so a
       bolder checked label would be wider than the button and get clipped. */
    font-weight: 600;
}}
QPushButton#Chip:hover {{ color: {C.TEXT}; border-color: {C.TEXT_DIM}; }}
QPushButton#Chip:checked {{
    background: {C.ACCENT};
    color: {C.ON_ACCENT};
    border-color: {C.ACCENT};
}}
/* A page's toolbar: pills 42 high (the radius under half: past half, Qt
   draws the corners square), and a segmented tray of 34-high choices. */
QPushButton#Pill {{
    background: #161311;
    border: 1px solid #2B2520;
    border-radius: 20px;
    padding: 0 16px;
    color: {C.TEXT};
    font-size: 10pt;
    font-weight: 600;
}}
QPushButton#Pill:hover {{ background: {C.SURFACE_HOVER}; border-color: {C.TEXT_FAINT}; }}
QPushButton#Pill:checked {{ border-color: {C.ACCENT}; color: {C.ACCENT}; }}
QPushButton#Pill:disabled {{ color: #5E554C; }}
QPushButton#Segment {{
    background: transparent;
    border: none;
    border-radius: 16px;
    min-height: 34px;
    max-height: 34px;
    padding: 0 15px;
    color: {C.TEXT_DIM};
    font-size: 10pt;
    font-weight: 600;
}}
QPushButton#Segment:hover {{ color: {C.TEXT}; background: rgba(246, 240, 230, 0.06); }}
QPushButton#Segment:checked {{ background: {C.ACCENT}; color: {C.ON_ACCENT}; }}
/* A chip with nothing behind it (Search's Music, with no music found). */
QPushButton#Chip:disabled {{ color: #5E554C; border-color: {C.BORDER}; }}

/* ---------- inputs ---------- */
QLineEdit {{
    background: {C.SURFACE};
    border: 1px solid {C.BORDER};
    border-radius: 14px;
    padding: 9px 14px;
    selection-background-color: {C.ACCENT};
    selection-color: {C.ON_ACCENT};
}}
QLineEdit:hover {{ border-color: {C.BORDER_STRONG}; }}
QLineEdit:focus {{ border-color: {C.ACCENT}; }}

QComboBox {{
    background: {C.SURFACE};
    border: 1px solid {C.BORDER};
    border-radius: 12px;
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

QSpinBox {{
    background: {C.SURFACE};
    border: 1px solid {C.BORDER};
    border-radius: 12px;
    padding: 6px 10px;
}}

/* The box itself is drawn by style.CozyStyle (a rounded box, a tick when
   ticked): a rule for ::indicator here would take it back from the style. */
QCheckBox {{ spacing: 10px; color: {C.TEXT}; }}

/* ---------- menus ---------- */
QMenu {{
    background: {C.BG_ELEV};
    border: 1px solid {C.BORDER_STRONG};
    border-radius: 16px;
    padding: 8px;
}}
QMenu::item {{ padding: 9px 30px 9px 14px; border-radius: 10px; color: {C.TEXT}; }}
QMenu::item:selected {{ background: {C.SURFACE_ACTIVE}; color: {C.TEXT}; }}
QMenu::item:disabled {{ color: {C.TEXT_FAINT}; }}
QMenu::item:checked {{ color: {C.ACCENT}; }}
QMenu::separator {{ height: 1px; background: {C.BORDER}; margin: 6px 10px; }}
QMenu::right-arrow {{ width: 10px; height: 10px; }}

/* ---------- scroll ---------- */
QScrollArea {{ border: none; background: transparent; }}
/* Slim and quiet: a 4 px bar in a 10 px lane, thickening under the pointer. */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {C.BORDER_STRONG};
    margin: 0 3px;
    border-radius: 2px;
    min-height: 40px;
}}
QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed {{
    background: #564B41;
    margin: 0 1px;
    border-radius: 4px;
}}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: {C.BORDER_STRONG};
    margin: 3px 0;
    border-radius: 2px;
    min-width: 40px;
}}
QScrollBar::handle:horizontal:hover, QScrollBar::handle:horizontal:pressed {{
    background: #564B41;
    margin: 1px 0;
    border-radius: 4px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- cards ---------- */
#Card {{
    background: {C.BG_ELEV};
    border: 1px solid {C.BORDER};
    border-radius: 18px;
}}
#Badge {{
    background: rgba(246, 240, 230, 0.07);
    border: 1px solid rgba(246, 240, 230, 0.14);
    border-radius: 8px;
    padding: 3px 9px;
    color: {C.TEXT_DIM};
    font-size: 8.5pt;
    font-weight: 600;
}}
#BadgeAccent {{
    background: rgba(255, 210, 63, 0.13);
    border: 1px solid rgba(255, 210, 63, 0.55);
    border-radius: 8px;
    padding: 3px 9px;
    color: {C.ACCENT};
    font-size: 8.5pt;
    font-weight: 700;
}}

QToolTip {{
    background: {C.BG_ELEV};
    color: {C.TEXT};
    border: 1px solid {C.BORDER_STRONG};
    padding: 7px 10px;
    border-radius: 10px;
}}

QSplitter::handle {{ background: {C.BORDER}; }}
"""
