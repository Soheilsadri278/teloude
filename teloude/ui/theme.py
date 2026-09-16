# teloude/ui/theme.py
"""Design tokens and global styling (Apple UI Design System, applied to PySide6).

One module owns every visual constant, so the whole application can be re-skinned
in one place and the values can be checked mechanically. The tokens follow the
system the project adopted: 8pt spacing grid, radii 8/12/20 (small / control /
card), 44px minimum interactive targets, SF Pro with a native system fallback,
`#007AFF` (light) and `#0A84FF` (dark) accents, 300ms motion with the standard
and spring curves, and - where a surface is allowed to be translucent - the
"Liquid Glass" material: a translucent fill with a hairline border and a soft
inner highlight.

What this module deliberately does NOT do:

* It never fakes a backdrop blur. Qt cannot blur the pixels *behind* a widget,
  so the glass surfaces here are translucent fills composited over the window's
  own painted canvas (which Qt does natively and exactly), never screenshots or
  offscreen blur passes.
* It does not restyle content. Cards, lists, tables and the transfer area keep
  an opaque fill: text must never sit on something that scrolls underneath it.

Light mode is the default; dark mode is the same token set with Apple's dark
values. Everything is applied by `apply_theme()`; nothing is applied at import
time, so importing this module in a test never needs a QApplication.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets

logger = logging.getLogger("TeloudeTheme")

# --------------------------------------------------------------- tokens ------
BASE = 8                        # the 8pt grid
SPACING = {"xs": 8, "sm": 16, "md": 24, "lg": 32}
RADIUS_SMALL = 8                # badges, fields, small elements
RADIUS_CONTROL = 12             # buttons, bars
RADIUS_CARD = 20                # cards, lists, modals
RADII = (RADIUS_SMALL, RADIUS_CONTROL, RADIUS_CARD)
MIN_TARGET = 44                 # minimum height of anything clickable
FIELD_HEIGHT = 36               # inputs and search fields
NAV_WIDTH = 160                 # unchanged: the rail keeps its existing width
FIELD_MAX = 288                 # 36 x 8: a settings field, not a full-width bar
BUTTON_MAX = 320                # primary buttons stop growing at 40 x 8

MOTION_MS = 300
LIFT_MS = 300

# SF Pro on Apple hardware, the native system face everywhere else. The skill
# forbids substituting a web font (Inter/Roboto/Helvetica/Arial), so the
# fallback chain ends at the platform's own UI font.
FONT_FAMILIES_DISPLAY = ("SF Pro Display", "SF Pro Text", "Segoe UI Variable Display",
                         "Segoe UI", "Noto Sans", "sans-serif")
FONT_FAMILIES_TEXT = ("SF Pro Text", "SF Pro Display", "Segoe UI Variable Text",
                      "Segoe UI", "Noto Sans", "sans-serif")
FORBIDDEN_FAMILIES = ("inter", "roboto", "helvetica", "arial", "open sans")

HEADLINE_PX = 26                # unchanged size of the dashboard title
SECTION_PX = 15
CAPTION_PX = 12


def _rgba(rgb: Tuple[int, int, int], alpha: float) -> str:
    red, green, blue = rgb
    return f"rgba({red}, {green}, {blue}, {int(round(alpha * 255))})"


@dataclass(frozen=True)
class Tokens:
    """Resolved values for one appearance (light or dark)."""

    name: str
    canvas: str                 # window background
    surface: str                # opaque content surfaces
    field: str
    hover: str
    separator: str
    separator_soft: str
    highlight: str              # the inner top highlight of a glass surface
    glass: str                  # navigation / toolbar material
    glass_strong: str
    label: str
    label_secondary: str
    accent: str
    accent_text: str
    accent_tint: str
    accent_tint_border: str
    on_accent: str
    scrim: str
    danger: str
    shadow: Tuple[int, int, int, int] = field(default=(0, 0, 0, 20))
    shadow_strong: Tuple[int, int, int, int] = field(default=(0, 0, 0, 46))

    # convenience for painting / effects --------------------------------
    def color(self, token: str) -> QtGui.QColor:
        value = getattr(self, token)
        return parse_color(value)

    @property
    def is_dark(self) -> bool:
        return self.name == "dark"


LIGHT = Tokens(
    name="light",
    canvas="#F2F2F7",
    surface="#FFFFFF",
    field=_rgba((0, 0, 0), 0.05),
    hover=_rgba((0, 0, 0), 0.04),
    separator=_rgba((0, 0, 0), 0.10),
    separator_soft=_rgba((0, 0, 0), 0.08),
    highlight=_rgba((255, 255, 255), 0.60),
    glass=_rgba((255, 255, 255), 0.72),
    glass_strong=_rgba((255, 255, 255), 0.92),
    label="#000000",
    label_secondary="#6E6E73",
    accent="#007AFF",
    accent_text="#0055B8",
    accent_tint=_rgba((0, 122, 255), 0.14),
    accent_tint_border=_rgba((0, 122, 255), 0.24),
    on_accent="#FFFFFF",
    scrim=_rgba((0, 0, 0), 0.20),
    danger="#FF3B30",
    shadow=(0, 0, 0, 20),
    shadow_strong=(0, 0, 0, 46),
)

DARK = Tokens(
    name="dark",
    canvas="#000000",
    surface="#1C1C1E",
    field=_rgba((255, 255, 255), 0.10),
    hover=_rgba((255, 255, 255), 0.06),
    separator=_rgba((255, 255, 255), 0.15),
    separator_soft=_rgba((255, 255, 255), 0.12),
    highlight=_rgba((255, 255, 255), 0.08),
    glass=_rgba((28, 28, 30), 0.70),
    glass_strong=_rgba((28, 28, 30), 0.92),
    label="#FFFFFF",
    label_secondary="#A1A1A6",
    accent="#0A84FF",
    accent_text="#409CFF",
    accent_tint=_rgba((10, 132, 255), 0.16),
    accent_tint_border=_rgba((10, 132, 255), 0.32),
    on_accent="#FFFFFF",
    scrim=_rgba((0, 0, 0), 0.45),
    danger="#FF453A",
    shadow=(0, 0, 0, 90),
    shadow_strong=(0, 0, 0, 140),
)

_APPEARANCE_KEY = "ui.appearance"
_current: Tokens = LIGHT


# ---------------------------------------------------------------- helpers ----
def parse_color(value) -> QtGui.QColor:
    """`#RRGGBB` or `rgba(r, g, b, a)` -> QColor (alpha included)."""
    if isinstance(value, QtGui.QColor):
        return QtGui.QColor(value)
    text = str(value).strip()
    if text.startswith("#"):
        return QtGui.QColor(text)
    if text.startswith("rgba("):
        parts = [int(part.strip()) for part in text[5:-1].split(",")]
        red, green, blue, alpha = parts
        return QtGui.QColor(red, green, blue, alpha)
    return QtGui.QColor(text)


def tokens(dark: Optional[bool] = None) -> Tokens:
    """The active token set (or one of the two explicitly)."""
    if dark is None:
        return _current
    return DARK if dark else LIGHT


def motion_curve(spring: bool = False) -> QtCore.QEasingCurve:
    """The 300ms standard curve, or the spring used for dialogs."""
    curve = QtCore.QEasingCurve(QtCore.QEasingCurve.Type.BezierSpline)
    if spring:
        curve.addCubicBezierSegment(
            QtCore.QPointF(0.4, 0.0), QtCore.QPointF(0.2, 1.4), QtCore.QPointF(1.0, 1.0)
        )
    else:
        curve.addCubicBezierSegment(
            QtCore.QPointF(0.25, 0.1), QtCore.QPointF(0.25, 1.0), QtCore.QPointF(1.0, 1.0)
        )
    return curve


def font(families=FONT_FAMILIES_TEXT, size_px: Optional[int] = None,
         weight: Optional[QtGui.QFont.Weight] = None,
         tracking_em: float = 0.0) -> QtGui.QFont:
    """A QFont with the family fallback chain and optional optical tracking."""
    result = QtGui.QFont()
    result.setFamilies([name for name in families])
    if size_px:
        result.setPixelSize(int(size_px))
    if weight is not None:
        result.setWeight(weight)
    if tracking_em:
        result.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing,
                                round(tracking_em * (size_px or 13), 2))
    return result


def apply_role(widget: QtWidgets.QWidget, role: str, indent_px: int = 0) -> QtWidgets.QWidget:
    """Applies one typographic role: `headline`, `section` or `caption`.

    Only the three roles the app actually needs exist, so the existing
    hierarchy stays intact: the page title keeps its size, section labels get a
    little more weight, captions stay small. Colours come from the stylesheet.
    """
    if role == "headline":
        widget.setFont(font(FONT_FAMILIES_DISPLAY, HEADLINE_PX, QtGui.QFont.Weight.DemiBold,
                            tracking_em=-0.022))
    elif role == "section":
        widget.setFont(font(FONT_FAMILIES_TEXT, SECTION_PX, QtGui.QFont.Weight.DemiBold,
                            tracking_em=-0.022))
    elif role == "caption":
        widget.setFont(font(FONT_FAMILIES_TEXT, CAPTION_PX, QtGui.QFont.Weight.Normal,
                            tracking_em=-0.011))
    else:
        raise ValueError(f"Unknown typographic role: {role}")
    if indent_px:
        widget.setContentsMargins(indent_px, 0, 0, 0)
    return widget


def soft_shadow(widget: QtWidgets.QWidget, color, blur: int, offset_y: int,
                parent=None) -> QtWidgets.QGraphicsDropShadowEffect:
    """A restrained drop shadow (one effect, one offscreen pass)."""
    effect = QtWidgets.QGraphicsDropShadowEffect(parent or widget)
    effect.setColor(parse_color(color))
    effect.setBlurRadius(blur)
    effect.setOffset(0, offset_y)
    widget.setGraphicsEffect(effect)
    return effect


# ------------------------------------------------------------ 8pt spacing ----
def snap(value: int) -> int:
    """Snaps a margin/spacing to the 8pt grid; 0 stays 0, anything else >= 8."""
    if value <= 0:
        return 0
    return max(BASE, int(value / BASE + 0.5) * BASE)


def _snap_layout(layout: QtWidgets.QLayout) -> int:
    changed = 0
    margins = layout.contentsMargins()
    snapped = (snap(margins.left()), snap(margins.top()),
               snap(margins.right()), snap(margins.bottom()))
    if snapped != (margins.left(), margins.top(), margins.right(), margins.bottom()):
        layout.setContentsMargins(*snapped)
        changed += 1
    if layout.spacing() >= 0 and snap(layout.spacing()) != layout.spacing():
        layout.setSpacing(snap(layout.spacing()))
        changed += 1
    if isinstance(layout, QtWidgets.QFormLayout):
        for getter, setter in ((layout.horizontalSpacing, layout.setHorizontalSpacing),
                               (layout.verticalSpacing, layout.setVerticalSpacing)):
            current = getter()
            if current >= 0 and snap(current) != current:
                setter(snap(current))
                changed += 1
    return changed


def _snap_layout_tree(layout: QtWidgets.QLayout) -> int:
    """Snaps one layout and every layout nested inside it."""
    changed = _snap_layout(layout)
    for child in layout.children():
        if isinstance(child, QtWidgets.QLayout):
            changed += _snap_layout_tree(child)
    return changed


def normalize_layout_spacing(root: QtWidgets.QWidget) -> int:
    """Snaps every margin/spacing below `root` onto the 8pt grid.

    Layout properties only: no widget is resized, re-parented or re-ordered and
    no signal is touched, so this cannot change behaviour - it only removes the
    platform's default 9/11px gaps that would break the grid. Layouts nested in
    other layouts (a row of buttons inside a page column) are included.
    """
    changed = 0
    pending = [root]
    seen = set()
    while pending:
        widget = pending.pop()
        if id(widget) in seen:
            continue
        seen.add(id(widget))
        layout = widget.layout()
        if layout is not None:
            changed += _snap_layout_tree(layout)
        for child in widget.children():
            if isinstance(child, QtWidgets.QWidget):
                pending.append(child)
    return changed


# -------------------------------------------------------------- stylesheet ---
def build_stylesheet(dark: bool = False) -> str:
    """The global stylesheet for one appearance.

    Selectors are scoped by object name for the glass surfaces (`NavPanel`,
    `GlassSurface`, `GlassCard`, `Scrim`) so content widgets keep the opaque
    treatment this system requires.
    """
    t = DARK if dark else LIGHT
    return f"""
/* Teloude - Apple UI Design System: 8pt grid, SF Pro / native fallback,
   material depth on chrome only, spring motion. Generated by ui/theme.py. */
QWidget {{ color: {t.label}; }}
QMainWindow, QDialog, QWidget#Canvas {{ background: {t.canvas}; }}

/* --- material 1: navigation rail (translucent over the window canvas) ----- */
QFrame#NavPanel {{
    background: {t.glass};
    border: 0;
    border-right: 1px solid {t.separator};
    border-top: 1px solid {t.highlight};
}}
QListWidget#NavRail {{
    background: transparent;
    border: 0;
    outline: 0;
    padding: {SPACING['xs']}px;
}}
QListWidget#NavRail::item {{ color: {t.label}; }}

/* --- material 2: floating action bars ------------------------------------ */
QFrame#GlassSurface {{
    background: {t.glass};
    border: 1px solid {t.separator};
    border-top: 1px solid {t.highlight};
    border-radius: {RADIUS_CONTROL}px;
}}

/* --- material 3: dialog surface (opaque: a top-level window cannot be
       translucent without a frameless window, which this project rules out) - */
QFrame#GlassCard {{
    background: {t.surface};
    border: 1px solid {t.separator};
    border-top: 1px solid {t.highlight};
    border-radius: {RADIUS_CARD}px;
}}
QWidget#Scrim {{ background: {t.scrim}; }}

/* --- content: opaque, hairline borders, radius from the scale ------------- */
QListWidget, QTreeWidget, QTableView, QTableWidget, QListView, QTreeView {{
    background: {t.surface};
    border: 1px solid {t.separator};
    border-radius: {RADIUS_CARD}px;
    padding: {SPACING['xs']}px;
    outline: 0;
}}
QListWidget#NavRail {{ border-radius: 0; border: 0; }}
QHeaderView::section {{
    background: transparent;
    border: 0;
    border-bottom: 1px solid {t.separator};
    padding: {SPACING['xs']}px;
    color: {t.label_secondary};
}}
QTextEdit, QPlainTextEdit {{
    background: {t.surface};
    border: 1px solid {t.separator};
    border-radius: {RADIUS_CARD}px;
    padding: {SPACING['xs']}px;
}}
QGroupBox {{
    background: {t.surface};
    border: 1px solid {t.separator};
    border-radius: {RADIUS_CARD}px;
    margin-top: {SPACING['sm']}px;
    padding: {SPACING['md']}px {SPACING['xs']}px {SPACING['xs']}px {SPACING['xs']}px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: {SPACING['sm']}px;
    padding: 0 {SPACING['xs']}px;
    color: {t.label_secondary};
}}

/* --- controls: 44px targets, 12px radius, hairline borders --------------- */
QPushButton {{
    min-height: {MIN_TARGET}px;
    max-width: {BUTTON_MAX}px;
    padding: 0 {SPACING['md']}px;
    border-radius: {RADIUS_CONTROL}px;
    background: {t.surface};
    border: 1px solid {t.separator_soft};
    color: {t.label};
}}
QPushButton:hover {{ background: {t.hover}; }}
QPushButton:pressed {{ background: {t.field}; }}
QPushButton:disabled {{ color: {t.label_secondary}; border-color: {t.separator_soft}; }}
QPushButton[primary="true"] {{
    background: {t.accent};
    border: 1px solid {t.accent};
    color: {t.on_accent};
}}
QPushButton[primary="true"]:hover {{ background: {t.accent_text}; }}
QPushButton[primary="true"]:disabled {{
    background: {t.field};
    border-color: {t.separator_soft};
    color: {t.label_secondary};
}}
QPushButton:focus {{ outline: 0; border: 1px solid {t.accent}; }}
QPushButton[primary="true"]:focus {{ border: 1px solid {t.on_accent}; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit {{
    min-height: {FIELD_HEIGHT}px;
    max-width: {FIELD_MAX}px;
    padding: 0 {SPACING['xs']}px;
    border-radius: {RADIUS_SMALL}px;
    background: {t.field};
    border: 1px solid {t.separator_soft};
    color: {t.label};
    selection-background-color: {t.accent};
    selection-color: {t.on_accent};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {t.accent};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {t.label_secondary};
}}
QComboBox QAbstractItemView {{
    background: {t.surface};
    border: 1px solid {t.separator};
    border-radius: {RADIUS_SMALL}px;
    selection-background-color: {t.accent};
    selection-color: {t.on_accent};
    padding: {SPACING['xs']}px;
}}
QCheckBox, QRadioButton {{ spacing: {SPACING['xs']}px; }}

QProgressBar {{
    height: {SPACING['sm']}px;
    border-radius: {RADIUS_SMALL}px;
    background: {t.field};
    border: 0;
    text-align: center;
    color: {t.label};
}}
QProgressBar::chunk {{ background: {t.accent}; border-radius: {RADIUS_SMALL}px; }}

QSlider::groove:horizontal {{ height: {SPACING['xs']}px; border-radius: 4px; background: {t.field}; }}
QSlider::handle:horizontal {{
    width: {SPACING['md']}px; margin: -8px 0; border-radius: 12px; background: {t.accent};
}}

/* --- chrome: status bar, toolbars, splitter, scrollbars, tooltips -------- */
QStatusBar {{
    background: {t.glass};
    border-top: 1px solid {t.separator};
    color: {t.label_secondary};
    padding: 0 {SPACING['sm']}px;
}}
QStatusBar::item {{ border: 0; }}
QToolBar {{ background: {t.glass}; border: 0; border-bottom: 1px solid {t.separator};
            padding: {SPACING['xs']}px; spacing: {SPACING['xs']}px; }}
QSplitter::handle {{ background: {t.separator}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QSplitter::handle:hover {{ background: {t.accent}; }}
QScrollBar:vertical {{ background: transparent; width: {SPACING['xs']}px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: {SPACING['xs']}px; margin: 0; }}
QScrollBar::handle {{ background: {t.separator}; border-radius: 4px; min-height: {SPACING['lg']}px; }}
QScrollBar::handle:hover {{ background: {t.label_secondary}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent; height: 0; width: 0;
}}
QToolTip {{
    background: {t.surface};
    color: {t.label};
    border: 1px solid {t.separator};
    border-radius: {RADIUS_SMALL}px;
    padding: {SPACING['xs']}px;
}}
QMenu {{ background: {t.surface}; border: 1px solid {t.separator}; padding: {SPACING['xs']}px; }}
QMenu::item {{ padding: {SPACING['xs']}px {SPACING['sm']}px; border-radius: {RADIUS_SMALL}px; }}
QMenu::item:selected {{ background: {t.accent}; color: {t.on_accent}; }}
QMessageBox {{ background: {t.canvas}; }}
QMessageBox QLabel {{ color: {t.label}; }}
QMessageBox QPushButton {{ min-height: {MIN_TARGET}px; }}
QLabel[role="secondary"] {{ color: {t.label_secondary}; }}
QLabel[role="danger"] {{ color: {t.danger}; }}
"""


def apply_theme(app: QtWidgets.QApplication, dark: bool = False) -> Tokens:
    """Applies the tokens, the font stack and the stylesheet to a QApplication.

    Idempotent and cheap: call it once at startup and again whenever the
    appearance changes. Returns the token set that is now active.
    """
    global _current
    resolved = DARK if dark else LIGHT
    _current = resolved

    app.setStyleSheet(build_stylesheet(dark))
    app.setFont(font(FONT_FAMILIES_TEXT))
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, parse_color(resolved.canvas))
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, parse_color(resolved.label))
    palette.setColor(QtGui.QPalette.ColorRole.Base, parse_color(resolved.surface))
    palette.setColor(QtGui.QPalette.ColorRole.Text, parse_color(resolved.label))
    palette.setColor(QtGui.QPalette.ColorRole.Button, parse_color(resolved.surface))
    palette.setColor(QtGui.QPalette.ColorRole.ButtonText, parse_color(resolved.label))
    palette.setColor(QtGui.QPalette.ColorRole.Highlight, parse_color(resolved.accent))
    palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, parse_color(resolved.on_accent))
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipBase, parse_color(resolved.surface))
    palette.setColor(QtGui.QPalette.ColorRole.ToolTipText, parse_color(resolved.label))
    palette.setColor(QtGui.QPalette.ColorRole.PlaceholderText, parse_color(resolved.label_secondary))
    app.setPalette(palette)
    return resolved


def stored_appearance(services) -> str:
    """The persisted appearance choice ('light' by default)."""
    try:
        value = services.settings.get(_APPEARANCE_KEY, "light")
    except Exception:  # a broken settings row must never block startup
        return "light"
    return "dark" if str(value).strip().lower() == "dark" else "light"


def remember_appearance(services, appearance: str) -> str:
    """Persists the appearance choice through the existing settings service."""
    choice = "dark" if str(appearance).strip().lower() == "dark" else "light"
    try:
        services.settings.set(_APPEARANCE_KEY, choice)
    except Exception as exc:
        logger.warning(f"Could not store the appearance preference: {exc}")
    return choice


def make_primary(button: QtWidgets.QPushButton) -> QtWidgets.QPushButton:
    """Marks a button as the single primary action of its surface."""
    button.setProperty("primary", True)
    return button


# Backwards-compatible aliases used by older call sites in this package.
APPEARANCE_KEY = _APPEARANCE_KEY
TOKEN_SETS: Dict[str, Tokens] = {"light": LIGHT, "dark": DARK}
