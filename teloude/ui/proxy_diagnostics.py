# teloude/ui/proxy_diagnostics.py
"""Step-by-step tracing of the proxy sheet, for the packaged Windows build.

A frozen, windowed PyInstaller build has no console: ``sys.stderr`` is not a
terminal, Qt's own warnings (``QPainter::begin: A paint device can only be
painted by one painter at a time``) go nowhere, and an exception raised inside
a Qt slot is printed by PySide6 and then *swallowed* - the click simply appears
to do nothing. This module is the only way to see what really happens between

    mouse click -> signal -> open_proxy_settings -> ProxyDialog() -> present -> visible

Everything is written to ``%LOCALAPPDATA%\\Teloude\\startup_error.log`` (the path
the handoff asks for; ``TELOUDE_DIAG_LOG`` overrides it). Writing is best
effort: diagnostics must never be the reason the dialog fails to open.

SECURITY: this module never records a proxy secret, an API hash, an API id, a
session string or a phone number. It records widget geometry, window flags,
types and visibility only. ``scrub()`` is applied to every exception text and
every free-form message before it reaches the file, so a secret that leaked
into an exception string cannot land in the log either.
"""
import logging
import os
import platform
import re
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ProxyDiagnostics")

_LOCK = threading.Lock()
_ENABLED_CACHE: Optional[bool] = None

# Anything that looks like a credential is replaced before it is written.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(secret|api_hash|apihash|api_id|apiid|token|password|phone)\b"
               r"\s*[:=]\s*\S+"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),          # hex secrets / api hashes
    re.compile(r"\b[A-Za-z0-9+/]{22,}={0,2}\b"),  # base64 secrets
    re.compile(r"(?<![\w.])\+\d[\d\s\-()]{6,}\d"),  # phone numbers
)


def scrub(text: object) -> str:
    """Removes anything that could be a credential from a diagnostic string."""
    value = str(text)
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("<redacted>", value)
    return value


def log_path() -> Path:
    """Where the diagnostic log lives (``%LOCALAPPDATA%\\Teloude`` on Windows)."""
    override = os.environ.get("TELOUDE_DIAG_LOG")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "Teloude" / "startup_error.log"
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "Teloude" / "startup_error.log"


def enabled() -> bool:
    """Whether the proxy flow should be traced.

    On by default in a frozen build - that is the build nobody can attach a
    console to, and one 30-line trace per click costs nothing. Elsewhere it is
    opt-in via ``TELOUDE_DIAG=1``. ``TELOUDE_DIAG=0`` turns it off everywhere.
    """
    global _ENABLED_CACHE
    flag = os.environ.get("TELOUDE_DIAG")
    if flag is not None:
        return flag.strip().lower() not in ("", "0", "false", "no")
    if _ENABLED_CACHE is None:
        _ENABLED_CACHE = bool(getattr(sys, "frozen", False))
    return _ENABLED_CACHE


def record(step: str, **fields) -> None:
    """Appends one scrubbed ``key=value`` line for a step of the flow."""
    if not enabled():
        return
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        parts = " ".join(f"{key}={scrub(value)}" for key, value in fields.items())
        line = f"{stamp} PROXY {step}" + (f" {parts}" if parts else "")
        target = log_path()
        with _LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:  # pragma: no cover - diagnostics must never break the app
        logger.debug("Could not write the proxy diagnostic line.", exc_info=True)


def record_exception(step: str, exc: BaseException) -> None:
    """Records a full, scrubbed traceback - the thing a frozen build hides."""
    if not enabled():
        return
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    record(step, error=type(exc).__name__)
    try:
        target = log_path()
        with _LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(scrub(text).rstrip() + "\n")
    except Exception:  # pragma: no cover
        logger.debug("Could not write the proxy diagnostic traceback.", exc_info=True)


def record_environment() -> None:
    """One header per session: how this process is running."""
    if not enabled():
        return
    record(
        "environment",
        frozen=bool(getattr(sys, "frozen", False)),
        platform=sys.platform,
        release=platform.release(),
        python=platform.python_version(),
        meipass=bool(getattr(sys, "_MEIPASS", None)),
    )
    try:
        from PySide6 import QtCore

        record("qt", version=QtCore.qVersion(),
               platform_plugin=_platform_plugin())
    except Exception as exc:  # pragma: no cover - Qt missing is reported elsewhere
        record_exception("qt-probe-failed", exc)


def _platform_plugin() -> str:
    try:
        from PySide6 import QtGui

        app = QtGui.QGuiApplication.instance()
        return app.platformName() if app is not None else "no-application"
    except Exception:
        return "unknown"


def describe_widget(widget) -> dict:
    """Geometry/visibility facts about a widget - never its contents."""
    try:
        from PySide6 import QtWidgets

        if widget is None:
            return {"widget": None}
        info = {
            "type": type(widget).__name__,
            "visible": widget.isVisible(),
            "hidden": widget.isHidden(),
            "geometry": _rect(widget.geometry()),
            "size_hint": f"{widget.sizeHint().width()}x{widget.sizeHint().height()}",
            "enabled": widget.isEnabled(),
            "window_opacity": round(float(widget.windowOpacity()), 3),
        }
        if isinstance(widget, QtWidgets.QWidget):
            info["flags"] = hex(int(widget.windowFlags()))
            info["translucent"] = widget.testAttribute(
                _attr("WA_TranslucentBackground")
            )
            effect = widget.graphicsEffect()
            info["effect"] = type(effect).__name__ if effect is not None else None
            if effect is not None and hasattr(effect, "opacity"):
                info["effect_opacity"] = round(float(effect.opacity()), 3)
            parent = widget.parentWidget()
            info["parent_type"] = type(parent).__name__ if parent is not None else None
        return info
    except Exception as exc:  # pragma: no cover
        return {"describe_failed": type(exc).__name__, "detail": scrub(exc)}


def _attr(name: str):
    from PySide6 import QtCore

    return getattr(QtCore.Qt.WidgetAttribute, name)


def _rect(rect) -> str:
    return f"{rect.x()},{rect.y()} {rect.width()}x{rect.height()}"


def screen_report(widget=None) -> dict:
    """Whether a window's geometry actually lands on a visible screen."""
    try:
        from PySide6 import QtGui

        app = QtGui.QGuiApplication.instance()
        if app is None:
            return {"screens": "no-application"}
        screens = app.screens()
        report = {
            "screen_count": len(screens),
            "screens": ";".join(_rect(s.geometry()) for s in screens),
        }
        if widget is not None:
            geometry = widget.frameGeometry()
            on_screen = any(s.geometry().intersects(geometry) for s in screens)
            report["on_screen"] = on_screen
            report["frame"] = _rect(geometry)
        return report
    except Exception as exc:  # pragma: no cover
        return {"screen_report_failed": type(exc).__name__}


def painted_fraction(widget) -> Optional[float]:
    """Fraction of the widget's area that actually receives paint.

    This is the check that catches the failure the user reported: a window that
    Qt reports as ``isVisible() == True`` while nothing is drawn into it, so the
    user sees no sheet at all. Sampled on a grid - cheap enough to run once per
    open, exact enough to tell "drawn" from "completely empty".
    """
    try:
        from PySide6 import QtGui

        size = widget.size()
        if size.width() <= 0 or size.height() <= 0:
            return 0.0
        image = QtGui.QImage(size, QtGui.QImage.Format.Format_ARGB32)
        image.fill(QtGui.QColor(0, 0, 0, 0))
        widget.render(image)
        painted = total = 0
        step = max(1, min(size.width(), size.height()) // 40)
        for y in range(0, size.height(), step):
            for x in range(0, size.width(), step):
                total += 1
                if image.pixelColor(x, y).alpha() > 10:
                    painted += 1
        return round(painted / total, 3) if total else None
    except Exception:  # pragma: no cover
        return None
