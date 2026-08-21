"""Bound automatic Frigate object and review description work.

One queue and one worker are intentionally shared by both description
surfaces.  The queue bounds compressed media retained while waiting and the
single worker keeps the description provider below Cortex's route-wide
concurrency ceiling.  Jobs and logs carry only a fixed surface name; event,
camera, label, prompt, response, and media data remain inside caller-owned
closures.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Any

from frigate.genai.request_outcome import AttemptOutcome

logger = logging.getLogger(__name__)

SURFACES = ("object", "review")
MAX_ATTEMPTS = 3
RETRY_DELAYS = (1.0, 2.0)
PERSISTENCE_TIMEOUT_MS = 5_000

_SURFACE_METRICS = (
    "queued",
    "attempted",
    "successful",
    "empty",
    "timeout",
    "provider_error",
    "provider_unavailable",
    "invalid_response",
    "persistence_error",
    "invalid_input",
    "internal_error",
    "retried",
    "rejected",
    "failed",
    "cancelled",
    "pending",
    "active",
)
_GLOBAL_METRICS = ("queue_capacity", "active_limit", "pending", "active")
METRIC_KEYS = _GLOBAL_METRICS + tuple(
    f"{surface}_{metric}" for surface in SURFACES for metric in _SURFACE_METRICS
)

_OUTCOME_METRICS = {
    AttemptOutcome.success: "successful",
    AttemptOutcome.empty: "empty",
    AttemptOutcome.timeout: "timeout",
    AttemptOutcome.provider_error: "provider_error",
    AttemptOutcome.provider_unavailable: "provider_unavailable",
    AttemptOutcome.invalid_response: "invalid_response",
    AttemptOutcome.persistence_error: "persistence_error",
    AttemptOutcome.invalid_input: "invalid_input",
    AttemptOutcome.internal_error: "internal_error",
}
_RETRYABLE_OUTCOMES = frozenset(
    {
        AttemptOutcome.empty,
        AttemptOutcome.timeout,
        AttemptOutcome.provider_error,
        AttemptOutcome.provider_unavailable,
        AttemptOutcome.invalid_response,
        AttemptOutcome.persistence_error,
        AttemptOutcome.internal_error,
    }
)


def _default_requestor_factory() -> Any:
    """Create the ZMQ requestor lazily inside the worker thread."""

    import zmq

    from frigate.comms.inter_process import InterProcessRequestor

    requestor = InterProcessRequestor()
    requestor.socket.setsockopt(zmq.SNDTIMEO, PERSISTENCE_TIMEOUT_MS)
    requestor.socket.setsockopt(zmq.RCVTIMEO, PERSISTENCE_TIMEOUT_MS)
    return requestor


@dataclass(frozen=True, slots=True)
class DescriptionJob:
    """One object or review attempt closure admitted to the shared queue."""

    surface: str
    attempt: Callable[[Any], AttemptOutcome]
    on_complete: Callable[[AttemptOutcome], None] | None = None


class DescriptionWorkQueue:
    """A bounded, retrying queue with exactly one daemon worker."""

    def __init__(
        self,
        *,
        maxsize: int = 10,
        metrics: MutableMapping[str, int] | None = None,
        requestor_factory: Callable[[], Any] | None = None,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be positive")

        self._queue: queue.Queue[DescriptionJob] = queue.Queue(maxsize=maxsize)
        self._metrics: MutableMapping[str, int] = metrics if metrics is not None else {}
        # ``multiprocessing.Manager().dict()`` exposes a DictProxy whose
        # iterator depends on private manager registry state that is absent
        # after the proxy crosses a spawned-process boundary.  Materializing
        # its public keys view works for both a normal mapping and DictProxy.
        unexpected_keys = set(self._metrics.keys()) - set(METRIC_KEYS)
        if unexpected_keys:
            raise ValueError("metrics mapping contains unsupported keys")

        for key in METRIC_KEYS:
            self._metrics[key] = 0
        self._metrics["queue_capacity"] = maxsize
        self._metrics["active_limit"] = 1

        self._requestor_factory = requestor_factory or _default_requestor_factory
        self._stop_event = threading.Event()
        self._wait = wait or self._stop_event.wait
        self._lock = threading.RLock()
        self._accepting = True
        self._stopped = False

        self._worker = threading.Thread(
            target=self._run,
            name="frigate_genai_description_worker",
            daemon=True,
        )
        self._worker.start()

    @property
    def metrics(self) -> MutableMapping[str, int]:
        """Return the fixed-key metrics mapping used by existing stats wiring."""

        return self._metrics

    @property
    def worker_alive(self) -> bool:
        """Return whether the daemon worker is still running."""

        return self._worker.is_alive()

    def submit(
        self,
        surface: str,
        attempt: Callable[[Any], AttemptOutcome],
        on_complete: Callable[[AttemptOutcome], None] | None = None,
    ) -> bool:
        """Admit one job without blocking the embeddings-maintainer loop."""

        return self._submit(surface, attempt, on_complete, recovery=False)

    def submit_recovery(
        self,
        surface: str,
        attempt: Callable[[Any], AttemptOutcome],
        on_complete: Callable[[AttemptOutcome], None] | None = None,
    ) -> bool:
        """Admit recovery only while no live description work is queued or active."""

        return self._submit(surface, attempt, on_complete, recovery=True)

    def _submit(
        self,
        surface: str,
        attempt: Callable[[Any], AttemptOutcome],
        on_complete: Callable[[AttemptOutcome], None] | None,
        *,
        recovery: bool,
    ) -> bool:
        """Validate and atomically admit one live or recovery job."""

        if surface not in SURFACES:
            raise ValueError("surface must be object or review")
        if not callable(attempt):
            raise TypeError("attempt must be callable")
        if on_complete is not None and not callable(on_complete):
            raise TypeError("on_complete must be callable")

        job = DescriptionJob(
            surface=surface,
            attempt=attempt,
            on_complete=on_complete,
        )
        reason: str | None = None
        with self._lock:
            if not self._accepting:
                self._increment(surface, "rejected")
                reason = "stopped"
            elif not self._worker.is_alive():
                self._accepting = False
                self._increment(surface, "rejected")
                reason = "worker-dead"
            elif recovery and (
                int(self._metrics["pending"]) > 0 or int(self._metrics["active"]) > 0
            ):
                self._increment(surface, "rejected")
                reason = "live-work-present"
            else:
                try:
                    self._queue.put_nowait(job)
                except queue.Full:
                    self._increment(surface, "rejected")
                    reason = "full"
                else:
                    self._increment(surface, "queued")
                    self._change_gauge(surface, "pending", 1)
                    return True

        logger.warning(
            "GenAI description queue rejected work surface=%s reason=%s",
            surface,
            reason,
        )
        return False

    def stop(self, join_timeout: float = 1.0) -> None:
        """Reject new jobs, cancel waiting jobs, and interrupt retry backoff.

        The worker is a daemon and the join is bounded, so an in-flight provider
        call cannot hold Frigate shutdown past its own process lifetime.
        """

        if join_timeout < 0:
            raise ValueError("join_timeout must be non-negative")

        with self._lock:
            if self._stopped:
                return
            self._accepting = False
            self._stopped = True
            self._stop_event.set()

        self._drain_waiting()
        self._worker.join(timeout=join_timeout)

    def _drain_waiting(self) -> None:
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                return

            with self._lock:
                self._change_gauge(job.surface, "pending", -1)
                self._increment(job.surface, "cancelled")
            self._notify_completion(job, AttemptOutcome.internal_error)
            self._queue.task_done()

    def _run(self) -> None:
        requestor: Any | None = None
        try:
            while True:
                if self._stop_event.is_set() and self._queue.empty():
                    return

                try:
                    job = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue

                with self._lock:
                    self._change_gauge(job.surface, "pending", -1)
                    if self._stop_event.is_set():
                        self._increment(job.surface, "cancelled")
                        self._notify_completion(job, AttemptOutcome.internal_error)
                        self._queue.task_done()
                        continue
                    self._change_gauge(job.surface, "active", 1)

                try:
                    requestor = self._process_job(job, requestor)
                finally:
                    with self._lock:
                        self._change_gauge(job.surface, "active", -1)
                    self._queue.task_done()
        finally:
            if requestor is not None:
                self._close_requestor(requestor)

    def _process_job(self, job: DescriptionJob, requestor: Any | None) -> Any | None:
        terminal_outcome = AttemptOutcome.provider_error

        for attempt_number in range(1, MAX_ATTEMPTS + 1):
            if self._stop_event.is_set():
                with self._lock:
                    self._increment(job.surface, "cancelled")
                self._notify_completion(job, terminal_outcome)
                return requestor

            with self._lock:
                self._increment(job.surface, "attempted")

            if requestor is None:
                try:
                    requestor = self._requestor_factory()
                except Exception:
                    outcome = AttemptOutcome.persistence_error
                else:
                    outcome = self._call_attempt(job, requestor)
            else:
                outcome = self._call_attempt(job, requestor)

            terminal_outcome = outcome
            with self._lock:
                self._increment(job.surface, _OUTCOME_METRICS[outcome])

            if outcome == AttemptOutcome.persistence_error and requestor is not None:
                self._close_requestor(requestor)
                requestor = None

            if outcome == AttemptOutcome.success:
                self._notify_completion(job, outcome)
                return requestor

            if outcome not in _RETRYABLE_OUTCOMES:
                break

            if attempt_number < MAX_ATTEMPTS:
                with self._lock:
                    self._increment(job.surface, "retried")
                if self._wait(RETRY_DELAYS[attempt_number - 1]):
                    with self._lock:
                        self._increment(job.surface, "cancelled")
                    self._notify_completion(job, terminal_outcome)
                    return requestor

        with self._lock:
            self._increment(job.surface, "failed")
        logger.warning(
            "GenAI description work failed surface=%s outcome=%s",
            job.surface,
            terminal_outcome.value,
        )
        self._notify_completion(job, terminal_outcome)
        return requestor

    @staticmethod
    def _notify_completion(job: DescriptionJob, outcome: AttemptOutcome) -> None:
        if job.on_complete is None:
            return
        try:
            job.on_complete(outcome)
        except Exception as error:
            logger.warning(
                "GenAI description completion callback failed surface=%s error=%s",
                job.surface,
                type(error).__name__,
            )

    @staticmethod
    def _close_requestor(requestor: Any) -> None:
        closer = getattr(requestor, "stop", None) or getattr(requestor, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                logger.warning("GenAI description worker requestor close failed")

    @staticmethod
    def _call_attempt(job: DescriptionJob, requestor: Any) -> AttemptOutcome:
        try:
            outcome = job.attempt(requestor)
        except Exception as error:
            logger.warning(
                "GenAI description processor failed surface=%s error=%s",
                job.surface,
                type(error).__name__,
            )
            return AttemptOutcome.internal_error
        return (
            outcome
            if isinstance(outcome, AttemptOutcome)
            else AttemptOutcome.invalid_input
        )

    def _increment(self, surface: str, metric: str) -> None:
        key = f"{surface}_{metric}"
        self._metrics[key] = int(self._metrics[key]) + 1

    def _change_gauge(self, surface: str, metric: str, amount: int) -> None:
        surface_key = f"{surface}_{metric}"
        self._metrics[surface_key] = max(0, int(self._metrics[surface_key]) + amount)
        self._metrics[metric] = max(0, int(self._metrics[metric]) + amount)


__all__ = [
    "AttemptOutcome",
    "DescriptionJob",
    "DescriptionWorkQueue",
    "MAX_ATTEMPTS",
    "METRIC_KEYS",
    "PERSISTENCE_TIMEOUT_MS",
    "RETRY_DELAYS",
    "SURFACES",
]
