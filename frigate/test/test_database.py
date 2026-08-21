import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest import TestCase

from frigate.util.database import create_database_backup


class TestDatabaseUtils(TestCase):
    def test_backup_includes_committed_wal_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "frigate.db"
            raw_copy = root / "raw-copy.db"
            backup = root / "backup.db"

            connection = sqlite3.connect(source)
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA wal_autocheckpoint = 0")
            connection.execute(
                "CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT)"
            )
            connection.execute("INSERT INTO marker VALUES (1, 'main-file')")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("INSERT INTO marker VALUES (2, 'wal-only')")
            connection.commit()

            shutil.copyfile(source, raw_copy)
            with sqlite3.connect(raw_copy) as raw_connection:
                self.assertEqual(
                    [(1, "main-file")],
                    raw_connection.execute("SELECT * FROM marker").fetchall(),
                )

            create_database_backup(source, backup)
            with sqlite3.connect(backup) as backup_connection:
                self.assertEqual(
                    "ok", backup_connection.execute("PRAGMA quick_check").fetchone()[0]
                )
                self.assertEqual(
                    [(1, "main-file"), (2, "wal-only")],
                    backup_connection.execute(
                        "SELECT * FROM marker ORDER BY id"
                    ).fetchall(),
                )

            connection.close()

    def test_backup_is_consistent_during_active_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "frigate.db"
            backup = root / "backup.db"
            seed = sqlite3.connect(source)
            seed.execute("PRAGMA journal_mode = WAL")
            seed.execute("PRAGMA wal_autocheckpoint = 0")
            seed.execute(
                "CREATE TABLE writes (sequence INTEGER PRIMARY KEY, payload BLOB)"
            )
            seed.executemany(
                "INSERT INTO writes VALUES (?, zeroblob(65536))",
                ((sequence,) for sequence in range(1, 257)),
            )
            seed.commit()
            seed.close()

            stop = threading.Event()
            started = threading.Event()
            commit_times: list[float] = []

            def writer() -> None:
                connection = sqlite3.connect(source, timeout=10)
                sequence = 257
                try:
                    while not stop.is_set():
                        connection.execute(
                            "INSERT INTO writes VALUES (?, zeroblob(1024))",
                            (sequence,),
                        )
                        connection.commit()
                        commit_times.append(time.monotonic())
                        sequence += 1
                        started.set()
                finally:
                    connection.close()

            thread = threading.Thread(target=writer, daemon=True)
            thread.start()
            try:
                self.assertTrue(started.wait(timeout=5))
                backup_started = time.monotonic()
                create_database_backup(source, backup)
                backup_finished = time.monotonic()
            finally:
                stop.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

            self.assertTrue(
                any(
                    backup_started <= timestamp <= backup_finished
                    for timestamp in commit_times
                ),
                "writer did not commit while the online backup was running",
            )
            with sqlite3.connect(backup) as backup_connection:
                self.assertEqual(
                    "ok", backup_connection.execute("PRAGMA quick_check").fetchone()[0]
                )
                count, minimum, maximum = backup_connection.execute(
                    "SELECT COUNT(*), MIN(sequence), MAX(sequence) FROM writes"
                ).fetchone()
                self.assertEqual(1, minimum)
                self.assertEqual(count, maximum)

    def test_failed_backup_preserves_previous_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing = root / "missing.db"
            backup = root / "backup.db"
            backup.write_bytes(b"previous-good-backup")

            with self.assertRaises(sqlite3.OperationalError):
                create_database_backup(missing, backup)

            self.assertEqual(b"previous-good-backup", backup.read_bytes())
            self.assertFalse((root / ".backup.db.tmp").exists())
