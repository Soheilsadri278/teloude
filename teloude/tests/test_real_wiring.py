# teloude/tests/test_real_wiring.py
"""Production-path tests over a scripted in-memory Telegram server.

Unlike the Fake* doubles (which bypass the gateway layer), ScriptedRawClient
speaks REAL Telethon request classes, so this exercises the true production
stack: TelethonAuth + TelethonStorageGateway + TelethonFileGateway + engines +
services + repositories. No network, no real account.
"""
import hashlib
import os
import threading
import time
from types import SimpleNamespace as NS

import pytest

from telethon.errors import PhoneCodeInvalidError

from teloude.application.services import ServiceError
from teloude.config import AppConfig
from teloude.core.transfers import TransferState
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
        # fault injection (network loss simulation) + observation hooks
        self.fail_upload_parts = 0     # raise OSError for the next N part writes
        self.fail_download_chunks = 0  # raise OSError for the next N reads
        self.part_writes = []          # part numbers written, in order
        self.read_offsets = []         # GetFileRequest offsets, in order
        self.on_part = None            # callback(part_no) after a part is stored

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

    def _store_part(self, request):
        if self.fail_upload_parts > 0:
            self.fail_upload_parts -= 1
            raise OSError("simulated network outage")
        part_no = request.file_part
        self.part_writes.append(part_no)
        self.parts[(request.file_id, part_no)] = bytes(request.bytes)
        if self.on_part is not None:
            self.on_part(part_no)
        return True

    def _req_SaveFilePartRequest(self, request):
        return self._store_part(request)

    def _req_SaveBigFilePartRequest(self, request):
        return self._store_part(request)

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
        if self.fail_download_chunks > 0:
            self.fail_download_chunks -= 1
            raise OSError("simulated network outage")
        self.read_offsets.append(request.offset)
        content = self.docs[request.location.id]["content"]
        sl = content[request.offset:request.offset + request.limit]
        return ns(bytes=sl)

    def _req_GetHistoryRequest(self, request):
        chat = self.chats[request.peer]
        messages = []
        for mid, m in sorted(chat["messages"].items()):
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
    # keep retry backoff instant in tests while still exercising the retry path
    ctx.backup_manager._sleeper = lambda _seconds: None
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
        # Bug 1: restore recreates the selected root folder "src"
        assert (dest / "src" / "a.txt").read_bytes() == b"alpha-bytes"
        assert (dest / "src" / "sub" / "b.bin").read_bytes() == bytes(range(256)) * 40

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
            assert rows[0].relative_path == "src/keep.txt"
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


def _paused_flag(statuses):
    return any(s == "paused" for s in statuses)


def _stored_blob(real_ctx) -> bytes:
    """Content of the most recently posted document, as Telegram would serve it."""
    if not real_ctx.raw.docs:
        return b""
    return real_ctx.raw.docs[max(real_ctx.raw.docs)]["content"]


class TestRealResilience:
    """Network loss, pause/resume and crash recovery over the real gateways."""

    def _login(self, real_ctx):
        auth = real_ctx.services.auth
        auth.start_login("+10000000000")
        auth.submit_code("+10000000000", "11111")

    def _watch_states(self, real_ctx, seen):
        original = real_ctx.registry.transition

        def spy(transfer_id, new_state):
            seen.append(new_state.value if hasattr(new_state, "value") else str(new_state))
            return original(transfer_id, new_state)

        real_ctx.registry.transition = spy

    def _run_backup(self, real_ctx, src, name="Docs"):
        record = real_ctx.services.storages.create_storage(name)
        done = threading.Event()
        real_ctx.bus.subscribe("backup_done", lambda _p: done.set())
        real_ctx.services.backup.start(record.id, src)
        _wait(done)
        return record

    def test_network_drop_mid_upload_recovers(self, real_ctx, tmp_path):
        payload = bytes(range(256)) * 4096  # 1 MiB -> several parts
        src = tmp_path / "src"
        src.mkdir()
        (src / "payload.bin").write_bytes(payload)
        self._login(real_ctx)
        seen = []
        self._watch_states(real_ctx, seen)

        def drop_after_first_part(part_no):
            if part_no == 0 and not drop_after_first_part.armed:
                drop_after_first_part.armed = True
                real_ctx.raw.fail_upload_parts = 1  # network dies mid-file

        drop_after_first_part.armed = False

        real_ctx.raw.on_part = drop_after_first_part
        record = self._run_backup(real_ctx, src)
        rows = real_ctx.repos.files.list_by_storage(record.id)
        assert len(rows) == 1 and rows[0].is_backed_up
        assert rows[0].sha256 == hashlib.sha256(payload).hexdigest()
        assert "waiting_for_network" in seen, seen
        # parts may have expired on Telegram's side, so the retry restarts the file
        assert real_ctx.raw.part_writes.count(0) == 2
        assert _stored_blob(real_ctx) == payload
        # progress never exceeds the file size (no double counting on retries)
        assert [t.done_bytes for t in real_ctx.repos.transfers.list_recent()
                if t.kind == "upload"] == [len(payload)]

        dest = tmp_path / "out"
        done = threading.Event()
        real_ctx.bus.subscribe("restore_done", lambda _p: done.set())
        real_ctx.services.restore.start_storage(record.id, dest)
        _wait(done)
        assert (dest / "src" / "payload.bin").read_bytes() == payload

    def test_pause_resumes_from_part_checkpoint(self, real_ctx, tmp_path):
        payload = bytes(range(256)) * 4096  # 8 parts of 128 KiB at 1 MiB
        src = tmp_path / "src"
        src.mkdir()
        (src / "payload.bin").write_bytes(payload)
        self._login(real_ctx)
        seen = []
        self._watch_states(real_ctx, seen)

        def resume_when_paused():
            deadline = time.time() + 15.0
            while time.time() < deadline:
                if _paused_flag([t.status for t in real_ctx.registry.active()]):
                    real_ctx.services.backup.resume()
                    return
                time.sleep(0.01)
            raise AssertionError("backup never reported the paused state")

        def on_part(part_no):
            if part_no == 0 and not on_part.paused:
                on_part.paused = True
                threading.Thread(target=resume_when_paused, daemon=True).start()
                real_ctx.services.backup.pause()  # user hits Pause mid-upload

        on_part.paused = False

        real_ctx.raw.on_part = on_part
        record = self._run_backup(real_ctx, src)
        assert _paused_flag(seen), seen

        rows = real_ctx.repos.files.list_by_storage(record.id)
        assert len(rows) == 1 and rows[0].is_backed_up
        # resumed upload continued from a later part (no full re-upload of part 0)…
        assert real_ctx.raw.part_writes.count(0) == 1
        assert max(real_ctx.raw.part_writes) >= 1
        # …and the document Telegram ended up with is the complete file
        assert _stored_blob(real_ctx) == payload
        dest = tmp_path / "out"
        done = threading.Event()
        real_ctx.bus.subscribe("restore_done", lambda _p: done.set())
        real_ctx.services.restore.start_storage(record.id, dest)
        _wait(done)
        assert (dest / "src" / "payload.bin").read_bytes() == payload

    def test_crash_recovery_requeues_and_finishes(self, real_ctx, tmp_path):
        payload = bytes(range(256)) * 4096  # 1 MiB -> pause lands mid-file
        src = tmp_path / "src"
        src.mkdir()
        (src / "data.bin").write_bytes(payload)
        self._login(real_ctx)
        record = real_ctx.services.storages.create_storage("Docs")

        # Interrupt the process mid-upload: pause on the first part, then let the
        # engine be cancelled (its thread stops) while the row still reads paused.
        def on_part(part_no):
            if part_no == 0 and not on_part.paused:
                on_part.paused = True  # only the first run pauses
                real_ctx.services.backup.pause()

        on_part.paused = False
        real_ctx.raw.on_part = on_part
        first_done = threading.Event()
        real_ctx.bus.subscribe("backup_done", lambda _p: first_done.set())
        real_ctx.services.backup.start(record.id, src)
        deadline = time.time() + 15.0
        while time.time() < deadline and not _paused_flag(
            [t.status for t in real_ctx.registry.active()]
        ):
            time.sleep(0.01)
        real_ctx.services.backup.cancel()  # engine thread stops here
        _wait(first_done)
        stale = [t for t in real_ctx.repos.transfers.list_recent() if t.kind == "upload"]
        assert stale, "expected the interrupted upload row to survive"
        transfer_id = stale[0].id
        # A killed process leaves the row in its last active state; running the
        # cancel in-process would otherwise record a clean cancellation, so put
        # the row back to exactly what the crash survivor looks like.
        real_ctx.registry.set_status_quiet(transfer_id, TransferState.PAUSED)

        assert real_ctx.backup_manager.recover_pending() >= 1
        queued = real_ctx.repos.transfers.get(transfer_id)
        assert queued is not None and queued.status == TransferState.QUEUED.value

        # The recovered run starts over (no upload id survives a crash) and finishes.
        done = threading.Event()
        real_ctx.bus.subscribe("backup_done", lambda _p: done.set())
        real_ctx.services.backup.start(record.id, src)
        _wait(done)
        rows = real_ctx.repos.files.list_by_storage(record.id)
        assert [r.is_backed_up for r in rows] == [True]
        assert rows[0].sha256 == hashlib.sha256(payload).hexdigest()
        finished = real_ctx.repos.transfers.get(transfer_id)
        assert finished is not None and finished.status == TransferState.COMPLETED.value
        assert finished.done_bytes == len(payload)
        assert _stored_blob(real_ctx) == payload

    def test_restore_network_drop_resumes_at_offset(self, real_ctx, tmp_path):
        payload = bytes(range(256)) * 4096
        src = tmp_path / "src"
        src.mkdir()
        (src / "payload.bin").write_bytes(payload)
        self._login(real_ctx)
        record = self._run_backup(real_ctx, src)

        real_ctx.raw.fail_download_chunks = 1
        real_ctx.raw.read_offsets.clear()
        dest = tmp_path / "out"
        done = threading.Event()
        payloads = []
        real_ctx.bus.subscribe("restore_done", lambda p: (payloads.append(p), done.set()))
        real_ctx.services.restore.start_storage(record.id, dest)
        _wait(done)
        assert payloads[0]["restored"] == 1 and payloads[0]["failed"] == []
        assert (dest / "src" / "payload.bin").read_bytes() == payload
        # downloads resume at the recorded byte offset instead of restarting
        assert any(offset > 0 for offset in real_ctx.raw.read_offsets)

    def test_large_file_uses_big_file_protocol(self, real_ctx, tmp_path):
        payload = os.urandom(11 * 1024 * 1024)  # > 10 MiB -> SaveBigFilePart
        src = tmp_path / "src"
        src.mkdir()
        (src / "big.bin").write_bytes(payload)
        self._login(real_ctx)
        record = self._run_backup(real_ctx, src)
        rows = real_ctx.repos.files.list_by_storage(record.id)
        assert len(rows) == 1 and rows[0].is_backed_up
        stored = real_ctx.raw.docs[real_ctx.raw._next_doc - 1]["content"]
        assert stored == payload  # reassembled from SaveBigFilePartRequest parts
        dest = tmp_path / "out"
        done = threading.Event()
        real_ctx.bus.subscribe("restore_done", lambda _p: done.set())
        real_ctx.services.restore.start_storage(record.id, dest)
        _wait(done)
        assert (dest / "src" / "big.bin").read_bytes() == payload


class TestRealRootFolderMapping:
    """Bug 1 + Bug 4 over the real gateway stack (no fakes below the services).

    ScriptedRawClient records the forum topic of every posted document and the
    true on-wire topic titles, so these assertions are what Telegram itself
    would see - the strongest check available without a live account.
    """

    def _login(self, real_ctx):
        auth = real_ctx.services.auth
        auth.start_login("+10000000000")
        auth.submit_code("+10000000000", "11111")

    def _run_backup(self, real_ctx, storage_id, root):
        done = threading.Event()
        real_ctx.bus.subscribe("backup_done", lambda _p: done.set())
        real_ctx.services.backup.start(storage_id, root)
        _wait(done)

    def _rows(self, real_ctx, storage_id):
        return {r.relative_path: r
                for r in real_ctx.repos.files.list_by_storage(storage_id)}

    def test_each_root_folder_gets_its_own_topic_and_its_own_tree(self, real_ctx, tmp_path):
        self._login(real_ctx)
        storage = real_ctx.services.storages.create_storage("Topics")
        chat = real_ctx.raw.chats[storage.telegram_chat_id]

        parent = tmp_path / "sources"
        parent.mkdir()
        folder_a = parent / "FolderA"
        folder_a.mkdir()
        (folder_a / "a1.txt").write_bytes(b"a1")
        folder_b = parent / "FolderB"
        folder_b.mkdir()
        (folder_b / "b1.txt").write_bytes(b"b1")

        self._run_backup(real_ctx, storage.id, folder_a)
        self._run_backup(real_ctx, storage.id, folder_b)

        topics = {title: topic_id for topic_id, title in chat["topics"].items()}
        assert "Topics / FolderA" in topics and "Topics / FolderB" in topics
        assert topics["Topics / FolderA"] != topics["Topics / FolderB"]

        messages = chat["messages"]
        rows = self._rows(real_ctx, storage.id)
        assert messages[rows["FolderA/a1.txt"].telegram_msg_id]["topic"] == \
            topics["Topics / FolderA"]
        assert messages[rows["FolderB/b1.txt"].telegram_msg_id]["topic"] == \
            topics["Topics / FolderB"]

        # B was not uploaded into the topic A used last, and a second run of A
        # reuses A's topic instead of creating another one
        before = dict(chat["topics"])
        (folder_a / "a2.txt").write_bytes(b"a2")
        self._run_backup(real_ctx, storage.id, folder_a)
        assert chat["topics"] == before
        rows = self._rows(real_ctx, storage.id)
        assert messages[rows["FolderA/a2.txt"].telegram_msg_id]["topic"] == \
            topics["Topics / FolderA"]

    def test_restore_from_the_real_stack_recreates_the_root_folder(self, real_ctx, tmp_path):
        self._login(real_ctx)
        storage = real_ctx.services.storages.create_storage("Mirror")
        source = tmp_path / "Camera"
        (source / "DCIM").mkdir(parents=True)
        (source / "top.txt").write_bytes(b"top")
        (source / "DCIM" / "photo.jpg").write_bytes(b"jpeg-bytes")
        self._run_backup(real_ctx, storage.id, source)

        destination = tmp_path / "Restored"
        done = threading.Event()
        payloads = []
        real_ctx.bus.subscribe("restore_done", lambda p: (payloads.append(p), done.set()))
        real_ctx.services.restore.start_storage(storage.id, destination)
        _wait(done)
        assert payloads[0]["restored"] == 2 and payloads[0]["failed"] == []
        assert (destination / "Camera" / "top.txt").read_bytes() == b"top"
        assert (destination / "Camera" / "DCIM" / "photo.jpg").read_bytes() == b"jpeg-bytes"
