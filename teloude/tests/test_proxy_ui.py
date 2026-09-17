# teloude/tests/test_proxy_ui.py
"""The connection icon and the proxy page (offscreen, offline stack, no network).

What these tests pin, beyond "it opens":

* the icon is reachable before authentication (sign-in window) and after it
  (main window), and clicking it opens the proxy page;
* the login wizard still holds only phone/code/password - no proxy fields;
* the four states are animated, not switched instantly;
* the proxy secret is never displayed in the clear and never left in plain text
  in the settings table.
"""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtCore = pytest.importorskip("PySide6.QtCore")
QtGui = pytest.importorskip("PySide6.QtGui")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")
QtTest = pytest.importorskip("PySide6.QtTest")

from teloude.config import AppConfig  # noqa: E402
from teloude.infrastructure.telegram.connection import ConnectionState  # noqa: E402
from teloude.infrastructure.telegram.proxy import ProxyConfig  # noqa: E402
from teloude.ui import theme  # noqa: E402
from teloude.ui.app import build_offline  # noqa: E402
from teloude.ui.auth_dialog import AuthDialog  # noqa: E402
from teloude.ui.bridge import ServiceBridge  # noqa: E402
from teloude.ui.connection_indicator import (  # noqa: E402
    SUCCESS, ConnectionIndicator, color_for_state, state_label,
)
from teloude.ui.dialogs import UiThreadAsker  # noqa: E402
from teloude.ui.main_window import MainWindow  # noqa: E402
from teloude.ui.proxy_dialog import ProxyDialog  # noqa: E402
from teloude.ui.workers import pending_tasks  # noqa: E402

SECRET = "00112233445566778899aabbccddeeff"


@pytest.fixture(scope="module")
def qt_app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture()
def ctx(qt_app, tmp_path):
    config = AppConfig(data_dir=str(tmp_path / "data"),
                       database_path=str(tmp_path / "data" / "proxy.db"),
                       session_dir=str(tmp_path / "sessions"))
    context = build_offline(config)
    context.bridge = ServiceBridge(context.bus)
    context.asker = UiThreadAsker()
    yield context
    try:
        context.shutdown()
    except Exception:
        pass


def pump_until(qt_app, predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def open_dialog(ctx, parent=None, phone=None):
    return ProxyDialog(ctx.connection, parent=parent, bridge=ctx.bridge,
                       phone_provider=phone if phone is not None else (lambda: None))


# ------------------------------------------------------------------ icon -----
class TestConnectionIndicator:
    def test_it_starts_from_the_connection_it_is_given(self, qt_app, ctx):
        indicator = ConnectionIndicator(ctx.connection, ctx.bridge)
        assert indicator.state is ConnectionState.DISCONNECTED
        ctx.connection.apply_proxy(ProxyConfig(host="p.example.com", port=443,
                                               secret=SECRET, enabled=True))
        ctx.connection.connect("+15005550006")
        assert indicator.state is ConnectionState.CONNECTED

    def test_a_state_change_is_animated_not_switched(self, qt_app, ctx):
        indicator = ConnectionIndicator(ctx.connection, ctx.bridge)
        indicator.set_state(ConnectionState.CONNECTING)
        assert indicator.is_animating(), "connecting must animate"
        first = indicator.rotation_phase
        QtTest.QTest.qWait(160)
        assert indicator.rotation_phase != first, "the connecting arc must rotate"
        assert indicator.glyph_scale < 1.05, "the entry pop stays restrained"

    def test_the_connecting_animation_stops_when_it_connects(self, qt_app, ctx):
        indicator = ConnectionIndicator(ctx.connection, ctx.bridge)
        indicator.set_state(ConnectionState.CONNECTING)
        QtTest.QTest.qWait(80)
        indicator.set_state(ConnectionState.CONNECTED)
        assert pump_until(qt_app, lambda: indicator.current_color().name().upper()
                          == SUCCESS["light"].upper())
        QtTest.QTest.qWait(theme.MOTION_MS + 60)
        assert not indicator.is_animating(), "a settled state must not keep animating"
        assert indicator.glyph_scale == pytest.approx(1.0, abs=0.01)

    def test_an_error_shakes_before_it_settles(self, qt_app, ctx):
        indicator = ConnectionIndicator(ctx.connection, ctx.bridge)
        indicator.set_state(ConnectionState.ERROR, "no answer")
        assert indicator.is_animating()
        QtTest.QTest.qWait(theme.MOTION_MS + 200)
        assert not indicator.is_animating()
        assert indicator.state is ConnectionState.ERROR

    def test_each_state_has_its_own_colour(self, qt_app):
        theme.apply_theme(qt_app, dark=False)
        tokens = theme.tokens()
        assert color_for_state(ConnectionState.CONNECTED).name().upper() == SUCCESS["light"].upper()
        assert color_for_state(ConnectionState.ERROR) == tokens.color("danger")
        assert color_for_state(ConnectionState.CONNECTING) == tokens.color("accent")
        assert color_for_state(ConnectionState.DISCONNECTED) == tokens.color("label_secondary")
        assert len({color_for_state(state).name() for state in ConnectionState}) == 4

    def test_the_tooltip_follows_the_state(self, qt_app, ctx):
        indicator = ConnectionIndicator(ctx.connection, ctx.bridge)
        indicator.set_state(ConnectionState.CONNECTED, "Connected through p.example.com:443.")
        text = indicator.toolTip()
        assert state_label(ConnectionState.CONNECTED) in text
        assert "p.example.com:443" in text
        assert "proxy settings" in text.lower(), "the tooltip says what clicking does"
        indicator.set_state(ConnectionState.ERROR, "Could not reach Telegram.")
        assert state_label(ConnectionState.ERROR) in indicator.toolTip()

    def test_the_detailed_tooltip_is_whatever_the_layer_reports(self, qt_app, ctx):
        """The icon shows the layer's words - and the layer never reports a secret."""
        ctx.connection.apply_proxy(ProxyConfig(host="p.example.com", port=443,
                                               secret=SECRET, enabled=True))
        indicator = ConnectionIndicator(ctx.connection, ctx.bridge)
        ctx.bridge.connection_state.emit({
            "state": "connecting", "detail": ctx.connection.detail,
        })
        tooltip = indicator.toolTip()
        assert "p.example.com:443" in tooltip
        assert SECRET not in tooltip

    def test_a_bridge_event_moves_the_icon(self, qt_app, ctx):
        indicator = ConnectionIndicator(ctx.connection, ctx.bridge)
        ctx.bridge.connection_state.emit({"state": "connecting", "detail": "Connecting..."})
        assert indicator.state is ConnectionState.CONNECTING
        ctx.bridge.connection_state.emit({"state": "connected", "detail": "Connected."})
        assert indicator.state is ConnectionState.CONNECTED
        ctx.bridge.connection_state.emit({"state": "nonsense"})
        assert indicator.state is ConnectionState.CONNECTED, "junk is ignored"

    def test_it_is_clickable(self, qt_app, ctx):
        indicator = ConnectionIndicator(ctx.connection, ctx.bridge)
        clicks = []
        indicator.clicked.connect(lambda: clicks.append(True))
        indicator.click()
        assert clicks == [True]

    def test_every_state_actually_paints(self, qt_app):
        """The icon draws something in all four states, with or without a backend."""
        indicator = ConnectionIndicator(None, None)
        for state in ConnectionState:
            indicator.set_state(state)
            image = QtGui.QImage(indicator.size(), QtGui.QImage.Format.Format_ARGB32)
            image.fill(0)
            indicator.render(image)
            painted = sum(
                1 for y in range(image.height()) for x in range(image.width())
                if image.pixelColor(x, y).alpha() > 0
            )
            assert painted > 0, f"{state} painted nothing"
            # The glyph stays inside its 44px hit area.
            assert painted <= indicator.width() * indicator.height()


# ------------------------------------------------------- the sign-in window --
class TestTheIconOnTheSignInWindow:
    def test_the_wizard_carries_the_icon_in_its_corner(self, qt_app, ctx):
        dialog = AuthDialog(ctx.services.auth, ctx=ctx)
        try:
            dialog.show()
            qt_app.processEvents()
            indicator = dialog.connection_indicator
            assert isinstance(indicator, ConnectionIndicator)
            assert dialog.card.isAncestorOf(indicator), "the icon lives on the card"
            assert indicator.x() > dialog.card.width() / 2, "…in its right-hand corner"
            assert dialog.stack.count() == 3, "the wizard is still phone/code/password"
        finally:
            dialog.close()

    def test_the_login_form_has_no_proxy_fields(self, qt_app, ctx):
        dialog = AuthDialog(ctx.services.auth, ctx=ctx)
        try:
            for page in range(dialog.stack.count()):
                widget = dialog.stack.widget(page)
                assert widget.findChildren(QtWidgets.QSpinBox) == []
                assert widget.findChildren(QtWidgets.QComboBox) == []
                for field in widget.findChildren(QtWidgets.QLineEdit):
                    text = f"{field.placeholderText()} {field.objectName()}".lower()
                    assert not any(word in text for word in
                                   ("proxy", "server", "port", "secret")), text
            # The wizard's own three fields are untouched.
            assert dialog.phone_edit.text() == "+"
            assert dialog.code_edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Normal
            assert dialog.password_edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Password
        finally:
            dialog.close()

    def test_clicking_the_icon_opens_the_proxy_page(self, qt_app, ctx, monkeypatch):
        calls = []
        monkeypatch.setattr("teloude.ui.auth_dialog.open_proxy_settings",
                            lambda context, parent=None: calls.append((context, parent)))
        dialog = AuthDialog(ctx.services.auth, ctx=ctx)
        try:
            dialog.connection_indicator.click()
            assert calls and calls[0][0] is ctx
        finally:
            dialog.close()

    def test_a_wizard_without_a_context_still_opens_something(self, qt_app, ctx):
        """`AuthDialog(auth)` is used by tests and previews; the icon must not break."""
        dialog = AuthDialog(ctx.services.auth)
        try:
            assert dialog.connection_indicator.state is ConnectionState.DISCONNECTED
        finally:
            dialog.close()


# ---------------------------------------------------------- the main window --
class TestTheIconAfterSignIn:
    def test_the_status_bar_keeps_the_icon_available(self, qt_app, ctx):
        window = MainWindow(ctx)
        try:
            window.show()
            qt_app.processEvents()
            indicator = window.connection_indicator
            assert isinstance(indicator, ConnectionIndicator)
            assert window.statusBar().isAncestorOf(indicator)
            assert indicator.isVisible()
        finally:
            window.close()

    def test_the_window_follows_the_connection_layer(self, qt_app, ctx):
        window = MainWindow(ctx)
        try:
            ctx.bridge.connection_state.emit({"state": "error", "detail": "proxy unreachable"})
            assert window.connection_indicator.state is ConnectionState.ERROR
            ctx.bridge.connection_state.emit({"state": "connected", "detail": "Connected."})
            assert window.connection_indicator.state is ConnectionState.CONNECTED
        finally:
            window.close()

    def test_clicking_it_opens_the_proxy_page(self, qt_app, ctx, monkeypatch):
        calls = []
        monkeypatch.setattr("teloude.ui.main_window.open_proxy_settings",
                            lambda context, parent=None: calls.append(context))
        window = MainWindow(ctx)
        try:
            window.connection_indicator.click()
            assert calls == [ctx]
        finally:
            window.close()


# --------------------------------------------------------------- the page ----
class TestProxyDialog:
    def test_it_shows_the_saved_configuration(self, qt_app, ctx):
        ctx.connection.apply_proxy(ProxyConfig(host="p.example.com", port=8443,
                                               secret=SECRET, enabled=True))
        dialog = open_dialog(ctx)
        try:
            assert dialog.host_edit.text() == "p.example.com"
            assert dialog.port_spin.value() == 8443
            assert dialog.secret_edit.text() == SECRET
            assert dialog.enable_check.isChecked()
            assert dialog.current_config().secret == SECRET
            assert SECRET not in dialog.storage_label.text()
        finally:
            dialog.close()

    def test_the_secret_field_is_masked_and_can_be_revealed(self, qt_app, ctx):
        dialog = open_dialog(ctx)
        try:
            assert dialog.secret_edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Password
            dialog.reveal_check.setChecked(True)
            assert dialog.secret_edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Normal
            dialog.reveal_check.setChecked(False)
            assert dialog.secret_edit.echoMode() == QtWidgets.QLineEdit.EchoMode.Password
        finally:
            dialog.close()

    def test_the_protection_mechanism_is_named(self, qt_app, ctx):
        dialog = open_dialog(ctx)
        try:
            label = dialog.storage_label.text()
            assert "stored with" in label.lower()
            assert ctx.connection.proxy_mechanism() in label
        finally:
            dialog.close()

    @pytest.mark.parametrize("host,port,secret,expected", [
        ("", 443, SECRET, "server address"),
        ("p.example.com", 443, "", "secret"),
        ("p.example.com", 443, "nope", "not usable"),
    ])
    def test_validation_is_explained_where_the_user_is_looking(
        self, qt_app, ctx, host, port, secret, expected
    ):
        dialog = open_dialog(ctx)
        try:
            dialog.host_edit.setText(host)
            dialog.port_spin.setValue(port)
            dialog.secret_edit.setText(secret)
            dialog.test_button.click()
            assert expected in dialog.status_label.text()
            assert pending_tasks() == 0, "nothing was sent anywhere"
        finally:
            dialog.close()

    def test_test_connection_reports_success_and_keeps_the_dialog_open(self, qt_app, ctx):
        dialog = open_dialog(ctx)
        try:
            dialog.host_edit.setText("mtproxy.example.com")
            dialog.port_spin.setValue(443)
            dialog.secret_edit.setText(SECRET)
            dialog.test_button.click()
            assert pump_until(qt_app, lambda: dialog.indicator.state is ConnectionState.CONNECTED)
            assert "Connected through mtproxy.example.com:443" in dialog.status_label.text()
            assert dialog.result() == 0, "a test never closes the page"
            assert ctx.connection.proxy_config().enabled is False, "a test does not save"
        finally:
            dialog.close()

    def test_connect_saves_enables_and_closes(self, qt_app, ctx):
        dialog = open_dialog(ctx)
        try:
            dialog.host_edit.setText("mtproxy.example.com")
            dialog.port_spin.setValue(443)
            dialog.secret_edit.setText(SECRET)
            dialog.enable_check.setChecked(True)
            dialog.connect_button.click()
            assert pump_until(qt_app, lambda:
                              dialog.result() == QtWidgets.QDialog.DialogCode.Accepted)
            saved = ctx.connection.proxy_config()
            assert saved.enabled and saved.host == "mtproxy.example.com"
            assert saved.secret == SECRET
            # …and it really is in the settings table, protected.
            from teloude.infrastructure.telegram.proxy import KEY_SECRET

            stored = ctx.repos.settings.get(KEY_SECRET, "")
            assert stored and SECRET not in stored
        finally:
            dialog.close()

    def test_connect_can_switch_the_proxy_off(self, qt_app, ctx):
        ctx.connection.apply_proxy(ProxyConfig(host="mtproxy.example.com", port=443,
                                               secret=SECRET, enabled=True))
        dialog = open_dialog(ctx)
        try:
            dialog.enable_check.setChecked(False)
            dialog.connect_button.click()
            assert pump_until(qt_app, lambda:
                              dialog.result() == QtWidgets.QDialog.DialogCode.Accepted)
            assert ctx.connection.proxy_config().enabled is False
            assert ctx.connection.active_proxy() is None
        finally:
            dialog.close()

    def test_the_secret_is_never_printed_anywhere_on_the_page(self, qt_app, ctx):
        ctx.connection.apply_proxy(ProxyConfig(host="p.example.com", port=443,
                                               secret=SECRET, enabled=True))
        dialog = open_dialog(ctx)
        try:
            dialog.status_label.setText("")
            dialog.connect_button.click()          # drives the busy/status text
            texts = [widget.text() for widget in dialog.findChildren(QtWidgets.QLabel)]
            texts += [widget.toolTip() for widget in dialog.findChildren(QtWidgets.QWidget)]
            texts.append(dialog.windowTitle())
            assert all(SECRET not in text for text in texts)
            assert SECRET not in dialog.secret_edit.toolTip()
            # Let the work the click started finish before the page goes away:
            # a result delivered to a window that is already gone is exactly the
            # kind of teardown this suite is supposed to keep predictable.
            assert pump_until(qt_app, lambda: pending_tasks() == 0)
            assert SECRET not in dialog.status_label.text()
        finally:
            dialog.close()

    def test_a_page_without_a_connection_layer_explains_itself(self, qt_app):
        dialog = ProxyDialog(None)
        try:
            assert not dialog.host_edit.isEnabled()
            assert not dialog.connect_button.isEnabled()
            assert "no telegram connection layer" in dialog.status_label.text().lower()
        finally:
            dialog.close()
