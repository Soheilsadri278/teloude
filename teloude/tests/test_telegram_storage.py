# teloude/tests/test_telegram_storage.py
"""Storage/forum gateway tests: fake behavior + Telethon request construction."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from teloude.infrastructure.telegram.exceptions import RateLimitExceeded
from teloude.infrastructure.telegram.fakes import FakeStorageGateway, canned_updates
from teloude.infrastructure.telegram.storage import (
    DialogState,
    ITelegramStorage,
    TelethonStorageGateway,
)


class TestFakeStorage:
    def test_create_find_roundtrip(self):
        gateway = FakeStorageGateway()
        assert isinstance(gateway, ITelegramStorage)
        info = gateway.create_storage("Photos")
        assert info.title == "Teloude - Photos" and info.is_forum is True
        found = gateway.find_storage("Photos")
        assert found is not None and found.chat_id == info.chat_id
        assert gateway.find_storage("Missing") is None

    def test_root_topic_reuses_general(self):
        gateway = FakeStorageGateway()
        info = gateway.create_storage("Photos")
        root = gateway.ensure_topic(info.chat_id, "Photos", is_root=True)
        assert root.topic_id == 1  # renamed General topic
        assert gateway.ensure_topic(info.chat_id, "Photos", is_root=True).topic_id == 1

    def test_nested_topics_created_once(self):
        gateway = FakeStorageGateway()
        info = gateway.create_storage("Photos")
        first = gateway.ensure_topic(info.chat_id, "Photos / 2026 / Wedding")
        second = gateway.ensure_topic(info.chat_id, "Photos / 2026 / Wedding")
        assert first.topic_id == second.topic_id
        assert len(gateway.list_topics(info.chat_id)) == 2  # root + wedding

    def test_deletion_calls_recorded(self):
        gateway = FakeStorageGateway()
        info = gateway.create_storage("Temp")
        gateway.delete_topic_messages(info.chat_id, 2)
        gateway.delete_storage(info.chat_id)
        assert gateway.find_storage("Temp") is None
        assert any(c.startswith("delete_storage") for c in gateway.calls)


class ScriptedInvoke:
    """Records requests; responds from a type-name -> handler mapping."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.seen = []

    def __call__(self, request):
        self.seen.append(type(request).__name__)
        handler = self.handlers.get(type(request).__name__)
        if handler is None:
            raise AssertionError(f"Unexpected request: {type(request).__name__}")
        if isinstance(handler, Exception):
            raise handler
        return handler(request)


def _chat_updates(chat_id=-100555, title="Teloude - Photos"):
    return canned_updates(chats=[SimpleNamespace(id=chat_id, title=title)])


def _topics_response(topics):
    return SimpleNamespace(
        topics=[SimpleNamespace(id=tid, title=title) for tid, title in topics]
    )


class TestTelethonStorageGateway:
    def _gateway(self, handlers, dialogs=()):
        invoke = ScriptedInvoke(handlers)
        gateway = TelethonStorageGateway(
            invoke=invoke, list_dialogs=lambda: list(dialogs)
        )
        return gateway, invoke

    def test_create_storage_request(self):
        gateway, invoke = self._gateway({"CreateChannelRequest": lambda r: _chat_updates()})
        info = gateway.create_storage("Photos")
        assert info.chat_id == -100555 and info.is_forum is True
        assert invoke.seen == ["CreateChannelRequest"]

    def test_create_construction_params(self):
        seen_requests = []

        def capture(request):
            seen_requests.append(request)
            return _chat_updates()

        gateway = TelethonStorageGateway(invoke=capture, list_dialogs=list)
        gateway.create_storage("Docs")
        req = seen_requests[0]
        assert req.title == "Teloude - Docs"
        assert req.megagroup is True and req.broadcast is False and req.forum is True

    def test_find_storage(self):
        dialogs = [
            DialogState(chat_id=-1, title="Random Chat", is_megagroup=False),
            DialogState(chat_id=-100555, title="Teloude - Photos", is_megagroup=True),
        ]
        gateway, _ = self._gateway({}, dialogs=dialogs)
        found = gateway.find_storage("Photos")
        assert found is not None and found.chat_id == -100555
        assert gateway.find_storage("Other") is None

    def test_ensure_topic_creates_then_finds(self):
        state = {"topics": [(1, "General")]}

        def get_topics(request):
            return _topics_response(state["topics"])

        def create_topic(request):
            state["topics"].append((5, request.title))
            return canned_updates()

        gateway, invoke = self._gateway(
            {"GetForumTopicsRequest": get_topics, "CreateForumTopicRequest": create_topic}
        )
        topic = gateway.ensure_topic(-100555, "Photos / 2026")
        assert topic.topic_id == 5
        # Second call finds without creating.
        invoke.seen.clear()
        assert gateway.ensure_topic(-100555, "Photos / 2026").topic_id == 5
        assert "CreateForumTopicRequest" not in invoke.seen

    def test_root_reuses_general_via_rename(self):
        renamed = {}

        def get_topics(request):
            title = renamed.get(1, "General")
            return _topics_response([(1, title)])

        def rename(request):
            renamed[request.topic_id] = request.title
            return canned_updates()

        gateway, invoke = self._gateway(
            {"GetForumTopicsRequest": get_topics, "EditForumTopicRequest": rename}
        )
        topic = gateway.ensure_topic(-100555, "Photos", is_root=True)
        assert topic.topic_id == 1 and topic.title == "Photos"
        assert "CreateForumTopicRequest" not in invoke.seen

    def test_flood_wait_mapped(self):
        from telethon.errors import FloodWaitError

        gateway, _ = self._gateway(
            {"GetForumTopicsRequest": FloodWaitError(MagicMock(), 30)}
        )
        with pytest.raises(RateLimitExceeded) as exc_info:
            gateway.list_topics(-100555)
        assert exc_info.value.retry_after == 30
