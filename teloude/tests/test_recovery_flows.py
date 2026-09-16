# teloude/tests/test_recovery_flows.py
"""What happens when Telegram says "you cannot do this any more".

Three conditions users really hit, each needing a different reaction:
the session was revoked (sign in again), the storage group was deleted
(re-create it), and a single file's message is gone (upload it again). They used
to arrive as one vague "operation failed" plus pointless retries.
"""
import pytest

from teloude.application.services import ServiceError
from teloude.config import AppConfig
from teloude.core.backup import BackupManager
from teloude.core.restore import RestoreManager
from teloude.core.transfers import TransferRegistry, TransferState
from teloude.infrastructure.database import DatabaseManager, close_db_connection
from teloude.infrastructure.repositories import (
    FileRepository,
    FolderRepository,
    StorageRepository,
    TransferRepository,
)
from teloude.infrastructure.telegram.exceptions import (
    RemoteItemMissingError,
    SessionExpiredError,
    StorageUnavailableError,
    TeloudeTelegramError,
)
from teloude.infrastructure.telegram.fakes import FakeFileGateway, FakeStorageGateway
from teloude.infrastructure.telegram.bridge import map_rpc_error


class TestErrorMapping:
    """Telegram's specific RPC errors become distinct, actionable messages."""

    def test_session_errors_map_to_sign_in_again(self):
        from telethon.errors import (
            AuthKeyUnregisteredError,
            SessionRevokedError,
            UserDeactivatedBanError,
        )

        for error_cls in (AuthKeyUnregisteredError, SessionRevokedError, UserDeactivatedBanError):
            mapped = map_rpc_error(error_cls(request=None), "uploading a file")
            assert isinstance(mapped, SessionExpiredError), error_cls.__name__
            assert "sign in again" in str(mapped).lower()
            assert "RPC" not in str(mapped)

    def test_missing_group_errors_map_to_storage_unavailable(self):
        from telethon.errors import ChannelPrivateError, ChatWriteForbiddenError

        for error_cls in (ChannelPrivateError, ChatWriteForbiddenError):
            mapped = map_rpc_error(error_cls(request=None), "reading topic history")
            assert isinstance(mapped, StorageUnavailableError), error_cls.__name__
            assert "no longer available" in str(mapped).lower()

    def test_deleted_message_maps_to_remote_item_missing(self):
        from telethon.errors import MessageIdInvalidError

        mapped = map_rpc_error(MessageIdInvalidError(request=None), "locating the backup message")
        assert isinstance(mapped, RemoteItemMissingError)
        assert "run a backup again" in str(mapped).lower()

    def test_generic_and_network_errors_are_unchanged(self):
        from teloude.infrastructure.telegram.exceptions import ConnectionStateError

        assert isinstance(map_rpc_error(ValueError("weird"), "x"), TeloudeTelegramError)
        assert not isinstance(map_rpc_error(ValueError("weird"), "x"), SessionExpiredError)
        assert isinstance(map_rpc_error(ConnectionError("offline"), "x"), ConnectionStateError)


@pytest.fixture()
def env(tmp_path):
    manager = DatabaseManager(AppConfig(database_path=str(tmp_path / "rec.db")))
    assert manager.initialize()
    storages = StorageRepository(manager)
    folders = FolderRepository(manager)
    files = FileRepository(manager)
    registry = TransferRegistry(TransferRepository(manager))
    storage_gw, file_gw = FakeStorageGateway(), FakeFileGateway()
    backup = BackupManager(storages, folders, files, registry, storage_gw, file_gw,
                           max_retries=3, retry_sleeper=lambda _s: None)
    restore = RestoreManager(files, registry, file_gw, max_retries=3)
    yield {
        "manager": manager, "storages": storages, "folders": folders,
        "files": files, "registry": registry, "backup": backup, "restore": restore,
        "storage_gw": storage_gw, "file_gw": file_gw, "tmp": tmp_path,
    }
    close_db_connection(manager)


def _link(env, name):
    info = env["storage_gw"].create_storage(name)
    sid = env["storages"].create(name)
    env["storages"].set_telegram(sid, info.chat_id, True)
    return sid


class TestExpiredSessionDuringTransfer:
    def test_upload_stops_at_once_with_the_sign_in_message(self, env, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        (source / "a.txt").write_bytes(b"payload")
        sid = _link(env, "Session")
        env["file_gw"].fail_next_upload_with = SessionExpiredError()

        with pytest.raises(SessionExpiredError) as excinfo:
            env["backup"].run(env["backup"].plan(sid, source))
        assert "sign in again" in str(excinfo.value).lower()
        # no useless retries: the file fails on the first attempt
        transfer = env["registry"]._repo.list_recent()[0]
        assert transfer.status == TransferState.FAILED.value
        assert transfer.attempts <= 1

    def test_an_expired_session_stops_a_multi_file_run_early(self, env, tmp_path):
        """No point uploading 200 more files once Telegram rejects the session."""
        source = tmp_path / "src"
        source.mkdir()
        for index in range(200):
            (source / f"f{index:03d}.txt").write_bytes(b"payload")
        sid = _link(env, "Batch")
        env["file_gw"].fail_next_upload_with = SessionExpiredError()

        with pytest.raises(SessionExpiredError):
            env["backup"].run(env["backup"].plan(sid, source))
        # only the first file was attempted; the run stopped there
        assert len(env["file_gw"].uploaded_parts) == 0
        rows = env["registry"]._repo.list_recent(500)
        assert len(rows) == 1, [r.local_path for r in rows]

    def test_download_stops_at_once_with_the_sign_in_message(self, env, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        (source / "a.txt").write_bytes(b"payload")
        sid = _link(env, "Session")
        env["backup"].run(env["backup"].plan(sid, source))
        record = [f for f in env["files"].list_by_storage(sid) if f.is_backed_up][0]

        env["file_gw"].fail_next_download_with = SessionExpiredError()
        with pytest.raises(SessionExpiredError) as excinfo:
            env["restore"].restore_files([record], tmp_path / "out")
        assert "sign in again" in str(excinfo.value).lower()
        transfer = env["registry"]._repo.list_recent()[0]
        assert transfer.status == TransferState.FAILED.value

    def test_deleted_message_fails_that_file_only(self, env, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        for name in ("a.txt", "b.txt"):
            (source / name).write_bytes(b"x")
        sid = _link(env, "Gone")
        env["backup"].run(env["backup"].plan(sid, source))
        records = [f for f in env["files"].list_by_storage(sid) if f.is_backed_up]
        env["file_gw"].fail_next_download_with = RemoteItemMissingError()

        result = env["restore"].restore_files(records, tmp_path / "out")
        assert result.restored == 1
        assert len(result.failed) == 1
        assert "run a backup again" in result.failed[0][1].lower()


class TestStorageRepair:
    """A deleted group must be recoverable without losing the local index."""

    def _service(self, env, events):
        from teloude.application.services import EventBus, StorageService

        bus = EventBus()
        bus.subscribe("storages_changed", events.append)
        return StorageService(
            env["storages"], env["folders"], env["files"], env["storage_gw"], bus,
        )

    def test_repair_creates_a_new_group_and_clears_cloud_links(self, env, tmp_path):
        source = tmp_path / "src"
        source.mkdir()
        (source / "a.txt").write_bytes(b"payload")
        sid = _link(env, "Damaged")
        env["backup"].run(env["backup"].plan(sid, source))
        assert all(f.is_backed_up for f in env["files"].list_by_storage(sid))
        old_chat_id = env["storages"].get(sid).telegram_chat_id

        events = []
        service = self._service(env, events)
        repaired = service.repair_storage(sid, confirm=True)

        assert repaired.telegram_chat_id is not None
        assert repaired.telegram_chat_id != old_chat_id, "a new group must be created"
        rows = env["files"].list_by_storage(sid)
        assert rows and all(not r.is_backed_up for r in rows), \
            "cloud links of a deleted group must not survive"
        assert all(r.telegram_msg_id is None for r in rows)
        # the index itself is kept: the next backup knows what to upload
        assert [r.relative_path for r in rows] == ["src/a.txt"]
        assert any(e.get("repaired") for e in events), events

        # and that next backup really uploads everything again
        report = env["backup"].run(env["backup"].plan(sid, source))
        assert report.uploaded == 1 and report.failed == []
        assert all(f.is_backed_up for f in env["files"].list_by_storage(sid))

    def test_repair_requires_confirmation(self, env, tmp_path):
        sid = _link(env, "NeedsConfirm")
        service = self._service(env, [])
        with pytest.raises(ServiceError) as excinfo:
            service.repair_storage(sid)
        assert "confirmation" in str(excinfo.value).lower()
        # nothing changed: no second group was created
        assert len([c for c in env["storage_gw"].calls if c.startswith("create_storage")]) == 1

    def test_repair_reports_unknown_storage(self, env):
        service = self._service(env, [])
        with pytest.raises(ServiceError) as excinfo:
            service.repair_storage(9999, confirm=True)
        assert "unknown storage" in str(excinfo.value).lower()

    def test_refresh_after_the_group_vanished_points_at_repair(self, env, tmp_path):
        sid = _link(env, "Vanished")

        class DeadGroupGateway(FakeStorageGateway):
            def list_topics(self, chat_id):
                raise StorageUnavailableError()

        events = []
        from teloude.application.services import EventBus, StorageService

        bus = EventBus()
        service = StorageService(
            env["storages"], env["folders"], env["files"], DeadGroupGateway(), bus,
        )
        with pytest.raises(ServiceError) as excinfo:
            service.refresh_storage(sid)
        message = str(excinfo.value)
        assert "no longer available" in message.lower()
        assert "repair" in message.lower(), message
        _ = events


class TestExpiredSessionSurfacesInTheServices:
    def test_backup_service_reports_sign_in_again_on_the_bus(self, env, tmp_path):
        from teloude.application.services import BackupService, EventBus

        source = tmp_path / "src"
        source.mkdir()
        (source / "a.txt").write_bytes(b"payload")
        sid = _link(env, "Bus")
        env["file_gw"].fail_next_upload_with = SessionExpiredError()

        bus = EventBus()
        seen = []
        bus.subscribe("auth_state", seen.append)
        done = []
        bus.subscribe("backup_done", done.append)

        service = BackupService(env["backup"], env["files"], bus)
        service.start(sid, source)
        import time

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not done:
            time.sleep(0.01)

        assert done, "the backup run must end"
        assert seen and seen[0].get("expired") is True
        assert seen[0]["state"] == "signed_out"
        assert "sign in again" in seen[0]["reason"].lower()
        assert done[0].get("error") is True

    def test_auth_service_clears_its_state_when_marked_expired(self):
        from teloude.application.services import AuthService, EventBus
        from teloude.infrastructure.telegram.auth import AuthState
        from teloude.infrastructure.telegram.fakes import FakeAuth

        bus = EventBus()
        seen = []
        bus.subscribe("auth_state", seen.append)
        auth = AuthService(FakeAuth(), bus)
        auth.start_login("+10000000000")

        auth.mark_session_expired("Your Telegram session has ended.")

        assert seen[-1]["expired"] is True
        assert seen[-1]["state"] == AuthState.SIGNED_OUT.value
