"""Preservation and query-plan tests for migration 036."""

from __future__ import annotations

import importlib.util
import sqlite3
import unittest
from pathlib import Path

MIGRATION_PATH = Path(__file__).parents[2] / "migrations" / "036_add_query_indexes.py"
SPEC = importlib.util.spec_from_file_location("migration_036", MIGRATION_PATH)
assert SPEC and SPEC.loader
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class Migrator:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.statements: list[str] = []

    def sql(self, statement: str) -> None:
        self.statements.append(statement)
        self.connection.execute(statement)


class QueryIndexMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.database = sqlite3.connect(":memory:")
        self.database.executescript(
            """
            CREATE TABLE event (
                id TEXT PRIMARY KEY, camera TEXT NOT NULL, label TEXT NOT NULL,
                start_time REAL NOT NULL, end_time REAL, data TEXT NOT NULL
            );
            CREATE INDEX event_start_time_end_time
                ON event(start_time DESC, end_time DESC);
            CREATE TABLE reviewsegment (
                id TEXT PRIMARY KEY, camera TEXT NOT NULL, start_time REAL NOT NULL,
                end_time REAL, severity TEXT NOT NULL, data TEXT NOT NULL
            );
            CREATE TABLE timeline (
                timestamp REAL NOT NULL, camera TEXT NOT NULL, source TEXT NOT NULL,
                source_id TEXT, class_type TEXT NOT NULL, data TEXT
            );
            CREATE TABLE previews (
                id TEXT PRIMARY KEY, camera TEXT NOT NULL, path TEXT NOT NULL,
                start_time REAL NOT NULL, end_time REAL NOT NULL, duration REAL NOT NULL
            );
            CREATE TABLE recordings (
                id TEXT PRIMARY KEY, camera TEXT NOT NULL, start_time REAL NOT NULL,
                end_time REAL NOT NULL, duration REAL NOT NULL
            );
            CREATE TABLE userreviewstatus (
                id INTEGER PRIMARY KEY, user_id TEXT NOT NULL,
                review_segment_id TEXT NOT NULL, has_been_reviewed INTEGER NOT NULL
            );
            """
        )
        for camera_index in range(8):
            camera = f"camera_{camera_index:02d}"
            for item in range(120):
                start = 1_700_000_000 + item * 10 + camera_index
                event_id = f"event-{camera_index}-{item}"
                self.database.execute(
                    "INSERT INTO event VALUES (?, ?, 'person', ?, ?, '{}')",
                    (event_id, camera, start, start + 5),
                )
                self.database.execute(
                    "INSERT INTO timeline VALUES (?, ?, 'tracked_object', ?, 'visible', '{}')",
                    (start, camera, event_id),
                )
                self.database.execute(
                    "INSERT INTO recordings VALUES (?, ?, ?, ?, 10)",
                    (f"recording-{camera_index}-{item}", camera, start, start + 10),
                )
            for item in range(40):
                start = 1_700_000_000 + item * 60 + camera_index
                self.database.execute(
                    "INSERT INTO reviewsegment VALUES (?, ?, ?, ?, ?, '{}')",
                    (
                        f"review-{camera_index}-{item}",
                        camera,
                        start,
                        start + 30,
                        "alert" if item % 3 == 0 else "detection",
                    ),
                )
                self.database.execute(
                    "INSERT INTO previews VALUES (?, ?, ?, ?, ?, 60)",
                    (
                        f"preview-{camera_index}-{item}",
                        camera,
                        f"/{item}.mp4",
                        start,
                        start + 60,
                    ),
                )
        self.database.execute(
            "INSERT INTO userreviewstatus VALUES (1, 'admin', 'review-0-0', 1)"
        )
        self.database.commit()
        self.before_counts = self.counts()

    def tearDown(self) -> None:
        self.database.close()

    def counts(self) -> dict[str, int]:
        return {
            table: self.database.execute(f'SELECT count(*) FROM "{table}"').fetchone()[
                0
            ]
            for table in MIGRATION.ANALYZE_TABLES
        }

    def plan(self, sql: str, parameters: tuple = ()) -> list[str]:
        return [
            row[-1]
            for row in self.database.execute(f"EXPLAIN QUERY PLAN {sql}", parameters)
        ]

    def test_migration_is_idempotent_and_preserves_rows(self) -> None:
        migrator = Migrator(self.database)
        MIGRATION.migrate(migrator, self.database)
        MIGRATION.migrate(migrator, self.database)

        self.assertEqual(self.before_counts, self.counts())
        names = {
            row[0]
            for row in self.database.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'index'"
            )
        }
        self.assertTrue(set(MIGRATION.INDEXES).issubset(names))
        analyzed = {
            row[0] for row in self.database.execute("SELECT tbl FROM sqlite_stat1")
        }
        self.assertTrue(set(MIGRATION.ANALYZE_TABLES).issubset(analyzed))
        self.assertFalse(
            any(
                token in statement.upper()
                for statement in migrator.statements
                for token in ("DELETE ", "UPDATE ", "VACUUM")
            )
        )

    def test_critical_single_camera_paths_use_ordered_indexes(self) -> None:
        MIGRATION.migrate(Migrator(self.database), self.database)
        cases = (
            (
                "SELECT * FROM event WHERE camera = ? ORDER BY start_time DESC LIMIT 100",
                "event_camera_start_time",
            ),
            (
                "SELECT * FROM reviewsegment WHERE camera = ? ORDER BY start_time DESC LIMIT 100",
                "review_segment_camera_start_time_end_time",
            ),
            (
                "SELECT * FROM timeline WHERE camera = ? ORDER BY timestamp DESC LIMIT 100",
                "timeline_camera_timestamp",
            ),
            (
                "SELECT * FROM previews WHERE camera = ? ORDER BY start_time DESC LIMIT 100",
                "previews_camera_start_time_end_time",
            ),
        )
        for sql, expected_index in cases:
            with self.subTest(expected_index=expected_index):
                plan = self.plan(sql, ("camera_03",))
                self.assertTrue(any(expected_index in row for row in plan), plan)
                self.assertFalse(any("TEMP B-TREE" in row for row in plan), plan)

    def test_rollback_removes_only_owned_indexes(self) -> None:
        migrator = Migrator(self.database)
        MIGRATION.migrate(migrator, self.database)
        MIGRATION.rollback(migrator, self.database)
        self.assertEqual(self.before_counts, self.counts())
        names = {
            row[0]
            for row in self.database.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'index'"
            )
        }
        self.assertTrue(set(MIGRATION.INDEXES).isdisjoint(names))
        self.assertIn("event_start_time_end_time", names)


if __name__ == "__main__":
    unittest.main()
