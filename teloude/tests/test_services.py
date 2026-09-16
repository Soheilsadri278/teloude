# teloude/tests/test_services.py
"""Application service tests (Qt-free orchestration over fakes)."""
import threading

import pytest

from teloude.config import AppConfig
from teloude.application.services import (
    AuthService,
    BackupService,
    EventBus,
    RestoreService,
    SearchService,
    ServiceError,
    SettingsService,
    StorageService,
)
from teloude.core.backup import BackupManager
from teloude.core.restore import RestoreManager
from teloude.core.transfers import TransferRegistry
from teloude.infrastructure.database import DatabaseManager, close_db_connection
from teloude.infrastructure.repositories import (
    FileRepository,
    FolderRepository,
    SettingsRepository,
    StorageRepository,
    TransferRepository,
)
from teloude.infrastructure.telegram.auth import AuthState
from teloude.infrastructure.telegram.fakes import FakeAuth, FakeFileGateway, FakeStorageGateway
from teloude.infrastructure.telegram.storage import DocumentMeta


@pytest.fixture()
def ctx(tmp_path):
    manager = DatabaseManager(AppConfig(database_path=str(tmp_path / "s.db")))
    assert manager.initialize()
    storages = StorageRepository(manager)
    folders = FolderRepository(manager)
    files = FileRepository(manager)
    transfers = TransferRepository(manager)
    registry = TransferRegistry(transfers)
    settings = SettingsRepository(manager)
    storage_gw = FakeStorageGateway()
    file_gw = FakeFileGateway()
    backup_manager = BackupManager(
        storages, folders, files, registry, storage_gw, file_gw,
        max_retries=1, retry_sleeper=lambda s: None,
    )
    restore_manager = RestoreManager(files, registry, file_gw, max_retries=1)
    bus = EventBus()
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"alpha")
    yield {
        "manager": manager, "bus": bus, "storages": storages, "files": files,
        "auth": AuthService(FakeAuth(), bus),
        "storage_svc": StorageService(storages, folders, files, storage_gw, bus),
        "backup_svc": BackupService(backup_manager, files, bus),
        "restore_svc": RestoreService(restore_manager, files, storages, bus),
        "search_svc": SearchService(manager),
        "settings_svc": SettingsService(settings),
        "storage_gw": storage_gw, "file_gw": file_gw, "src": src, "tmp": tmp_path,
    }
    close_db_connection(manager)


def _wait(event, timeout=10.0):
    assert event.wait(timeout), "timed out waiting for service event"


class TestEventBus:
    def test_emit_and_isolation(self):
        bus = EventBus()
        seen = []
        bus.subscribe("x", seen.append)
        bus.subscribe("x", lambda p: 1 / 0)  # failing subscriber must not break others
        bus.subscribe("x", lambda p: seen.append("second"))
        bus.emit("x", 1)
        assert seen == [1, "second"]


class TestAuthService:
    def test_login_flow_and_events(self, ctx):
        states = []
        ctx["bus"].subscribe("auth_state", lambda p: states.append(p["state"]))
        assert ctx["auth"].start_login("+1000") is AuthState.CODE_SENT
        assert ctx["auth"].submit_code("+1000", "11111") is AuthState.AUTHORIZED
        assert ctx["auth"].state is AuthState.AUTHORIZED
        ctx["auth"].logout()
        assert ctx["auth"].state is AuthState.SIGNED_OUT
        assert states == ["code_sent", "authorized", "signed_out"]


class TestStorageService:
    def test_create_storage(self, ctx):
        changed = []
        ctx["bus"].subscribe("storages_changed", lambda p: changed.append(p))
        record = ctx["storage_svc"].create_storage("Photos")
        assert record.telegram_chat_id is not None
        assert changed and changed[0]["storage_id"] == record.id
        folders = ctx["storage_svc"]._folders.list_by_storage(record.id)
        assert any(f.telegram_topic_id == 1 for f in folders)  # renamed General root

    def test_create_validates(self, ctx):
        with pytest.raises(ServiceError):
            ctx["storage_svc"].create_storage("   ")
        ctx["storage_svc"].create_storage("Photos")
        with pytest.raises(ServiceError):
            ctx["storage_svc"].create_storage("Photos")

    def test_adopt_rebuilds_index(self, ctx):
        info = ctx["storage_gw"].create_storage("Docs")  # "another PC" created it
        ctx["storage_gw"].ensure_topic(info.chat_id, "Docs", is_root=True)
        ctx["storage_gw"].add_document(
            info.chat_id, 1, DocumentMeta(msg_id=501, file_name="old.pdf", size=9)
        )
        record = ctx["storage_svc"].adopt_storage("Docs")
        assert record.telegram_chat_id == info.chat_id
        rows = ctx["files"].list_by_storage(record.id)
        assert len(rows) == 1 and rows[0].is_backed_up is True
        assert rows[0].telegram_msg_id == 501

    def test_adopt_missing_group(self, ctx):
        with pytest.raises(ServiceError, match="No Telegram group"):
            ctx["storage_svc"].adopt_storage("Ghost")

    def test_delete_storage_cloud(self, ctx):
        record = ctx["storage_svc"].create_storage("Temp")
        ctx["storage_svc"].delete_storage_cloud(record.id)
        assert ctx["storages"].get(record.id) is None
        assert ctx["storage_gw"].find_storage("Temp") is None


class TestBackupService:
    def _run_backup(self, ctx):
        record = ctx["storage_svc"].create_storage("Photos")
        done = threading.Event()
        payloads = []
        ctx["bus"].subscribe("backup_done", lambda p: (payloads.append(p), done.set()))
        ctx["backup_svc"].start(record.id, ctx["src"])
        _wait(done)
        return payloads[0]

    def test_start_runs_to_completion(self, ctx):
        payload = self._run_backup(ctx)
        assert payload["uploaded"] == 1 and payload["failed"] == []

    def test_double_start_rejected(self, ctx):
        record = ctx["storage_svc"].create_storage("Photos")
        done = threading.Event()
        ctx["bus"].subscribe("backup_done", lambda p: done.set())
        ctx["backup_svc"].start(record.id, ctx["src"])
        with pytest.raises(ServiceError, match="already running"):
            ctx["backup_svc"].start(record.id, ctx["src"])
        _wait(done)

    def test_pause_resume_cancel_guards(self, ctx):
        with pytest.raises(ServiceError):
            ctx["backup_svc"].pause()
        with pytest.raises(ServiceError):
            ctx["backup_svc"].resume()
        with pytest.raises(ServiceError):
            ctx["backup_svc"].cancel()


class TestRestoreService:
    def _backed_up(self, ctx):
        record = ctx["storage_svc"].create_storage("Photos")
        done = threading.Event()
        ctx["bus"].subscribe("backup_done", lambda p: done.set())
        ctx["backup_svc"].start(record.id, ctx["src"])
        _wait(done)
        return record

    def test_restore_storage(self, ctx):
        record = self._backed_up(ctx)
        done = threading.Event()
        payloads = []
        ctx["bus"].subscribe("restore_done", lambda p: (payloads.append(p), done.set()))
        dest = ctx["tmp"] / "out"
        ctx["restore_svc"].start_storage(record.id, dest)
        _wait(done)
        assert payloads[0]["restored"] == 1
        assert (dest / "a.txt").read_bytes() == b"alpha"

    def test_restore_unknown(self, ctx):
        with pytest.raises(ServiceError):
            ctx["restore_svc"].start_files([424242], ctx["tmp"] / "out")
        with pytest.raises(ServiceError):
            ctx["restore_svc"].start_storage(424242, ctx["tmp"] / "out")

    def test_search_and_settings(self, ctx):
        self._backed_up(ctx)
        assert len(ctx["search_svc"].search("a.txt")) == 1
        assert ctx["settings_svc"].get_speed_limit_mbps() is None
        ctx["settings_svc"].set_speed_limit_mbps(5.0)
        assert ctx["settings_svc"].get_speed_limit_mbps() == 5.0
        with pytest.raises(ServiceError):
            ctx["settings_svc"].set_speed_limit_mbps(-1)
