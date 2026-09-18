# teloude/tests/test_notifications.py
"""Completion notifications: lifecycle-driven, deduplicated, tray-delivered.

The rules under test: notifications ride the services' own backup_done /
restore_done events (no timers, no widgets), a success notification never
fires for a failed or cancelled run, and the exact same delivered event never
notifies twice - while a genuinely new run always does.
"""
import logging


from teloude.application.services import EventBus
from teloude.ui.notifications import (
    OperationNotifier,
    SystemNotifier,
    completion_payload,
    failure_summary,
    install,
)


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, kind, title, body):
        self.calls.append((kind, title, body))


class FakeTray:
    def __init__(self, supported=True):
        self.supported = supported
        self.shown = []

    @property
    def is_supported(self):
        return self.supported

    def show_message(self, title, message, kind="info"):
        self.shown.append((title, message, kind))


def _backup_payload(**overrides):
    payload = {
        "uploaded": 3, "skipped": 1, "unchanged": 0,
        "failed": [], "cancelled": False,
    }
    payload.update(overrides)
    return payload


def _restore_payload(**overrides):
    payload = {
        "restored": 5, "skipped": 0,
        "failed": [], "cancelled": False,
    }
    payload.update(overrides)
    return payload


class TestCompletionPayload:
    def test_clean_backup_is_a_success(self):
        message = completion_payload(_backup_payload(), "backup")
        assert message == {
            "severity": "info",
            "title": "Backup completed",
            "body": "Your backup has finished successfully.",
        }

    def test_clean_restore_is_a_success(self):
        message = completion_payload(_restore_payload(), "restore")
        assert message == {
            "severity": "info",
            "title": "Restore completed",
            "body": "Your restore has finished successfully.",
        }

    def test_partial_failure_is_an_error_never_a_success(self):
        payload = _backup_payload(
            uploaded=2, failed=[("docs/big.tif", "size mismatch")]
        )
        message = completion_payload(payload, "backup")
        assert message["severity"] == "error"
        assert message["title"] == "Backup failed"
        assert "docs/big.tif" in message["body"]

    def test_hard_failure_flag_is_an_error(self):
        payload = _backup_payload(
            uploaded=0, failed=[("", "Traceback ...")], error=True
        )
        message = completion_payload(payload)
        assert message["severity"] == "error"
        assert "successfully" not in message["body"]

    def test_cancelled_is_silence(self):
        assert completion_payload(_backup_payload(cancelled=True), "backup") == {}
        assert completion_payload(_restore_payload(cancelled=True), "restore") == {}

    def test_failure_names_three_then_counts_the_rest(self):
        failed = [(f"f{i}.bin", "x") for i in range(5)]
        body = completion_payload(_restore_payload(failed=failed), "restore")["body"]
        assert "f0.bin, f1.bin, f2.bin" in body
        assert "…and 2 more" in body

    def test_unknown_verb_fails_closed_to_a_generic_error(self):
        message = completion_payload({"verb": "sync", "failed": [("a", "b")]})
        assert message["title"] == "Operation failed"


class TestFailureSummary:
    def test_empty_is_empty(self):
        assert failure_summary([]) == ""

    def test_junk_rows_do_not_crash(self):
        assert failure_summary([None, 42, ("ok.txt", "e")]) == "ok.txt"
        assert failure_summary([("", "e")]) == ""

    def test_counts_the_untold(self):
        rows = [(f"p/{i}", "e") for i in range(4)]
        assert failure_summary(rows, limit=2) == "p/0, p/1 …and 2 more"


class TestOperationNotifier:
    def test_success_notifies_once(self):
        bus, recorder = EventBus(), Recorder()
        notifier = OperationNotifier(bus, recorder)
        bus.emit("backup_done", _backup_payload())
        assert len(recorder.calls) == 1
        assert recorder.calls[0][0] == "info"
        assert recorder.calls[0][1] == "Backup completed"
        notifier.close()

    def test_restore_events_reach_the_same_notifier(self):
        bus, recorder = EventBus(), Recorder()
        OperationNotifier(bus, recorder)
        bus.emit("restore_done", _restore_payload())
        assert recorder.calls[0][1] == "Restore completed"

    def test_replayed_event_does_not_notify_twice(self):
        bus, recorder = EventBus(), Recorder()
        OperationNotifier(bus, recorder)
        payload = _backup_payload()
        bus.emit("backup_done", payload)
        bus.emit("backup_done", payload)  # the identical delivered event
        assert len(recorder.calls) == 1

    def test_a_new_run_with_identical_counters_notifies_again(self):
        bus, recorder = EventBus(), Recorder()
        OperationNotifier(bus, recorder)
        bus.emit("backup_done", _backup_payload())
        bus.emit("backup_done", _backup_payload())  # fresh dict = a new run
        assert len(recorder.calls) == 2

    def test_cancelled_run_is_silent(self):
        bus, recorder = EventBus(), Recorder()
        OperationNotifier(bus, recorder)
        bus.emit("backup_done", _backup_payload(cancelled=True))
        bus.emit("restore_done", _restore_payload(cancelled=True))
        assert recorder.calls == []

    def test_failure_notifies_as_error(self):
        bus, recorder = EventBus(), Recorder()
        OperationNotifier(bus, recorder)
        bus.emit("restore_done", _restore_payload(
            restored=1, failed=[("x.bin", "boom")]
        ))
        assert recorder.calls[0][0] == "error"
        assert recorder.calls[0][1] == "Restore failed"
        assert "x.bin" in recorder.calls[0][2]

    def test_close_stops_the_notifications(self):
        bus, recorder = EventBus(), Recorder()
        notifier = OperationNotifier(bus, recorder)
        notifier.close()
        bus.emit("backup_done", _backup_payload())
        assert recorder.calls == []

    def test_close_is_idempotent(self):
        bus = EventBus()
        notifier = OperationNotifier(bus, Recorder())
        notifier.close()
        notifier.close()  # must not raise

    def test_broken_delegate_is_swallowed(self):
        bus = EventBus()

        def broken(kind, title, body):
            raise RuntimeError("no surface")

        OperationNotifier(bus, broken)
        bus.emit("backup_done", _backup_payload())  # must not raise

    def test_non_dict_payload_is_ignored(self):
        bus, recorder = EventBus(), Recorder()
        OperationNotifier(bus, recorder)
        bus.emit("backup_done", None)
        bus.emit("backup_done", "garbage")
        assert recorder.calls == []


class TestSystemNotifier:
    def test_unsupported_tray_degrades_to_the_log(self, caplog):
        notifier = SystemNotifier(FakeTray(supported=False))
        assert notifier.available is False
        with caplog.at_level(logging.INFO, logger="Notifications"):
            notifier.show("info", "Backup completed", "All good.")
        assert "Backup completed" in caplog.text

    def test_supported_tray_receives_kind_and_text(self):
        tray = FakeTray()
        notifier = SystemNotifier(tray)
        assert notifier.available is True
        notifier.show("error", "Backup failed", "Some files failed.")
        assert tray.shown == [("Backup failed", "Some files failed.", "error")]

    def test_classic_two_argument_tray_still_works(self):
        class LegacyTray:
            is_supported = True

            def __init__(self):
                self.shown = []

            def show_message(self, title, message):
                self.shown.append((title, message))

        legacy = LegacyTray()
        SystemNotifier(legacy).show("info", "T", "M")
        assert legacy.shown == [("T", "M")]

    def test_none_tray_degrades_to_the_log(self, caplog):
        notifier = SystemNotifier(None)
        assert notifier.available is False
        with caplog.at_level(logging.INFO, logger="Notifications"):
            notifier.show("info", "T", "M")
        assert "T" in caplog.text


class TestInstall:
    def test_install_subscribes_both_events(self):
        bus, tray = EventBus(), FakeTray(supported=False)
        notifier = install(bus, tray)
        assert isinstance(notifier, OperationNotifier)
        bus.emit("backup_done", _backup_payload())
        bus.emit("restore_done", _restore_payload())
        assert len(tray.shown) == 0  # unsupported: logged, not shown
        notifier.close()
