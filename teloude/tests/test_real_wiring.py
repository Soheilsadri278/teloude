# teloude/tests/test_real_wiring.py
"""Production-path tests over a scripted in-memory Telegram server.

Unlike the Fake* doubles (which bypass the gateway layer), ScriptedRawClient
speaks REAL Telethon request classes, so this exercises the true production
stack: TelethonAuth + TelethonStorageGateway + TelethonFileGateway + engines +
services + repositories. No network, no real account.
"""
import threading
from types import SimpleNamespace as NS

import pytest

from telethon.errors import PhoneCodeInvalidError

from teloude.application.services import ServiceError
from teloude.config import AppConfig
from teloude.infrastructure.database import close_db_connection
from teloude.infrastructure.telegram.auth import AuthState
from teloude.infrastructure.telegram.exceptions import AuthError
from teloude.ui.app import build_real


def ns(**kwargs):
    return NS(**kwargs)


class ScriptedRawClient:
    """In-memory Telegram server speaking real Telethon request types."""

    def __init__(self):
        self.authorized = False
        self.codes_requested = []
        self.disconnects = 0
        self.toggle_forum_calls = []
        self._next_chat = 777
        self._next_topic = 2
        self._next_msg = 9001
        self._next_doc = 5001
        self.chats = {}   # chat_id -> {"title", "topics": {id: title}, "messages": {...}}
        self.parts = {}   # (file_id, part_no) -> bytes
        self.docs = {}    # doc_id -> {"content", "name", "size"}

    # -- auth surface (mirrors TelegramClient methods used by TelethonAuth) --
    def send_code_request(self, phone):
        self.codes_requested.append(phone)
        return ns(phone_code_hash="h:123")

    def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if code != "11111":
            raise PhoneCodeInvalidError(request=None)
        self.authorized = True
        return ns()

    def is_user_authorized(self):
        return self.authorized

    def disconnect(self):
        self.disconnects += 1

    def log_out(self):
        self.authorized = False
        return True

    async def get_dialogs(self):
        return [
            ns(id=cid, title=c["title"], entity=ns(megagroup=True))
            for cid, c in self.chats.items()
        ]

    # -- request surface (mirrors TelegramClient(request)) --
    def __call__(self, request):
        handler = getattr(self, "_req_" + type(request).__name__, None)
        assert handler is not None, f"unhandled request {type(request).__name__}"
        return handler(request)

    def _req_CreateChannelRequest(self, request):
        chat_id = self._next_chat
        self._next_chat += 1
        self.chats[chat_id] = {
            "title": request.title, "topics": {1: "General"}, "messages": {},
        }
        return ns(chats=[ns(id=chat_id)])

    def _req_ToggleForumRequest(self, request):
        self.toggle_forum_calls.append(request.channel)
        return ns()

    def _req_GetForumTopicsRequest(self, request):
        chat = self.chats[request.peer]
        return ns(topics=[ns(id=tid, title=t) for tid, t in chat["topics"].items()])

    def _req_CreateForumTopicRequest(self, request):
        chat = self.chats[request.peer]
        topic_id = self._next_topic
        self._next_topic += 1
        chat["topics"][topic_id] = request.title
        return ns(updates=[])

    def _req_EditForumTopicRequest(self, request):
        if getattr(request, "title", None):
            self.chats[request.peer]["topics"][request.topic_id] = request.title
        return ns()

    def _req_DeleteTopicHistoryRequest(self, request):
        chat = self.chats[request.peer]
        chat["messages"] = {
            mid: m for mid, m in chat["messages"].items()
            if m["topic"] != request.top_msg_id
        }
        return ns()

    def _req_SaveFilePartRequest(self, request):
        self.parts[(request.file_id, request.file_part)] = bytes(request.bytes)
        return True

    def _req_SaveBigFilePartRequest(self, request):
        self.parts[(request.file_id, request.file_part)] = bytes(request.bytes)
        return True

    def _req_SendMediaRequest(self, request):
        tl_file = request.media.file
        blob = b"".join(
            self.parts[(tl_file.id, part)]
            for part in sorted(p for fid, p in self.parts if fid == tl_file.id)
        )
        doc_id = self._next_doc
        self._next_doc += 1
        name = tl_file.name
        self.docs[doc_id] = {"content": blob, "name": name, "size": len(blob)}
        msg_id = self._next_msg
        self._next_msg += 1
        topic = request.reply_to.reply_to_msg_id
        for cid, chat in self.chats.items():
            if cid == request.peer:
                chat["messages"][msg_id] = {"topic": topic, "doc": doc_id}
        return ns(updates=[ns(message=ns(id=msg_id))])

    def _doc_ns(self, doc_id):
        doc = self.docs[doc_id]
        return ns(
            id=doc_id, access_hash=7, file_reference=b"fr",
            size=doc["size"], mime_type="application/octet-stream",
            attributes=[ns(file_name=doc["name"])],
        )

    def _req_GetMessagesRequest(self, request):
        chat = self.chats[request.channel]
        messages = []
        for ref in request.id:
            mid = ref.id
            messages.append(ns(media=ns(document=self._doc_ns(chat["messages"][mid]["doc"]))))
        return ns(messages=messages)

    def _req_GetFileRequest(self, request):
        content = self.docs[request.location.id]["content"]
        sl = content[request.offset:request.offset + request.limit]
        return ns(bytes=sl)

    def _req_GetHistoryRequest(self, request):
        chat = self.chats[request.peer]
        messages = []
        for mid, m in sorted(chat["messages"].items()):
            doc = self.docs[m["doc"]]
            messages.append(ns(
                id=mid, date=None,
                reply_to=ns(reply_to_msg_id=m["topic"]),
                media=ns(document=self._doc_ns(m["doc"])),
            ))
        return ns(messages=messages)

    def _req_DeleteChannelRequest(self, request):
        self.chats.pop(request.channel, None)
        return ns()

    def _req_DeleteMessagesRequest(self, request):
        for chat in self.chats.values():
            for mid in request.id:
                chat["messages"].pop(mid, None)
        return ns()


@pytest.fixture()
def real_ctx(tmp_path):
    raw = ScriptedRawClient()
    config = AppConfig(
        data_dir=str(tmp_path / "data"),
        database_path=str(tmp_path / "data" / "real.db"),
        session_dir=str(tmp_path / "sessions"),
    )
    wrapper = NS(underlying_client=raw, disconnect=raw.disconnect)
    ctx = build_real(config, api_id=12345, api_hash="a" * 32,
                     connector=lambda phone: wrapper)
    ctx.raw = raw
    yield ctx
    for service in (ctx.services.backup, ctx.services.restore):
        try:
            service.cancel()
        except Exception:
            pass
    ctx.shutdown()
    close_db_connection(ctx.db)


def _wait(event, timeout=20.0):
    assert event.wait(timeout), "timed out waiting for service event"


class TestRealAuthWiring:
    def test_login_flow_over_real_gateway(self, real_ctx):
        auth = real_ctx.services.auth
        assert auth.start_login("+10000000000") is AuthState.CODE_SENT
        assert real_ctx.raw.codes_requested == ["+10000000000"]
        assert auth.submit_code("+10000000000", "11111") is AuthState.AUTHORIZED
        assert auth.state is AuthState.AUTHORIZED

    def test_wrong_code_maps_real_rpc_error(self, real_ctx):
        auth = real_ctx.services.auth
        auth.start_login("+10000000000")
        with pytest.raises(AuthError) as exc_info:
            auth.submit_code("+10000000000", "00000")
        message = str(exc_info.value).lower()
        assert "incorrect" in message or "invalid" in message
        assert "caused by nonetype" not in message  # no raw RPC internals leak


class TestRealBackupRestore:
    def _login(self, real_ctx):
        auth = real_ctx.services.auth
        auth.start_login("+10000000000")
        auth.submit_code("+10000000000", "11111")

    def _backup(self, real_ctx, src, name="Docs"):
        self._login(real_ctx)
        record = real_ctx.services.storages.create_storage(name)
        done = threading.Event()
        real_ctx.bus.subscribe("backup_done", lambda _p: done.set())
        real_ctx.services.backup.start(record.id, src)
        _wait(done)
        return record

    def test_backup_and_restore_end_to_end(self, real_ctx, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"alpha-bytes")
        (src / "sub").mkdir()
        (src / "sub" / "b.bin").write_bytes(bytes(range(256)) * 40)
        record = self._backup(real_ctx, src)
        assert record.telegram_chat_id == 777
        # forum=True is set at creation, so no redundant ToggleForum call goes out.
        assert real_ctx.raw.toggle_forum_calls == []
        rows = real_ctx.repos.files.list_by_storage(record.id)
        assert len(rows) == 2
        assert all(r.is_backed_up and r.telegram_msg_id for r in rows)

        dest = tmp_path / "out"
        done = threading.Event()
        payloads = []
        real_ctx.bus.subscribe("restore_done", lambda p: (payloads.append(p), done.set()))
        real_ctx.services.restore.start_storage(record.id, dest)
        _wait(done)
        assert payloads[0]["restored"] == 2
        assert (dest / "a.txt").read_bytes() == b"alpha-bytes"
        assert (dest / "sub" / "b.bin").read_bytes() == bytes(range(256)) * 40

    def test_adopt_rebuilds_index_from_server(self, real_ctx, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "keep.txt").write_bytes(b"keep me")
        record = self._backup(real_ctx, src, name="Archive")

        fresh = AppConfig(
            data_dir=str(tmp_path / "data2"),
            database_path=str(tmp_path / "data2" / "fresh.db"),
            session_dir=str(tmp_path / "sessions"),
        )
        wrapper = NS(underlying_client=real_ctx.raw, disconnect=lambda: None)
        ctx2 = build_real(fresh, api_id=12345, api_hash="a" * 32,
                          connector=lambda phone: wrapper)
        try:
            ctx2.services.auth.start_login("+10000000000")
            ctx2.services.auth.submit_code("+10000000000", "11111")
            adopted = ctx2.services.storages.adopt_storage("Archive")
            assert adopted.telegram_chat_id == record.telegram_chat_id
            rows = ctx2.repos.files.list_by_storage(adopted.id)
            assert len(rows) == 1 and rows[0].is_backed_up
            assert rows[0].relative_path == "keep.txt"
        finally:
            ctx2.shutdown()
            close_db_connection(ctx2.db)

    def test_delete_storage_cloud(self, real_ctx, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "x.txt").write_bytes(b"x")
        record = self._backup(real_ctx, src, name="Temp")
        with pytest.raises(ServiceError):
            real_ctx.services.storages.delete_storage_cloud(record.id + 9999)
        real_ctx.services.storages.delete_storage_cloud(record.id)
        assert real_ctx.repos.storages.get(record.id) is None
        assert 777 not in real_ctx.raw.chats
