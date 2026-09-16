# teloude/tests/test_ui_flows.py
"""UI flows that run work on the thread pool (sign-in, search, preview, storages).

These paths were silently broken once: the background runner dropped its result
signal as soon as the worker finished, so every callback a view passed (advance
the wizard, re-enable a button, show results) never ran. The tests below drive
the real widgets, so a regression fails here instead of in front of the user.
"""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from teloude.config import AppConfig  # noqa: E402
from teloude.infrastructure.telegram.auth import AuthState  # noqa: E402
from teloude.ui.app import build_offline  # noqa: E402
from teloude.ui.auth_dialog import AuthDialog  # noqa: E402
from teloude.ui.bridge import ServiceBridge  # noqa: E402
from teloude.ui.dialogs import UiThreadAsker  # noqa: E402
from teloude.ui.main_window import MainWindow  # noqa: E402
from teloude.ui.workers import pending_tasks, run_in_background  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _pump_until(qt_app, predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture()
def ctx(qt_app, tmp_path):
    config = AppConfig(data_dir=str(tmp_path / "data"),
                       database_path=str(tmp_path / "data" / "flows.db"))
    context = build_offline(config)
    context.bridge = ServiceBridge(context.bus)
    context.asker = UiThreadAsker()
    yield context
    for service in (context.services.backup, context.services.restore):
        try:
            service.cancel()
        except Exception:
            pass
    context.shutdown()


class TestBackgroundRunner:
    def test_result_is_delivered_even_when_the_caller_keeps_no_task(self, qt_app):
        """Regression: callers ignore the return value, so results must not be lost."""
        results = []
        run_in_background(lambda: 42, on_done=results.append)
        assert _pump_until(qt_app, lambda: results == [42]), results
        assert _pump_until(qt_app, lambda: pending_tasks() == 0)

    def test_failure_is_delivered_as_a_message(self, qt_app):
        errors = []

        def boom():
            raise OSError("network down")

        run_in_background(boom, on_error=errors.append)
        assert _pump_until(qt_app, lambda: errors == ["network down"]), errors

    def test_many_tasks_deliver_and_release(self, qt_app):
        seen = []
        for index in range(25):
            run_in_background(lambda i=index: i, on_done=seen.append)
        assert _pump_until(qt_app, lambda: sorted(seen) == list(range(25))), seen
        assert _pump_until(qt_app, lambda: pending_tasks() == 0)


class TestSignInWizard:
    def test_code_sign_in_advances_and_accepts(self, qt_app, ctx):
        dialog = AuthDialog(ctx.services.auth)
        dialog.show()
        try:
            dialog.phone_edit.setText("+15005550006")
            dialog._on_send_code()
            assert _pump_until(qt_app, lambda: dialog.stack.currentIndex() == 1), \
                dialog.status_label.text()
            assert dialog.send_button.isEnabled(), "buttons must not stay disabled"
            assert "code" in dialog.status_label.text().lower()

            dialog.code_edit.setText("00000")
            dialog._on_submit_code()
            assert _pump_until(
                qt_app, lambda: "incorrect" in dialog.status_label.text().lower()
            ), dialog.status_label.text()
            assert dialog.code_button.isEnabled()

            dialog.code_edit.setText("11111")
            dialog._on_submit_code()
            assert _pump_until(
                qt_app, lambda: dialog.result() == QtWidgets.QDialog.Accepted
            ), dialog.status_label.text()
            assert ctx.services.auth.state is AuthState.AUTHORIZED
        finally:
            dialog.close()

    def test_two_factor_branch_is_walked(self, qt_app, ctx):
        from teloude.application.services import AuthService
        from teloude.infrastructure.telegram.fakes import FakeAuth

        auth = AuthService(FakeAuth(needs_password=True), ctx.bus)
        dialog = AuthDialog(auth)
        dialog.show()
        try:
            dialog.phone_edit.setText("+15005550006")
            dialog._on_send_code()
            assert _pump_until(qt_app, lambda: dialog.stack.currentIndex() == 1)
            dialog.code_edit.setText("11111")
            dialog._on_submit_code()
            assert _pump_until(qt_app, lambda: dialog.stack.currentIndex() == 2), \
                dialog.status_label.text()
            assert auth.state is AuthState.PASSWORD_NEEDED
            assert "two-step" in dialog.status_label.text().lower()

            dialog.password_edit.setText("wrong")
            dialog._on_submit_password()
            assert _pump_until(
                qt_app, lambda: "2fa password is incorrect" in dialog.status_label.text().lower()
            ), dialog.status_label.text()
            assert dialog.password_button.isEnabled()

            dialog.password_edit.setText("secret")
            dialog._on_submit_password()
            assert _pump_until(qt_app, lambda: dialog.result() == QtWidgets.QDialog.Accepted)
            assert auth.state is AuthState.AUTHORIZED
        finally:
            dialog.close()

    def test_empty_phone_is_reported_without_a_worker(self, qt_app, ctx):
        dialog = AuthDialog(ctx.services.auth)
        try:
            dialog.phone_edit.setText("")
            dialog._on_send_code()
            assert _pump_until(qt_app, lambda: "international format" in dialog.status_label.text())
            assert dialog.stack.currentIndex() == 0
            assert dialog.send_button.isEnabled()
        finally:
            dialog.close()


class TestSearchAndPreviewFlows:
    def test_search_from_the_ui_lists_results(self, qt_app, ctx, tmp_path):
        window = MainWindow(ctx)
        window.show()
        try:
            source = tmp_path / "docs"
            source.mkdir()
            (source / "quarterly-report.txt").write_bytes(b"numbers" * 100)
            record = ctx.services.storages.create_storage("Searchable")
            from teloude.core.backup import DuplicateResolver

            manager = ctx.backup_manager
            ctx.backup_manager.run(
                manager.plan(record.id, source),
                resolver=DuplicateResolver(policy=DuplicateResolver.SKIP_ALL),
            )
            ui = window.search
            ui.query_edit.setText("quarterly")
            ui._on_search()
            assert _pump_until(qt_app, lambda: ui.results.rowCount() == 1), \
                [ui.results.item(0, c).text() if ui.results.item(0, c) else ""
                 for c in range(ui.results.columnCount())]
            assert ui.search_button.isEnabled()
            assert "quarterly-report.txt" in ui.results.item(0, 0).text() + \
                ui.results.item(0, 1).text()

            ui.query_edit.setText("no-such-file-anywhere")
            ui._on_search()
            assert _pump_until(qt_app, lambda: ui.results.rowCount() == 0)
            assert ui.search_button.isEnabled()
        finally:
            window.close()

    def test_preview_loads_in_the_background(self, qt_app, ctx, tmp_path):
        from PIL import Image

        from teloude.ui.views.preview_widget import PreviewWidget

        image_path = tmp_path / "picture.png"
        Image.new("RGB", (40, 25), (10, 120, 200)).save(image_path)

        widget = PreviewWidget(ctx)
        try:
            widget.show_path(str(image_path))
            assert _pump_until(
                qt_app, lambda: widget.detail_label.text() != "Loading preview..."
            ), widget.detail_label.text()
            assert "40x25" in widget.detail_label.text(), widget.detail_label.text()
        finally:
            widget.deleteLater()

    def test_preview_failure_is_shown_not_crashed(self, qt_app, ctx, tmp_path):
        from teloude.ui.views.preview_widget import PreviewWidget

        widget = PreviewWidget(ctx)
        try:
            widget.show_path(str(tmp_path / "missing.png"))
            assert _pump_until(
                qt_app, lambda: "unavailable" in widget.detail_label.text().lower()
            ), widget.detail_label.text()
        finally:
            widget.deleteLater()
