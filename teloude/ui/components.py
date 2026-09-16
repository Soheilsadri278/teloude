# teloude/ui/components.py
"""The Liquid Glass surfaces: navigation rail, floating bars, dialogs, scrim.

Each surface here is a *component*, not a one-off: the rail paints the same
capsule everywhere it is used, the floating bar carries the same material and
the same lift, and every modal overlay dims the app through one shared scrim.
That keeps the treatment consistent and makes it testable in one place.

Native Qt, no imitation:

* Translucency comes from a translucent stylesheet fill on a widget that is a
  child of the window (`WA_StyledBackground`), so Qt composites it over the
  window's own painted canvas. Nothing is screenshotted and nothing is blurred
  offscreen - the one thing Qt cannot do (blurring the pixels *behind* a widget)
  is simply not attempted.
* Depth comes from one `QGraphicsDropShadowEffect` plus a hairline border and a
  soft top highlight. Shadows cost an offscreen render pass, so there are
  exactly two in the whole application: the rail panel carries none, the
  floating bar carries one, dialogs rely on the window manager's own shadow.
* Motion is a `QVariantAnimation` on the 300ms standard curve / spring curve.
  Targets are set immediately, so a headless run (no event loop) ends in the
  correct final state instead of an unfinished transition.
"""
from __future__ import annotations

import contextlib
from typing import Optional

from PySide6 import QtCore, QtGui, QtWidgets

from teloude.ui import theme

GLASS = "GlassSurface"
NAV_PANEL = "NavPanel"
GLASS_CARD = "GlassCard"
SCRIM = "Scrim"


def _styled(widget: QtWidgets.QWidget, name: str) -> QtWidgets.QWidget:
    widget.setObjectName(name)
    widget.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
    return widget


class GlassPanel(QtWidgets.QFrame):
    """A translucent chrome surface (navigation rail, toolbar)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        _styled(self, NAV_PANEL)


class GlassCard(QtWidgets.QFrame):
    """The dialog surface: opaque fill, hairline border, inner highlight.

    Opaque on purpose - a top-level window cannot be translucent without a
    frameless window, and a fake one would trade readability for an effect.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        _styled(self, GLASS_CARD)


class FloatingBar(QtWidgets.QFrame):
    """The action bar that floats above a page while a transfer runs.

    Idle: a soft, short shadow. Running: the shadow deepens and drops - the
    controls visibly sit on a higher layer while something is happening. Only
    depth changes; nothing moves, so the buttons never jump under the pointer.
    """

    IDLE_BLUR = 16.0
    LIFTED_BLUR = 32.0
    IDLE_OFFSET = 4.0
    LIFTED_OFFSET = 8.0

    def __init__(self, parent=None):
        super().__init__(parent)
        _styled(self, GLASS)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                           QtWidgets.QSizePolicy.Policy.Fixed)
        self._layout = QtWidgets.QHBoxLayout(self)
        self._layout.setContentsMargins(*([theme.SPACING["sm"]] * 4))
        self._layout.setSpacing(theme.SPACING["sm"])

        t = theme.tokens()
        self._effect = QtWidgets.QGraphicsDropShadowEffect(self)
        self._effect.setColor(theme.parse_color(t.shadow))
        self._effect.setBlurRadius(self.IDLE_BLUR)
        self._effect.setOffset(0, self.IDLE_OFFSET)
        self.setGraphicsEffect(self._effect)

        self._lift = 0.0        # 0.0 idle ... 1.0 lifted
        self._animation: Optional[QtCore.QVariantAnimation] = None

    # -- contents ---------------------------------------------------------
    def add_widget(self, widget: QtWidgets.QWidget) -> None:
        self._layout.addWidget(widget)

    def add_stretch(self) -> None:
        self._layout.addStretch(1)

    # -- lift -------------------------------------------------------------
    def is_lifted(self) -> bool:
        return self._lift >= 0.5

    def set_lifted(self, lifted: bool) -> None:
        """Sets the target depth and animates to it (final state is immediate)."""
        target = 1.0 if lifted else 0.0
        self._lift = target
        self._apply(target)
        if self._animation is not None:
            self._animation.stop()
        start = max(0.0, 1.0 - target)
        self._animation = QtCore.QVariantAnimation(self)
        self._animation.setStartValue(start)
        self._animation.setEndValue(target)
        self._animation.setDuration(theme.LIFT_MS)
        self._animation.setEasingCurve(theme.motion_curve())
        self._animation.valueChanged.connect(lambda value: self._apply(float(value)))
        self._animation.start()

    def _apply(self, progress: float) -> None:
        t = theme.tokens()
        alpha = t.shadow[3] + progress * (t.shadow_strong[3] - t.shadow[3])
        self._effect.setColor(QtGui.QColor(t.shadow[0], t.shadow[1], t.shadow[2], int(alpha)))
        self._effect.setBlurRadius(self.IDLE_BLUR + progress * (self.LIFTED_BLUR - self.IDLE_BLUR))
        self._effect.setOffset(0, self.IDLE_OFFSET + progress * (self.LIFTED_OFFSET - self.IDLE_OFFSET))


class Scrim(QtWidgets.QWidget):
    """One dimming layer per window, shared by every modal surface.

    Purely visual: it never carries text and never receives mouse events (the
    modal dialog it sits under already owns the interaction), so putting it up
    cannot change what a click does.
    """

    def __init__(self, window: QtWidgets.QWidget):
        super().__init__(window)
        _styled(self, SCRIM)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setGeometry(window.rect())
        self.hide()
        self._depth = 0
        window.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.parent() and event.type() in (
            QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.Show,
        ):
            self.setGeometry(self.parent().rect())
        return False

    def push(self) -> None:
        self._depth += 1
        self.setGeometry(self.parent().rect())
        self.raise_()
        self.show()

    def pop(self) -> None:
        self._depth = max(0, self._depth - 1)
        if self._depth == 0:
            self.hide()

    @property
    def depth(self) -> int:
        return self._depth


def scrim_for(window: QtWidgets.QWidget) -> Scrim:
    """The window's scrim, created on first use."""
    existing = getattr(window, "_teloude_scrim", None)
    if isinstance(existing, Scrim):
        return existing
    scrim = Scrim(window)
    setattr(window, "_teloude_scrim", scrim)
    return scrim


def _host_window(parent) -> Optional[QtWidgets.QWidget]:
    if parent is None:
        parent = QtWidgets.QApplication.activeWindow()
    if isinstance(parent, Scrim):
        parent = parent.parent()
    if isinstance(parent, QtWidgets.QWidget):
        window = parent.window()
        if isinstance(window, (QtWidgets.QMainWindow, QtWidgets.QDialog)):
            return window
    return None


@contextlib.contextmanager
def with_scrim(parent):
    """Dims the window behind a modal surface for the duration of the block."""
    window = _host_window(parent)
    scrim = scrim_for(window) if window is not None else None
    if scrim is not None:
        scrim.push()
    try:
        yield scrim
    finally:
        if scrim is not None:
            scrim.pop()


def present_blocking(dialog: QtWidgets.QDialog, parent=None) -> int:
    """Runs a modal dialog with the app dimmed behind it.

    Same blocking contract as `QMessageBox.critical(...)`: returns the dialog's
    result code.
    """
    with with_scrim(parent if parent is not None else dialog.parent()):
        return dialog.exec()


def fade_in(widget: QtWidgets.QWidget, lift_px: int = 8,
            layout: Optional[QtWidgets.QLayout] = None) -> None:
    """The spring entry for a dialog surface: opacity plus a small lift.

    The final state is written first, so if no animation ever runs (headless
    tests, a stalled event loop) the widget is fully visible rather than stuck
    mid-transition.
    """
    effect = QtWidgets.QGraphicsOpacityEffect(widget)
    effect.setOpacity(1.0)
    widget.setGraphicsEffect(effect)
    animation = QtCore.QVariantAnimation(widget)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setDuration(theme.MOTION_MS)
    animation.setEasingCurve(theme.motion_curve(spring=True))
    animation.valueChanged.connect(lambda value: effect.setOpacity(float(value)))
    animation.start()
    if layout is not None:
        base = layout.contentsMargins()
        animation.valueChanged.connect(
            lambda value: layout.setContentsMargins(
                base.left(), base.top() + int(round(lift_px * (1.0 - float(value)))),
                base.right(), base.bottom()
            )
        )
    widget._teloude_entry_animation = animation  # kept alive for the duration


class NavItemDelegate(QtWidgets.QStyledItemDelegate):
    """Paints the rail's rows: text, a hover wash, and the selection capsule.

    Text only - no badges, no icons and no decorative dots. The selected row is
    a translucent accent capsule with a hairline edge and a soft top highlight,
    which is the same material language as the other chrome surfaces.
    """

    def sizeHint(self, option, index) -> QtCore.QSize:
        size = super().sizeHint(option, index)
        return QtCore.QSize(size.width(), max(theme.MIN_TARGET, size.height()))

    def paint(self, painter: QtGui.QPainter, option, index) -> None:
        t = theme.tokens()
        selected = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QtWidgets.QStyle.StateFlag.State_MouseOver)

        rect = QtCore.QRect(option.rect)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        if selected:
            painter.setPen(QtGui.QPen(theme.parse_color(t.accent_tint_border), 1))
            painter.setBrush(theme.parse_color(t.accent_tint))
            painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1),
                                    theme.RADIUS_SMALL, theme.RADIUS_SMALL)
            painter.setPen(QtGui.QPen(theme.parse_color(t.highlight), 1))
            painter.drawLine(rect.left() + theme.SPACING["xs"], rect.top() + 1,
                             rect.right() - theme.SPACING["xs"], rect.top() + 1)
        elif hovered:
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(theme.parse_color(t.hover))
            painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1),
                                    theme.RADIUS_SMALL, theme.RADIUS_SMALL)

        painter.setPen(theme.parse_color(t.label))
        text_rect = rect.adjusted(theme.SPACING["sm"], 0, -theme.SPACING["sm"], 0)
        painter.drawText(text_rect,
                         int(QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft),
                         str(index.data() or ""))
        painter.restore()


class NavRail(QtWidgets.QListWidget):
    """The navigation list: clean, text-only, 44px rows, capsule selection."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("NavRail")
        self.setItemDelegate(NavItemDelegate(self))
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.setMouseTracking(True)
        self.viewport().setAttribute(QtCore.Qt.WidgetAttribute.WA_Hover, True)

    def add_page(self, title: str) -> QtWidgets.QListWidgetItem:
        """Adds one page row. Deliberately text-only: no icon, no decoration."""
        item = QtWidgets.QListWidgetItem(title)
        item.setSizeHint(QtCore.QSize(0, theme.MIN_TARGET))
        self.addItem(item)
        return item


def make_action_bar(*buttons: QtWidgets.QWidget) -> FloatingBar:
    """A floating bar holding a page's action buttons."""
    bar = FloatingBar()
    for button in buttons:
        bar.add_widget(button)
    bar.add_stretch()
    return bar
