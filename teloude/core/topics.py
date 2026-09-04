# teloude/core/topics.py
"""Folder-hierarchy <-> flat Telegram forum-topic mapping.

Telegram forum topics are flat and cannot nest. The local database stays
authoritative for hierarchy; topic names only encode the path for display:

    <storage>                       (root folder itself)
    <storage> / 2026
    <storage> / 2026 / Wedding
"""
from hashlib import sha1
from typing import List, Optional
import re

SEPARATOR = " / "
MAX_TOPIC_TITLE = 100  # well under Telegram's 128-char forum-topic limit.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _clean(part: str) -> str:
    part = _CONTROL_CHARS.sub("", part).strip()
    return part if part else "?"


def topic_name_for(storage_name: str, relative_dir: str) -> str:
    """Maps a storage + relative dir ('', '.', '2026/Wedding') to a topic title."""
    base = _clean(storage_name)
    rel = (relative_dir or "").strip().strip("/").strip("\\")
    if rel in ("", "."):
        return _fit(base)
    parts = [_clean(p) for p in re.split(r"[\\/]+", rel) if p.strip()]
    if not parts:
        return _fit(base)
    return _fit(f"{base}{SEPARATOR}{SEPARATOR.join(parts)}")


def _fit(title: str) -> str:
    if len(title) <= MAX_TOPIC_TITLE:
        return title
    # Deterministic truncation with a content hash so long paths stay unique.
    digest = sha1(title.encode("utf-8")).hexdigest()[:8]
    return f"{title[: MAX_TOPIC_TITLE - 10]}~{digest}"


def parse_topic_name(topic: str, storage_name: str) -> Optional[str]:
    """Reverses topic_name_for: returns the relative dir ('') for root, None if foreign."""
    base = _clean(storage_name)
    title = topic.strip()
    if title == base or title == _fit(base):
        return ""
    prefix = f"{base}{SEPARATOR}"
    if title.startswith(prefix):
        # Inverse of topic_name_for: separator-joined parts back to slashes.
        return "/".join(p.strip() for p in title[len(prefix):].split(SEPARATOR))
    return None


def topic_chain(storage_name: str, relative_dir: str) -> List[tuple]:
    """Yields (relative_dir, topic_title) from the root down to relative_dir.

    Used to create missing topics top-down. Root '' is always first.
    """
    rel = (relative_dir or "").strip().strip("/")
    if rel in ("", "."):
        return [("", topic_name_for(storage_name, ""))]
    parts = [p for p in rel.split("/") if p]
    chain = [("", topic_name_for(storage_name, ""))]
    for i in range(1, len(parts) + 1):
        sub = "/".join(parts[:i])
        chain.append((sub, topic_name_for(storage_name, sub)))
    return chain
