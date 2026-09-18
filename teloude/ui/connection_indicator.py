# teloude/ui/connection_indicator.py
"""The Telegram-style connection icon.

One small painted button that says what the Telegram connection is doing and
opens the proxy settings when clicked. It lives in the sign-in window's corner
and, after authentication, in the main window's status bar - the same widget in
both places, fed by the same connection layer, so the state can never disagree
between the two.

States and their motion:

* disconnected - a quiet grey ring, nothing moves;
* connecting   - an accent-coloured arc rotating continuously;
* connected    - a green disc with a check mark, entering with a spring pop;
* error        - a red disc with an exclamation mark, entering with a shake.

Every change cross-fades the outgoing glyph into the new one over
``theme.MOTION_MS``, so a state change is visible without being jarring. The
widget never shows or stores the proxy secret - only the address.
"""
import logging
import math

from PySide6 import QtCore, QtGui, QtWidgets

from teloude.infrastructure.telegram.connection import ConnectionState
from teloude.ui import theme

logger = logging.getLogger("ConnectionIndicator")

# Apple's system green, the one colour the token set does not carry (the design
# system defines accent and danger only). Kept next to the states it describes.
SUCCESS = {"light": "#34C759", "dark": "#30D158"}

_SIZE = theme.MIN_TARGET          # 44px: the whole widget is the tap target
_GLYPH = 20                       # diameter of the painted glyph
_RING_WIDTH = 2
_SPIN_MS = 900
_SHAKE_MS = 420


def color_for_state(state: ConnectionState, tokens=None) -> QtGui.QColor:
    """The colour a state is painted in (light/dark aware)."""
    tokens = tokens or theme.tokens()
    if state is ConnectionState.CONNECTED:
        return QtGui.QColor(SUCCESS.get(tokens.name, SUCCESS["light"]))
    if state is ConnectionState.ERROR:
        return tokens.color("danger")
    if state is ConnectionState.CONNECTING:
        return tokens.color("accent")
    return tokens.color("label_secondary")


def state_label(state: ConnectionState) -> str:
    """One sentence per state, for tooltips and status lines."""
    return {
        ConnectionState.CONNECTED: "Connected to Telegram",
        ConnectionState.CONNECTING: "Connecting to Telegram",
        ConnectionState.ERROR: "Telegram connection failed",
        ConnectionState.DISCONNECTED: "Not connected to Telegram",
    }[state]


class ConnectionIndicator(QtWidgets.QAbstractButton):
    """Animated proxy/connection status icon; clicking it opens the settings."""

    def __init__(self, connection=None, bridge=None, parent=None):
        super().__init__(parent)
        self._connection = connection
        self._bridge = bridge
        self._state = self._read_state(connection)
        self._detail = ""
        self._phase = 0.0            # rotation of the connecting arc, 0..1
        self._mix = 1.0              # progress of the cross-fade into _state
        self._previous_state = self._state
        self._scale = 1.0            # spring pop when a state settles
        self._shake = 0.0            # horizontal offset while an error lands

        self.setObjectName("ConnectionIndicator")
        self.setFixedSize(_SIZE, _SIZE)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.TabFocus)
        self.setAccessibleName("Telegram connection")
        self.update_tooltip()

        self._spin = QtCore.QVariantAnimation(self)
        self._spin.setStartValue(0.0)
        self._spin.setEndValue(1.0)
        self._spin.setDuration(_SPIN_MS)
        self._spin.setLoopCount(-1)
        self._spin.valueChanged.connect(self._on_spin)

        self._transition = QtCore.QVariantAnimation(self)
        self._transition.setStartValue(0.0)
        self._transition.setEndValue(1.0)
        self._transition.setDuration(theme.MOTION_MS)
        self._transition.setEasingCurve(theme.motion_curve(spring=True))
        self._transition.valueChanged.connect(self._on_transition)
        self._transition.finished.connect(self._on_transition_finished)

        self._shake_animation = QtCore.QVariantAnimation(self)
        self._shake_animation.setStartValue(0.0)
        self._shake_animation.setEndValue(1.0)
        self._shake_animation.setDuration(_SHAKE_MS)
        self._shake_animation.setEasingCurve(QtCore.QEasingCurve.Type.Linear)
        self._shake_animation.valueChanged.connect(self._on_shake)
        self._shake_animation.finished.connect(self._on_shake_finished)

        if bridge is not None:
            bridge.connection_state.connect(self.apply_snapshot)

        if self._state is ConnectionState.CONNECTING:
            self._spin.start()

    # -- input ---------------------------------------------------------------
    @staticmethod
    def _read_state(connection) -> ConnectionState:
        state = getattr(connection, "state", None)
        if isinstance(state, ConnectionState):
            return state
        return ConnectionState.DISCONNECTED

    @property
    def state(self) -> ConnectionState:
        return self._state

    def set_state(self, state: ConnectionState, detail: str = "") -> None:
        """Animates into a new state (idempotent for the same state)."""
        if not isinstance(state, ConnectionState):
            state = ConnectionState(str(state))
        self._detail = detail
        if state is self._state:
            self.update_tooltip()
            self.update()
            return
        self._previous_state, self._state = self._state, state
        self.update_tooltip()
        if state is ConnectionState.CONNECTING:
            if self._spin.state() != QtCore.QAbstractAnimation.State.Running:
                self._spin.start()
        else:
            self._spin.stop()
            self._phase = 0.0
        self._mix = 0.0
        self._transition.stop()
        self._transition.start()
        if state is ConnectionState.ERROR:
            self._shake_animation.stop()
            self._shake_animation.start()
        self.update()

    @QtCore.Slot(dict)
    def apply_snapshot(self, payload: dict) -> None:
        """Slot for the service bridge's ``connection_state`` signal."""
        payload = payload or {}
        try:
            state = ConnectionState(payload.get("state", ConnectionState.DISCONNECTED.value))
        except ValueError:
            return
        self.set_state(state, payload.get("detail", "") or "")

    def refresh_from_connection(self) -> None:
        """Re-reads the layer's state (used when a window is built late)."""
        self.set_state(self._read_state(self._connection))

    # -- state of the animation (used by tests and diagnostics) --------------
    @property
    def rotation_phase(self) -> float:
        return self._phase

    @property
    def glyph_scale(self) -> float:
        return self._scale

    def is_animating(self) -> bool:
        return any(
            animation.state() == QtCore.QAbstractAnimation.State.Running
            for animation in (self._spin, self._transition, self._shake_animation)
        )

    def current_color(self) -> QtGui.QColor:
        """The colour the glyph is being painted in right now."""
        return self._blended_color()

    # -- tooltip -------------------------------------------------------------
    def update_tooltip(self) -> None:
        text = state_label(self._state)
        if self._connection is None:
            text += " (connection layer unavailable)"
        elif self._detail:
            text += f" - {self._detail}"
        self.setToolTip(f"{text} Click for proxy settings.")

    # -- painting ------------------------------------------------------------
    def _on_spin(self, value) -> None:
        self._phase = float(value)
        self.update()

    def _on_transition(self, value) -> None:
        progress = float(value)
        self._mix = min(1.0, max(0.0, progress))
        # The spring overshoots above 1.0: that is the pop, damped a little so
        # the icon never looks like it is being thrown at the user.
        self._scale = 0.82 + 0.18 * min(1.15, progress)
        self.update()

    def _on_transition_finished(self) -> None:
        self._mix = 1.0
        self._scale = 1.0
        self.update()

    def _on_shake(self, value) -> None:
        progress = float(value)
        self._shake = math.sin(progress * math.pi * 6) * (1.0 - progress) * 3.0
        self.update()

    def _on_shake_finished(self) -> None:
        self._shake = 0.0
        self.update()

    def _blended_color(self) -> QtGui.QColor:
        target = color_for_state(self._state)
        if self._mix >= 1.0:
            return target
        start = color_for_state(self._previous_state)
        blend = (start.red() + (target.red() - start.red()) * self._mix,
                 start.green() + (target.green() - start.green()) * self._mix,
                 start.blue() + (target.blue() - start.blue()) * self._mix)
        return QtGui.QColor(int(blend[0]), int(blend[1]), int(blend[2]))

    def _paint_glyph(self, painter: QtGui.QPainter, state: ConnectionState,
                     color: QtGui.QColor, alpha: float) -> None:
        """Draws one state's glyph at the widget's centre."""
        color = QtGui.QColor(color)
        color.setAlphaF(max(0.0, min(1.0, alpha)))
        pen = QtGui.QPen(color)
        pen.setWidth(_RING_WIDTH)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)

        radius = _GLYPH / 2.0
        rect = QtCore.QRectF(-radius, -radius, _GLYPH, _GLYPH)

        if state is ConnectionState.CONNECTING:
            faint = QtGui.QColor(color)
            faint.setAlphaF(color.alphaF() * 0.25)
            quiet = QtGui.QPen(faint)
            quiet.setWidth(_RING_WIDTH)
            painter.setPen(quiet)
            painter.drawEllipse(rect)
            painter.setPen(pen)
            # Qt measures angles in 1/16th of a degree, counter-clockwise.
            painter.drawArc(rect, int(-self._phase * 360 * 16), int(100 * 16))
            return

        if state is ConnectionState.CONNECTED:
            painter.setBrush(QtGui.QBrush(color))
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.drawEllipse(rect)
            check = QtGui.QPen(QtGui.QColor("#FFFFFF"))
            check.setWidthF(2.0)
            check.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            painter.setPen(check)
            painter.drawLine(QtCore.QPointF(-4.6, 0.4), QtCore.QPointF(-1.4, 3.6))
            painter.drawLine(QtCore.QPointF(-1.4, 3.6), QtCore.QPointF(4.8, -3.8))
            return

        if state is ConnectionState.ERROR:
            painter.setBrush(QtGui.QBrush(color))
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.drawEllipse(rect)
            mark = QtGui.QPen(QtGui.QColor("#FFFFFF"))
            mark.setWidthF(2.0)
            mark.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            painter.setPen(mark)
            painter.drawLine(QtCore.QPointF(0.0, -5.0), QtCore.QPointF(0.0, 1.4))
            painter.drawPoint(QtCore.QPointF(0.0, 4.6))
            return

        # Disconnected: a quiet ring, deliberately the least visible state.
        color.setAlphaF(color.alphaF() * 0.65)
        quiet = QtGui.QPen(color)
        quiet.setWidth(_RING_WIDTH)
        painter.setPen(quiet)
        painter.drawEllipse(rect)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        center = QtCore.QPointF(self.width() / 2.0 + self._shake, self.height() / 2.0)
        painter.translate(center)
        painter.scale(self._scale, self._scale)

        if self._mix < 1.0:
            self._paint_glyph(painter, self._previous_state,
                              color_for_state(self._previous_state), 1.0 - self._mix)
        self._paint_glyph(painter, self._state, self._blended_color(), 1.0 if self._mix >= 1.0 else max(0.25, self._mix))
        painter.end()
