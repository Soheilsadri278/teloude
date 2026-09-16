# teloude/tests/test_root_folder_topics.py
"""Bug 4 regression tests: one topic per backup root folder, never a shared one.

The Windows acceptance test backed Folder A into a storage and then Folder B
into the *same* storage: Folder B's files silently landed in Folder A's topic,
because the storage's "root folder" row (and therefore its topic) was keyed
only by the storage, and every file was indexed relative to the folder the user
picked. The ten scenarios below are the acceptance list from that report, plus
the two rules the architecture adds around them:

* one Storage = one Supergroup (never one Supergroup per folder),
* every *root folder* backed up into that Storage owns its own forum topic,
  created on its first backup, reused by later backups of the same folder,
  and never shared with another root folder.

Telegram itself is not reachable from the test suite, so these scenarios run on
the in-memory fakes and assert the exact topic every document was posted into.
"""
import pytest

from teloude.config import AppConfig
from teloude.core.backup import BackupManager, backup_root_name
from teloude.core.transfers import TransferRegistry
from teloude.infrastructure.database import DatabaseManager, close_db_connection
from teloude.infrastructure.repositories import (
    FileRepository,
    FolderRepository,
    StorageRepository,
    TransferRepository,
)
from teloude.infrastructure.telegram.fakes import FakeFileGateway, FakeStorageGateway


@pytest.fixture()
def env(tmp_path):
    manager = DatabaseManager(AppConfig(database_path=str(tmp_path / "topics.db")))
    assert manager.initialize()
    storages = StorageRepository(manager)
    folders = FolderRepository(manager)
    files = FileRepository(manager)
    storage_gw, file_gw = FakeStorageGateway(), FakeFileGateway()
    backup = BackupManager(
        storages, folders, files, TransferRegistry(TransferRepository(manager)),
        storage_gw, file_gw, max_retries=1, retry_sleeper=lambda _s: None,
    )
    env = {
        "manager": manager, "storages": storages, "folders": folders,
        "files": files, "backup": backup, "storage_gw": storage_gw,
        "file_gw": file_gw, "tmp": tmp_path,
    }
    yield env
    close_db_connection(manager)
    close_db_connection(env.get("reopened"))


def _folder_with(tmp_path, name, *files):
    """Creates a source folder containing ``files`` (name -> payload)."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    created = []
    for relative, payload in files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        created.append(relative)
    return root


def _topic_titles(env, chat_id):
    return {t.title: t.topic_id for t in env["storage_gw"].list_topics(chat_id)}


def _topic_of_file(env, record):
    """The forum topic a backed-up file was actually posted into."""
    return env["file_gw"].topic_of(record.telegram_chat_id, record.telegram_msg_id)


class TestRootFolderTopicMapping:
    def test_a_storage_creates_exactly_one_supergroup(self, env, tmp_path):
        from teloude.application.services import EventBus, StorageService

        service = StorageService(
            env["storages"], env["folders"], env["files"], env["storage_gw"],
            EventBus(),
        )
        first = service.create_storage("Family")
        second = service.create_storage("Work")
        assert first.telegram_chat_id != second.telegram_chat_id
        assert env["storage_gw"].find_storage("Family").chat_id == first.telegram_chat_id
        assert env["storage_gw"].find_storage("Work").chat_id == second.telegram_chat_id
        # one supergroup per storage, each holding its own root topic
        assert len(env["storage_gw"].list_topics(first.telegram_chat_id)) == 1
        assert len(env["storage_gw"].list_topics(second.telegram_chat_id)) == 1

        # two root folders inside ONE storage never produce a second supergroup
        folder = _folder_with(tmp_path, "FolderA", ("a.txt", b"a"))
        env["backup"].run(env["backup"].plan(first.id, folder))
        assert env["storage_gw"].find_storage("Family").chat_id == first.telegram_chat_id
        assert len(env["storages"].list_all()) == 2  # no storage was created

    def test_first_backup_of_a_root_folder_creates_its_own_topic(self, env, tmp_path):
        sid = _linked_storage(env, "Photos")
        chat_id = env["storages"].get(sid).telegram_chat_id
        before = set(_topic_titles(env, chat_id))
        folder_a = _folder_with(tmp_path, "FolderA", ("a1.txt", b"a1"))
        report = env["backup"].run(env["backup"].plan(sid, folder_a))
        assert report.uploaded == 1 and report.failed == []
        after = _topic_titles(env, chat_id)
        assert "Photos / FolderA" in after
        assert after["Photos / FolderA"] != after["Photos"]  # its own thread
        assert set(after) - before == {"Photos", "Photos / FolderA"}

    def test_folder_b_into_the_same_storage_gets_its_own_topic(self, env, tmp_path):
        sid = _linked_storage(env, "Photos")
        chat_id = env["storages"].get(sid).telegram_chat_id
        folder_a = _folder_with(tmp_path, "FolderA", ("a1.txt", b"a1"))
        folder_b = _folder_with(tmp_path, "FolderB", ("b1.txt", b"b1"))
        env["backup"].run(env["backup"].plan(sid, folder_a))
        env["backup"].run(env["backup"].plan(sid, folder_b))
        titles = _topic_titles(env, chat_id)
        assert "Photos / FolderA" in titles
        assert "Photos / FolderB" in titles

    def test_topic_a_and_topic_b_have_different_thread_ids(self, env, tmp_path):
        sid = _linked_storage(env, "Photos")
        chat_id = env["storages"].get(sid).telegram_chat_id
        folder_a = _folder_with(tmp_path, "FolderA", ("a1.txt", b"a1"))
        folder_b = _folder_with(tmp_path, "FolderB", ("b1.txt", b"b1"))
        env["backup"].run(env["backup"].plan(sid, folder_a))
        env["backup"].run(env["backup"].plan(sid, folder_b))
        titles = _topic_titles(env, chat_id)
        assert titles["Photos / FolderA"] != titles["Photos / FolderB"]

    def test_files_of_a_land_in_topic_a(self, env, tmp_path):
        sid = _linked_storage(env, "Photos")
        folder_a = _folder_with(tmp_path, "FolderA", ("a1.txt", b"a1"),
                                ("nested/a2.txt", b"a2"))
        env["backup"].run(env["backup"].plan(sid, folder_a))
        chat_id = env["storages"].get(sid).telegram_chat_id
        topic_a = _topic_titles(env, chat_id)["Photos / FolderA"]
        topic_nested = _topic_titles(env, chat_id)["Photos / FolderA / nested"]
        rows = {r.relative_path: r for r in env["files"].list_by_storage(sid)}
        assert _topic_of_file(env, rows["FolderA/a1.txt"]) == topic_a
        assert _topic_of_file(env, rows["FolderA/nested/a2.txt"]) == topic_nested
        # the root files never fall back to the storage's own root topic
        assert _topic_of_file(env, rows["FolderA/a1.txt"]) != 1

    def test_files_of_b_land_in_topic_b(self, env, tmp_path):
        sid = _linked_storage(env, "Photos")
        parent = tmp_path / "sources"
        parent.mkdir()
        folder_a = _folder_with(parent, "FolderA", ("shared.txt", b"from A"))
        folder_b = _folder_with(parent, "FolderB", ("shared.txt", b"from B"))
        env["backup"].run(env["backup"].plan(sid, folder_a))
        env["backup"].run(env["backup"].plan(sid, folder_b))
        chat_id = env["storages"].get(sid).telegram_chat_id
        titles = _topic_titles(env, chat_id)
        rows = {r.relative_path: r for r in env["files"].list_by_storage(sid)}
        # same file name in two root folders: two index entries, two topics
        assert set(rows) == {"FolderA/shared.txt", "FolderB/shared.txt"}
        assert _topic_of_file(env, rows["FolderA/shared.txt"]) == titles["Photos / FolderA"]
        assert _topic_of_file(env, rows["FolderB/shared.txt"]) == titles["Photos / FolderB"]
        assert titles["Photos / FolderA"] != titles["Photos / FolderB"]

    def test_second_backup_of_a_reuses_its_topic(self, env, tmp_path):
        sid = _linked_storage(env, "Photos")
        chat_id = env["storages"].get(sid).telegram_chat_id
        folder_a = _folder_with(tmp_path, "FolderA", ("a1.txt", b"a1"))
        env["backup"].run(env["backup"].plan(sid, folder_a))
        first_topics = _topic_titles(env, chat_id)
        (folder_a / "a2.txt").write_bytes(b"a2")
        env["backup"].run(env["backup"].plan(sid, folder_a))
        assert _topic_titles(env, chat_id) == first_topics  # no new topic
        rows = {r.relative_path: r for r in env["files"].list_by_storage(sid)}
        assert _topic_of_file(env, rows["FolderA/a1.txt"]) == first_topics["Photos / FolderA"]
        assert _topic_of_file(env, rows["FolderA/a2.txt"]) == first_topics["Photos / FolderA"]

    def test_a_later_backup_of_b_never_reuses_the_last_used_topic(self, env, tmp_path):
        """Regression: B used to be uploaded into whatever topic A used last."""
        sid = _linked_storage(env, "Photos")
        chat_id = env["storages"].get(sid).telegram_chat_id
        parent = tmp_path / "sources"
        parent.mkdir()
        folder_a = _folder_with(parent, "FolderA", ("a1.txt", b"a1"))
        folder_b = _folder_with(parent, "FolderB", ("b1.txt", b"b1"))

        env["backup"].run(env["backup"].plan(sid, folder_a))   # A creates its topic
        env["backup"].run(env["backup"].plan(sid, folder_a))   # A again: reused
        env["backup"].run(env["backup"].plan(sid, folder_b))   # B after A
        titles = _topic_titles(env, chat_id)
        rows = {r.relative_path: r for r in env["files"].list_by_storage(sid)}
        topic_a = titles["Photos / FolderA"]
        topic_b = titles["Photos / FolderB"]
        assert topic_a != topic_b
        assert _topic_of_file(env, rows["FolderA/a1.txt"]) == topic_a
        assert _topic_of_file(env, rows["FolderB/b1.txt"]) == topic_b

        # and A again after B: still its own topic, not the most recent one
        (folder_a / "a2.txt").write_bytes(b"a2")
        env["backup"].run(env["backup"].plan(sid, folder_a))
        rows = {r.relative_path: r for r in env["files"].list_by_storage(sid)}
        assert _topic_of_file(env, rows["FolderA/a2.txt"]) == topic_a

    def test_the_same_folder_in_another_storage_uses_that_supergroup(self, env, tmp_path):
        first = _linked_storage(env, "Photos")
        second = _linked_storage(env, "Archive")
        left = _folder_with(tmp_path / "photos-src", "FolderA", ("a1.txt", b"left"))
        right = _folder_with(tmp_path / "archive-src", "FolderA", ("a1.txt", b"right"))
        assert env["backup"].run(env["backup"].plan(first, left)).uploaded == 1
        assert env["backup"].run(env["backup"].plan(second, right)).uploaded == 1

        chat_first = env["storages"].get(first).telegram_chat_id
        chat_second = env["storages"].get(second).telegram_chat_id
        assert chat_first != chat_second
        assert "Photos / FolderA" in _topic_titles(env, chat_first)
        assert "Archive / FolderA" in _topic_titles(env, chat_second)
        # same thread id in two different supergroups stays two different topics
        rows_first = {r.relative_path: r for r in env["files"].list_by_storage(first)}
        rows_second = {r.relative_path: r for r in env["files"].list_by_storage(second)}
        record_first = rows_first["FolderA/a1.txt"]
        record_second = rows_second["FolderA/a1.txt"]
        assert record_first.telegram_chat_id == chat_first
        assert record_second.telegram_chat_id == chat_second

    def test_the_mapping_survives_a_restart(self, env, tmp_path):
        """Reopening the database must not lose or re-create any topic."""
        sid = _linked_storage(env, "Photos")
        chat_id = env["storages"].get(sid).telegram_chat_id
        parent = tmp_path / "sources"
        parent.mkdir()
        folder_a = _folder_with(parent, "FolderA", ("a1.txt", b"a1"))
        folder_b = _folder_with(parent, "FolderB", ("b1.txt", b"b1"))
        env["backup"].run(env["backup"].plan(sid, folder_a))
        env["backup"].run(env["backup"].plan(sid, folder_b))
        topics_before = _topic_titles(env, chat_id)

        # new process: a fresh connection, fresh repositories, fresh managers -
        # only the database file carries the folder -> topic mapping across
        reopened = DatabaseManager(AppConfig(database_path=str(env["tmp"] / "topics.db")))
        assert reopened.initialize()
        env["reopened"] = reopened
        storages = StorageRepository(reopened)
        folders = FolderRepository(reopened)
        files = FileRepository(reopened)
        storage_gw, file_gw = FakeStorageGateway(), FakeFileGateway()
        storage_gw._storages = env["storage_gw"]._storages  # Telegram keeps its state
        backup = BackupManager(
            storages, folders, files, TransferRegistry(TransferRepository(reopened)),
            storage_gw, file_gw, max_retries=1, retry_sleeper=lambda _s: None,
        )
        created = []
        real_ensure_topic = storage_gw.ensure_topic

        def spying_ensure_topic(chat, title, is_root=False):
            known = {t.title for t in storage_gw.list_topics(chat)}
            topic = real_ensure_topic(chat, title, is_root=is_root)
            if title not in known:
                created.append(title)
            return topic

        storage_gw.ensure_topic = spying_ensure_topic

        # the persisted mapping alone decides where files go: no topic is
        # created again and nothing falls back to the storage root topic
        (folder_a / "a2.txt").write_bytes(b"a2")
        (folder_b / "b2.txt").write_bytes(b"b2")
        assert backup.run(backup.plan(sid, folder_a)).uploaded == 1
        assert backup.run(backup.plan(sid, folder_b)).uploaded == 1
        assert created == [], f"topics were re-created after a restart: {created}"
        assert _topic_titles(env, chat_id) == topics_before
        rows = {r.relative_path: r for r in files.list_by_storage(sid)}
        assert file_gw.topic_of(chat_id, rows["FolderA/a2.txt"].telegram_msg_id) == \
            topics_before["Photos / FolderA"]
        assert file_gw.topic_of(chat_id, rows["FolderB/b2.txt"].telegram_msg_id) == \
            topics_before["Photos / FolderB"]


class TestRootFolderNames:
    def test_the_topic_is_named_after_the_root_folder(self, env, tmp_path):
        sid = _linked_storage(env, "Photos")
        chat_id = env["storages"].get(sid).telegram_chat_id
        folder = _folder_with(tmp_path, "Holiday 2026", ("a.txt", b"a"))
        env["backup"].run(env["backup"].plan(sid, folder))
        titles = set(_topic_titles(env, chat_id))
        assert "Photos / Holiday 2026" in titles

    def test_two_folders_sharing_a_name_stay_apart_by_storage(self, env, tmp_path):
        photos = _linked_storage(env, "Photos")
        archive = _linked_storage(env, "Archive")
        left = _folder_with(tmp_path / "left", "Trip", ("a.txt", b"left"))
        right = _folder_with(tmp_path / "right", "Trip", ("a.txt", b"right"))
        assert env["backup"].run(env["backup"].plan(photos, left)).uploaded == 1
        assert env["backup"].run(env["backup"].plan(archive, right)).uploaded == 1
        assert "Photos / Trip" in _topic_titles(env, env["storages"].get(photos).telegram_chat_id)
        assert "Archive / Trip" in _topic_titles(env, env["storages"].get(archive).telegram_chat_id)

    def test_a_drive_or_filesystem_root_still_gets_a_stable_name(self, tmp_path):
        # Windows writes "D:\\", POSIX "/": neither has a folder name of its own,
        # so the anchor must still be non-empty and stable across runs.
        assert backup_root_name(tmp_path / "x") == "x"
        anchor = backup_root_name(tmp_path.anchor or "/")
        assert anchor and anchor == backup_root_name(tmp_path.anchor or "/")
        assert backup_root_name(tmp_path) == tmp_path.name


def _linked_storage(env, name):
    info = env["storage_gw"].create_storage(name)
    sid = env["storages"].create(name)
    env["storages"].set_telegram(sid, info.chat_id, True)
    return sid
