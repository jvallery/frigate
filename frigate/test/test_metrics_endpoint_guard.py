"""Concurrency and recovery tests for Prometheus endpoint serialization."""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

from frigate.stats.metrics_endpoint_guard import MetricsBusy, MetricsEndpointGuard


class MetricsEndpointGuardTest(unittest.TestCase):
    @staticmethod
    def rows():
        yield {"camera": "fixture-a", "label": "person", "Count": 7}
        yield {"camera": "fixture-b", "label": "car", "Count": 11}

    @staticmethod
    def collector():
        return SimpleNamespace(complete_stats={}, process_stats={}, all_events=[])

    def test_materializes_iterator_before_publishing_and_rendering(self) -> None:
        endpoint_guard = MetricsEndpointGuard()
        stages = []

        class RecordingCollector:
            def __init__(self):
                object.__setattr__(self, "complete_stats", {})
                object.__setattr__(self, "process_stats", {})
                object.__setattr__(self, "all_events", [])

            def __setattr__(self, name, value):
                stages.append((name, value))
                object.__setattr__(self, name, value)

        collector = RecordingCollector()

        def loader():
            stages.append("load-start")
            yield {"camera": "fixture-a", "label": "person", "Count": 7}
            stages.append("load-finished")

        def render():
            stages.append("render")
            with self.assertRaises(MetricsBusy):
                endpoint_guard.render(
                    stats={},
                    load_event_counts=self.rows,
                    collector=collector,
                    render_metrics=lambda: (b"unexpected", "text/plain"),
                )
            return b"fixture_metric 7\n", "text/plain"

        payload = endpoint_guard.render(
            stats={"service": {"uptime": 1}},
            load_event_counts=loader,
            collector=collector,
            render_metrics=render,
        )

        self.assertEqual(b"fixture_metric 7\n", payload.content)
        self.assertEqual("text/plain", payload.content_type)
        self.assertEqual("load-start", stages[0])
        self.assertEqual("load-finished", stages[1])
        self.assertEqual("complete_stats", stages[2][0])
        self.assertEqual("process_stats", stages[3][0])
        self.assertEqual(
            (
                "all_events",
                [{"camera": "fixture-a", "label": "person", "Count": 7}],
            ),
            stages[4],
        )
        self.assertEqual("render", stages[5])

    def test_failure_releases_lock_for_recovery(self) -> None:
        endpoint_guard = MetricsEndpointGuard()

        def fail():
            raise RuntimeError("renderer failed")

        with self.assertRaisesRegex(RuntimeError, "renderer failed"):
            endpoint_guard.render(
                stats={},
                load_event_counts=self.rows,
                collector=self.collector(),
                render_metrics=fail,
            )

        recovered = endpoint_guard.render(
            stats={},
            load_event_counts=self.rows,
            collector=self.collector(),
            render_metrics=lambda: (b"recovered", "text/plain"),
        )
        self.assertEqual(b"recovered", recovered.content)

    def test_concurrent_wave_has_one_success_and_no_state_leak(self) -> None:
        endpoint_guard = MetricsEndpointGuard()
        collector = self.collector()
        barrier = threading.Barrier(12)
        winner_entered = threading.Event()
        release_winner = threading.Event()
        successes = []
        busy = []
        failures = []
        result_lock = threading.Lock()
        losers_done = threading.Event()

        def loader():
            winner_entered.set()
            if not release_winner.wait(timeout=2):
                raise TimeoutError("fixture wave was not released")
            return [{"camera": "current", "label": "fixture", "Count": 1}]

        def render():
            camera = collector.all_events[0]["camera"]
            return f'current{{camera="{camera}"}} 1\n'.encode(), "text/plain"

        def request():
            barrier.wait(timeout=2)
            try:
                payload = endpoint_guard.render(
                    stats={},
                    load_event_counts=loader,
                    collector=collector,
                    render_metrics=render,
                )
                successes.append(payload.content)
            except MetricsBusy:
                with result_lock:
                    busy.append(1)
                    if len(busy) == 11:
                        losers_done.set()
            except Exception as error:  # pragma: no cover - asserted below
                failures.append(error)

        threads = [threading.Thread(target=request) for _ in range(12)]
        for thread in threads:
            thread.start()
        self.assertTrue(winner_entered.wait(timeout=2))
        self.assertTrue(losers_done.wait(timeout=2))
        release_winner.set()
        for thread in threads:
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

        self.assertEqual([], failures)
        self.assertEqual([b'current{camera="current"} 1\n'], successes)
        self.assertEqual(11, len(busy))


if __name__ == "__main__":
    unittest.main()
