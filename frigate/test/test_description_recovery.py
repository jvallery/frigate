"""Direct tests for the bounded description recovery coordinator."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import patch

from frigate.data_processing.post import description_recovery as recovery


@dataclass
class Candidate:
    id: str | None
    end_time: float
    data: dict = field(default_factory=dict)
    camera: str = "private-camera"
    label: str = "private-label"


class Processor:
    def __init__(
        self,
        admission: recovery.RecoveryAdmission = recovery.RecoveryAdmission.accepted,
        completion: recovery.AttemptOutcome | None = recovery.AttemptOutcome.success,
    ) -> None:
        self.admission = admission
        self.completion = completion
        self.models: list[Candidate] = []

    def submit_recovery(
        self,
        model: Candidate,
        on_complete,
    ) -> recovery.RecoveryAdmission:
        self.models.append(model)
        if (
            self.admission == recovery.RecoveryAdmission.accepted
            and self.completion is not None
        ):
            on_complete(self.completion)
        return self.admission


class RaisingProcessor(Processor):
    def submit_recovery(
        self,
        model: Candidate,
        _on_complete,
    ) -> recovery.RecoveryAdmission:
        raise RuntimeError(
            f"sensitive {model.id} {model.camera} {model.label} provider response"
        )


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Source:
    def __init__(self, candidates: list[Candidate]) -> None:
        self.candidates = candidates
        self.calls: list[tuple[float, float, int]] = []

    def __call__(
        self,
        start: float,
        end: float,
        limit: int,
        after: recovery.ScanCursor | None = None,
    ):
        self.calls.append((start, end, limit))
        candidates = sorted(
            (
                candidate
                for candidate in self.candidates
                if candidate.id is not None and start <= candidate.end_time <= end
            ),
            key=lambda candidate: (candidate.end_time, str(candidate.id)),
        )
        if after is not None:
            candidates = [
                candidate
                for candidate in candidates
                if (candidate.end_time, str(candidate.id)) > after
            ]
        return candidates[:limit]


def sqlite_model(connection: sqlite3.Connection, table: str):
    class Database:
        @staticmethod
        def execute_sql(sql: str, parameters: tuple):
            return connection.execute(sql, parameters)

    class Model:
        _meta = SimpleNamespace(table_name=table, database=Database())

        @classmethod
        def get_by_id(cls, model_id: str):
            row = connection.execute(
                f'SELECT data FROM "{table}" WHERE id = ?', (model_id,)
            ).fetchone()
            if row is None:
                raise KeyError(model_id)
            return SimpleNamespace(data=json.loads(row[0]))

    return Model


def coordinator(
    *,
    objects: Source | None = None,
    reviews: Source | None = None,
    object_processor: Processor | None = None,
    review_processor: Processor | None = None,
    clock: FakeClock | None = None,
    **kwargs,
):
    arguments = {
        "enabled": True,
        "start": 10,
        "end": 20,
        "max_items": 512,
        "interval": 15,
        "initial_delay": 0,
    }
    arguments.update(kwargs)
    return recovery.DescriptionRecoveryCoordinator(
        config=object(),
        object_processor=object_processor or Processor(),
        review_processor=review_processor or Processor(),
        clock=clock or FakeClock(),
        object_candidates=objects or Source([]),
        review_candidates=reviews or Source([]),
        **arguments,
    )


class DescriptionRecoveryCoordinatorTest(unittest.TestCase):
    def test_disabled_and_invalid_windows_never_query(self) -> None:
        def fail_source(
            _start: float,
            _end: float,
            _limit: int,
            _after: recovery.ScanCursor | None = None,
        ):
            raise AssertionError("disabled coordinator queried candidates")

        disabled = recovery.DescriptionRecoveryCoordinator(
            object(),
            Processor(),
            Processor(),
            enabled=False,
            object_candidates=fail_source,
            review_candidates=fail_source,
        )
        self.assertFalse(disabled.tick())

        for start, end in ((None, 20), (10, None), (20, 10), (10, float("inf"))):
            with self.subTest(start=start, end=end):
                invalid = recovery.DescriptionRecoveryCoordinator(
                    object(),
                    Processor(),
                    Processor(),
                    enabled=True,
                    start=start,
                    end=end,
                    object_candidates=fail_source,
                    review_candidates=fail_source,
                )
                self.assertFalse(invalid.enabled)
                self.assertFalse(invalid.tick())

    def test_defaults_are_read_from_environment(self) -> None:
        objects = Source([Candidate("one", 15, {})])
        processor = Processor()
        env = {
            recovery.ENABLED_ENV: "true",
            recovery.START_ENV: "10",
            recovery.END_ENV: "20",
            recovery.MAX_ITEMS_ENV: "2",
            recovery.INTERVAL_ENV: "3",
            recovery.INITIAL_DELAY_ENV: "0",
        }
        with patch.dict(os.environ, env, clear=False):
            subject = recovery.DescriptionRecoveryCoordinator(
                object(),
                processor,
                Processor(),
                object_candidates=objects,
                review_candidates=Source([]),
                clock=FakeClock(),
            )

        self.assertTrue(subject.enabled)
        self.assertEqual(2, subject.max_items)
        self.assertEqual(3, subject.interval)
        self.assertTrue(subject.tick())

    def test_tick_is_rate_limited_and_alternates_surfaces(self) -> None:
        clock = FakeClock()
        objects = Source(
            [Candidate("object-one", 11, {}), Candidate("object-two", 13, {})]
        )
        reviews = Source([Candidate("review-one", 12, {"metadata": None})])
        object_processor = Processor()
        review_processor = Processor()
        subject = coordinator(
            objects=objects,
            reviews=reviews,
            object_processor=object_processor,
            review_processor=review_processor,
            clock=clock,
        )

        self.assertTrue(subject.tick())
        self.assertFalse(subject.tick())
        clock.advance(14.9)
        self.assertFalse(subject.tick())
        clock.advance(0.1)
        self.assertTrue(subject.tick())
        clock.advance(15)
        self.assertTrue(subject.tick())

        self.assertEqual(
            ["object-one", "object-two"],
            [model.id for model in object_processor.models],
        )
        self.assertEqual(
            ["review-one"], [model.id for model in review_processor.models]
        )

    def test_initial_delay_and_stop_are_immediate_kill_switches(self) -> None:
        clock = FakeClock()
        objects = Source([Candidate("object-one", 11, {})])
        processor = Processor()
        subject = coordinator(
            objects=objects,
            object_processor=processor,
            clock=clock,
            initial_delay=180,
        )

        self.assertFalse(subject.tick())
        clock.advance(179.9)
        self.assertFalse(subject.tick())
        clock.advance(0.1)
        self.assertTrue(subject.tick())
        subject.stop()
        clock.advance(15)
        self.assertFalse(subject.tick())

    def test_invalid_candidates_do_not_consume_the_inference_budget(self) -> None:
        clock = FakeClock()
        objects = Source(
            [Candidate("already-described", 11, {"description": "present"})]
        )
        reviews = Source([Candidate("review-one", 12, {"metadata": None})])
        object_processor = Processor()
        review_processor = Processor()
        subject = recovery.DescriptionRecoveryCoordinator(
            object(),
            object_processor,
            review_processor,
            enabled=True,
            start=10,
            end=20,
            max_items=2,
            interval=1,
            initial_delay=0,
            clock=clock,
            object_candidates=objects,
            review_candidates=reviews,
        )

        self.assertTrue(subject.tick())
        self.assertEqual(1, subject.attempted_count)
        self.assertEqual([], object_processor.models)
        self.assertEqual(
            ["review-one"], [model.id for model in review_processor.models]
        )
        clock.advance(1)
        self.assertFalse(subject.tick())
        self.assertTrue(objects.calls)
        self.assertTrue(reviews.calls)
        self.assertTrue(
            all(call[2] == recovery.DEFAULT_SCAN_LIMIT for call in objects.calls)
        )
        self.assertTrue(
            all(call[2] == recovery.DEFAULT_SCAN_LIMIT for call in reviews.calls)
        )

    def test_queue_backpressure_retries_without_consuming_budget(self) -> None:
        class BackpressureThenAccept(Processor):
            def submit_recovery(
                self,
                model: Candidate,
                on_complete,
            ) -> recovery.RecoveryAdmission:
                self.models.append(model)
                if len(self.models) == 1:
                    return recovery.RecoveryAdmission.retry_later
                on_complete(recovery.AttemptOutcome.success)
                return recovery.RecoveryAdmission.accepted

        clock = FakeClock()
        objects = Source([Candidate("object-one", 11, {})])
        processor = BackpressureThenAccept()
        subject = coordinator(
            objects=objects,
            object_processor=processor,
            clock=clock,
            interval=1,
        )

        self.assertFalse(subject.tick())
        self.assertEqual(0, subject.attempted_count)
        clock.advance(1)
        self.assertTrue(subject.tick())
        self.assertEqual(1, subject.attempted_count)
        self.assertEqual(["object-one", "object-one"], [m.id for m in processor.models])

    def test_accepted_but_still_missing_candidate_gets_one_delayed_readmission(
        self,
    ) -> None:
        class FailThenSucceed(Processor):
            def submit_recovery(
                self,
                model: Candidate,
                on_complete,
            ) -> recovery.RecoveryAdmission:
                self.models.append(model)
                outcome = (
                    recovery.AttemptOutcome.provider_error
                    if len(self.models) == 1
                    else recovery.AttemptOutcome.success
                )
                on_complete(outcome)
                return recovery.RecoveryAdmission.accepted

        clock = FakeClock()
        objects = Source([Candidate("object-one", 11, {})])
        processor = FailThenSucceed()
        subject = coordinator(
            objects=objects,
            object_processor=processor,
            clock=clock,
            interval=1,
        )

        self.assertTrue(subject.tick())
        clock.advance(recovery.RECOVERY_RETRY_DELAY_SECONDS - 1)
        self.assertFalse(subject.tick())
        clock.advance(1)
        self.assertTrue(subject.tick())
        clock.advance(recovery.RECOVERY_RETRY_DELAY_SECONDS)
        self.assertFalse(subject.tick())

        self.assertEqual(2, subject.attempted_count)
        self.assertEqual(2, len(processor.models))

    def test_inflight_candidate_is_never_readmitted_before_completion(self) -> None:
        clock = FakeClock()
        objects = Source([Candidate("object-one", 11, {})])
        reviews = Source([Candidate("review-one", 12, {"metadata": None})])
        processor = Processor(completion=None)
        review_processor = Processor()
        subject = coordinator(
            objects=objects,
            reviews=reviews,
            object_processor=processor,
            review_processor=review_processor,
            clock=clock,
            interval=1,
        )

        self.assertTrue(subject.tick())
        clock.advance(recovery.RECOVERY_RETRY_DELAY_SECONDS * 10)
        self.assertFalse(subject.tick())
        self.assertEqual(1, subject.attempted_count)
        self.assertEqual(1, len(processor.models))
        self.assertEqual([], review_processor.models)

    def test_recovery_gate_stops_live_failures_and_enabled_gate_repairs(self) -> None:
        clock = FakeClock()
        candidate = Candidate("live-object", 21, {})
        disabled = recovery.DescriptionRecoveryCoordinator(
            object(),
            Processor(),
            Processor(),
            enabled=False,
            interval=1,
            initial_delay=0,
            clock=clock,
            object_candidates=Source([]),
            review_candidates=Source([]),
        )

        self.assertTrue(disabled.note_live_failure("object", candidate.id))
        self.assertFalse(disabled.note_live_failure("object", candidate.id))
        with patch.object(
            recovery.DescriptionRecoveryCoordinator,
            "_load_live_model",
            return_value=candidate,
        ):
            self.assertFalse(disabled.tick())

        processor = Processor()
        enabled = recovery.DescriptionRecoveryCoordinator(
            object(),
            processor,
            Processor(),
            enabled=True,
            start=10,
            end=20,
            interval=1,
            initial_delay=0,
            clock=clock,
            wall_clock=lambda: 400,
            object_candidates=Source([]),
            review_candidates=Source([]),
        )
        self.assertTrue(enabled.note_live_failure("object", candidate.id))
        with patch.object(
            recovery.DescriptionRecoveryCoordinator,
            "_load_live_model",
            return_value=candidate,
        ):
            self.assertTrue(enabled.tick())

        self.assertEqual([candidate], processor.models)
        self.assertEqual(1, enabled.attempted_count)
        clock.advance(1)
        self.assertFalse(enabled.tick())

    def test_configured_end_is_exact_and_future_cutoff_is_rejected(self) -> None:
        clock = FakeClock()
        objects = Source([Candidate("object-one", 16, {})])
        processor = Processor()
        future = coordinator(
            objects=objects,
            object_processor=processor,
            clock=clock,
            wall_clock=lambda: 17,
        )

        self.assertFalse(future.enabled)
        self.assertIsNone(future.end)

        frozen = coordinator(
            objects=objects,
            object_processor=processor,
            clock=clock,
            wall_clock=lambda: 21,
        )
        self.assertEqual(20, frozen.end)
        self.assertTrue(frozen.tick())
        self.assertEqual((10, 20, recovery.DEFAULT_SCAN_LIMIT), objects.calls[-1])

    def test_recent_missing_scan_survives_process_memory_loss(self) -> None:
        candidate = Candidate("durable-missing", 650, {})
        objects = Source([candidate])

        for _process_start in range(2):
            processor = Processor()
            subject = recovery.DescriptionRecoveryCoordinator(
                object(),
                processor,
                Processor(),
                enabled=True,
                start=500,
                end=600,
                interval=1,
                initial_delay=0,
                clock=FakeClock(),
                wall_clock=lambda: 1_000,
                object_candidates=objects,
                review_candidates=Source([]),
            )
            self.assertTrue(subject.tick())
            self.assertEqual([candidate], processor.models)
            self.assertEqual(1, subject.attempted_count)

    def test_recent_scan_honors_media_grace_before_repair(self) -> None:
        too_new = Candidate("too-new", 701, {})
        old_enough = Candidate("old-enough", 700, {})
        processor = Processor()
        subject = recovery.DescriptionRecoveryCoordinator(
            object(),
            processor,
            Processor(),
            enabled=True,
            start=500,
            end=600,
            interval=1,
            initial_delay=0,
            clock=FakeClock(),
            wall_clock=lambda: 1_000,
            object_candidates=Source([too_new, old_enough]),
            review_candidates=Source([]),
        )

        self.assertTrue(subject.tick())
        self.assertEqual([old_enough], processor.models)

    def test_recent_scan_starts_strictly_after_frozen_incident_end(self) -> None:
        before = Candidate("before-start", 699, {})
        inside = Candidate("inside-incident", 750, {})
        after = Candidate("after-cutoff", 850, {})
        processor = Processor()
        subject = recovery.DescriptionRecoveryCoordinator(
            object(),
            processor,
            Processor(),
            enabled=True,
            start=700,
            end=800,
            interval=1,
            initial_delay=0,
            clock=FakeClock(),
            wall_clock=lambda: 1_200,
            object_candidates=Source([before, inside, after]),
            review_candidates=Source([]),
        )

        self.assertTrue(subject.tick())
        self.assertEqual([after], processor.models)

    def test_live_registry_never_evicts_inflight_recovery(self) -> None:
        subject = recovery.DescriptionRecoveryCoordinator(
            object(),
            Processor(),
            Processor(),
            enabled=False,
            initial_delay=0,
        )
        protected = ("object", "inflight")
        with subject._state_lock:
            subject._inflight.add(protected)
            subject._live_failures[protected] = None
        for index in range(recovery.MAX_LIVE_FAILURES - 1):
            self.assertTrue(subject.note_live_failure("object", f"queued-{index}"))

        self.assertTrue(subject.note_live_failure("review", "newest"))
        self.assertIn(protected, subject._live_failures)
        self.assertEqual(recovery.MAX_LIVE_FAILURES, len(subject._live_failures))

    def test_completed_tombstones_are_bounded(self) -> None:
        subject = recovery.DescriptionRecoveryCoordinator(
            object(),
            Processor(),
            Processor(),
            enabled=False,
        )
        with subject._state_lock:
            for index in range(recovery.MAX_COMPLETED_TOMBSTONES + 25):
                subject._mark_completed(("object", f"done-{index}"))
        self.assertEqual(
            recovery.MAX_COMPLETED_TOMBSTONES,
            len(subject._completed),
        )

    def test_retry_tracking_maps_share_one_bound(self) -> None:
        subject = recovery.DescriptionRecoveryCoordinator(
            object(),
            Processor(),
            Processor(),
            enabled=False,
        )
        with subject._state_lock:
            for index in range(recovery.MAX_TRACKED_RETRIES + 88):
                key = ("object", f"retry-{index}")
                subject._retry_after[key] = float(index)
                subject._admissions[key] = 1
                subject._submission_retries[key] = 1
                subject._touch_tracking(key)

        self.assertEqual(recovery.MAX_TRACKED_RETRIES, len(subject._tracking_order))
        self.assertLessEqual(len(subject._retry_after), recovery.MAX_TRACKED_RETRIES)
        self.assertLessEqual(len(subject._admissions), recovery.MAX_TRACKED_RETRIES)
        self.assertLessEqual(
            len(subject._submission_retries), recovery.MAX_TRACKED_RETRIES
        )

    def test_keyset_scan_reaches_candidate_after_first_512_rows(self) -> None:
        clock = FakeClock()
        candidates = [
            Candidate(f"candidate-{index:04d}", 11 + index / 10_000, {})
            for index in range(513)
        ]
        processor = Processor()
        subject = coordinator(
            objects=Source(candidates),
            object_processor=processor,
            clock=clock,
            interval=1,
            max_items=513,
        )

        for _index in range(513):
            self.assertTrue(subject.tick())
            clock.advance(1)

        self.assertEqual(513, len(processor.models))
        self.assertEqual("candidate-0512", processor.models[-1].id)

    def test_duplicate_suppression_is_per_surface(self) -> None:
        clock = FakeClock()
        objects = Source([Candidate("same", 11, {})])
        reviews = Source([Candidate("same", 12, {"metadata": None})])
        object_processor = Processor()
        review_processor = Processor()
        subject = coordinator(
            objects=objects,
            reviews=reviews,
            object_processor=object_processor,
            review_processor=review_processor,
            clock=clock,
            interval=1,
        )

        self.assertTrue(subject.tick())
        clock.advance(1)
        self.assertTrue(subject.tick())
        clock.advance(1)
        self.assertFalse(subject.tick())

        self.assertEqual(2, subject.attempted_count)
        self.assertEqual(1, len(object_processor.models))
        self.assertEqual(1, len(review_processor.models))

    def test_logs_never_include_private_candidate_or_error_content(self) -> None:
        private_id = "private-event-id"
        private_camera = "private-camera-name"
        private_label = "private-label-name"
        objects = Source(
            [
                Candidate(
                    private_id,
                    11,
                    {"private_context": "private-description-content"},
                    camera=private_camera,
                    label=private_label,
                )
            ]
        )
        log_name = "description-recovery-privacy-test"
        subject = coordinator(
            objects=objects,
            object_processor=RaisingProcessor(),
            logger=logging.getLogger(log_name),
        )

        with self.assertLogs(log_name, level="WARNING") as captured:
            self.assertFalse(subject.tick())

        rendered = "\n".join(captured.output)
        self.assertIn("RuntimeError", rendered)
        for private_value in (
            private_id,
            private_camera,
            private_label,
            "private-description-content",
            "provider response",
        ):
            self.assertNotIn(private_value, rendered)


class ConditionalPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.database = sqlite3.connect(":memory:")
        self.database.execute("CREATE TABLE event (id TEXT PRIMARY KEY, data TEXT)")
        self.database.execute(
            "CREATE TABLE reviewsegment (id TEXT PRIMARY KEY, data TEXT)"
        )
        self.event_model = sqlite_model(self.database, "event")
        self.review_model = sqlite_model(self.database, "reviewsegment")

    def tearDown(self) -> None:
        self.database.close()

    def data(self, table: str, model_id: str) -> dict:
        value = self.database.execute(
            f'SELECT data FROM "{table}" WHERE id = ?', (model_id,)
        ).fetchone()[0]
        return json.loads(value)

    def test_dispatcher_acknowledgement_is_typed_and_fail_closed(self) -> None:
        self.assertTrue(recovery.dispatcher_write_succeeded({"status": "written"}))
        self.assertFalse(recovery.dispatcher_write_succeeded({"status": "failed"}))
        self.assertFalse(recovery.dispatcher_write_succeeded("written"))
        self.assertFalse(
            recovery.dispatcher_write_succeeded({"status": "already_present"})
        )
        self.assertTrue(
            recovery.dispatcher_write_succeeded(
                {"status": "already_present"},
                allow_already_present=True,
            )
        )

    def test_object_fill_is_conditional_and_preserves_other_json(self) -> None:
        self.database.execute(
            "INSERT INTO event VALUES (?, ?)",
            ("missing", json.dumps({"region": [1, 2, 3, 4]})),
        )
        self.database.execute(
            "INSERT INTO event VALUES (?, ?)",
            (
                "existing",
                json.dumps({"description": "newer", "region": [4, 3, 2, 1]}),
            ),
        )

        self.assertEqual(
            recovery.ConditionalWriteResult.written,
            recovery.persist_object_description(
                "missing", "recovered", self.event_model
            ),
        )
        self.assertEqual(
            recovery.ConditionalWriteResult.already_present,
            recovery.persist_object_description("existing", "stale", self.event_model),
        )
        self.assertEqual(
            {"region": [1, 2, 3, 4], "description": "recovered"},
            self.data("event", "missing"),
        )
        self.assertEqual("newer", self.data("event", "existing")["description"])

    def test_review_merge_handles_absent_and_json_null_without_overwrite(self) -> None:
        for model_id, data in (
            ("absent", {"objects": ["person"]}),
            ("null", {"objects": ["car"], "metadata": None}),
            ("empty", {"objects": ["cat"], "metadata": {}}),
            ("existing", {"objects": ["dog"], "metadata": {"title": "newer"}}),
        ):
            self.database.execute(
                "INSERT INTO reviewsegment VALUES (?, ?)",
                (model_id, json.dumps(data)),
            )

        for model_id, expected in (
            ("absent", recovery.ConditionalWriteResult.written),
            ("null", recovery.ConditionalWriteResult.written),
            ("empty", recovery.ConditionalWriteResult.written),
            ("existing", recovery.ConditionalWriteResult.already_present),
        ):
            self.assertEqual(
                expected,
                recovery.persist_review_metadata(
                    model_id, {"title": "recovered"}, self.review_model
                ),
            )

        self.assertEqual(
            {"objects": ["person"], "metadata": {"title": "recovered"}},
            self.data("reviewsegment", "absent"),
        )
        self.assertEqual(
            {"objects": ["car"], "metadata": {"title": "recovered"}},
            self.data("reviewsegment", "null"),
        )
        self.assertEqual(
            {"objects": ["cat"], "metadata": {"title": "recovered"}},
            self.data("reviewsegment", "empty"),
        )
        self.assertEqual(
            {"objects": ["dog"], "metadata": {"title": "newer"}},
            self.data("reviewsegment", "existing"),
        )


if __name__ == "__main__":
    unittest.main()
