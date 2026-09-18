# teloude/core/search.py
"""Global search across locally indexed storages.

Uses FTS5 when the database has the index, otherwise a LIKE fallback with
proper escaping. Searches file names and relative paths; never touches the
network (Telegram is not a file browser in v1).
"""
from dataclasses import dataclass
from typing import List, Optional
import re

from teloude.infrastructure.database import DatabaseManager

_FTS_SPECIAL = re.compile(r'["*()]')


@dataclass
class SearchResult:
    file_id: int
    storage_id: int
    storage_name: str
    relative_path: str
    file_name: str
    size: int
    is_backed_up: bool
    backup_at: Optional[str]


def _sanitize_fts(query: str) -> str:
    # Quote every token and drop FTS operators so user input cannot break MATCH.
    tokens = [t for t in _FTS_SPECIAL.sub(" ", query).split() if t]
    if not tokens:
        return '""'
    return " ".join(f'"{t}"*' for t in tokens[:10])


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_files(
    db: DatabaseManager,
    query: str,
    storage_id: Optional[int] = None,
    backed_only: bool = False,
    limit: int = 200,
) -> List[SearchResult]:
    query = (query or "").strip()
    if not query:
        return []
    storage_filter = "AND f.storage_id = ?" if storage_id is not None else ""
    backed_filter = "AND f.is_backed_up = 1" if backed_only else ""
    params: list = []
    if db.has_fts():
        match = _sanitize_fts(query)
        sql = (
            "SELECT f.id, f.storage_id, s.name, f.relative_path, f.file_name,"
            " f.size, f.is_backed_up, f.backup_at FROM files_fts"
            " JOIN files f ON f.id = files_fts.rowid"
            " JOIN storages s ON s.id = f.storage_id"
            f" WHERE files_fts MATCH ? {storage_filter} {backed_filter}"
            " ORDER BY f.relative_path LIMIT ?"
        )
        params = [match]
    else:
        like = f"%{_escape_like(query)}%"
        sql = (
            "SELECT f.id, f.storage_id, s.name, f.relative_path, f.file_name,"
            " f.size, f.is_backed_up, f.backup_at FROM files f"
            " JOIN storages s ON s.id = f.storage_id"
            " WHERE (f.file_name LIKE ? ESCAPE '\\' OR f.relative_path LIKE ? ESCAPE '\\')"
            f" {storage_filter} {backed_filter}"
            " ORDER BY f.relative_path LIMIT ?"
        )
        params = [like, like]
    if storage_id is not None:
        params.append(storage_id)
    params.append(limit)
    rows = db.execute_query(sql, tuple(params), fetch=True)
    return [
        SearchResult(
            file_id=r[0], storage_id=r[1], storage_name=r[2], relative_path=r[3],
            file_name=r[4], size=r[5] or 0, is_backed_up=bool(r[6]), backup_at=r[7],
        )
        for r in rows
    ]
