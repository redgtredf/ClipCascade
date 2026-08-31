"""Approved design tokens for the Windows clipboard-history window.

Source of truth: the windows-clipboard-history-design artifact ("Design
system" section). Every colour, radius, spacing step and font choice in the
history UI comes from here so the widgets never hard-code hex values.

Fonts are sized in *points* (never fixed pixels for text) so Windows text
scaling from 100% to 200% grows the whole layout instead of clipping it;
the stylesheet therefore also avoids fixed heights on any text-bearing
control and uses padding + minimum sizes instead.
"""

from dataclasses import dataclass

FAMILY = "Segoe UI Variable"
FAMILY_FALLBACK = "Segoe UI"

# Body 14 px at 96 dpi == 10.5 pt; see the design's modular scale
# 12 / 14 / 16 / 20 / 24 / 32 px.
BASE_POINT_SIZE = 10.5


@dataclass(frozen=True)
class Colors:
    app_background: str = "#F4F7F6"
    surface: str = "#FFFFFF"
    text: str = "#17211F"
    muted: str = "#53645F"
    primary: str = "#086C63"
    primary_hover: str = "#055149"
    accent: str = "#C7511F"
    border: str = "#CBD7D3"
    soft: str = "#E8F2F0"
    list_background: str = "#EEF3F1"
    success: str = "#147D50"
    warning: str = "#8A5A00"
    danger: str = "#B42318"
    field_background: str = "#F9FBFA"


COLORS = Colors()


# Spacing rhythm 4 / 8 / 12 / 16 / 24 / 32 / 48 px.
SPACING_XS = 4
SPACING_S = 8
SPACING_M = 12
SPACING_L = 16
SPACING_XL = 24
SPACING_XXL = 32
SPACING_XXXL = 48

# Radii: 6 px controls, 10 px fields/buttons, 12 px cards/panels.
RADIUS_CONTROL = 6
RADIUS_FIELD = 10
RADIUS_CARD = 12

# Borders: 1 px resting, 2 px selected/focus.
BORDER_RESTING = 1
BORDER_EMPHASIS = 2

TYPE_LABELS = {
    "text": "TEXT",
    "link": "LINK",
    "image": "IMAGE",
    "files": "FILES",
}

FILE_STATE_LABELS = {
    "ready": "Ready to download",
    "downloaded": "Downloaded",
    "expired": "Expired",
    "unavailable": "Unavailable",
}

FILE_STATE_COLORS = {
    "ready": COLORS.primary,
    "downloaded": COLORS.success,
    "expired": COLORS.warning,
    "unavailable": COLORS.danger,
}


def font_family() -> str:
    return f'"{FAMILY}", "{FAMILY_FALLBACK}"'


def _pt(px_size, scale):
    """Point size for a design pixel size at the given text scale."""
    return px_size * 0.75 * scale


def build_stylesheet(scale: float = 1.0) -> str:
    """QSS derived purely from the tokens above. Text keeps point sizes
    (multiplied by `scale` to simulate Windows 100-200% text scaling);
    paddings/min-heights give controls their size so scaled text grows the
    chrome rather than being clipped by it."""
    c = COLORS
    return f"""
    QWidget {{
        background: {c.app_background};
        color: {c.text};
        font-family: {font_family()};
    }}
    QLabel#brandLabel {{
        font-size: {_pt(18, scale)}pt;
        font-weight: 600;
    }}
    QLabel#sectionLabel {{
        color: {c.muted};
        font-size: {_pt(12, scale)}pt;
        font-weight: 600;
        letter-spacing: 1px;
    }}
    QLabel#detailTitle {{
        font-size: {_pt(24, scale)}pt;
        font-weight: 600;
    }}
    QLabel#mutedLabel {{
        color: {c.muted};
        font-size: {_pt(14, scale)}pt;
    }}
    QLineEdit {{
        background: {c.field_background};
        border: 1px solid {c.border};
        border-radius: {RADIUS_FIELD}px;
        padding: 8px 14px;
        min-height: 20px;
        font-size: {_pt(14, scale)}pt;
    }}
    QLineEdit:focus {{
        border: 2px solid {c.primary};
        padding: 7px 13px;
    }}
    QPushButton {{
        background: {c.surface};
        border: 1px solid {c.border};
        border-radius: {RADIUS_FIELD}px;
        padding: 8px 12px;
        font-size: {_pt(14, scale)}pt;
        font-weight: 600;
    }}
    QPushButton:hover {{
        border-color: {c.primary};
    }}
    QPushButton:focus {{
        border: 2px solid {c.primary};
        padding: 7px 11px;
    }}
    QListView {{
        background: {c.list_background};
        border: none;
        font-size: {_pt(14, scale)}pt;
    }}
    QListView::item {{
        selection-background-color: transparent;
    }}
    QScrollArea {{
        background: {c.surface};
        border: none;
    }}
    QLabel#stateChip {{
        border-radius: {RADIUS_CONTROL}px;
        padding: 3px 10px;
        font-size: {_pt(12, scale)}pt;
        font-weight: 700;
    }}
    QLabel#warningChip {{
        background: {c.warning};
        color: #FFFFFF;
        border-radius: {RADIUS_CONTROL}px;
        padding: 3px 10px;
        font-size: {_pt(12, scale)}pt;
        font-weight: 700;
    }}
    QLabel#secureChip {{
        background: {c.success};
        color: #FFFFFF;
        border-radius: {RADIUS_CONTROL}px;
        padding: 3px 10px;
        font-size: {_pt(12, scale)}pt;
        font-weight: 700;
    }}
    QFrame#paneSurface {{
        background: {c.surface};
        border: 1px solid {c.border};
        border-radius: {RADIUS_CARD}px;
    }}
    QFrame#footer {{
        background: {c.surface};
        border-top: 1px solid {c.border};
    }}
    """


def nav_button_stylesheet(scale: float = 1.0) -> str:
    """Checkable nav buttons: rest = transparent, active = soft teal with
    primary text; state is conveyed by weight and background, never colour
    alone (the check state is also exposed to accessibility)."""
    return f"""
    QPushButton[navButton="true"] {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: {RADIUS_CONTROL}px;
        padding: 8px 10px;
        text-align: left;
        font-weight: 400;
        font-size: {_pt(14, scale)}pt;
    }}
    QPushButton[navButton="true"]:hover {{
        background: {COLORS.soft};
    }}
    QPushButton[navButton="true"]:checked {{
        background: {COLORS.soft};
        color: {COLORS.primary};
        font-weight: 600;
        border: 1px solid {COLORS.border};
    }}
    QPushButton[navButton="true"]:focus {{
        border: 2px solid {COLORS.primary};
        padding: 7px 9px;
    }}
    """
