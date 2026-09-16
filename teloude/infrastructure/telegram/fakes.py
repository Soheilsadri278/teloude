# teloude/infrastructure/telegram/fakes.py
"""In-memory doubles for the Telegram gateways.

Used by the test suite (no network, no real account) and handy for offline
UI development. NEVER part of the production backup path.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .auth import AuthState, ITelegramAuth
from .files import (
    DocumentRef,
    ITelegramFileGateway,
    SentMessage,
    UploadCancelled,
    UploadedFile,
    UploadPaused,
)
from .storage import (
    GENERAL_TOPIC_ID,
    GENERAL_TOPIC_TITLE,
    DocumentMeta,
    ITelegramStorage,
    StorageInfo,
    TopicInfo,
)


class FakeAuth(ITelegramAuth):
    """Scripted login flow: needs_password=True exercises the 2FA branch."""

    def __init__(self, needs_password: bool = False):
        super().__init__()
        self._needs_password = needs_password
        self._authorized = False
        self.codes_sent: List[str] = []
        self.sign_outs = 0

    def send_code(self, phone: str) -> AuthState:
        from .auth import AuthSession

        self.codes_sent.append(phone)
        self._session = AuthSession(phone=phone, state=AuthState.CODE_SENT)
        return AuthState.CODE_SENT

    def sign_in_code(self, phone: str, code: str) -> AuthState:
        from .auth import AuthSession

        if code != "11111":
            from .exceptions import AuthError

            raise AuthError("The code is incorrect.")
        if self._needs_password:
            self._session = AuthSession(phone=phone, state=AuthState.PASSWORD_NEEDED)
            return AuthState.PASSWORD_NEEDED
        self._authorized = True
        self._session = AuthSession(phone=phone, state=AuthState.AUTHORIZED)
        return AuthState.AUTHORIZED

    def sign_in_password(self, password: str) -> AuthState:
        from .auth import AuthSession
        from .exceptions import AuthError

        if password != "secret":
            raise AuthError("The 2FA password is incorrect.")
        self._authorized = True
        phone = self._session.phone if self._session else ""
        self._session = AuthSession(phone=phone, state=AuthState.AUTHORIZED)
        return AuthState.AUTHORIZED

    def sign_out(self) -> None:
        self._authorized = False
        self._session = None
        self.sign_outs += 1

    def is_authorized(self) -> bool:
        return self._authorized


@dataclass
class _FakeTopic:
    topic_id: int
    title: str


@dataclass
class _FakeStorage:
    chat_id: int
    title: str
    is_forum: bool = True
    topics: Dict[int, _FakeTopic] = field(default_factory=dict)
    next_topic: int = 2
    messages: Dict[int, bytes] = field(default_factory=dict)
    next_msg: int = 1
    documents: Dict[int, List[Any]] = field(default_factory=dict)


class FakeStorageGateway(ITelegramStorage):
    """In-memory storage/topic registry recording every call."""

    def __init__(self):
        self._storages: Dict[int, _FakeStorage] = {}
        self._next_chat = -1001000
        self.calls: List[str] = []

    def create_storage(self, name: str) -> StorageInfo:
        from .storage import STORAGE_PREFIX

        self.calls.append(f"create_storage:{name}")
        chat_id = self._next_chat
        self._next_chat -= 1
        title = f"{STORAGE_PREFIX}{name}"
        self._storages[chat_id] = _FakeStorage(
            chat_id=chat_id, title=title,
            topics={GENERAL_TOPIC_ID: _FakeTopic(GENERAL_TOPIC_ID, GENERAL_TOPIC_TITLE)},
        )
        return StorageInfo(chat_id=chat_id, title=title, is_forum=True)

    def find_storage(self, name: str) -> Optional[StorageInfo]:
        from .storage import STORAGE_PREFIX

        wanted = f"{STORAGE_PREFIX}{name}"
        for storage in self._storages.values():
            if storage.title == wanted:
                return StorageInfo(chat_id=storage.chat_id, title=storage.title,
                                   is_forum=storage.is_forum)
        return None

    def ensure_forum(self, chat_id: int) -> None:
        self.calls.append(f"ensure_forum:{chat_id}")
        self._get(chat_id).is_forum = True

    def list_topics(self, chat_id: int) -> List[TopicInfo]:
        return [TopicInfo(t.topic_id, t.title) for t in self._get(chat_id).topics.values()]

    def ensure_topic(self, chat_id: int, title: str, is_root: bool = False) -> TopicInfo:
        storage = self._get(chat_id)
        for topic in storage.topics.values():
            if topic.title == title:
                return TopicInfo(topic.topic_id, topic.title)
        if is_root:
            general = storage.topics.get(GENERAL_TOPIC_ID)
            if general is not None and general.title == GENERAL_TOPIC_TITLE:
                general.title = title
                return TopicInfo(general.topic_id, general.title)
        topic_id = storage.next_topic
        storage.next_topic += 1
        storage.topics[topic_id] = _FakeTopic(topic_id, title)
        return TopicInfo(topic_id, title)

    def close_topic(self, chat_id: int, topic_id: int) -> None:
        self.calls.append(f"close_topic:{chat_id}:{topic_id}")

    def delete_topic_messages(self, chat_id: int, topic_id: int) -> None:
        self.calls.append(f"delete_topic_messages:{chat_id}:{topic_id}")

    def delete_storage(self, chat_id: int) -> None:
        self.calls.append(f"delete_storage:{chat_id}")
        del self._storages[chat_id]

    def add_document(
        self, chat_id: int, topic_id: int, meta: DocumentMeta
    ) -> None:
        """Test helper: pretends a document message exists in a topic."""
        self._get(chat_id).documents.setdefault(topic_id, []).append(meta)

    def list_topic_documents(
        self, chat_id: int, topic_id: int, limit: int = 100
    ) -> List[DocumentMeta]:
        return list(self._get(chat_id).documents.get(topic_id, [])[:limit])

    def _get(self, chat_id: int) -> _FakeStorage:
        try:
            return self._storages[chat_id]
        except KeyError:
            raise ValueError(f"Unknown fake storage: {chat_id}") from None


class FakeFileGateway(ITelegramFileGateway):
    """In-memory remote file store: upload/download round-trips byte-exact.

    fail_next_upload_with / fail_next_download_with inject one-shot failures
    to exercise retry paths. max_bytes is configurable per test.
    """

    def __init__(self, max_bytes: int = 2 * 1024 * 1024 * 1024):
        self._max_bytes = max_bytes
        self._blobs: Dict[int, bytes] = {}
        self._next_file = 1
        self.fail_next_upload_with: Optional[Exception] = None
        self.fail_next_download_with: Optional[Exception] = None
        self.uploaded_parts: List[int] = []

    def max_upload_bytes(self) -> int:
        return self._max_bytes

    def suggest_part_size(self, file_size: int) -> int:
        return 64 * 1024

    def upload(
        self,
        local_path: Path,
        progress: Optional[Callable[[int], None]] = None,
        should_pause: Optional[Callable[[], bool]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        start_part: int = 0,
        part_size: Optional[int] = None,
        file_id: Optional[int] = None,
    ) -> UploadedFile:
        """Emulates Telegram's part semantics: parts live under a file id.

        Resuming therefore requires the same ``file_id``; without it the upload
        restarts from part 0, exactly like the real gateway.
        """
        import hashlib

        if self.fail_next_upload_with is not None:
            exc, self.fail_next_upload_with = self.fail_next_upload_with, None
            raise exc
        data = Path(local_path).read_bytes()
        if len(data) > self._max_bytes:
            raise ValueError("File exceeds the fake remote limit.")
        step = part_size or 1024
        parts_total = max(1, -(-len(data) // step))
        if start_part and (file_id is None or start_part >= parts_total):
            start_part = 0
            file_id = None
        if file_id is None:
            file_id = self._next_file
            self._next_file += 1
            self._blobs[file_id] = b""
        done = start_part * step
        for part in range(start_part, parts_total):
            if is_cancelled is not None and is_cancelled():
                raise UploadCancelled("cancelled")
            if should_pause is not None and should_pause():
                paused = UploadPaused("paused")
                paused.done_bytes = done  # type: ignore[attr-defined]
                paused.file_id = file_id  # type: ignore[attr-defined]
                raise paused
            begin = part * step
            self.uploaded_parts.append(part)
            stored = self._blobs[file_id]
            blob = stored + b"\x00" * max(0, (begin + step) - len(stored))
            self._blobs[file_id] = blob[:begin] + data[begin:begin + step] + blob[begin + step:]
            done = min(len(data), begin + step)
            if progress is not None:
                progress(done)
        # Telegram assembles the document with the file's exact size; the last
        # part only carries the remaining bytes.
        self._blobs[file_id] = self._blobs[file_id][:len(data)]
        return UploadedFile(
            file_id=file_id, parts=parts_total, name=Path(local_path).name,
            size=len(data), md5=hashlib.md5(data).hexdigest(), is_big=False,
        )

    def send_to_topic(
        self, chat_id: int, topic_id: int, uploaded: UploadedFile, caption: str = ""
    ) -> SentMessage:
        return SentMessage(chat_id=chat_id, msg_id=uploaded.file_id, topic_id=topic_id)

    def resolve_document(self, chat_id: int, msg_id: int) -> DocumentRef:
        try:
            blob = self._blobs[msg_id]
        except KeyError:
            from .exceptions import TeloudeTelegramError

            raise TeloudeTelegramError("Backup message not found on Telegram.") from None
        return DocumentRef(
            doc_id=msg_id, access_hash=1, file_reference=b"fake",
            size=len(blob), file_name="", mime="application/octet-stream",
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
        if self.fail_next_download_with is not None:
            exc, self.fail_next_download_with = self.fail_next_download_with, None
            raise exc
        blob = self._blobs[doc.doc_id]
        dest = Path(dest_path)
        mode = "r+b" if offset > 0 and dest.exists() else "wb"
        with open(dest, mode) as fh:
            if offset:
                fh.seek(offset)
            fh.write(blob[offset:])
        if progress is not None:
            progress(len(blob))
        return len(blob) - offset

    def delete_messages(self, chat_id: int, msg_ids: List[int]) -> None:
        for msg_id in msg_ids:
            self._blobs.pop(msg_id, None)

    # Helper for tests: direct access to the stored bytes.
    def stored_bytes(self, file_id: int) -> bytes:
        return self._blobs[file_id]


def canned_updates(chats: Optional[list] = None, messages: Optional[list] = None) -> Any:
    """Builds Updates-shaped responses for testing Telethon impl response parsing."""
    from types import SimpleNamespace

    return SimpleNamespace(chats=chats or [], updates=messages or [])


def canned_message_update(msg_id: int) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(message=SimpleNamespace(id=msg_id))
