# teloude/tests/test_telethon_contract.py
"""Pins the Telethon surface Teloude depends on.

The gateways speak raw MTProto request objects, so a Telethon upgrade that
renames a field or changes a request signature would only show up at runtime,
against a real account. These tests fail at test time instead, against whatever
Telethon version is installed.
"""
import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

telethon = pytest.importorskip("telethon")

from telethon import utils  # noqa: E402
from telethon.tl.functions.channels import GetMessagesRequest  # noqa: E402
from telethon.tl.functions.messages import GetHistoryRequest, SendMediaRequest  # noqa: E402
from telethon.tl.functions.upload import (  # noqa: E402
    GetFileRequest,
    SaveBigFilePartRequest,
    SaveFilePartRequest,
)
from telethon.tl.types import (  # noqa: E402
    DocumentAttributeFilename,
    InputDocumentFileLocation,
    InputFile,
    InputFileBig,
    InputMediaUploadedDocument,
    InputMessageID,
    InputReplyToMessage,
)

from teloude.infrastructure.telegram.bridge import extract_message_id, map_rpc_error  # noqa: E402


def _media(uploaded_is_big: bool):
    tl_file = (
        InputFileBig(1234, 3, "backup.bin")
        if uploaded_is_big
        else InputFile(1234, 3, "backup.bin", "d41d8cd98f00b204e9800998ecf8427e")
    )
    return InputMediaUploadedDocument(
        file=tl_file,
        mime_type="application/octet-stream",
        attributes=[DocumentAttributeFilename("backup.bin")],
        force_file=True,
    )


# Requests whose *fields* the gateways set by name. Constructing and
# serializing them is what a live run does; a renamed field fails here instead.
REQUEST_CALLS = [
    ("save small part", SaveFilePartRequest, dict(file_id=1, file_part=0, bytes=b"x")),
    ("save big part", SaveBigFilePartRequest, dict(
        file_id=1, file_part=0, file_total_parts=1, bytes=b"x",
    )),
    ("post document", SendMediaRequest, dict(
        peer=1, media=_media(False), message="caption", random_id=1,
        reply_to=InputReplyToMessage(reply_to_msg_id=2),
    )),
    ("resolve document", GetMessagesRequest, dict(channel=1, id=[InputMessageID(id=9)])),
    ("download chunk", GetFileRequest, dict(
        location=InputDocumentFileLocation(
            id=1, access_hash=2, file_reference=b"fr", thumb_size="",
        ),
        offset=0, limit=512 * 1024,
    )),
    ("read topic history", GetHistoryRequest, dict(
        peer=1, offset_id=0, offset_date=None, add_offset=0,
        limit=100, max_id=0, min_id=0, hash=0,
    )),
]


@pytest.mark.parametrize("label,request_cls,kwargs", REQUEST_CALLS,
                         ids=[c[0] for c in REQUEST_CALLS])
def test_request_fields_serialize(label, request_cls, kwargs):
    request = request_cls(**kwargs)
    assert request.to_dict() is not None, label


GATEWAY_MODULES = [
    "storage.py", "files.py", "auth.py", "session_manager.py",
    "telethon_client.py", "bridge.py",
]
TELEGRAM_DIR = Path(__file__).resolve().parents[1] / "infrastructure" / "telegram"


def _telethon_imports(path: Path):
    """Every Telethon name the module imports, wherever it happens."""
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("telethon"):
            found.extend((node.module, alias.name) for alias in node.names)
        elif isinstance(node, ast.Import):
            found.extend((alias.name, None) for alias in node.names
                         if alias.name.startswith("telethon"))
    return found


@pytest.mark.parametrize("filename", GATEWAY_MODULES)
def test_every_telethon_import_in_the_gateways_resolves(filename):
    """A Telethon upgrade that renames anything we import fails here, not live."""
    imports = _telethon_imports(TELEGRAM_DIR / filename)
    for module_name, attribute in imports:
        module = importlib.import_module(module_name)
        if attribute is not None:
            assert hasattr(module, attribute), f"{module_name}.{attribute} is gone"


def test_message_id_extraction_handles_real_update_types():
    from telethon.tl.types import UpdateNewChannelMessage, UpdateNewMessage

    class _Message:
        id = 4242

    updates = type("Updates", (), {"updates": [UpdateNewChannelMessage(
        message=_Message(), pts=1, pts_count=1,
    )]})()
    assert extract_message_id(updates) == 4242
    assert extract_message_id(
        type("Updates", (), {"updates": [UpdateNewMessage(
            message=_Message(), pts=1, pts_count=1,
        )]})()
    ) == 4242
    assert extract_message_id(type("Updates", (), {"updates": []})()) is None


def test_bridge_maps_real_telethon_errors():
    from telethon.errors import FloodWaitError

    from teloude.infrastructure.telegram.exceptions import (
        ConnectionStateError,
        RateLimitExceeded,
        TeloudeTelegramError,
    )

    flooded = map_rpc_error(FloodWaitError(request=None, capture=42))
    assert isinstance(flooded, RateLimitExceeded)
    assert "42" in str(flooded) or flooded.retry_after == 42  # wait time reaches the UI

    offline = map_rpc_error(ConnectionError("no route to host"), "uploading")
    assert isinstance(offline, ConnectionStateError)
    assert "retry automatically" in str(offline)

    unmapped = map_rpc_error(ValueError("weird"), "doing something")
    assert isinstance(unmapped, TeloudeTelegramError)
    assert not isinstance(unmapped, (RateLimitExceeded, ConnectionStateError))


def test_auth_has_a_friendly_message_for_every_handled_error():
    """Every genuine auth failure must have user-facing text."""
    from teloude.infrastructure.telegram.auth import _friendly_message, _telethon_auth_errors

    classes = _telethon_auth_errors()
    assert classes, "Telethon error classes did not resolve"
    for error_cls in classes:
        if error_cls.__name__ == "SessionPasswordNeededError":
            continue  # not a failure: this is the "ask for the 2FA password" branch
        message = _friendly_message(error_cls.__name__, "signing in")
        assert message != "Could not complete signing in.", error_cls.__name__
        assert error_cls.__name__ not in message  # no raw class names to users


def test_two_factor_prompt_is_a_branch_not_a_failure():
    """The 2FA prompt must be recognised, or sign-in would look like an error."""
    from telethon.errors import SessionPasswordNeededError

    from teloude.infrastructure.telegram.auth import _is_password_needed

    assert _is_password_needed(SessionPasswordNeededError(request=None)) is True
    assert _is_password_needed(ValueError("nope")) is False


def test_part_size_helper_is_available():
    for size in (0, 1, 5 * 1024 * 1024, 700 * 1024 * 1024, 3 * 1024 ** 3):
        part = int(utils.get_appropriated_part_size(size)) * 1024
        assert part >= 64 * 1024  # Telegram's floor for small files
        assert part <= 1024 * 1024


def test_telegram_package_imports_without_telethon():
    """Importing the Telegram infrastructure must not require Telethon."""
    code = (
        "import sys; sys.modules['telethon'] = None;"
        "import teloude.infrastructure.telegram as t;"
        "assert 'telethon' not in dir(t); print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
