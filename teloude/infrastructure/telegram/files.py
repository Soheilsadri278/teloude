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
import asyncio
import hashlib
import inspect
import logging
import mimetypes
import random
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, List, Optional, Tuple

from .bridge import extract_message_id, get_telegram_loop, map_rpc_error, run_sync
from .exceptions import RemoteItemMissingError, TeloudeTelegramError

logger = logging.getLogger("TelegramFiles")

BIG_FILE_THRESHOLD = 10 * 1024 * 1024  # Telethon parity: big parts above 10 MiB.
DOWNLOAD_CHUNK = 512 * 1024
FREE_TIER_MAX_BYTES = 2 * 1024 * 1024 * 1024
PREMIUM_TIER_MAX_BYTES = 4 * 1024 * 1024 * 1024

# Upload parts are numbered and the server reassembles them at commit time, so
# several parts of ONE file may be in flight at once (this is what the official
# clients do; it still transfers one logical file at a time). A fully sequential
# part loop caps throughput at part_size / round-trip-time - measured: 2.2 MB/s
# with 128 KiB parts at 60 ms RTT on a link that moves 680 MB/s locally - so the
# gateway keeps up to this many parts unacknowledged per file.
DEFAULT_UPLOAD_WINDOW = 8
MIN_PART_BYTES = 16 * 1024
MAX_PART_BYTES = 512 * 1024  # MTProto accepts at most 512 KiB per saveFilePart.


def clamp_part_bytes(kbytes: int) -> int:
    """Clamps a configured chunk size (KiB) to what MTProto part calls accept.

    Part sizes must be multiples of 4 KiB between 16 and 512 KiB; the default
    configuration (512 KiB) is exactly the protocol maximum.
    """
    value = max(MIN_PART_BYTES, min(MAX_PART_BYTES, int(kbytes) * 1024))
    value -= value % 4096
    return value or MIN_PART_BYTES


class _Ready:
    """Instantly-finished stand-in for a part future (synchronous doubles)."""

    __slots__ = ("_value",)

    def __init__(self, value: Any):
        self._value = value

    def result(self, timeout: Optional[float] = None) -> Any:
        return self._value


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
        file_id: Optional[int] = None,
        window: int = DEFAULT_UPLOAD_WINDOW,
    ) -> UploadedFile:
        """Streams a file to Telegram storage; returns an opaque handle.

        ``progress`` always reports absolute bytes of the file (in order, as
        parts are acknowledged). ``window`` is how many parts of this one file
        may be unacknowledged at once; 1 degenerates to the old strictly
        sequential loop. Resuming with ``start_part > 0`` requires ``file_id``
        of the interrupted upload: Telegram stores parts per file id, so a
        resume without it would produce a document missing its first parts.
        """
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
        chunk: Optional[int] = None,
    ) -> int:
        """Streams a document to disk from an offset; returns bytes written.

        ``chunk`` overrides the GetFile request size (the configured transfer
        chunk); None keeps the gateway default.
        """
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

    def _dispatch(self, request: Any, operation: str) -> Any:
        """Starts one part request without waiting for its answer.

        Returns a future-like object with ``.result()``: a real
        ``concurrent.futures.Future`` when the invoke produced an awaitable
        (the live Telethon client - the request then runs on the shared
        Telegram loop thread), or an instantly-ready holder for synchronous
        test doubles. Failures are mapped to Teloude exceptions exactly as in
        ``_call``, at ``.result()`` time for awaitables.
        """
        try:
            value = self._invoke(request)
        except TeloudeTelegramError:
            raise
        except Exception as exc:
            raise map_rpc_error(exc, operation) from exc
        if not inspect.isawaitable(value):
            return _Ready(value)

        async def _wrap() -> Any:
            try:
                return await value
            except TeloudeTelegramError:
                raise
            except Exception as exc:
                raise map_rpc_error(exc, operation) from exc

        return asyncio.run_coroutine_threadsafe(_wrap(), get_telegram_loop())

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
        file_id: Optional[int] = None,
        window: int = DEFAULT_UPLOAD_WINDOW,
    ) -> UploadedFile:
        from telethon.tl.functions.upload import SaveBigFilePartRequest, SaveFilePartRequest

        local_path = Path(local_path)
        size = local_path.stat().st_size
        is_big = size > BIG_FILE_THRESHOLD
        if part_size is None:
            part_size = self.suggest_part_size(size)
        parts_total = max(1, -(-size // part_size))  # ceil, min 1 (empty file = 1 empty part)
        if start_part and (file_id is None or start_part >= parts_total):
            # Telegram keeps upload parts keyed by file id: continuing without the
            # original id (or past the end) would post a document missing its
            # first parts. Restart the file instead of risking silent corruption.
            logger.warning(
                f"Cannot resume {local_path.name} from part {start_part}; "
                "restarting the upload from the beginning."
            )
            start_part = 0
            file_id = None
        if file_id is None:
            file_id = random.getrandbits(63)
        md5 = hashlib.md5()
        if start_part and not is_big:
            # The uploaded md5 must cover the whole file, so hash the skipped
            # prefix locally (no network) before continuing.
            remaining = start_part * part_size
            with open(local_path, "rb") as prefix:
                while remaining > 0:
                    block = prefix.read(min(1 << 20, remaining))
                    if not block:
                        break
                    md5.update(block)
                    remaining -= len(block)
            if remaining > 0:
                start_part = 0
                file_id = random.getrandbits(63)
                md5 = hashlib.md5()
        window = max(1, int(window))
        done = start_part * part_size  # progress is absolute (interface contract)
        stopped: Optional[str] = None  # "pause" | "cancel" | "shrink"
        in_flight: Deque[Tuple[int, bytes, Any]] = deque()
        next_part = start_part
        with open(local_path, "rb") as fh:  # read-only; source untouched
            if start_part:
                fh.seek(start_part * part_size)
            while True:
                # Fill the window: dispatch parts without waiting for the
                # previous acknowledgements. Pause/cancel are honoured before
                # each dispatch, exactly where the sequential loop checked them.
                while len(in_flight) < window and next_part < parts_total \
                        and stopped is None:
                    if is_cancelled is not None and is_cancelled():
                        stopped = "cancel"
                        break
                    if should_pause is not None and should_pause():
                        stopped = "pause"
                        break
                    chunk = fh.read(part_size)
                    if not chunk and size > 0:
                        stopped = "shrink"  # file shrank mid-upload; re-validate
                        break
                    if is_big:
                        request = SaveBigFilePartRequest(
                            file_id, next_part, parts_total, chunk
                        )
                    else:
                        request = SaveFilePartRequest(file_id, next_part, chunk)
                    try:
                        future = self._dispatch(
                            request, f"uploading part {next_part + 1}/{parts_total}"
                        )
                    except Exception:
                        # A synchronous dispatch failure must not leak the
                        # parts already sent: consume their answers, re-raise.
                        while in_flight:
                            _, _, leftover = in_flight.popleft()
                            try:
                                leftover.result()
                            except Exception:
                                pass
                        raise
                    in_flight.append((next_part, chunk, future))
                    next_part += 1
                if in_flight:
                    # Retire strictly in part order, so the md5 (small files)
                    # and the reported progress always cover a file prefix.
                    part, chunk, future = in_flight.popleft()
                    first_error: Optional[BaseException] = None
                    try:
                        future.result()
                    except Exception as exc:
                        first_error = exc
                    if first_error is not None:
                        # Drain whatever is still flying (their answers must be
                        # consumed) and surface the first failure; the engine's
                        # retry path restarts from the checkpoint as before.
                        while in_flight:
                            _, _, other = in_flight.popleft()
                            try:
                                other.result()
                            except Exception:
                                pass  # the first failure is the reported cause
                        raise first_error
                    done += len(chunk)
                    if not is_big:
                        md5.update(chunk)
                    if progress is not None:
                        progress(done)
                    continue
                # Nothing in flight: either the file is fully dispatched, or a
                # stop condition arrived. Parts already sent were retired above,
                # so a pause keeps their bytes in the checkpoint.
                if stopped is None and next_part < parts_total:
                    continue
                break
        if stopped == "cancel":
            raise UploadCancelled(f"Upload cancelled at part {next_part}.")
        if stopped == "pause":
            paused = UploadPaused(f"Upload paused at part {next_part}.")
            paused.done_bytes = done  # type: ignore[attr-defined]
            # Parts live under this id; the caller needs it to continue
            # instead of restarting the file.
            paused.file_id = file_id  # type: ignore[attr-defined]
            raise paused
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
            raise RemoteItemMissingError(
                "This file's copy no longer exists on Telegram (the message was "
                "deleted). Run a backup again to upload it."
            )
        media = getattr(messages[0], "media", None)
        document = getattr(media, "document", None)
        if document is None:
            raise RemoteItemMissingError(
                "The Telegram message no longer carries a downloadable file. "
                "Run a backup again to upload it."
            )
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
        chunk: Optional[int] = None,
    ) -> int:
        from telethon.tl.functions.upload import GetFileRequest
        from telethon.tl.types import InputDocumentFileLocation

        dest_path = Path(dest_path)
        written = 0
        limit = clamp_part_bytes(chunk // 1024) if chunk else DOWNLOAD_CHUNK
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
                        offset=cursor, limit=limit,
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

    def suggest_part_size(self, file_size: int) -> int:
        try:
            from telethon import utils

            return int(utils.get_appropriated_part_size(file_size)) * 1024
        except Exception:
            return 512 * 1024

    @staticmethod
    def _part_size(file_size: int) -> int:
        return TelethonFileGateway.suggest_part_size(object(), file_size)
