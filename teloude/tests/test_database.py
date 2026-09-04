# teloude/tests/test_database.py
"""Database layer tests: schema, migrations, repositories, transactions."""
import sqlite3

import pytest

from teloude.config import AppConfig
from teloude.infrastructure.database import (
    DatabaseManager,
    close_db_connection,
    SCHEMA_VERSION,
)
from teloude.infrastructure.repositories import (
    StorageRepository,
    FolderRepository,
    FileRepository,
    TransferRepository,
    SettingsRepository,
)


@pytest.fixture()
def db(tmp_path):
    cfg = AppConfig(database_path=str(tmp_path / "test.db"))
    manager = DatabaseManager(cfg)
    assert manager.initialize()
    yield manager
    close_db_connection(manager)


class TestSchema:
    def test_version_and_tables(self, db):
        assert db.get_schema_version() == SCHEMA_VERSION
        tables = {
            r[0]
            for r in db.execute_query(
                "SELECT name FROM sqlite_master WHERE type='table'", fetch=True
            )
        }
        for expected in (
            "storages", "folders", "files", "telegram_messages",
            "transfers", "settings", "schema_migrations",
        ):
            assert expected in tables

    def test_migrations_idempotent(self, db):
        db.run_migrations()
        db.run_migrations()
        assert db.get_schema_version() == SCHEMA_VERSION

    def test_upgrade_from_legacy_shape(self, tmp_path):
        """A Phase-0-shaped database upgrades in place with data preserved."""
        path = str(tmp_path / "old.db")
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE storages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT UNIQUE NOT NULL, total_size REAL DEFAULT 0.0,"
            " file_count INTEGER DEFAULT 0, last_backup_date TEXT)"
        )
        con.execute(
            "CREATE TABLE files (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " storage_id INTEGER REFERENCES storages(id),"
            " local_path TEXT UNIQUE NOT NULL, relative_path TEXT NOT NULL,"
            " file_name TEXT NOT NULL, sha256_hash TEXT,"
            " is_backed_up BOOLEAN DEFAULT 0)"
        )
        con.execute(
            "CREATE TABLE transfers (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " file_id INTEGER REFERENCES files(id),"
            " storage_id INTEGER REFERENCES storages(id),"
            " status TEXT NOT NULL DEFAULT 'queued',"
            " uploaded_bytes BLOB DEFAULT 0, last_error TEXT,"
            " UNIQUE (file_id))"
        )
        con.execute("INSERT INTO storages(name, total_size) VALUES ('Legacy', 5.0)")
        con.commit()
        con.close()
        manager = DatabaseManager(AppConfig(database_path=path))
        assert manager.initialize()
        try:
            assert manager.get_schema_version() == SCHEMA_VERSION
            # Legacy data and renamed columns survive the upgrade.
            rows = manager.execute_query(
                "SELECT storages.name, storages.total_size,"
                " storages.telegram_chat_id, files.sha256 FROM storages"
                " LEFT JOIN files ON files.storage_id = storages.id",
                fetch=True,
            )
            assert rows[0][0] == "Legacy" and rows[0][2] is None
            # New-shape writes work on the upgraded database.
            repos = StorageRepository(manager)
            assert repos.get_by_name("Legacy") is not None
        finally:
            close_db_connection(manager)

    def test_transaction_rollback(self, db):
        with pytest.raises(RuntimeError):
            with db.transaction() as cur:
                cur.execute(
                    "INSERT INTO storages(name, created_at) VALUES (?, ?)",
                    ("Tmp", "2020-01-01"),
                )
                raise RuntimeError("boom")
        rows = db.execute_query("SELECT * FROM storages WHERE name='Tmp'", fetch=True)
        assert rows == []


class TestRepositories:
    def test_storage_crud(self, db):
        repos = StorageRepository(db)
        sid = repos.create("Photos")
        rec = repos.get(sid)
        assert rec is not None and rec.name == "Photos"
        assert rec.telegram_chat_id is None
        repos.set_telegram(sid, -100123, True)
        assert repos.get(sid).telegram_chat_id == -100123
        repos.update_stats(sid, 100, 5, 2, "2026-01-01")
        rec = repos.get(sid)
        assert (rec.total_size, rec.file_count, rec.folder_count) == (100, 5, 2)
        assert [s.name for s in repos.list_all()] == ["Photos"]

    def test_folder_ensure_idempotent(self, db):
        sid = StorageRepository(db).create("Docs")
        folders = FolderRepository(db)
        fid = folders.ensure(sid, "2026/Wedding", "Wedding")
        assert folders.ensure(sid, "2026/Wedding", "Wedding") == fid
        folders.set_topic(fid, 42, "2026 / Wedding")
        assert folders.get_by_path(sid, "2026/Wedding").telegram_topic_id == 42

    def test_file_upsert_and_rebackup_flag(self, db):
        sid = StorageRepository(db).create("Docs")
        files = FileRepository(db)
        fid = files.upsert(sid, None, "/a/b.txt", "b.txt", "b.txt", 10, 1.0, "sha1", "fp1")
        assert files.upsert(sid, None, "/a/b.txt", "b.txt", "b.txt", 10, 1.0, "sha1", "fp1") == fid
        files.mark_backed_up(fid, -1001, 7)
        assert files.get(fid).is_backed_up is True
        # Content change resets the backed-up flag.
        files.upsert(sid, None, "/a/b.txt", "b.txt", "b.txt", 11, 2.0, "sha2", "fp2")
        rec = files.get(fid)
        assert rec.is_backed_up is False and rec.telegram_msg_id is None
        files.record_message(sid, -1001, 7, None, fid)

    def test_transfer_lifecycle(self, db):
        sid = StorageRepository(db).create("Docs")
        fid = FileRepository(db).upsert(
            sid, None, "/a/b.txt", "b.txt", "b.txt", 100, 1.0, "s", "f"
        )
        transfers = TransferRepository(db)
        tid = transfers.create_or_reset("upload", sid, fid, 100, "/a/b.txt")
        # Reset path reuses the same row.
        assert transfers.create_or_reset("upload", sid, fid, 100, "/a/b.txt") == tid
        transfers.set_status(tid, "uploading")
        transfers.update_progress(tid, 40)
        rec = transfers.get(tid)
        assert (rec.status, rec.done_bytes) == ("uploading", 40)
        assert [t.id for t in transfers.list_active()] == [tid]
        transfers.set_status(tid, "completed")
        assert transfers.list_active() == []
        transfers.bump_attempts(tid)
        assert transfers.get(tid).attempts == 1

    def test_settings(self, db):
        settings = SettingsRepository(db)
        assert settings.get("missing", "dflt") == "dflt"
        settings.set("speed", "5")
        assert settings.get("speed") == "5"
        settings.set("speed", "10")
        assert settings.get("speed") == "10"
