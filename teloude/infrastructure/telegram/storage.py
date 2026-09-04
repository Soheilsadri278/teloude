# teloude/infrastructure/telegram/storage.py
"""Telegram storage management: private forum supergroups + flat topics.

One Teloude storage = one private Telegram supergroup in forum mode.
Topic titles encode the folder path (see teloude.core.topics); the local
database stays authoritative for hierarchy.

The gateway talks to Telegram through two injected callables so tests never
touch the network:

- invoke(request): sends one raw MTProto request, returns the response
  (Telethon's ``client(request)`` in production; sync or async).
- list_dialogs(): returns DialogState(chat_id, title, is_megagroup) rows.

Telethon request classes are imported lazily inside methods, keeping this
module importable without Telethon installed.
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from .bridge import extract_chat_id, map_rpc_error, run_sync
from .exceptions import ConnectionStateError, TeloudeTelegramError

logger = logging.getLogger("TelegramStorage")

STORAGE_PREFIX = "Teloude - "
STORAGE_ABOUT = (
    "Teloude backup storage. Files here are managed by the Teloude app - "
    "please do not edit or delete them manually."
)
GENERAL_TOPIC_ID = 1
GENERAL_TOPIC_TITLE = "General"


@dataclass
class DialogState:
    chat_id: int
    title: str
    is_megagroup: bool = False


@dataclass
class StorageInfo:
    chat_id: int
    title: str
    is_forum: bool = False


@dataclass
class TopicInfo:
    topic_id: int
    title: str


class ITelegramStorage(ABC):
    """Abstract storage/topic management; services depend only on this."""

    @abstractmethod
    def create_storage(self, name: str) -> StorageInfo:
        """Creates a private forum supergroup titled 'Teloude - <name>'."""
        raise NotImplementedError

    @abstractmethod
    def find_storage(self, name: str) -> Optional[StorageInfo]:
        """Finds an existing storage group by name; None when absent."""
        raise NotImplementedError

    @abstractmethod
    def ensure_forum(self, chat_id: int) -> None:
        """Enables forum mode on a group (idempotent; heals legacy groups)."""
        raise NotImplementedError

    @abstractmethod
    def list_topics(self, chat_id: int) -> List[TopicInfo]:
        """Lists forum topics of a storage group."""
        raise NotImplementedError

    @abstractmethod
    def ensure_topic(self, chat_id: int, title: str, is_root: bool = False) -> TopicInfo:
        """Idempotent topic lookup-or-create; renames 'General' for the root topic."""
        raise NotImplementedError

    @abstractmethod
    def close_topic(self, chat_id: int, topic_id: int) -> None:
        """Closes a topic (used before/after explicit cloud deletion flows)."""
        raise NotImplementedError

    @abstractmethod
    def delete_topic_messages(self, chat_id: int, topic_id: int) -> None:
        """Deletes all messages inside a topic. Explicit user confirmation required upstream."""
        raise NotImplementedError

    @abstractmethod
    def delete_storage(self, chat_id: int) -> None:
        """Deletes the storage group. Explicit user confirmation required upstream."""
        raise NotImplementedError

    @abstractmethod
    def list_topic_documents(
        self, chat_id: int, topic_id: int, limit: int = 100
    ) -> List["DocumentMeta"]:
        """Lists document messages inside a topic (for index rebuild/adoption)."""
        raise NotImplementedError


@dataclass
class DocumentMeta:
    msg_id: int
    file_name: str
    size: int
    date: Optional[str] = None
    mime: str = ""


def telethon_list_dialogs(client: Any) -> Callable[[], List["DialogState"]]:
    """Builds the ``list_dialogs`` callable for :class:`TelethonStorageGateway`.

    Uses Telethon's high-level ``get_dialogs()`` (one call, no per-chat
    round-trips) and maps rows to :class:`DialogState`.
    """

    def _list() -> List["DialogState"]:
        dialogs = run_sync(client.get_dialogs())
        rows = []
        for dialog in dialogs or []:
            entity = getattr(dialog, "entity", None)
            rows.append(
                DialogState(
                    chat_id=int(getattr(dialog, "id", 0) or 0),
                    title=str(getattr(dialog, "title", "") or ""),
                    is_megagroup=bool(getattr(entity, "megagroup", False)),
                )
            )
        return rows

    return _list


class TelethonStorageGateway(ITelegramStorage):
    """ITelegramStorage over raw MTProto requests (verified against Telethon 1.44)."""

    def __init__(
        self,
        invoke: Callable[[Any], Any],
        list_dialogs: Callable[[], List[DialogState]],
    ):
        self._invoke = invoke
        self._list_dialogs = list_dialogs

    # -- low-level helper -------------------------------------------------
    def _call(self, request: Any, operation: str) -> Any:
        try:
            return run_sync(self._invoke(request))
        except TeloudeTelegramError:
            raise
        except Exception as exc:
            raise map_rpc_error(exc, operation) from exc

    # -- ITelegramStorage -------------------------------------------------
    def create_storage(self, name: str) -> StorageInfo:
        from telethon.tl.functions.channels import CreateChannelRequest

        title = f"{STORAGE_PREFIX}{name}"
        updates = self._call(
            CreateChannelRequest(
                title=title, about=STORAGE_ABOUT,
                broadcast=False, megagroup=True, forum=True,
            ),
            "creating the Telegram storage",
        )
        chat_id = extract_chat_id(updates)
        if chat_id is None:
            raise ConnectionStateError("Telegram did not return the new storage group.")
        logger.info("Telegram storage group created.")
        return StorageInfo(chat_id=chat_id, title=title, is_forum=True)

    def find_storage(self, name: str) -> Optional[StorageInfo]:
        try:
            dialogs = list(self._list_dialogs())
        except Exception as exc:
            raise map_rpc_error(exc, "listing Telegram chats") from exc
        wanted = f"{STORAGE_PREFIX}{name}"
        for dialog in dialogs:
            if dialog.title == wanted and dialog.is_megagroup:
                return StorageInfo(chat_id=dialog.chat_id, title=dialog.title)
        return None

    def ensure_forum(self, chat_id: int) -> None:
        from telethon.tl.functions.channels import ToggleForumRequest

        self._call(
            ToggleForumRequest(channel=chat_id, enabled=True, tabs=True),
            "enabling forum mode",
        )

    def list_topics(self, chat_id: int) -> List[TopicInfo]:
        from telethon.tl.functions.messages import GetForumTopicsRequest

        response = self._call(
            GetForumTopicsRequest(
                peer=chat_id, offset_date=None, offset_id=0,
                offset_topic=0, limit=100,
            ),
            "listing storage topics",
        )
        topics = []
        for topic in getattr(response, "topics", []) or []:
            topic_id = getattr(topic, "id", None)
            title = getattr(topic, "title", None)
            if topic_id is not None and title:
                topics.append(TopicInfo(topic_id=int(topic_id), title=str(title)))
        return topics

    def ensure_topic(self, chat_id: int, title: str, is_root: bool = False) -> TopicInfo:
        for topic in self.list_topics(chat_id):
            if topic.title == title:
                return topic
        if is_root:
            general = next(
                (t for t in self.list_topics(chat_id)
                 if t.topic_id == GENERAL_TOPIC_ID and t.title == GENERAL_TOPIC_TITLE),
                None,
            )
            if general is not None:
                return self._rename_topic(chat_id, GENERAL_TOPIC_ID, title)
        self._create_topic(chat_id, title)
        for topic in self.list_topics(chat_id):
            if topic.title == title:
                return topic
        raise ConnectionStateError(
            "Telegram did not return the new topic; please retry."
        )

    def close_topic(self, chat_id: int, topic_id: int) -> None:
        from telethon.tl.functions.messages import EditForumTopicRequest

        self._call(
            EditForumTopicRequest(peer=chat_id, topic_id=topic_id, closed=True),
            "closing the storage topic",
        )

    def delete_topic_messages(self, chat_id: int, topic_id: int) -> None:
        from telethon.tl.functions.messages import DeleteTopicHistoryRequest

        self._call(
            DeleteTopicHistoryRequest(peer=chat_id, top_msg_id=topic_id),
            "deleting topic messages",
        )

    def delete_storage(self, chat_id: int) -> None:
        from telethon.tl.functions.channels import DeleteChannelRequest

        self._call(DeleteChannelRequest(channel=chat_id), "deleting the storage group")

    def list_topic_documents(
        self, chat_id: int, topic_id: int, limit: int = 100
    ) -> List[DocumentMeta]:
        from telethon.tl.functions.messages import GetHistoryRequest

        response = self._call(
            GetHistoryRequest(
                peer=chat_id, offset_id=0, offset_date=None, add_offset=0,
                limit=max(1, limit), max_id=0, min_id=0, hash=0,
            ),
            "reading topic history",
        )
        documents = []
        for message in getattr(response, "messages", []) or []:
            if not self._in_topic(message, topic_id):
                continue
            media = getattr(message, "media", None)
            document = getattr(media, "document", None)
            if document is None:
                continue
            name = ""
            for attr in getattr(document, "attributes", []) or []:
                if hasattr(attr, "file_name") and getattr(attr, "file_name"):
                    name = str(getattr(attr, "file_name"))
                    break
            if not name:
                continue
            date = getattr(message, "date", None)
            documents.append(DocumentMeta(
                msg_id=int(message.id),
                file_name=name,
                size=int(getattr(document, "size", 0) or 0),
                date=date.isoformat() if date is not None and hasattr(date, "isoformat") else None,
                mime=str(getattr(document, "mime_type", "") or ""),
            ))
        return documents

    @staticmethod
    def _in_topic(message: Any, topic_id: int) -> bool:
        reply = getattr(message, "reply_to", None)
        if reply is None:
            return False
        for field in ("reply_to_msg_id", "reply_to_top_id", "top_msg_id"):
            if getattr(reply, field, None) == topic_id:
                return True
        return False

    # -- internals --------------------------------------------------------
    def _create_topic(self, chat_id: int, title: str) -> None:
        from telethon.tl.functions.messages import CreateForumTopicRequest
        import random

        self._call(
            CreateForumTopicRequest(
                peer=chat_id, title=title, random_id=random.getrandbits(63),
            ),
            "creating the storage topic",
        )

    def _rename_topic(self, chat_id: int, topic_id: int, title: str) -> TopicInfo:
        from telethon.tl.functions.messages import EditForumTopicRequest

        self._call(
            EditForumTopicRequest(peer=chat_id, topic_id=topic_id, title=title),
            "renaming the root topic",
        )
        return TopicInfo(topic_id=topic_id, title=title)
