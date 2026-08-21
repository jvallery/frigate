"""Semantics and bounded-query tests for recording-day summaries."""

from __future__ import annotations

import sqlite3
import unittest

from frigate.api import recording_days


class CountingDatabase:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    def execute_sql(self, sql: str, parameters: tuple[object, ...]):
        self.queries.append((sql, parameters))
        return self.connection.execute(sql, parameters)


class FakeMetadata:
    table_name = "recordings"
    fields = {name: object() for name in ("camera", "start_time", "end_time")}

    def __init__(self, database: CountingDatabase) -> None:
        self.database = database


class RecordingDaysTest(unittest.TestCase):
    def setUp(self) -> None:
        self.database = CountingDatabase()

        class FakeRecordings:
            _meta = FakeMetadata(self.database)

        self.recordings_model = FakeRecordings
        self.database.connection.executescript(
            """
            CREATE TABLE recordings (
                id TEXT PRIMARY KEY,
                camera TEXT NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL NOT NULL
            );
            CREATE INDEX recordings_camera_start_time_end_time
                ON recordings(camera, start_time DESC, end_time DESC);
            """
        )
        day = recording_days.SECONDS_PER_DAY
        self.database.connection.executemany(
            "INSERT INTO recordings VALUES (?, ?, ?, ?)",
            [
                ("front-1", "front", day + 10, day + 20),
                ("front-2", "front", 2 * day + 10, 2 * day + 20),
                ("back-1", "back", 3 * day + 10, 3 * day + 20),
                ("private-1", "private", 4 * day + 10, 4 * day + 20),
            ],
        )
        self.database.queries.clear()

    def tearDown(self) -> None:
        self.database.connection.close()

    @staticmethod
    def utc_periods(_timezone: str, start: float, end: float):
        return [(start, end, 0.0)]

    def test_days_are_exact_authorized_and_use_bounded_index_probes(self) -> None:
        days = recording_days.query_recording_days(
            self.recordings_model,
            ["front", "back"],
            "utc",
            self.utc_periods,
        )

        self.assertEqual(
            {
                "1970-01-02": True,
                "1970-01-03": True,
                "1970-01-04": True,
            },
            days,
        )
        sql = "\n".join(query for query, _ in self.database.queries)
        self.assertNotIn("DISTINCT", sql)
        self.assertNotIn("GROUP BY", sql)
        self.assertTrue(
            all(
                f'INDEXED BY "{recording_days.RECORDING_INDEX}"' in query
                for query, _ in self.database.queries
            )
        )
        self.assertNotIn(
            "private", [parameter for _, p in self.database.queries for parameter in p]
        )

    def test_timezone_offset_maps_to_local_calendar_day(self) -> None:
        offset = -7 * 3600

        def periods(_timezone: str, start: float, end: float):
            return [(start, end, offset)]

        days = recording_days.query_recording_days(
            self.recordings_model,
            ["front"],
            "America/Denver",
            periods,
        )

        self.assertEqual({"1970-01-01": True, "1970-01-02": True}, days)

    def test_empty_camera_scope_performs_no_query(self) -> None:
        self.assertEqual(
            {},
            recording_days.query_recording_days(
                self.recordings_model,
                [],
                "utc",
                self.utc_periods,
            ),
        )
        self.assertEqual([], self.database.queries)

    def test_model_drift_fails_closed(self) -> None:
        self.recordings_model._meta.table_name = "recording"
        with self.assertRaisesRegex(
            RuntimeError, "unexpected Frigate Recordings table"
        ):
            recording_days.query_recording_days(
                self.recordings_model,
                ["front"],
                "utc",
                self.utc_periods,
            )


if __name__ == "__main__":
    unittest.main()
