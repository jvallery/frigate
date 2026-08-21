"""Semantics and bounded-query tests for Review summaries."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from peewee import BooleanField, CharField, FloatField, ForeignKeyField, Model
from playhouse.sqlite_ext import JSONField, SqliteExtDatabase

from frigate.api import review_summary_query

database = SqliteExtDatabase(":memory:")


class ReviewSegment(Model):
    id = CharField(primary_key=True)
    camera = CharField(index=True)
    start_time = FloatField(index=True)
    end_time = FloatField(null=True)
    severity = CharField()
    data = JSONField()

    class Meta:
        database = database
        table_name = "reviewsegment"


class UserReviewStatus(Model):
    user_id = CharField()
    review_segment = ForeignKeyField(ReviewSegment, backref="statuses")
    has_been_reviewed = BooleanField(default=False)

    class Meta:
        database = database
        table_name = "userreviewstatus"
        indexes = ((("user_id", "review_segment"), True),)


class Severity:
    alert = "alert"
    detection = "detection"


class ReviewSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        database.bind([ReviewSegment, UserReviewStatus])
        database.create_tables([ReviewSegment, UserReviewStatus])
        day = review_summary_query.DAY_SECONDS
        rows = [
            ("old-alert", "front", day + 100, "alert", ["person"], ["porch"]),
            ("back-detection", "back", 9 * day + 100, "detection", ["car"], ["yard"]),
            ("new-alert", "front", 10 * day + 100, "alert", ["person"], ["porch"]),
            ("new-detection", "front", 10 * day + 200, "detection", ["dog"], ["porch"]),
            (
                "private-alert",
                "private",
                10 * day + 300,
                "alert",
                ["person"],
                ["inside"],
            ),
        ]
        for review_id, camera, start, severity, objects, zones in rows:
            ReviewSegment.create(
                id=review_id,
                camera=camera,
                start_time=start,
                end_time=start + 30,
                severity=severity,
                data={"objects": objects, "audio": [], "zones": zones},
            )
        UserReviewStatus.create(
            user_id="admin",
            review_segment="new-alert",
            has_been_reviewed=True,
        )
        UserReviewStatus.create(
            user_id="other",
            review_segment="back-detection",
            has_been_reviewed=True,
        )
        self.sql: list[str] = []
        database.connection().set_trace_callback(self.sql.append)

    def tearDown(self) -> None:
        database.connection().set_trace_callback(None)
        database.drop_tables([UserReviewStatus, ReviewSegment])

    @staticmethod
    def periods(_timezone: str, start: float, end: float):
        return [(start, end, 0.0)]

    def query(self, **overrides):
        params = SimpleNamespace(
            cameras=overrides.get("cameras", "all"),
            labels=overrides.get("labels", "all"),
            zones=overrides.get("zones", "all"),
            timezone="utc",
        )
        return review_summary_query.query_review_summary(
            params,
            "admin",
            ["front", "back"],
            ReviewSegment,
            UserReviewStatus,
            Severity,
            self.periods,
            now_timestamp=10 * review_summary_query.DAY_SECONDS + 300,
        )

    def test_counts_match_existing_api_without_all_row_status_join(self) -> None:
        result = self.query()

        self.assertEqual(
            {
                "reviewed_alert": 1,
                "reviewed_detection": 0,
                "total_alert": 1,
                "total_detection": 1,
            },
            result["last24Hours"],
        )
        self.assertEqual(
            (1, 0, 0, 0),
            tuple(
                result["1970-01-02"][key]
                for key in (
                    "total_alert",
                    "total_detection",
                    "reviewed_alert",
                    "reviewed_detection",
                )
            ),
        )
        self.assertEqual(1, result["1970-01-11"]["reviewed_alert"])
        self.assertNotIn("private-alert", "\n".join(self.sql))

        statements = "\n".join(self.sql).upper()
        self.assertNotIn("LEFT OUTER JOIN", statements)
        self.assertIn('FROM "USERREVIEWSTATUS"', statements)
        self.assertLessEqual(len(self.sql), 5)

    def test_camera_label_and_zone_filters_remain_authorized(self) -> None:
        result = self.query(cameras="front,private", labels="person", zones="porch")

        self.assertEqual(1, result["last24Hours"]["total_alert"])
        self.assertEqual(0, result["last24Hours"]["total_detection"])
        self.assertEqual(1, result["1970-01-02"]["total_alert"])
        self.assertNotIn("1970-01-10", result)

    def test_empty_requested_camera_intersection_returns_empty(self) -> None:
        self.assertEqual({}, self.query(cameras="private"))


if __name__ == "__main__":
    unittest.main()
