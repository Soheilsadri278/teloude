# teloude/infrastructure/database.py
"""Centralized SQLite access and versioned schema migrations.

The database is Teloude's local index/cache: storages, folder hierarchy,
file metadata, Telegram message references, transfer state, and settings.
Telegram remains the remote source of backed-up data.

All SQL lives here (plus repositories.py which builds on DatabaseManager).
UI and business logic must never embed SQL.
"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, List, Optional
from teloude.config import AppConfig
import logging

logger = logging.getLogger("DatabaseManager")

SCHEMA_VERSION = 2


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """Base schema: storages, folders, files, telegram_messages, transfers, settings."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS storages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            telegram_chat_id INTEGER NULL,
            is_forum INTEGER NOT NULL DEFAULT 0,
            total_size INTEGER NOT NULL DEFAULT 0,
            file_count INTEGER NOT NULL DEFAULT 0,
            folder_count INTEGER NOT NULL DEFAULT 0,
            last_backup_at TEXT NULL,
            created_at TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            storage_id INTEGER NOT NULL REFERENCES storages(id) ON DELETE CASCADE,
            parent_id INTEGER NULL REFERENCES folders(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            telegram_topic_id INTEGER NULL,
            topic_name TEXT NULL,
            UNIQUE (storage_id, relative_path)
        );
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            storage_id INTEGER NOT NULL REFERENCES storages(id) ON DELETE CASCADE,
            folder_id INTEGER NULL REFERENCES folders(id) ON DELETE SET NULL,
            local_path TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            mtime REAL NULL,
            sha256 TEXT NULL,
            fingerprint TEXT NULL,
            telegram_chat_id INTEGER NULL,
            telegram_msg_id INTEGER NULL,
            is_backed_up INTEGER NOT NULL DEFAULT 0,
            backup_at TEXT NULL,
            UNIQUE (storage_id, relative_path)
        );
        CREATE TABLE IF NOT EXISTS telegram_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            storage_id INTEGER NOT NULL REFERENCES storages(id) ON DELETE CASCADE,
            telegram_chat_id INTEGER NOT NULL,
            telegram_msg_id INTEGER NOT NULL,
            topic_id INTEGER NULL,
            file_id INTEGER NULL REFERENCES files(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            UNIQUE (telegram_chat_id, telegram_msg_id)
        );
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NULL REFERENCES files(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            storage_id INTEGER NULL REFERENCES storages(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'queued',
            total_bytes INTEGER NOT NULL DEFAULT 0,
            done_bytes INTEGER NOT NULL DEFAULT 0,
            local_path TEXT NULL,
            error TEXT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE (kind, file_id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    _backfill_legacy_shapes(conn)


def _existing_columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _existing_tables(conn: sqlite3.Connection) -> set:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _ensure_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name not in _existing_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        logger.info(f"Backfilled column {table}.{name}.")


def _rename_column(conn: sqlite3.Connection, table: str, old: str, new: str) -> None:
    cols = _existing_columns(conn, table)
    if old in cols and new not in cols:
        conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
        logger.info(f"Renamed column {table}.{old} to {new}.")


def _ensure_index(
    conn: sqlite3.Connection, name: str, table: str, columns: str
) -> None:
    """Creates an index only when the table and all indexed columns exist.

    Plain CREATE INDEX inside the migration script would fail on legacy table
    shapes that lack the new columns, so index creation is guarded here.
    """
    if table not in _existing_tables(conn):
        return
    existing = _existing_columns(conn, table)
    wanted = [c.strip().strip('"') for c in columns.split(",")]
    if all(c in existing for c in wanted):
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})")


def _backfill_legacy_shapes(conn: sqlite3.Connection) -> None:
    """Upgrades pre-v1 (Phase 0) table shapes in place: renames + missing columns.

    Fresh databases are unaffected (exercises as no-ops). Legacy user data is
    preserved; new columns receive safe defaults.
    """
    tables = _existing_tables(conn)
    if "storages" in tables:
        _rename_column(conn, "storages", "last_backup_date", "last_backup_at")
        _ensure_column(conn, "storages", "telegram_chat_id", "telegram_chat_id INTEGER NULL")
        _ensure_column(conn, "storages", "is_forum", "is_forum INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "storages", "folder_count", "folder_count INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "storages", "last_backup_at", "last_backup_at TEXT NULL")
        # NB: ALTER TABLE ADD COLUMN rejects expression defaults, so the
        # column is added nullable and existing rows are stamped explicitly.
        # Fresh tables still get the strftime() default from CREATE TABLE.
        _ensure_column(conn, "storages", "created_at", "created_at TEXT NULL")
        conn.execute(
            "UPDATE storages SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            " WHERE created_at IS NULL"
        )
    if "files" in tables:
        _rename_column(conn, "files", "sha256_hash", "sha256")
        _ensure_column(conn, "files", "folder_id", "folder_id INTEGER NULL")
        _ensure_column(conn, "files", "size", "size INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "files", "mtime", "mtime REAL NULL")
        _ensure_column(conn, "files", "sha256", "sha256 TEXT NULL")
        _ensure_column(conn, "files", "fingerprint", "fingerprint TEXT NULL")
        _ensure_column(conn, "files", "telegram_chat_id", "telegram_chat_id INTEGER NULL")
        _ensure_column(conn, "files", "telegram_msg_id", "telegram_msg_id INTEGER NULL")
        _ensure_column(conn, "files", "backup_at", "backup_at TEXT NULL")
    if "transfers" in tables:
        _rename_column(conn, "transfers", "uploaded_bytes", "done_bytes")
        _rename_column(conn, "transfers", "last_error", "error")
        _ensure_column(conn, "transfers", "kind", "kind TEXT NOT NULL DEFAULT 'upload'")
        _ensure_column(conn, "transfers", "total_bytes", "total_bytes INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "transfers", "done_bytes", "done_bytes INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "transfers", "local_path", "local_path TEXT NULL")
        _ensure_column(conn, "transfers", "error", "error TEXT NULL")
        _ensure_column(conn, "transfers", "attempts", "attempts INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "transfers", "updated_at", "updated_at TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_transfers_kind_file"
            " ON transfers(kind, file_id) WHERE file_id IS NOT NULL"
        )
    _ensure_index(conn, "idx_folders_storage", "folders", "storage_id")
    _ensure_index(conn, "idx_files_storage", "files", "storage_id")
    _ensure_index(conn, "idx_files_sha", "files", "sha256")
    _ensure_index(conn, "idx_transfers_status", "transfers", "status")


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts_probe")
        return True
    except sqlite3.Error:
        return False


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Full-text search index over file names and relative paths (FTS5 if available)."""
    if not _fts5_available(conn):
        logger.warning("SQLite FTS5 unavailable; search will use LIKE fallback.")
        return
    if "files" not in _existing_tables(conn):
        # External-content FTS needs the files table; without it the LIKE
        # fallback covers search until v1 has been applied.
        logger.warning("Skipping FTS index: files table is missing.")
        return
    conn.executescript(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
            file_name, relative_path, content='files', content_rowid='id'
        );
        INSERT INTO files_fts(files_fts) VALUES('rebuild');
        CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
            INSERT INTO files_fts(rowid, file_name, relative_path)
            VALUES (new.id, new.file_name, new.relative_path);
        END;
        CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
            INSERT INTO files_fts(files_fts, rowid, file_name, relative_path)
            VALUES ('delete', old.id, old.file_name, old.relative_path);
        END;
        CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
            INSERT INTO files_fts(files_fts, rowid, file_name, relative_path)
            VALUES ('delete', old.id, old.file_name, old.relative_path);
            INSERT INTO files_fts(rowid, file_name, relative_path)
            VALUES (new.id, new.file_name, new.relative_path);
        END;
        """
    )


MIGRATIONS: List[tuple] = [(1, _migrate_v1), (2, _migrate_v2)]


class DatabaseManager:
    """
    Handles all SQLite connection and schema management.
    Centralizes database access, fulfilling the infrastructure layer role.

    One connection is shared by every thread (the backup/restore engines run in
    workers while the UI keeps reading), so all access is serialized through a
    re-entrant lock. Without it, a second thread's implicit transaction could
    join an in-flight one: its commit would persist half-written work, and on
    failure its rollback would discard the other thread's rows.
    """

    def __init__(self, config: AppConfig):
        self._db_path = config.database_path
        self.connection: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()

    def connect(self) -> None:
        """Establishes the SQLite connection."""
        try:
            with self._lock:
                self.connection = sqlite3.connect(
                    self._db_path, check_same_thread=False
                )
                self.connection.execute("PRAGMA foreign_keys = ON")
                try:
                    self.connection.execute("PRAGMA journal_mode = WAL")
                    self.connection.execute("PRAGMA synchronous = NORMAL")
                except sqlite3.Error as exc:
                    logger.warning(f"Could not enable WAL mode: {exc}")
            logger.info(f"Successfully connected to database at {self._db_path}")
        except sqlite3.Error as e:
            logger.error(f"Database connection failed: {e}")
            raise

    def execute_query(self, query: str, params: tuple = (), fetch: bool = False) -> Any:
        """Helper method to execute read/write queries (thread-safe)."""
        with self._lock:
            if not self.connection:
                raise ConnectionError("Database connection is not established.")
            cursor = self.connection.cursor()
            try:
                cursor.execute(query, params)
                if fetch:
                    return cursor.fetchall()
                self.connection.commit()
                return cursor.lastrowid
            except sqlite3.Error as e:
                logger.error(f"SQL Error executing '{query[:50]}...': {e}")
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """Yields a cursor inside a transaction (commit on success, rollback on error).

        The lock is held for the whole transaction so another thread can neither
        commit nor roll back somebody else's half-finished work.
        """
        with self._lock:
            if not self.connection:
                raise ConnectionError("Database connection is not established.")
            cursor = self.connection.cursor()
            try:
                yield cursor
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise

    def get_schema_version(self) -> int:
        with self._lock:
            if not self.connection:
                raise ConnectionError("Database connection is not established.")
            row = self.connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
                if self._table_exists("schema_migrations")
                else "SELECT 0"
            ).fetchone()
            return int(row[0] or 0)

    def _table_exists(self, name: str) -> bool:
        with self._lock:
            assert self.connection is not None
            row = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            return row is not None

    def has_fts(self) -> bool:
        """True when the FTS5 search index exists (search uses LIKE fallback otherwise)."""
        with self._lock:
            if not self.connection:
                return False
            return self._table_exists("files_fts")

    def close(self) -> None:
        """Closes the connection; waits for any in-flight transaction first."""
        with self._lock:
            if self.connection is None:
                return
            try:
                self.connection.close()
            except sqlite3.Error as exc:
                logger.warning(f"Error closing database connection: {exc}")
            finally:
                self.connection = None
            logger.info("Database connection closed.")

    def initialize(self) -> bool:
        """Initializes the DB connection and runs all necessary migrations."""
        try:
            self.connect()
            self.run_migrations()
            return True
        except Exception as e:
            logger.critical(f"Database initialization failed critically: {e}")
            return False

    def run_migrations(self) -> None:
        """Applies every pending migration in order; safe to run repeatedly."""
        if not self.connection:
            raise ConnectionError("Database connection is not established.")
        with self.transaction() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
        current = self.get_schema_version()
        for version, func in MIGRATIONS:
            if version > current:
                logger.info(f"Applying database migration v{version}.")
                func(self.connection)
                self.execute_query(
                    "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
                    "VALUES (?, ?)",
                    (version, utcnow()),
                )
                logger.info(f"Migration to version {version} completed successfully.")


# Helper function to ensure clean closure
def close_db_connection(manager: Optional[DatabaseManager]) -> None:
    if manager is not None:
        manager.close()
