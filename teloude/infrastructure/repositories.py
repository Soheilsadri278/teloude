# teloude/infrastructure/repositories.py
"""Typed repositories over DatabaseManager.

This module (with database.py) is the ONLY place SQL may appear.
Application, core, and UI layers use these repositories, never raw SQL.
"""
from dataclasses import dataclass
from typing import List, Optional
from .database import DatabaseManager, utcnow


@dataclass
class StorageRecord:
    id: int
    name: str
    telegram_chat_id: Optional[int] = None
    is_forum: bool = False
    total_size: int = 0
    file_count: int = 0
    folder_count: int = 0
    last_backup_at: Optional[str] = None


@dataclass
class FolderRecord:
    id: int
    storage_id: int
    parent_id: Optional[int]
    name: str
    relative_path: str
    telegram_topic_id: Optional[int] = None
    topic_name: Optional[str] = None


@dataclass
class FileRecord:
    id: int
    storage_id: int
    folder_id: Optional[int]
    local_path: str
    relative_path: str
    file_name: str
    size: int = 0
    mtime: Optional[float] = None
    sha256: Optional[str] = None
    fingerprint: Optional[str] = None
    telegram_chat_id: Optional[int] = None
    telegram_msg_id: Optional[int] = None
    is_backed_up: bool = False
    backup_at: Optional[str] = None


@dataclass
class TransferRecord:
    id: int
    file_id: Optional[int]
    kind: str  # 'upload' | 'download'
    storage_id: Optional[int]
    status: str
    total_bytes: int = 0
    done_bytes: int = 0
    local_path: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0


class StorageRepository:
    def __init__(self, db: DatabaseManager):
        self._db = db

    def create(self, name: str) -> int:
        return int(
            self._db.execute_query(
                "INSERT INTO storages(name, created_at) VALUES (?, ?)",
                (name, utcnow()),
            )
        )

    def get(self, storage_id: int) -> Optional[StorageRecord]:
        rows = self._db.execute_query(
            "SELECT id, name, telegram_chat_id, is_forum, total_size, file_count,"
            " folder_count, last_backup_at FROM storages WHERE id=?",
            (storage_id,),
            fetch=True,
        )
        return self._row(rows[0]) if rows else None

    def get_by_name(self, name: str) -> Optional[StorageRecord]:
        rows = self._db.execute_query(
            "SELECT id, name, telegram_chat_id, is_forum, total_size, file_count,"
            " folder_count, last_backup_at FROM storages WHERE name=?",
            (name,),
            fetch=True,
        )
        return self._row(rows[0]) if rows else None

    def list_all(self) -> List[StorageRecord]:
        rows = self._db.execute_query(
            "SELECT id, name, telegram_chat_id, is_forum, total_size, file_count,"
            " folder_count, last_backup_at FROM storages ORDER BY name",
            fetch=True,
        )
        return [self._row(r) for r in rows]

    def set_telegram(self, storage_id: int, chat_id: int, is_forum: bool = True) -> None:
        self._db.execute_query(
            "UPDATE storages SET telegram_chat_id=?, is_forum=? WHERE id=?",
            (chat_id, int(is_forum), storage_id),
        )

    def update_stats(
        self,
        storage_id: int,
        total_size: int,
        file_count: int,
        folder_count: int,
        last_backup_at: Optional[str] = None,
    ) -> None:
        self._db.execute_query(
            "UPDATE storages SET total_size=?, file_count=?, folder_count=?,"
            " last_backup_at=? WHERE id=?",
            (total_size, file_count, folder_count, last_backup_at, storage_id),
        )

    def delete(self, storage_id: int) -> None:
        self._db.execute_query("DELETE FROM storages WHERE id=?", (storage_id,))

    @staticmethod
    def _row(r: tuple) -> StorageRecord:
        return StorageRecord(
            id=r[0], name=r[1], telegram_chat_id=r[2], is_forum=bool(r[3]),
            total_size=r[4] or 0, file_count=r[5] or 0, folder_count=r[6] or 0,
            last_backup_at=r[7],
        )


class FolderRepository:
    def __init__(self, db: DatabaseManager):
        self._db = db

    def ensure(
        self,
        storage_id: int,
        relative_path: str,
        name: str,
        parent_id: Optional[int] = None,
    ) -> int:
        """Idempotent folder registration; returns the folder id."""
        existing = self.get_by_path(storage_id, relative_path)
        if existing:
            return existing.id
        return int(
            self._db.execute_query(
                "INSERT INTO folders(storage_id, parent_id, name, relative_path)"
                " VALUES (?, ?, ?, ?)",
                (storage_id, parent_id, name, relative_path),
            )
        )

    def get_by_path(self, storage_id: int, relative_path: str) -> Optional[FolderRecord]:
        rows = self._db.execute_query(
            "SELECT id, storage_id, parent_id, name, relative_path,"
            " telegram_topic_id, topic_name FROM folders"
            " WHERE storage_id=? AND relative_path=?",
            (storage_id, relative_path),
            fetch=True,
        )
        return self._row(rows[0]) if rows else None

    def list_by_storage(self, storage_id: int) -> List[FolderRecord]:
        rows = self._db.execute_query(
            "SELECT id, storage_id, parent_id, name, relative_path,"
            " telegram_topic_id, topic_name FROM folders"
            " WHERE storage_id=? ORDER BY relative_path",
            (storage_id,),
            fetch=True,
        )
        return [self._row(r) for r in rows]

    def set_topic(self, folder_id: int, topic_id: int, topic_name: str) -> None:
        self._db.execute_query(
            "UPDATE folders SET telegram_topic_id=?, topic_name=? WHERE id=?",
            (topic_id, topic_name, folder_id),
        )

    @staticmethod
    def _row(r: tuple) -> FolderRecord:
        return FolderRecord(
            id=r[0], storage_id=r[1], parent_id=r[2], name=r[3],
            relative_path=r[4], telegram_topic_id=r[5], topic_name=r[6],
        )


class FileRepository:
    def __init__(self, db: DatabaseManager):
        self._db = db

    _COLS = (
        "id, storage_id, folder_id, local_path, relative_path, file_name, size,"
        " mtime, sha256, fingerprint, telegram_chat_id, telegram_msg_id,"
        " is_backed_up, backup_at"
    )

    def upsert(
        self,
        storage_id: int,
        folder_id: Optional[int],
        local_path: str,
        relative_path: str,
        file_name: str,
        size: int,
        mtime: Optional[float],
        sha256: Optional[str],
        fingerprint: Optional[str],
    ) -> int:
        """Insert or refresh the file index row; resets backed-up flag on content change."""
        existing = self.get_by_path(storage_id, relative_path)
        if existing is None:
            return int(
                self._db.execute_query(
                    "INSERT INTO files(storage_id, folder_id, local_path, relative_path,"
                    " file_name, size, mtime, sha256, fingerprint)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (storage_id, folder_id, local_path, relative_path, file_name,
                     size, mtime, sha256, fingerprint),
                )
            )
        changed = (
            existing.sha256 != sha256
            or existing.size != size
            or existing.local_path != local_path
        )
        with self._db.transaction() as cur:
            cur.execute(
                "UPDATE files SET folder_id=?, local_path=?, file_name=?, size=?,"
                " mtime=?, sha256=?, fingerprint=?,"
                " is_backed_up=CASE WHEN ? THEN 0 ELSE is_backed_up END,"
                " telegram_chat_id=CASE WHEN ? THEN NULL ELSE telegram_chat_id END,"
                " telegram_msg_id=CASE WHEN ? THEN NULL ELSE telegram_msg_id END"
                " WHERE id=?",
                (folder_id, local_path, file_name, size, mtime, sha256, fingerprint,
                 int(changed), int(changed), int(changed), existing.id),
            )
        return existing.id

    def get_by_path(self, storage_id: int, relative_path: str) -> Optional[FileRecord]:
        rows = self._db.execute_query(
            f"SELECT {self._COLS} FROM files WHERE storage_id=? AND relative_path=?",
            (storage_id, relative_path),
            fetch=True,
        )
        return self._row(rows[0]) if rows else None

    def get(self, file_id: int) -> Optional[FileRecord]:
        rows = self._db.execute_query(
            f"SELECT {self._COLS} FROM files WHERE id=?", (file_id,), fetch=True
        )
        return self._row(rows[0]) if rows else None

    def list_by_storage(self, storage_id: int) -> List[FileRecord]:
        rows = self._db.execute_query(
            f"SELECT {self._COLS} FROM files WHERE storage_id=? ORDER BY relative_path",
            (storage_id,),
            fetch=True,
        )
        return [self._row(r) for r in rows]

    def list_unbacked(self, storage_id: int) -> List[FileRecord]:
        rows = self._db.execute_query(
            f"SELECT {self._COLS} FROM files WHERE storage_id=? AND is_backed_up=0"
            " ORDER BY relative_path",
            (storage_id,),
            fetch=True,
        )
        return [self._row(r) for r in rows]

    def find_by_sha(self, sha256: str) -> List[FileRecord]:
        rows = self._db.execute_query(
            f"SELECT {self._COLS} FROM files WHERE sha256=? AND is_backed_up=1",
            (sha256,),
            fetch=True,
        )
        return [self._row(r) for r in rows]

    def mark_backed_up(
        self, file_id: int, chat_id: int, msg_id: int
    ) -> None:
        self._db.execute_query(
            "UPDATE files SET is_backed_up=1, telegram_chat_id=?, telegram_msg_id=?,"
            " backup_at=? WHERE id=?",
            (chat_id, msg_id, utcnow(), file_id),
        )

    def forget_cloud_state(self, storage_id: int) -> int:
        """Clears every cloud link of a storage whose group is gone.

        Used when the Telegram group was deleted: the index (paths, sizes,
        hashes) is kept so the next backup re-uploads everything, but no row may
        keep claiming a cloud copy that no longer exists.
        """
        rows = self._db.execute_query(
            "SELECT COUNT(*) FROM files WHERE storage_id=? AND is_backed_up=1",
            (storage_id,),
            fetch=True,
        )
        forgotten = int(rows[0][0] or 0)
        self._db.execute_query(
            "UPDATE files SET is_backed_up=0, telegram_chat_id=NULL,"
            " telegram_msg_id=NULL, backup_at=NULL WHERE storage_id=?",
            (storage_id,),
        )
        self._db.execute_query(
            "DELETE FROM telegram_messages WHERE storage_id=?", (storage_id,)
        )
        return forgotten

    def record_message(
        self,
        storage_id: int,
        chat_id: int,
        msg_id: int,
        topic_id: Optional[int],
        file_id: Optional[int],
    ) -> None:
        self._db.execute_query(
            "INSERT OR IGNORE INTO telegram_messages"
            "(storage_id, telegram_chat_id, telegram_msg_id, topic_id, file_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (storage_id, chat_id, msg_id, topic_id, file_id, utcnow()),
        )

    @staticmethod
    def _row(r: tuple) -> FileRecord:
        return FileRecord(
            id=r[0], storage_id=r[1], folder_id=r[2], local_path=r[3],
            relative_path=r[4], file_name=r[5], size=r[6] or 0, mtime=r[7],
            sha256=r[8], fingerprint=r[9], telegram_chat_id=r[10],
            telegram_msg_id=r[11], is_backed_up=bool(r[12]), backup_at=r[13],
        )


class TransferRepository:
    ACTIVE = ("queued", "hashing", "uploading", "downloading", "paused",
              "waiting_for_network", "verifying")

    def __init__(self, db: DatabaseManager):
        self._db = db

    def create_or_reset(
        self,
        kind: str,
        storage_id: Optional[int],
        file_id: Optional[int],
        total_bytes: int,
        local_path: Optional[str] = None,
    ) -> int:
        """One transfer row per (kind, file_id); resets progress for retries."""
        if file_id is None:
            return int(
                self._db.execute_query(
                    "INSERT INTO transfers(kind, storage_id, file_id, status,"
                    " total_bytes, done_bytes, local_path, attempts, updated_at)"
                    " VALUES (?, ?, NULL, 'queued', ?, 0, ?, 0, ?)",
                    (kind, storage_id, total_bytes, local_path, utcnow()),
                )
            )
        with self._db.transaction() as cur:
            cur.execute(
                "INSERT INTO transfers(kind, storage_id, file_id, status, total_bytes,"
                " done_bytes, local_path, attempts, updated_at)"
                " VALUES (?, ?, ?, 'queued', ?, 0, ?, 0, ?)"
                " ON CONFLICT(kind, file_id) DO UPDATE SET status='queued',"
                " total_bytes=excluded.total_bytes, done_bytes=0, error=NULL,"
                " local_path=excluded.local_path, updated_at=excluded.updated_at",
                (kind, storage_id, file_id, total_bytes, local_path, utcnow()),
            )
            cur.execute(
                "SELECT id FROM transfers WHERE kind=? AND file_id=?", (kind, file_id)
            )
            row = cur.fetchone()
            assert row is not None
            return int(row[0])

    def get(self, transfer_id: int) -> Optional[TransferRecord]:
        rows = self._db.execute_query(
            "SELECT id, file_id, kind, storage_id, status, total_bytes, done_bytes,"
            " local_path, error, attempts FROM transfers WHERE id=?",
            (transfer_id,),
            fetch=True,
        )
        return self._row(rows[0]) if rows else None

    def list_active(self) -> List[TransferRecord]:
        placeholders = ",".join("?" for _ in self.ACTIVE)
        rows = self._db.execute_query(
            "SELECT id, file_id, kind, storage_id, status, total_bytes, done_bytes,"
            f" local_path, error, attempts FROM transfers WHERE status IN ({placeholders})"
            " ORDER BY id",
            self.ACTIVE,
            fetch=True,
        )
        return [self._row(r) for r in rows]

    def list_recent(self, limit: int = 20) -> List[TransferRecord]:
        """Most recently updated transfers (history for dashboards)."""
        rows = self._db.execute_query(
            "SELECT id, file_id, kind, storage_id, status, total_bytes, done_bytes,"
            " local_path, error, attempts FROM transfers"
            " ORDER BY updated_at DESC LIMIT ?",
            (limit,),
            fetch=True,
        )
        return [self._row(r) for r in rows]

    def list_history(self, limit: int = 50) -> List[TransferRecord]:
        """Finished transfers (completed/failed/cancelled), newest first."""
        rows = self._db.execute_query(
            "SELECT id, file_id, kind, storage_id, status, total_bytes, done_bytes,"
            " local_path, error, attempts FROM transfers"
            " WHERE status IN ('completed','failed','cancelled')"
            " ORDER BY updated_at DESC LIMIT ?",
            (limit,),
            fetch=True,
        )
        return [self._row(r) for r in rows]

    def prune_history(self, keep: int = 200) -> int:
        """Deletes the oldest finished transfers, keeping the newest ``keep``.

        Active rows (queued/uploading/...) are always kept, so a long-lived
        install cannot accumulate an unbounded history table.
        """
        rows = self._db.execute_query(
            "SELECT id FROM transfers WHERE status IN"
            " ('completed','failed','cancelled')"
            " ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
            (max(0, keep),),
            fetch=True,
        )
        stale = [int(row[0]) for row in rows]
        if not stale:
            return 0
        placeholders = ",".join("?" for _ in stale)
        self._db.execute_query(
            f"DELETE FROM transfers WHERE id IN ({placeholders})", tuple(stale)
        )
        return len(stale)

    def update_progress(self, transfer_id: int, done_bytes: int) -> None:
        self._db.execute_query(
            "UPDATE transfers SET done_bytes=?, updated_at=? WHERE id=?",
            (done_bytes, utcnow(), transfer_id),
        )

    def set_status(
        self, transfer_id: int, status: str, error: Optional[str] = None
    ) -> None:
        self._db.execute_query(
            "UPDATE transfers SET status=?, error=?, updated_at=? WHERE id=?",
            (status, error, utcnow(), transfer_id),
        )

    def bump_attempts(self, transfer_id: int) -> None:
        self._db.execute_query(
            "UPDATE transfers SET attempts=attempts+1, updated_at=? WHERE id=?",
            (utcnow(), transfer_id),
        )

    def delete(self, transfer_id: int) -> None:
        self._db.execute_query("DELETE FROM transfers WHERE id=?", (transfer_id,))

    @staticmethod
    def _row(r: tuple) -> TransferRecord:
        return TransferRecord(
            id=r[0], file_id=r[1], kind=r[2], storage_id=r[3], status=r[4],
            total_bytes=r[5] or 0, done_bytes=r[6] or 0, local_path=r[7],
            error=r[8], attempts=r[9] or 0,
        )


class SettingsRepository:
    def __init__(self, db: DatabaseManager):
        self._db = db

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = self._db.execute_query(
            "SELECT value FROM settings WHERE key=?", (key,), fetch=True
        )
        return rows[0][0] if rows else default

    def set(self, key: str, value: str) -> None:
        self._db.execute_query(
            "INSERT INTO settings(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
