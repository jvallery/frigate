"""Serialize mutation and rendering of the process-global metrics collector."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from typing import Any, NamedTuple


class MetricsBusy(RuntimeError):
    """Raised when another request is using the process-global collector."""


class MetricsPayload(NamedTuple):
    """Rendered Prometheus payload."""

    content: bytes
    content_type: str


class MetricsEndpointGuard:
    """Admit one scrape without queuing API worker threads behind it."""

    def __init__(self) -> None:
        self._render_lock = threading.Lock()

    def render(
        self,
        *,
        stats: dict[str, Any],
        load_event_counts: Callable[[], Iterable[Mapping[str, Any]]],
        collector: Any,
        render_metrics: Callable[[], tuple[bytes, str]],
    ) -> MetricsPayload:
        """Materialize, publish, and render one coherent collector snapshot."""
        if not self._render_lock.acquire(blocking=False):
            raise MetricsBusy("metrics render already in progress")

        try:
            event_counts = [dict(row) for row in load_event_counts()]
            collector.complete_stats = stats.copy()
            collector.process_stats = stats.copy()
            collector.all_events = event_counts
            content, content_type = render_metrics()
            return MetricsPayload(content, content_type)
        finally:
            self._render_lock.release()
