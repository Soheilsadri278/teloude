# teloude/tests/test_database_concurrency.py
"""Thread-safety of the shared SQLite connection.

The backup/restore engines write from worker threads while the UI keeps reading
and writing through the same connection. These tests pin the serialization that
makes that safe.
"""
import sqlite3
import threading
import time

import pytest

from teloude.config import AppConfig
from teloude.infrastructure.database import DatabaseManager, close_db_connection


@pytest.fixture()
def db(tmp_path):
    manager = DatabaseManager(AppConfig(database_path=str(tmp_path / "conc.db")))
    assert manager.initialize()
    manager.execute_query(
        "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, body TEXT)"
    )
    yield manager
    close_db_connection(manager)


def _rows_from_a_second_connection(path: str):
    """Reads through an independent connection, like another process would."""
    other = sqlite3.connect(path, timeout=5)
    try:
        return [row[0] for row in other.execute("SELECT body FROM notes ORDER BY id")]
    finally:
        other.close()


def test_another_threads_write_waits_for_the_open_transaction(db, tmp_path):
    """A write must not join (and commit) a transaction another thread started."""
    in_transaction = threading.Event()
    transaction_finished = threading.Event()
    observed_inside = []
    write_finished = []

    def writer_with_transaction():
        with db.transaction() as cursor:
            cursor.execute("INSERT INTO notes (body) VALUES ('first')")
            in_transaction.set()
            time.sleep(0.4)  # the "engine" is busy inside its transaction
            observed_inside.append(
                _rows_from_a_second_connection(db._db_path)
            )
            cursor.execute("INSERT INTO notes (body) VALUES ('second')")
        transaction_finished.set()

    def writer_from_other_thread():
        assert in_transaction.wait(5)
        started = time.monotonic()
        db.execute_query("INSERT INTO notes (body) VALUES ('other-thread')")
        write_finished.append(time.monotonic() - started)

    t1 = threading.Thread(target=writer_with_transaction, name="engine")
    t2 = threading.Thread(target=writer_from_other_thread, name="ui")
    t1.start()
    t2.start()
    t1.join(10)
    t2.join(10)
    assert not t1.is_alive() and not t2.is_alive()

    # The other thread waited for the transaction instead of committing into it.
    assert write_finished[0] >= 0.3, "the write joined a transaction in flight"
    # And the transaction never became visible half-written.
    assert observed_inside == [[]], observed_inside
    assert _rows_from_a_second_connection(db._db_path) == [
        "first", "second", "other-thread",
    ]


def test_concurrent_writers_do_not_interleave_or_fail(db, tmp_path):
    errors = []
    workers, per_worker = 6, 40

    def work(index: int):
        try:
            for i in range(per_worker):
                db.execute_query(
                    "INSERT INTO notes (body) VALUES (?)", (f"w{index}-{i}",),
                )
                db.execute_query("SELECT COUNT(*) FROM notes", fetch=True)
                with db.transaction() as cursor:
                    cursor.execute(
                        "INSERT INTO notes (body) VALUES (?)", (f"t{index}-{i}",),
                    )
        except Exception as exc:  # noqa: BLE001 - the failure itself is the assertion
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
        assert not thread.is_alive()

    assert errors == []
    total = db.execute_query("SELECT COUNT(*) FROM notes", fetch=True)[0][0]
    assert total == workers * per_worker * 2


def test_close_from_another_thread_is_not_a_programming_error(db):
    """Closing must be clean; later calls report Teloude's own error."""
    db.execute_query("INSERT INTO notes (body) VALUES ('before-close')")
    closer = threading.Thread(target=close_db_connection, args=(db,))
    closer.start()
    closer.join(10)
    with pytest.raises(ConnectionError):
        db.execute_query("SELECT 1", fetch=True)
    # closing twice is a no-op, not a crash
    close_db_connection(db)
