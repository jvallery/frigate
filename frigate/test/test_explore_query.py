"""Semantics and bounded-query tests for Explore."""

from __future__ import annotations

import json
import sqlite3
import unittest

from frigate.api import explore_query


def identity(value):
    return value


class FakeField:
    def __init__(self, name: str, converter=lambda value: value) -> None:
        self.column_name = name
        self.converter = converter

    def python_value(self, value):
        return self.converter(value)


class CountingDatabase:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.queries: list[tuple[str, list[object]]] = []

    def execute_sql(self, sql: str, parameters: list[object]):
        self.queries.append((sql, parameters))
        return self.connection.execute(sql, parameters)


class FakeMetadata:
    table_name = "event"

    def __init__(self, database: CountingDatabase) -> None:
        json_fields = {"zones", "box", "data"}
        bool_fields = {
            "has_clip",
            "has_snapshot",
            "retain_indefinitely",
            "false_positive",
        }
        self.fields = {}
        for name in explore_query.EXPLORE_EVENT_FIELDS:
            converter = identity
            if name in json_fields:
                converter = json.loads
            elif name in bool_fields:
                converter = bool
            self.fields[name] = FakeField(name, converter)
        self.database = database


class ExploreQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.database = CountingDatabase()

        class FakeEvent:
            _meta = FakeMetadata(self.database)

        self.event_model = FakeEvent
        self.database.connection.executescript(
            """
            CREATE TABLE event (
                id TEXT PRIMARY KEY, camera TEXT, label TEXT, zones TEXT,
                start_time REAL, end_time REAL, has_clip INTEGER,
                has_snapshot INTEGER, plus_id TEXT, retain_indefinitely INTEGER,
                sub_label TEXT, top_score REAL, false_positive INTEGER,
                box TEXT, data TEXT
            );
            CREATE INDEX event_label_start_time ON event(label, start_time DESC);
            """
        )
        self.database.connection.executemany(
            "INSERT INTO event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                self.event("person-new", "front", "person", 30, 0.93),
                self.event("person-mid", "back", "person", 20, 0.82),
                self.event("person-old", "front", "person", 10, 0.71),
                self.event("car-new", "front", "car", 25, 0.88),
                self.event("car-old", "front", "car", 15, 0.76),
                self.event("cat-only", "front", "cat", 40, 0.64),
                self.event("hidden", "private", "person", 100, 0.99),
            ],
        )
        self.database.queries.clear()

    def tearDown(self) -> None:
        self.database.connection.close()

    @staticmethod
    def event(event_id: str, camera: str, label: str, start: float, score: float):
        return (
            event_id,
            camera,
            label,
            json.dumps(["zone"]),
            start,
            start + 1,
            1,
            0,
            None,
            0,
            None,
            score,
            0,
            json.dumps([1, 2, 3, 4]),
            json.dumps(
                {
                    "score": score,
                    "description": f"{label} description",
                    "path_data": [[1, 2]],
                    "internal": "must not escape",
                }
            ),
        )

    def test_two_queries_preserve_authorization_counts_limit_and_projection(
        self,
    ) -> None:
        events = explore_query.query_explore_events(
            self.event_model, ["front", "back"], 2
        )
        self.assertEqual(2, len(self.database.queries))
        self.assertEqual(
            ["person-new", "person-mid", "car-new", "car-old", "cat-only"],
            [event["id"] for event in events],
        )
        self.assertEqual([3, 3, 2, 2, 1], [event["event_count"] for event in events])
        self.assertNotIn("hidden", [event["id"] for event in events])
        self.assertEqual({"score", "description", "path_data"}, set(events[0]["data"]))
        self.assertIn("GROUP BY", self.database.queries[0][0])
        self.assertIn("UNION ALL", self.database.queries[1][0])
        self.assertNotIn("OVER", self.database.queries[1][0])

    def test_zero_and_empty_authorization_do_not_query(self) -> None:
        self.assertEqual(
            [], explore_query.query_explore_events(self.event_model, [], 10)
        )
        self.assertEqual(
            [], explore_query.query_explore_events(self.event_model, ["front"], 0)
        )
        self.assertEqual([], self.database.queries)

    def test_chunking_and_model_drift_fail_closed(self) -> None:
        original = explore_query.MAX_LABELS_PER_RECENT_QUERY
        explore_query.MAX_LABELS_PER_RECENT_QUERY = 2
        try:
            events = explore_query.query_explore_events(
                self.event_model, ["front", "back"], 1
            )
        finally:
            explore_query.MAX_LABELS_PER_RECENT_QUERY = original
        self.assertEqual(3, len(self.database.queries))
        self.assertEqual(3, len(events))
        self.event_model._meta.table_name = "events"
        with self.assertRaisesRegex(RuntimeError, "unexpected Frigate Event table"):
            explore_query.query_explore_events(self.event_model, ["front"], 1)


if __name__ == "__main__":
    unittest.main()
