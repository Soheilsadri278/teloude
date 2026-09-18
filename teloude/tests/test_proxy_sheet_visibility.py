# teloude/tests/test_proxy_sheet_visibility.py
"""The proxy sheet must actually be *drawn*, not merely "visible".

Why this file exists
--------------------

The existing proxy UI tests asserted window flags and stylesheet tokens, and
the two "clicking the icon opens the proxy page" tests monkeypatched
``open_proxy_settings`` away - so the real presentation path
(``ProxyDialog()`` -> ``present_blocking`` -> ``exec``) was never executed by
any test. A frameless, WA_TranslucentBackground sheet that composites to
nothing still reports ``isVisible() == True`` and still carries the right
flags, which is exactly how a completely invisible proxy window passed a green
test suite while the packaged Windows application showed nothing at all.

The regression that motivated these tests: a QGraphicsOpacityEffect on the
dialog's host frame with a QGraphicsDropShadowEffect on its *descendant* card.
Qt cannot nest graphics effects - the inner one fails with "QPainter::begin: A
paint device can only be painted by one painter at a time" and the composited
window can come back empty.

These tests therefore assert what the user sees: pixels.
"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtCore = pytest.importorskip("PySide6.QtCore")
QtGui = pytest.importorskip("PySide6.QtGui")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from teloude.ui import proxy_diagnostics  # noqa: E402
from teloude.ui import theme  # noqa: E402
from teloude.ui.proxy_dialog import ProxyDialog, open_proxy_settings  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    theme.apply_theme(app, dark=False)
    yield app


@pytest.fixture()
def host(qt_app):
    window = QtWidgets.QMainWindow()
    window.resize(1000, 700)
    window.show()
    qt_app.processEvents()
    yield window
    window.close()


def painted_fraction(widget) -> float:
    """The share of the widget's area that receives any paint at all."""
    image = QtGui.QImage(widget.size(), QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(0, 0, 0, 0))
    widget.render(image)
    painted = total = 0
    for y in range(0, image.height(), 3):
        for x in range(0, image.width(), 3):
            total += 1
            if image.pixelColor(x, y).alpha() > 10:
                painted += 1
    return painted / max(total, 1)


def painter_errors(qt_app, action) -> list:
    """Qt warnings emitted while ``action`` runs (Qt logs these, not Python)."""
    captured = []
    previous = QtCore.qInstallMessageHandler(
        lambda mode, context, text: captured.append(text)
    )
    try:
        action()
        qt_app.processEvents()
    finally:
        QtCore.qInstallMessageHandler(previous)
    return [t for t in captured
            if "QPainter" in t or "paint device" in t or "Painter not active" in t]


class TestTheSheetIsActuallyDrawn:
    def test_no_nested_graphics_effects(self, qt_app, host):
        """Qt cannot nest effects: at most one per ancestor chain.

        This is the invariant whose violation made the packaged sheet invisible.
        """
        dialog = ProxyDialog(None, host)
        try:
            dialog.show()
            qt_app.processEvents()
            with_effects = [
                w for w in dialog.findChildren(QtWidgets.QWidget)
                if w.graphicsEffect() is not None
            ]
            for widget in with_effects:
                for other in with_effects:
                    if other is widget:
                        continue
                    assert not other.isAncestorOf(widget), (
                        f"{type(other).__name__} has a graphics effect and is an "
                        f"ancestor of {type(widget).__name__}, which also has one; "
                        "Qt cannot nest graphics effects and the sheet will not paint"
                    )
        finally:
            dialog.close()

    def test_showing_the_sheet_raises_no_painter_error(self, qt_app, host):
        dialog = ProxyDialog(None, host)
        try:
            errors = painter_errors(qt_app, lambda: (dialog.show(),
                                                     dialog.card.update(),
                                                     dialog.update()))
            assert errors == [], f"Qt could not paint the sheet: {errors[:3]}"
        finally:
            dialog.close()

    def test_the_sheet_paints_a_substantial_area(self, qt_app, host):
        """The failure mode was a window that is 'visible' but fully empty."""
        dialog = ProxyDialog(None, host)
        try:
            dialog.show()
            qt_app.processEvents()
            assert dialog.isVisible()
            fraction = painted_fraction(dialog)
            assert fraction > 0.25, (
                f"only {fraction:.1%} of the sheet was painted - the window is "
                "effectively invisible to the user"
            )
        finally:
            dialog.close()

    def test_the_sheet_stays_opaque_and_on_screen(self, qt_app, host):
        """A fade that never runs must not leave a transparent window."""
        dialog = ProxyDialog(None, host)
        try:
            dialog.show()
            qt_app.processEvents()
            assert dialog.windowOpacity() > 0.99, "the entry fade left the sheet faded out"
            geometry = dialog.frameGeometry()
            assert any(s.geometry().intersects(geometry)
                       for s in QtGui.QGuiApplication.screens()), \
                "the sheet opened outside every screen"
            assert dialog.width() > 100 and dialog.height() > 100
        finally:
            dialog.close()

    def test_the_sheet_survives_repeated_repaints(self, qt_app, host):
        """Windows repaints constantly; the first paint being fine is not enough."""
        dialog = ProxyDialog(None, host)
        try:
            dialog.show()
            qt_app.processEvents()
            for _ in range(3):
                errors = painter_errors(qt_app, lambda: (dialog.card.update(),
                                                         dialog._host.update(),
                                                         dialog.update()))
                assert errors == [], f"repaint failed: {errors[:2]}"
                assert painted_fraction(dialog) > 0.25
        finally:
            dialog.close()


class TestTheRealPresentationPath:
    """``open_proxy_settings`` end to end - the code the old tests stubbed out."""

    def test_open_proxy_settings_really_shows_a_drawn_sheet(self, qt_app, host):
        seen = {}

        class Ctx:
            connection = None
            bridge = None
            services = None

        def probe():
            dialogs = [w for w in QtWidgets.QApplication.topLevelWidgets()
                       if isinstance(w, ProxyDialog) and w.isVisible()]
            seen["count"] = len(dialogs)
            if dialogs:
                dialog = dialogs[0]
                seen["visible"] = dialog.isVisible()
                seen["opacity"] = dialog.windowOpacity()
                seen["painted"] = painted_fraction(dialog)
                dialog.reject()

        # Sampled after the entry fade has finished: mid-animation the window is
        # legitimately semi-transparent, and the invariant under test is that it
        # ends up fully opaque and drawn.
        QtCore.QTimer.singleShot(theme.MOTION_MS + 250, probe)
        open_proxy_settings(Ctx(), host)

        assert seen.get("count"), "no ProxyDialog was ever shown by open_proxy_settings"
        assert seen["visible"] is True
        assert seen["opacity"] > 0.99
        assert seen["painted"] > 0.25, (
            f"the presented sheet painted only {seen['painted']:.1%} of its area"
        )


class TestDiagnosticsNeverLeakSecrets:
    """The diagnostic log is shipped in the build; it must stay credential-free."""

    @pytest.mark.parametrize("text", [
        "secret=00112233445566778899aabbccddeeff",
        "api_hash: 0123456789abcdef0123456789abcdef",
        "API_ID=1234567",
        "phone +15005550006 failed",
        "token = AAHfiq9-abcdefghijklmnopqrstuvwxyz",
    ])
    def test_credentials_are_scrubbed(self, text):
        cleaned = proxy_diagnostics.scrub(text)
        for leak in ("00112233445566778899aabbccddeeff",
                     "0123456789abcdef0123456789abcdef",
                     "1234567", "15005550006",
                     "AAHfiq9-abcdefghijklmnopqrstuvwxyz"):
            assert leak not in cleaned, f"{leak} survived scrubbing: {cleaned}"

    def test_the_log_records_the_flow_without_secrets(self, qt_app, host, tmp_path,
                                                      monkeypatch):
        log = tmp_path / "startup_error.log"
        monkeypatch.setenv("TELOUDE_DIAG", "1")
        monkeypatch.setenv("TELOUDE_DIAG_LOG", str(log))

        class Ctx:
            connection = None
            bridge = None
            services = None

        def probe():
            for widget in QtWidgets.QApplication.topLevelWidgets():
                if isinstance(widget, ProxyDialog) and widget.isVisible():
                    widget.reject()

        QtCore.QTimer.singleShot(150, probe)
        open_proxy_settings(Ctx(), host)

        assert log.exists(), "the diagnostic log was not written"
        text = log.read_text(encoding="utf-8")
        for step in ("handler-entered", "dialog-construction-start",
                     "dialog-construction-done", "presentation-start",
                     "presentation", "painted", "closed"):
            assert step in text, f"the log is missing the {step!r} step:\n{text}"
        assert "secret=" not in text.lower().replace("secret=<redacted>", "")

    def test_a_failure_in_construction_is_logged_not_swallowed(self, qt_app, host,
                                                               tmp_path, monkeypatch):
        """The frozen build hides slot exceptions; the log must still show them."""
        log = tmp_path / "startup_error.log"
        monkeypatch.setenv("TELOUDE_DIAG", "1")
        monkeypatch.setenv("TELOUDE_DIAG_LOG", str(log))

        boom = RuntimeError("proxy dialog exploded")

        def explode(*args, **kwargs):
            raise boom

        monkeypatch.setattr("teloude.ui.proxy_dialog.ProxyDialog", explode)

        class Ctx:
            connection = None
            bridge = None
            services = None

        with pytest.raises(RuntimeError):
            open_proxy_settings(Ctx(), host)

        text = log.read_text(encoding="utf-8")
        assert "dialog-construction-failed" in text
        assert "RuntimeError" in text
        assert "proxy dialog exploded" in text
