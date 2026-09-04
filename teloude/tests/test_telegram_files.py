# teloude/tests/test_telegram_files.py
"""File gateway tests: fake round-trips + Telethon part/send/download wiring."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from teloude.infrastructure.telegram.exceptions import TeloudeTelegramError
from teloude.infrastructure.telegram.fakes import FakeFileGateway, canned_message_update, canned_updates
from teloude.infrastructure.telegram.files import (
    ITelegramFileGateway,
    TelethonFileGateway,
    UploadCancelled,
    UploadPaused,
)


class TestFakeFileGateway:
    def test_upload_download_roundtrip(self, tmp_path):
        gateway = FakeFileGateway()
        assert isinstance(gateway, ITelegramFileGateway)
        source = tmp_path / "data.bin"
        source.write_bytes(bytes(range(256)) * 100)
        progress = []
        uploaded = gateway.upload(source, progress=progress.append, part_size=1024)
        assert uploaded.size == 25600 and progress[-1] == 25600
        sent = gateway.send_to_topic(-1001, 2, uploaded, caption="backup")
        assert sent.msg_id == uploaded.file_id
        doc = gateway.resolve_document(-1001, sent.msg_id)
        dest = tmp_path / "restored.bin"
        written = gateway.download(doc, dest, progress=lambda n: None)
        assert written == 25600 and dest.read_bytes() == source.read_bytes()

    def test_pause_and_cancel_controls(self, tmp_path):
        gateway = FakeFileGateway()
        source = tmp_path / "a.bin"
        source.write_bytes(b"x" * 5000)
        with pytest.raises(UploadPaused):
            gateway.upload(source, should_pause=lambda: True, part_size=64)
        with pytest.raises(UploadCancelled):
            gateway.upload(source, is_cancelled=lambda: True, part_size=64)

    def test_one_shot_failure_then_success(self, tmp_path):
        gateway = FakeFileGateway()
        gateway.fail_next_upload_with = ConnectionError("net down")
        source = tmp_path / "a.bin"
        source.write_bytes(b"y" * 100)
        with pytest.raises(ConnectionError):
            gateway.upload(source)
        uploaded = gateway.upload(source)  # failure consumed; retry works
        assert uploaded.size == 100

    def test_missing_message(self):
        gateway = FakeFileGateway()
        with pytest.raises(TeloudeTelegramError):
            gateway.resolve_document(-1001, 424242)

    def test_tier_limit(self):
        assert FakeFileGateway(max_bytes=10).max_upload_bytes() == 10


class ScriptedInvoke:
    def __init__(self, handlers):
        self.handlers = handlers
        self.seen = []

    def __call__(self, request):
        self.seen.append(request)
        handler = self.handlers.get(type(request).__name__)
        if handler is None:
            raise AssertionError(f"Unexpected request: {type(request).__name__}")
        if isinstance(handler, Exception):
            raise handler
        return handler(request)


def _doc_message(doc_id=77, size=3000):
    from telethon.tl.types import Document, DocumentAttributeFilename, Message, MessageMediaDocument, PeerChannel

    doc = Document(
        id=doc_id, access_hash=888, file_reference=b"ref",
        date=None, mime_type="application/octet-stream", size=size,
        thumbs=None, video_thumbs=None, dc_id=2,
        attributes=[DocumentAttributeFilename("a.bin")],
    )
    return SimpleNamespace(messages=[
        Message(id=9, peer_id=PeerChannel(-1001), date=None, message="",
                media=MessageMediaDocument(document=doc))
    ])


class TestTelethonFileGateway:
    def test_small_upload_parts_and_md5(self, tmp_path):
        saved = []

        def save_part(request):
            saved.append((request.file_part, request.bytes))
            return True

        gateway = TelethonFileGateway(invoke=ScriptedInvoke({"SaveFilePartRequest": save_part}))
        source = tmp_path / "a.bin"
        data = bytes(range(256)) * 10  # 2560 bytes
        source.write_bytes(data)
        progress = []
        uploaded = gateway.upload(source, progress=progress.append, part_size=1000)
        assert uploaded.parts == 3 and uploaded.is_big is False
        assert [p for p, _ in saved] == [0, 1, 2]
        assert b"".join(b for _, b in saved) == data
        import hashlib
        assert uploaded.md5 == hashlib.md5(data).hexdigest()
        assert progress[-1] == len(data)

    def test_big_file_uses_big_parts(self, tmp_path, monkeypatch):
        import teloude.infrastructure.telegram.files as files_mod

        monkeypatch.setattr(files_mod, "BIG_FILE_THRESHOLD", 100)
        saved = []

        def save_big(request):
            saved.append((request.file_part, request.file_total_parts))
            return True

        gateway = TelethonFileGateway(invoke=ScriptedInvoke({"SaveBigFilePartRequest": save_big}))
        source = tmp_path / "big.bin"
        source.write_bytes(b"z" * 250)
        uploaded = gateway.upload(source, part_size=100)
        assert uploaded.is_big is True and uploaded.md5 == ""
        assert saved == [(0, 3), (1, 3), (2, 3)]

    def test_pause_between_parts(self, tmp_path):
        gateway = TelethonFileGateway(
            invoke=ScriptedInvoke({"SaveFilePartRequest": lambda r: True})
        )
        source = tmp_path / "a.bin"
        source.write_bytes(b"x" * 3000)
        calls = {"n": 0}

        def pause_once():
            calls["n"] += 1
            return calls["n"] > 1  # allow first part, pause before second

        with pytest.raises(UploadPaused) as exc_info:
            gateway.upload(source, part_size=1000, should_pause=pause_once)
        assert exc_info.value.done_bytes == 1000

    def test_send_to_topic_parses_msg_id(self, tmp_path):
        sent_requests = []

        def send_media(request):
            sent_requests.append(request)
            return canned_updates(messages=[canned_message_update(4242)])

        gateway = TelethonFileGateway(invoke=ScriptedInvoke({"SendMediaRequest": send_media}))
        from teloude.infrastructure.telegram.files import UploadedFile
        handle = UploadedFile(file_id=1, parts=1, name="a.bin", size=3, md5="d41d", is_big=False)
        sent = gateway.send_to_topic(-1001, 7, handle, caption="backup of a.bin")
        assert (sent.chat_id, sent.msg_id, sent.topic_id) == (-1001, 4242, 7)
        req = sent_requests[0]
        assert req.reply_to.reply_to_msg_id == 7
        assert req.media.attributes[0].file_name == "a.bin"

    def test_download_streams_to_offset(self, tmp_path):
        blob = bytes(range(256)) * 20  # 5120 bytes

        def get_file(request):
            start = request.offset
            return SimpleNamespace(bytes=blob[start: start + request.limit])

        gateway = TelethonFileGateway(invoke=ScriptedInvoke({
            "GetMessagesRequest": lambda r: _doc_message(size=len(blob)),
            "GetFileRequest": get_file,
        }))
        from teloude.infrastructure.telegram.files import DocumentRef
        doc = gateway.resolve_document(-1001, 9)
        assert (doc.file_name, doc.size) == ("a.bin", len(blob))
        dest = tmp_path / "out.bin"
        written = gateway.download(doc, dest)
        assert written == len(blob) and dest.read_bytes() == blob

    def test_download_resume_from_offset(self, tmp_path):
        blob = b"0123456789"
        calls = []

        def get_file(request):
            calls.append(request.offset)
            return SimpleNamespace(bytes=blob[request.offset: request.offset + 4])

        gateway = TelethonFileGateway(invoke=ScriptedInvoke({"GetFileRequest": get_file}))
        from teloude.infrastructure.telegram.files import DocumentRef
        doc = DocumentRef(doc_id=1, access_hash=1, file_reference=b"r",
                          size=len(blob), file_name="n", mime="m")
        dest = tmp_path / "out.bin"
        dest.write_bytes(b"0123")
        written = gateway.download(doc, dest, offset=4)
        assert written == 6 and dest.read_bytes() == blob
        assert calls[0] == 4

    def test_max_upload_bytes_by_tier(self):
        assert TelethonFileGateway(invoke=MagicMock()).max_upload_bytes() == 2 * 1024 ** 3
        premium = TelethonFileGateway(
            invoke=MagicMock(), get_me=lambda: SimpleNamespace(premium=True)
        )
        assert premium.max_upload_bytes() == 4 * 1024 ** 3
