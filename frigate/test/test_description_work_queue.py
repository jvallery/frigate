#!/usr/bin/env python3
"""Deterministic tests for bounded Frigate description resilience helpers."""

from __future__ import annotations

import logging
import multiprocessing
import threading
import time
import unittest
from collections import deque

from frigate.data_processing.post import description_work_queue as work_queue
from frigate.genai import request_outcome

AttemptOutcome = request_outcome.AttemptOutcome


def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not satisfied before timeout")


class FakeRequestor:
    def __init__(self) -> None:
        self.created_thread = threading.get_ident()
        self.closed_thread: int | None = None
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed_thread = threading.get_ident()
        self.closed.set()


def spawn_description_queue_probe(metrics, results) -> None:
    """Construct and exercise the queue after a real spawn boundary."""

    description_queue = None
    try:
        description_queue = work_queue.DescriptionWorkQueue(
            metrics=metrics,
            requestor_factory=FakeRequestor,
            wait=lambda _delay: False,
        )
        accepted = description_queue.submit(
            "object", lambda _requestor: AttemptOutcome.success
        )
        wait_until(
            lambda: (
                metrics["object_successful"] == 1
                and metrics["pending"] == 0
                and metrics["active"] == 0
            )
        )
        results.put(
            {
                "accepted": accepted,
                "keys": sorted(metrics.keys()),
                "pending": metrics["pending"],
                "active": metrics["active"],
                "worker_alive": description_queue.worker_alive,
            }
        )
    finally:
        if description_queue is not None:
            description_queue.stop()


class DescriptionWorkQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.queues: list[work_queue.DescriptionWorkQueue] = []

    def tearDown(self) -> None:
        for description_queue in self.queues:
            description_queue.stop()

    def make_queue(self, **kwargs) -> work_queue.DescriptionWorkQueue:
        description_queue = work_queue.DescriptionWorkQueue(**kwargs)
        self.queues.append(description_queue)
        return description_queue

    def assert_manager_dictproxy_context(self, start_method: str) -> None:
        """Exercise the real proxy type across a strict process boundary."""

        context = multiprocessing.get_context(start_method)
        with context.Manager() as manager:
            metrics = manager.dict()
            results = context.Queue()
            process = context.Process(
                target=spawn_description_queue_probe,
                args=(metrics, results),
            )
            process.start()
            process.join(timeout=5)

            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
                self.fail(f"{start_method} description queue probe did not exit")

            self.assertEqual(0, process.exitcode)
            result = results.get(timeout=1)
            self.assertTrue(result["accepted"])
            self.assertEqual(sorted(work_queue.METRIC_KEYS), result["keys"])
            self.assertEqual(0, result["pending"])
            self.assertEqual(0, result["active"])
            self.assertTrue(result["worker_alive"])

    def test_accepts_spawn_manager_dictproxy_metrics(self) -> None:
        self.assert_manager_dictproxy_context("spawn")

    @unittest.skipUnless(
        "forkserver" in multiprocessing.get_all_start_methods(),
        "forkserver is unavailable on this platform",
    )
    def test_accepts_production_forkserver_manager_dictproxy_metrics(self) -> None:
        """Mirror Frigate beta2's Linux multiprocessing start method."""

        self.assert_manager_dictproxy_context("forkserver")

    def test_object_and_review_share_one_active_slot(self) -> None:
        requestor = FakeRequestor()
        description_queue = self.make_queue(
            maxsize=4,
            requestor_factory=lambda: requestor,
            wait=lambda _delay: False,
        )
        first_started = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        active = 0
        maximum_active = 0
        active_lock = threading.Lock()

        def enter() -> None:
            nonlocal active, maximum_active
            with active_lock:
                active += 1
                maximum_active = max(maximum_active, active)

        def leave() -> None:
            nonlocal active
            with active_lock:
                active -= 1

        def object_attempt(_requestor) -> AttemptOutcome:
            enter()
            try:
                first_started.set()
                self.assertTrue(release_first.wait(1))
                return AttemptOutcome.success
            finally:
                leave()

        def review_attempt(_requestor) -> AttemptOutcome:
            enter()
            try:
                second_started.set()
                return AttemptOutcome.success
            finally:
                leave()

        self.assertTrue(description_queue.submit("object", object_attempt))
        self.assertTrue(first_started.wait(1))
        self.assertTrue(description_queue.submit("review", review_attempt))
        self.assertFalse(second_started.wait(0.05))
        self.assertEqual(1, description_queue.metrics["active"])

        release_first.set()
        wait_until(lambda: description_queue.metrics["review_successful"] == 1)

        self.assertEqual(1, maximum_active)
        self.assertEqual(1, description_queue.metrics["object_successful"])
        self.assertEqual(1, description_queue.metrics["review_successful"])
        self.assertEqual(0, description_queue.metrics["pending"])
        self.assertEqual(0, description_queue.metrics["active"])

    def test_capacity_bounds_waiting_work_and_rejects_without_blocking(self) -> None:
        description_queue = self.make_queue(
            maxsize=1,
            requestor_factory=FakeRequestor,
            wait=lambda _delay: False,
        )
        started = threading.Event()
        release = threading.Event()

        def blocked(_requestor) -> AttemptOutcome:
            started.set()
            self.assertTrue(release.wait(1))
            return AttemptOutcome.success

        self.assertTrue(description_queue.submit("object", blocked))
        self.assertTrue(started.wait(1))
        self.assertTrue(
            description_queue.submit(
                "review", lambda _requestor: AttemptOutcome.success
            )
        )
        with self.assertLogs(work_queue.__name__, level=logging.WARNING) as captured:
            self.assertFalse(
                description_queue.submit(
                    "object", lambda _requestor: AttemptOutcome.success
                )
            )

        self.assertEqual(1, description_queue.metrics["pending"])
        self.assertEqual(1, description_queue.metrics["active"])
        self.assertEqual(1, description_queue.metrics["object_rejected"])
        self.assertEqual(
            [
                f"WARNING:{work_queue.__name__}:GenAI description queue rejected work surface=object reason=full"
            ],
            captured.output,
        )

        release.set()
        wait_until(lambda: description_queue.metrics["review_successful"] == 1)

    def test_recovery_is_admitted_only_when_live_queue_is_idle(self) -> None:
        description_queue = self.make_queue(
            maxsize=2,
            requestor_factory=FakeRequestor,
            wait=lambda _delay: False,
        )
        started = threading.Event()
        release = threading.Event()

        def blocked(_requestor) -> AttemptOutcome:
            started.set()
            self.assertTrue(release.wait(1))
            return AttemptOutcome.success

        self.assertTrue(description_queue.submit("object", blocked))
        self.assertTrue(started.wait(1))
        with self.assertLogs(work_queue.__name__, level=logging.WARNING) as captured:
            self.assertFalse(
                description_queue.submit_recovery(
                    "review", lambda _requestor: AttemptOutcome.success
                )
            )
        self.assertIn("reason=live-work-present", captured.output[0])

        release.set()
        wait_until(lambda: description_queue.metrics["active"] == 0)
        self.assertTrue(
            description_queue.submit_recovery(
                "review", lambda _requestor: AttemptOutcome.success
            )
        )
        wait_until(lambda: description_queue.metrics["review_successful"] == 1)

    def test_retries_empty_and_timeout_then_succeeds_with_fixed_backoff(self) -> None:
        outcomes = deque(
            [AttemptOutcome.empty, AttemptOutcome.timeout, AttemptOutcome.success]
        )
        delays: list[float] = []
        description_queue = self.make_queue(
            requestor_factory=FakeRequestor,
            wait=lambda delay: delays.append(delay) or False,
        )

        self.assertTrue(
            description_queue.submit("review", lambda _requestor: outcomes.popleft())
        )
        wait_until(lambda: description_queue.metrics["review_successful"] == 1)

        self.assertEqual(3, description_queue.metrics["review_attempted"])
        self.assertEqual(1, description_queue.metrics["review_empty"])
        self.assertEqual(1, description_queue.metrics["review_timeout"])
        self.assertEqual(2, description_queue.metrics["review_retried"])
        self.assertEqual(0, description_queue.metrics["review_failed"])
        self.assertEqual([1.0, 2.0], delays)

    def test_exhaustion_is_counted_and_worker_processes_the_next_job(self) -> None:
        completions: list[AttemptOutcome] = []
        description_queue = self.make_queue(
            requestor_factory=FakeRequestor,
            wait=lambda _delay: False,
        )

        with self.assertLogs(work_queue.__name__, level=logging.WARNING) as captured:
            self.assertTrue(
                description_queue.submit(
                    "object",
                    lambda _requestor: AttemptOutcome.provider_error,
                    on_complete=completions.append,
                )
            )
            self.assertTrue(
                description_queue.submit(
                    "review", lambda _requestor: AttemptOutcome.success
                )
            )
            wait_until(lambda: description_queue.metrics["review_successful"] == 1)

        self.assertEqual(3, description_queue.metrics["object_attempted"])
        self.assertEqual(3, description_queue.metrics["object_provider_error"])
        self.assertEqual(2, description_queue.metrics["object_retried"])
        self.assertEqual(1, description_queue.metrics["object_failed"])
        self.assertEqual([AttemptOutcome.provider_error], completions)
        self.assertTrue(description_queue.worker_alive)
        self.assertEqual(
            [
                f"WARNING:{work_queue.__name__}:GenAI description work failed surface=object outcome=provider_error"
            ],
            captured.output,
        )

    def test_persistence_retry_replaces_the_worker_local_requestor(self) -> None:
        requestors: list[FakeRequestor] = []
        attempts = 0

        def requestor_factory() -> FakeRequestor:
            requestor = FakeRequestor()
            requestors.append(requestor)
            return requestor

        def operation(requestor: FakeRequestor) -> AttemptOutcome:
            nonlocal attempts
            attempts += 1
            self.assertIs(requestors[-1], requestor)
            if attempts == 1:
                return AttemptOutcome.persistence_error
            return AttemptOutcome.success

        description_queue = self.make_queue(
            requestor_factory=requestor_factory,
            wait=lambda _delay: False,
        )
        self.assertTrue(description_queue.submit("object", operation))
        wait_until(lambda: description_queue.metrics["object_successful"] == 1)

        self.assertEqual(2, attempts)
        self.assertEqual(2, len(requestors))
        self.assertTrue(requestors[0].closed.is_set())
        self.assertFalse(requestors[1].closed.is_set())
        self.assertEqual(1, description_queue.metrics["object_persistence_error"])
        self.assertEqual(1, description_queue.metrics["object_retried"])

    def test_invalid_input_is_terminal_without_retry(self) -> None:
        description_queue = self.make_queue(
            requestor_factory=FakeRequestor,
            wait=lambda _delay: False,
        )

        with self.assertLogs(work_queue.__name__, level=logging.WARNING):
            self.assertTrue(description_queue.submit("object", lambda _requestor: None))
            wait_until(lambda: description_queue.metrics["object_failed"] == 1)

        self.assertEqual(1, description_queue.metrics["object_attempted"])
        self.assertEqual(1, description_queue.metrics["object_invalid_input"])
        self.assertEqual(0, description_queue.metrics["object_retried"])

    def test_stop_interrupts_backoff_drains_waiters_and_closes_in_worker(self) -> None:
        requestors: list[FakeRequestor] = []

        def requestor_factory() -> FakeRequestor:
            requestor = FakeRequestor()
            requestors.append(requestor)
            return requestor

        description_queue = self.make_queue(
            maxsize=2,
            requestor_factory=requestor_factory,
        )
        main_thread = threading.get_ident()

        self.assertTrue(
            description_queue.submit(
                "object", lambda _requestor: AttemptOutcome.timeout
            )
        )
        wait_until(lambda: description_queue.metrics["object_retried"] == 1)
        self.assertEqual(1, len(requestors))
        requestor = requestors[0]
        self.assertTrue(
            description_queue.submit(
                "review", lambda _requestor: AttemptOutcome.success
            )
        )

        started = time.monotonic()
        description_queue.stop(join_timeout=1)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5)
        self.assertFalse(description_queue.worker_alive)
        self.assertTrue(requestor.closed.is_set())
        self.assertNotEqual(main_thread, requestor.created_thread)
        self.assertEqual(requestor.created_thread, requestor.closed_thread)
        self.assertEqual(0, description_queue.metrics["pending"])
        self.assertEqual(0, description_queue.metrics["active"])
        self.assertEqual(1, description_queue.metrics["object_cancelled"])
        self.assertEqual(1, description_queue.metrics["review_cancelled"])

        with self.assertLogs(work_queue.__name__, level=logging.WARNING):
            self.assertFalse(
                description_queue.submit(
                    "review", lambda _requestor: AttemptOutcome.success
                )
            )
        self.assertEqual(1, description_queue.metrics["review_rejected"])

    def test_metrics_and_terminal_logs_have_only_fixed_privacy_safe_values(
        self,
    ) -> None:
        private_marker = "private-event-camera-prompt-response"
        metrics: dict[str, int] = {}
        description_queue = self.make_queue(
            metrics=metrics,
            requestor_factory=FakeRequestor,
            wait=lambda _delay: False,
        )

        def raises_private_error(_requestor) -> AttemptOutcome:
            raise RuntimeError(private_marker)

        with self.assertLogs(work_queue.__name__, level=logging.WARNING) as captured:
            self.assertTrue(description_queue.submit("review", raises_private_error))
            wait_until(lambda: metrics["review_failed"] == 1)

        self.assertEqual(set(work_queue.METRIC_KEYS), set(metrics))
        self.assertTrue(all(isinstance(value, int) for value in metrics.values()))
        self.assertNotIn(private_marker, "\n".join(captured.output))
        self.assertEqual(3, metrics["review_attempted"])
        self.assertEqual(3, metrics["review_internal_error"])
        self.assertEqual(2, metrics["review_retried"])
        self.assertEqual(
            [
                f"WARNING:{work_queue.__name__}:GenAI description processor failed surface=review error=RuntimeError",
                f"WARNING:{work_queue.__name__}:GenAI description processor failed surface=review error=RuntimeError",
                f"WARNING:{work_queue.__name__}:GenAI description processor failed surface=review error=RuntimeError",
                f"WARNING:{work_queue.__name__}:GenAI description work failed surface=review outcome=internal_error",
            ],
            captured.output,
        )


class RequestOutcomeTest(unittest.TestCase):
    def tearDown(self) -> None:
        request_outcome.reset_outcome()

    def test_consume_is_destructive_and_defaults_fail_closed(self) -> None:
        request_outcome.reset_outcome()
        request_outcome.set_outcome(AttemptOutcome.empty)
        self.assertEqual(AttemptOutcome.empty, request_outcome.consume_outcome())
        self.assertEqual(
            AttemptOutcome.provider_error, request_outcome.consume_outcome()
        )

    def test_outcome_is_isolated_between_threads(self) -> None:
        request_outcome.set_outcome(AttemptOutcome.empty)
        child_result: list[AttemptOutcome] = []

        def child() -> None:
            request_outcome.reset_outcome()
            request_outcome.set_outcome(AttemptOutcome.timeout)
            child_result.append(request_outcome.consume_outcome())

        thread = threading.Thread(target=child)
        thread.start()
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual([AttemptOutcome.timeout], child_result)
        self.assertEqual(AttemptOutcome.empty, request_outcome.consume_outcome())

    def test_set_and_consume_reject_non_enum_values(self) -> None:
        with self.assertRaises(TypeError):
            request_outcome.set_outcome("empty")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            request_outcome.consume_outcome("empty")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
