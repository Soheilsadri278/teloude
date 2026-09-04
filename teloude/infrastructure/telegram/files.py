# teloude/infrastructure/telegram/files.py
"""Telegram file gateway: chunked MTProto upload/download behind an interface.

Resume semantics (honest, per PROJECT_SPEC.md):
- Same-session retry after a network blip MAY continue from the last
  checkpointed part via start_part (Telegram keeps uncommitted parts briefly).
- After an application restart the file is RE-UPLOADED from scratch; already
  completed files are never lost and never re-uploaded. Exact byte-resume
  across restarts is NOT promised because Telegram's temporary part state
  expires.

Large files stream in parts (default 512 KiB, adapted via Telethon's
get_appropriated_part_size); files are never loaded fully into RAM.
"""
import hashlib
import logging
import mimetypes
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

from .bridge import extract_message_id, map_rpc_error, run_sync
from .exceptions import TeloudeTelegramError

logger = logging.getLogger("TelegramFiles")

BIG_FILE_THRESHOLD = 10 * 1024 * 1024  # Telethon parity: big parts above 10 MiB.
DOWNLOAD_CHUNK = 512 * 1024
FREE_TIER_MAX_BYTES = 2 * 1024 * 1024 * 1024
PREMIUM_TIER_MAX_BYTES = 4 * 1024 * 1024 * 1024


class UploadPaused(Exception):
    """Cooperative pause raised between parts; carries uploaded bytes."""


class UploadCancelled(Exception):
    """Cooperative cancellation raised between parts."""


@dataclass
class UploadedFile:
    file_id: int
    parts: int
    name: str
    size: int
    md5: str  # hex digest for small files, '' for big files (Telethon parity)
    is_big: bool


@dataclass
class SentMessage:
    chat_id: int
    msg_id: int
    topic_id: Optional[int] = None


@dataclass
class DocumentRef:
    doc_id: int
    access_hash: int
    file_reference: bytes
    size: int
    file_name: str
    mime: str


class ITelegramFileGateway(ABC):
    """Abstract chunked file transfer; backup/restore engines depend on this."""

    @abstractmethod
    def max_upload_bytes(self) -> int:
        """Max single-file upload size, resolved at runtime (account tier)."""
        raise NotImplementedError

    @abstractmethod
    def upload(
        self,
        local_path: Path,
        progress: Optional[Callable[[int], None]] = None,
        should_pause: Optional[Callable[[], bool]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        start_part: int = 0,
        part_size: Optional[int] = None,
    ) -> UploadedFile:
        """Streams a file to Telegram storage; returns an opaque handle."""
        raise NotImplementedError

    @abstractmethod
    def send_to_topic(
        self, chat_id: int, topic_id: int, uploaded: UploadedFile, caption: str = ""
    ) -> SentMessage:
        """Posts an uploaded file as a document message inside a forum topic."""
        raise NotImplementedError

    @abstractmethod
    def resolve_document(self, chat_id: int, msg_id: int) -> DocumentRef:
        """Fetches fresh document identifiers (refreshes expiring file references)."""
        raise NotImplementedError

    @abstractmethod
    def download(
        self,
        doc: DocumentRef,
        dest_path: Path,
        offset: int = 0,
        progress: Optional[Callable[[int], None]] = None,
        should_pause: Optional[Callable[[], bool]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> int:
        """Streams a document to disk from an offset; returns bytes written."""
        raise NotImplementedError

    @abstractmethod
    def delete_messages(self, chat_id: int, msg_ids: List[int]) -> None:
        """Deletes backup messages. Explicit user confirmation required upstream."""
        raise NotImplementedError


class TelethonFileGateway(ITelegramFileGateway):
    """ITelegramFileGateway over raw MTProto upload/SendMedia/GetFile calls."""

    def __init__(
        self,
        invoke: Callable[[Any], Any],
        get_me: Optional[Callable[[], Any]] = None,
    ):
        self._invoke = invoke
        self._get_me = get_me

    def _call(self, request: Any, operation: str) -> Any:
        try:
            return run_sync(self._invoke(request))
        except TeloudeTelegramError:
            raise
        except Exception as exc:
            raise map_rpc_error(exc, operation) from exc

    def max_upload_bytes(self) -> int:
        premium = False
        if self._get_me is not None:
            try:
                me = run_sync(self._get_me())
                premium = bool(getattr(me, "premium", False))
            except Exception as exc:
                logger.warning(f"Could not determine account tier: {exc}")
        return PREMIUM_TIER_MAX_BYTES if premium else FREE_TIER_MAX_BYTES

    def upload(
        self,
        local_path: Path,
        progress: Optional[Callable[[int], None]] = None,
        should_pause: Optional[Callable[[], bool]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        start_part: int = 0,
        part_size: Optional[int] = None,
    ) -> UploadedFile:
        from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest

        local_path = Path(local_path)
        size = local_path.stat().st_size
        is_big = size > BIG_FILE_THRESHOLD
        if part_size is None:
            part_size = self._part_size(size)
        parts_total = max(1, -(-size // part_size))  # ceil, min 1 (empty file = 1 empty part)
        file_id = random.getrandbits(63)
        md5 = hashlib.md5()
        done = start_part * part_size
        with open(local_path, "rb") as fh:  # read-only; source untouched
            if start_part:
                fh.seek(start_part * part_size)
            for part in range(start_part, parts_total):
                if is_cancelled is not None and is_cancelled():
                    raise UploadCancelled(f"Upload cancelled at part {part}.")
                if should_pause is not None and should_pause():
                    paused = UploadPaused(f"Upload paused at part {part}.")
                    paused.done_bytes = done  # type: ignore[attr-defined]
                    raise paused
                chunk = fh.read(part_size)
                if not chunk and size > 0:
                    break  # file shrank mid-upload; caller re-validates
                if not is_big:
                    md5.update(chunk)
                if is_big:
                    request = SaveBigFilePartRequest(file_id, part, parts_total, chunk)
                else:
                    request = SaveFilePartRequest(file_id, part, chunk)
                self._call(request, f"uploading part {part + 1}/{parts_total}")
                done += len(chunk)
                if progress is not None:
                    progress(done)
        return UploadedFile(
            file_id=file_id, parts=parts_total, name=local_path.name,
            size=size, md5="" if is_big else md5.hexdigest(), is_big=is_big,
        )

    def send_to_topic(
        self, chat_id: int, topic_id: int, uploaded: UploadedFile, caption: str = ""
    ) -> SentMessage:
        from telethon.tl.functions.messages import SendMediaRequest
        from telethon.tl.types import (
            DocumentAttributeFilename,
            InputFile,
            InputFileBig,
            InputMediaUploadedDocument,
            InputReplyToMessage,
        )

        if uploaded.is_big:
            tl_file: Any = InputFileBig(uploaded.file_id, uploaded.parts, uploaded.name)
        else:
            tl_file = InputFile(uploaded.file_id, uploaded.parts, uploaded.name, uploaded.md5)
        mime, _ = mimetypes.guess_type(uploaded.name)
        media = InputMediaUploadedDocument(
            file=tl_file,
            mime_type=mime or "application/octet-stream",
            attributes=[DocumentAttributeFilename(uploaded.name)],
            force_file=True,
        )
        updates = self._call(
            SendMediaRequest(
                peer=chat_id, media=media, message=caption,
                reply_to=InputReplyToMessage(reply_to_msg_id=topic_id),
                random_id=random.getrandbits(63),
            ),
            "posting the backup message",
        )
        msg_id = extract_message_id(updates)
        if msg_id is None:
            raise TeloudeTelegramError(
                "Telegram did not return the backup message id.", details=None
            )
        return SentMessage(chat_id=chat_id, msg_id=msg_id, topic_id=topic_id)

    def resolve_document(self, chat_id: int, msg_id: int) -> DocumentRef:
        from telethon.tl.functions.channels import GetMessagesRequest
        from telethon.tl.types import InputMessageID

        response = self._call(
            GetMessagesRequest(channel=chat_id, id=[InputMessageID(id=msg_id)]),
            "locating the backup message",
        )
        messages = getattr(response, "messages", []) or []
        if not messages:
            raise TeloudeTelegramError("Backup message not found on Telegram.")
        media = getattr(messages[0], "media", None)
        document = getattr(media, "document", None)
        if document is None:
            raise TeloudeTelegramError("Backup message has no downloadable document.")
        name = ""
        for attr in getattr(document, "attributes", []) or []:
            if hasattr(attr, "file_name"):
                name = getattr(attr, "file_name") or ""
                break
        return DocumentRef(
            doc_id=int(document.id),
            access_hash=int(document.access_hash),
            file_reference=bytes(getattr(document, "file_reference", b"") or b""),
            size=int(getattr(document, "size", 0) or 0),
            file_name=name,
            mime=str(getattr(document, "mime_type", "") or ""),
        )

    def download(
        self,
        doc: DocumentRef,
        dest_path: Path,
        offset: int = 0,
        progress: Optional[Callable[[int], None]] = None,
        should_pause: Optional[Callable[[], bool]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> int:
        from telethon.tl.functions.upload import GetFileRequest
        from telethon.tl.types import InputDocumentFileLocation

        dest_path = Path(dest_path)
        written = 0
        mode = "r+b" if offset > 0 and dest_path.exists() else "wb"
        with open(dest_path, mode) as fh:
            if offset > 0:
                fh.seek(offset)
            cursor = offset
            while True:
                if is_cancelled is not None and is_cancelled():
                    raise UploadCancelled("Download cancelled.")
                if should_pause is not None and should_pause():
                    paused = UploadPaused(f"Download paused at byte {cursor}.")
                    paused.done_bytes = cursor  # type: ignore[attr-defined]
                    raise paused
                response = self._call(
                    GetFileRequest(
                        location=InputDocumentFileLocation(
                            id=doc.doc_id, access_hash=doc.access_hash,
                            file_reference=doc.file_reference, thumb_size="",
                        ),
                        offset=cursor, limit=DOWNLOAD_CHUNK,
                    ),
                    "downloading file data",
                )
                chunk = bytes(getattr(response, "bytes", b"") or b"")
                if not chunk:
                    break
                fh.write(chunk)
                cursor += len(chunk)
                written += len(chunk)
                if progress is not None:
                    progress(cursor)
        return written

    def delete_messages(self, chat_id: int, msg_ids: List[int]) -> None:
        from telethon.tl.functions.channels import DeleteMessagesRequest

        if not msg_ids:
            return
        self._call(
            DeleteMessagesRequest(channel=chat_id, id=list(msg_ids)),
            "deleting backup messages",
        )

    @staticmethod
    def _part_size(file_size: int) -> int:
        try:
            from telethon import utils

            return int(utils.get_appropriated_part_size(file_size)) * 1024
        except Exception:
            return 512 * 1024


def _unused_os_import_guard() -> None:  # pragma: no cover
    _ = os.sep
