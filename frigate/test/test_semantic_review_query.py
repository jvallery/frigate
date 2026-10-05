"""SQL regression coverage for semantic review lookup."""

import unittest

from peewee import JOIN, FloatField, Model, SqliteDatabase, TextField
from playhouse.sqlite_ext import JSONField

from frigate.api.semantic_review_query import semantic_review_matches


class SemanticReviewQueryTest(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDatabase(":memory:")

        class Event(Model):
            id = TextField(primary_key=True)
            camera = TextField()
            start_time = FloatField()
            end_time = FloatField(null=True)

            class Meta:
                database = self.db

        class Review(Model):
            id = TextField(primary_key=True)
            camera = TextField()
            data = JSONField()
            thumb_path = TextField()

            class Meta:
                database = self.db

        self.event = Event
        self.review = Review
        self.db.create_tables([Event, Review])
        Event.insert_many(
            [
                {"id": "object", "camera": "front", "start_time": 0, "end_time": 30},
                {
                    "id": "object-long",
                    "camera": "front",
                    "start_time": 0,
                    "end_time": None,
                },
                {"id": "no-review", "camera": "front", "start_time": 0, "end_time": 1},
                {"id": "hidden", "camera": "private", "start_time": 0, "end_time": 1},
            ]
        ).execute()
        Review.insert_many(
            [
                {
                    "id": "one",
                    "camera": "front",
                    "data": {"detections": ["object-long"]},
                    "thumb_path": "long.jpg",
                },
                {
                    "id": "two",
                    "camera": "front",
                    "data": {"detections": ["object"]},
                    "thumb_path": "ended.jpg",
                },
                {
                    "id": "three",
                    "camera": "front",
                    "data": {"detections": ["object"]},
                    "thumb_path": "later.jpg",
                },
                {
                    "id": "wrong-camera",
                    "camera": "private",
                    "data": {"detections": ["object", "hidden"]},
                    "thumb_path": "private.jpg",
                },
                {
                    "id": "empty",
                    "camera": "front",
                    "data": {},
                    "thumb_path": "empty.jpg",
                },
            ]
        ).execute()

    def tearDown(self):
        self.db.close()

    def query(self, ids, cameras):
        e = self.event
        reviews = semantic_review_matches(self.review, ids)
        return (
            e.select(e.id, reviews.c.thumb_path)
            .join(
                reviews,
                JOIN.LEFT_OUTER,
                on=(e.id == reviews.c.event_id) & (e.camera == reviews.c.camera),
            )
            .where(e.id.in_(ids), e.camera.in_(cameras))
            .with_cte(reviews)
            .order_by(e.id, reviews.c.thumb_path)
        )

    def test_exact_ids_left_join_duplicates_and_camera_authorization(self):
        rows = list(
            self.query(
                ["object", "object-long", "no-review", "hidden"], ["front"]
            ).dicts()
        )
        self.assertEqual(
            [
                ("no-review", None),
                ("object", "ended.jpg"),
                ("object", "later.jpg"),
                ("object-long", "long.jpg"),
            ],
            [(r["id"], r["thumb_path"]) for r in rows],
        )

    def test_repeated_array_ids_do_not_duplicate_a_review(self):
        self.review.create(
            id="repeated",
            camera="front",
            data={"detections": ["object", "object"]},
            thumb_path="ended.jpg",
        )
        rows = list(self.query(["object"], ["front"]).dicts())
        self.assertEqual(3, len(rows))
        self.assertEqual(2, sum(r["thumb_path"] == "ended.jpg" for r in rows))

    def test_ended_objects_keep_later_review_membership(self):
        # Time overlap is not a reliable substitute for object IDs in retained
        # review metadata. Both historical associations must remain visible.
        self.assertEqual(2, len(list(self.query(["object"], ["front"]).dicts())))

    def test_empty_ids_and_empty_authorization_return_nothing(self):
        self.assertEqual([], list(self.query([], ["front"]).dicts()))
        self.assertEqual([], list(self.query(["object"], []).dicts()))

    def test_candidate_ids_are_bound_parameters(self):
        value = "object'); DROP TABLE review; --"
        query = self.query([value], ["front"])
        sql, parameters = query.sql()
        self.assertNotIn(value, sql)
        self.assertIn(value, parameters)
        self.assertEqual([], list(query.dicts()))
        self.assertEqual(5, self.review.select().count())

    def test_review_archive_is_materialized_once(self):
        query = self.query(["object", "object-long"], ["front"])
        sql, parameters = query.sql()
        self.assertIn("AS MATERIALIZED", sql)
        plan = self.db.execute_sql("EXPLAIN QUERY PLAN " + sql, parameters).fetchall()
        descriptions = [row[3] for row in plan]
        self.assertEqual(
            1, sum("MATERIALIZE search_review_matches" in d for d in descriptions)
        )
        self.assertEqual(1, sum(d.startswith("SCAN t1") for d in descriptions))
        self.assertTrue(
            any(
                "SEARCH search_review_matches USING AUTOMATIC" in d
                for d in descriptions
            )
        )


if __name__ == "__main__":
    unittest.main()
