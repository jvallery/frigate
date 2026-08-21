"""Bounded coordinator for recovering missing Frigate GenAI descriptions.

The coordinator is deliberately small and synchronous: ``tick`` performs at
most one bounded database selection and one nonblocking processor submission.
The object and review processors retain responsibility for validating camera
configuration and media availability before accepting work.

Historical incident recovery is disabled unless it has an explicit, finite
time window. When that recovery gate is enabled, a delayed rolling
recent-missing scan makes queue failures survive process restarts. Candidate
identifiers are kept only in bounded memory for duplicate suppression and are
never logged.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, cast

from frigate.genai.request_outcome import AttemptOutcome

LOGGER = logging.getLogger(__name__)

ENABLED_ENV = "FRIGATE_GENAI_RECOVERY_ENABLED"
START_ENV = "FRIGATE_GENAI_RECOVERY_START_EPOCH"
END_ENV = "FRIGATE_GENAI_RECOVERY_END_EPOCH"
MAX_ITEMS_ENV = "FRIGATE_GENAI_RECOVERY_MAX_ITEMS"
INTERVAL_ENV = "FRIGATE_GENAI_RECOVERY_INTERVAL_SECONDS"
INITIAL_DELAY_ENV = "FRIGATE_GENAI_RECOVERY_INITIAL_DELAY_SECONDS"

DEFAULT_MAX_ITEMS = 512
DEFAULT_INTERVAL_SECONDS = 15.0
DEFAULT_INITIAL_DELAY_SECONDS = 180.0
DEFAULT_SCAN_LIMIT = 512
MAX_LIVE_FAILURES = 512
MAX_COMPLETED_TOMBSTONES = 512
MAX_TRACKED_RETRIES = 512
MAX_ADMISSIONS_PER_CANDIDATE = 2
MAX_SUBMISSION_RETRIES = 3
RECOVERY_RETRY_DELAY_SECONDS = 300.0
RECOVERY_LOOKBACK_SECONDS = 604_800.0
RECENT_SCAN_GRACE_SECONDS = 300.0


class ConditionalWriteResult(StrEnum):
    """Outcome of a recovery-only conditional JSON merge."""

    written = "written"
    already_present = "already_present"
    failed = "failed"


class RecoveryAdmission(StrEnum):
    """Outcome of asking a processor to admit one recovery candidate."""

    accepted = "accepted"
    retry_later = "retry_later"
    terminal_skip = "terminal_skip"


class RecoveryProcessor(Protocol):
    """Nonblocking processor admission contract."""

    def submit_recovery(
        self,
        model: Any,
        on_complete: Callable[[AttemptOutcome], None],
    ) -> RecoveryAdmission:
        """Classify whether the candidate was accepted, deferred, or skipped."""


ScanCursor = tuple[float, str]
CandidateSource = Callable[[float, float, int, ScanCursor | None], Iterable[Any]]


def dispatcher_write_succeeded(
    response: Any,
    *,
    allow_already_present: bool = False,
) -> bool:
    """Accept only an explicit typed persistence acknowledgement."""

    if not isinstance(response, Mapping):
        return False
    allowed = {ConditionalWriteResult.written.value}
    if allow_already_present:
        allowed.add(ConditionalWriteResult.already_present.value)
    return response.get("status") in allowed


def _parse_enabled(value: bool | str | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _as_epoch(value: Any) -> float | None:
    if isinstance(value, datetime):
        value = value.timestamp()
    return _parse_float(value)


def _model_data(model: Any) -> Mapping[str, Any] | None:
    data = getattr(model, "data", None)
    return data if isinstance(data, Mapping) else None


def _missing_object_description(model: Any) -> bool:
    data = _model_data(model)
    if data is None:
        return False
    description = data.get("description")
    return description is None or (
        isinstance(description, str) and not description.strip()
    )


def _missing_review_metadata(model: Any) -> bool:
    data = _model_data(model)
    if data is None:
        return False
    metadata = data.get("metadata")
    return metadata is None or metadata == {}


def persist_object_description(
    event_id: str,
    description: str,
    model_class: Any | None = None,
) -> ConditionalWriteResult:
    """Conditionally fill only a still-missing object description."""

    if model_class is None:
        from frigate.models import Event

        model_class = Event
    resolved_model: Any = model_class
    table = resolved_model._meta.table_name
    cursor = resolved_model._meta.database.execute_sql(
        f'UPDATE "{table}" '
        "SET data = json_set(data, '$.description', ?) "
        "WHERE id = ? "
        "AND trim(COALESCE(json_extract(data, '$.description'), '')) = ''",
        (description, event_id),
    )
    if cursor.rowcount == 1:
        return ConditionalWriteResult.written
    fresh = resolved_model.get_by_id(event_id)
    data = fresh.data if isinstance(fresh.data, Mapping) else {}
    if (data.get("description") or "").strip():
        return ConditionalWriteResult.already_present
    return ConditionalWriteResult.failed


def persist_review_metadata(
    review_id: str,
    metadata: Mapping[str, Any],
    model_class: Any | None = None,
) -> ConditionalWriteResult:
    """Merge only missing review metadata into the freshest JSON row."""

    import json

    if model_class is None:
        from frigate.models import ReviewSegment

        model_class = ReviewSegment
    resolved_model: Any = model_class
    table = resolved_model._meta.table_name
    encoded = json.dumps(dict(metadata), separators=(",", ":"))
    cursor = resolved_model._meta.database.execute_sql(
        f'UPDATE "{table}" '
        "SET data = json_set(data, '$.metadata', json(?)) "
        "WHERE id = ? "
        "AND (json_type(data, '$.metadata') IS NULL "
        "OR json_type(data, '$.metadata') = 'null' "
        "OR json_extract(data, '$.metadata') = json('{}'))",
        (encoded, review_id),
    )
    if cursor.rowcount == 1:
        return ConditionalWriteResult.written
    fresh = resolved_model.get_by_id(review_id)
    data = fresh.data if isinstance(fresh.data, Mapping) else {}
    if data.get("metadata"):
        return ConditionalWriteResult.already_present
    return ConditionalWriteResult.failed


def default_object_candidates(
    start: float,
    end: float,
    limit: int,
    after: ScanCursor | None = None,
) -> Iterable[Any]:
    """Select a deterministic, fixed-size set of missing object descriptions."""
    from peewee import fn

    from frigate.models import Event

    description = fn.json_extract(Event.data, "$.description")
    predicates = [
        Event.end_time.is_null(False),
        Event.end_time >= start,
        Event.end_time <= end,
        fn.TRIM(fn.COALESCE(description, "")) == "",
    ]
    if after is not None:
        after_end, after_id = after
        predicates.append(
            (Event.end_time > after_end)
            | ((Event.end_time == after_end) & (Event.id > after_id))
        )
    query: Any = (
        Event.select()
        .where(*predicates)
        .order_by(Event.end_time, Event.id)
        .limit(limit)
    )
    return cast(Iterable[Any], query)


def default_review_candidates(
    start: float,
    end: float,
    limit: int,
    after: ScanCursor | None = None,
) -> Iterable[Any]:
    """Select a deterministic, fixed-size set of missing review metadata."""
    from peewee import fn

    from frigate.models import ReviewSegment

    metadata = fn.json_extract(ReviewSegment.data, "$.metadata")
    metadata_type = fn.json_type(ReviewSegment.data, "$.metadata")
    predicates = [
        ReviewSegment.end_time.is_null(False),
        ReviewSegment.end_time >= start,
        ReviewSegment.end_time <= end,
        metadata_type.is_null(True)
        | (metadata_type == "null")
        | (metadata == fn.json("{}")),
    ]
    if after is not None:
        after_end, after_id = after
        predicates.append(
            (ReviewSegment.end_time > after_end)
            | ((ReviewSegment.end_time == after_end) & (ReviewSegment.id > after_id))
        )
    query: Any = (
        ReviewSegment.select()
        .where(*predicates)
        .order_by(ReviewSegment.end_time, ReviewSegment.id)
        .limit(limit)
    )
    return cast(Iterable[Any], query)


class DescriptionRecoveryCoordinator:
    """Admit missing-description recovery work at a bounded rate.

    ``tick`` is intended to be called from Frigate's maintainer loop. It never
    waits for inference and admits at most one candidate per interval. Only
    accepted jobs consume the fixed inference budget. Backpressure is retried,
    while invalid or ineligible rows are terminally skipped.
    """

    def __init__(
        self,
        config: Any,
        object_processor: RecoveryProcessor | None,
        review_processor: RecoveryProcessor | None,
        enabled: bool | str | None = None,
        start: float | str | None = None,
        end: float | str | None = None,
        max_items: int | str | None = None,
        interval: float | str | None = None,
        initial_delay: float | str | None = None,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        object_candidates: CandidateSource | None = None,
        review_candidates: CandidateSource | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.object_processor = object_processor
        self.review_processor = review_processor
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self._object_candidates = object_candidates or default_object_candidates
        self._review_candidates = review_candidates or default_review_candidates
        self._logger = logger or LOGGER

        enabled_value = os.getenv(ENABLED_ENV, "false") if enabled is None else enabled
        start_value = os.getenv(START_ENV) if start is None else start
        end_value = os.getenv(END_ENV) if end is None else end
        max_items_value = (
            os.getenv(MAX_ITEMS_ENV, str(DEFAULT_MAX_ITEMS))
            if max_items is None
            else max_items
        )
        interval_value = (
            os.getenv(INTERVAL_ENV, str(DEFAULT_INTERVAL_SECONDS))
            if interval is None
            else interval
        )
        initial_delay_value = (
            os.getenv(INITIAL_DELAY_ENV, str(DEFAULT_INITIAL_DELAY_SECONDS))
            if initial_delay is None
            else initial_delay
        )

        requested_enabled = _parse_enabled(enabled_value)
        parsed_start = _parse_float(start_value)
        parsed_end = _parse_float(end_value)
        parsed_max_items = _parse_positive_int(max_items_value)
        parsed_interval = _parse_float(interval_value)
        parsed_initial_delay = _parse_float(initial_delay_value)

        configured_window_valid = (
            parsed_start is not None
            and parsed_end is not None
            and parsed_start >= 0
            and parsed_start < parsed_end
        )
        valid_limits = (
            parsed_max_items is not None
            and parsed_interval is not None
            and parsed_interval > 0
            and parsed_initial_delay is not None
            and parsed_initial_delay >= 0
        )

        # The configured incident cutoff is immutable across process restarts. A
        # future cutoff is invalid instead of being silently advanced to each
        # process start. Ongoing repair uses the separate rolling recent scan.
        effective_end = (
            parsed_end
            if configured_window_valid
            and parsed_end is not None
            and parsed_end <= self._wall_clock()
            else None
        )
        effective_start = (
            max(parsed_start, effective_end - RECOVERY_LOOKBACK_SECONDS)
            if parsed_start is not None and effective_end is not None
            else None
        )
        effective_window_valid = (
            effective_start is not None
            and effective_end is not None
            and effective_start < effective_end
        )
        self.enabled = requested_enabled and effective_window_valid and valid_limits
        self.start = effective_start
        # The exact configured end remains frozen across every restart.
        self.end = effective_end
        self.max_items = parsed_max_items or DEFAULT_MAX_ITEMS
        self.interval = parsed_interval or DEFAULT_INTERVAL_SECONDS
        self.initial_delay = (
            parsed_initial_delay
            if parsed_initial_delay is not None
            else DEFAULT_INITIAL_DELAY_SECONDS
        )

        if requested_enabled and not self.enabled:
            self._logger.warning(
                "GenAI description recovery disabled because its frozen window or limits are invalid"
            )

        self._state_lock = threading.RLock()
        self._completed: dict[tuple[str, Any], None] = {}
        self._inflight: set[tuple[str, Any]] = set()
        self._retry_after: dict[tuple[str, Any], float] = {}
        self._admissions: dict[tuple[str, Any], int] = {}
        self._submission_retries: dict[tuple[str, Any], int] = {}
        self._tracking_order: dict[tuple[str, Any], None] = {}
        self._live_failures: dict[tuple[str, Any], None] = {}
        self._scan_cursors: dict[tuple[str, str], ScanCursor] = {}
        self._attempted_count = 0
        self._next_surface = "object"
        self._stopped = False
        self._next_tick_at: float | None = self._clock() + self.initial_delay

    @property
    def attempted_count(self) -> int:
        """Number of jobs admitted from the process-local inference budget."""
        with self._state_lock:
            return self._attempted_count

    def _candidate_key(self, surface: str, model: Any) -> tuple[str, Any]:
        model_id = getattr(model, "id", None)
        try:
            hash(model_id)
        except TypeError:
            model_id = None
        if model_id is None:
            model_id = ("anonymous", id(model))
        return (surface, model_id)

    def _candidate_valid(self, surface: str, model: Any) -> bool:
        if self.start is None or self.end is None:
            return False
        return self._candidate_valid_in_window(surface, model, self.start, self.end)

    def _candidate_valid_in_window(
        self,
        surface: str,
        model: Any,
        start: float,
        end: float,
    ) -> bool:
        ended = _as_epoch(getattr(model, "end_time", None))
        if ended is None or ended < start or ended > end:
            return False
        return self._candidate_missing(surface, model)

    @staticmethod
    def _candidate_missing(surface: str, model: Any) -> bool:
        ended = _as_epoch(getattr(model, "end_time", None))
        if ended is None:
            return False
        if surface == "object":
            return _missing_object_description(model)
        return _missing_review_metadata(model)

    def _clear_tracking(self, key: tuple[str, Any]) -> None:
        """Drop all retry state for one candidate while holding the state lock."""

        self._inflight.discard(key)
        self._retry_after.pop(key, None)
        self._admissions.pop(key, None)
        self._submission_retries.pop(key, None)
        self._tracking_order.pop(key, None)
        self._live_failures.pop(key, None)

    def _touch_tracking(self, key: tuple[str, Any]) -> None:
        """Bound retry/admission maps without evicting queued or live IDs."""

        self._tracking_order.pop(key, None)
        self._tracking_order[key] = None
        while len(self._tracking_order) > MAX_TRACKED_RETRIES:
            evicted: tuple[str, Any] | None = None
            for candidate in tuple(self._tracking_order):
                if (
                    candidate not in self._inflight
                    and candidate not in self._live_failures
                ):
                    evicted = candidate
                    break
            if evicted is None:
                return
            self._tracking_order.pop(evicted, None)
            self._retry_after.pop(evicted, None)
            self._admissions.pop(evicted, None)
            self._submission_retries.pop(evicted, None)

    def _mark_completed(self, key: tuple[str, Any]) -> None:
        """Keep a bounded process-local tombstone for stale query results."""

        self._clear_tracking(key)
        self._completed.pop(key, None)
        self._completed[key] = None
        while len(self._completed) > MAX_COMPLETED_TOMBSTONES:
            self._completed.pop(next(iter(self._completed)), None)

    def _remember_live_failure(self, key: tuple[str, Any]) -> bool:
        """Insert one live ID without evicting accepted recovery work."""

        if key in self._live_failures:
            self._live_failures.pop(key, None)
            self._live_failures[key] = None
            return False
        if len(self._live_failures) >= MAX_LIVE_FAILURES:
            evicted: tuple[str, Any] | None = None
            for candidate in tuple(self._live_failures):
                if candidate not in self._inflight:
                    evicted = candidate
                    break
            if evicted is None:
                self._logger.warning(
                    "GenAI live recovery registry is full with active work"
                )
                return False
            self._live_failures.pop(evicted, None)
            self._retry_after.pop(evicted, None)
            self._admissions.pop(evicted, None)
            self._submission_retries.pop(evicted, None)
            self._logger.warning(
                "GenAI live recovery registry evicted its oldest item surface=%s",
                evicted[0],
            )
        self._live_failures[key] = None
        return True

    def note_live_failure(self, surface: str, model_id: Any) -> bool:
        """Remember one failed live item by ID for bounded deferred repair."""

        if surface not in ("object", "review") or model_id is None:
            return False
        try:
            hash(model_id)
        except TypeError:
            return False

        key = (surface, model_id)
        with self._state_lock:
            if key in self._live_failures:
                return False
            # A fresh live failure supersedes an old bounded tombstone.
            self._completed.pop(key, None)
            return self._remember_live_failure(key)

    @staticmethod
    def _load_live_model(surface: str, model_id: Any) -> Any:
        if surface == "object":
            from frigate.models import Event

            return Event.get_by_id(model_id)
        from frigate.models import ReviewSegment

        return ReviewSegment.get_by_id(model_id)

    def _next_live_candidate(
        self, now: float
    ) -> tuple[str, Any, tuple[str, Any]] | None:
        with self._state_lock:
            keys = tuple(self._live_failures)

        for key in keys:
            surface, model_id = key
            with self._state_lock:
                if key not in self._live_failures:
                    continue
                if key in self._completed:
                    self._live_failures.pop(key, None)
                    continue
                retry_at = self._retry_after.get(key)
                if retry_at is not None and now < retry_at:
                    continue
                if self._admissions.get(key, 0) >= MAX_ADMISSIONS_PER_CANDIDATE:
                    # Live failures remain discoverable after bounded attempts.
                    # Reset the per-round admission count and defer the next
                    # round instead of permanently abandoning the database ID.
                    self._admissions.pop(key, None)
                    self._retry_after[key] = now + RECOVERY_RETRY_DELAY_SECONDS
                    self._touch_tracking(key)
                    self._live_failures.pop(key, None)
                    self._live_failures[key] = None
                    continue
            try:
                model = self._load_live_model(surface, model_id)
            except Exception as error:
                self._logger.warning(
                    "GenAI live recovery lookup failed for %s (%s)",
                    surface,
                    type(error).__name__,
                )
                with self._state_lock:
                    if type(error).__name__ == "DoesNotExist":
                        self._mark_completed(key)
                    else:
                        self._retry_after[key] = (
                            self._clock() + RECOVERY_RETRY_DELAY_SECONDS
                        )
                        self._touch_tracking(key)
                        self._live_failures.pop(key, None)
                        self._live_failures[key] = None
                continue
            return (surface, model, key)
        return None

    def _source_for(self, surface: str) -> CandidateSource:
        if surface == "object":
            return self._object_candidates
        return self._review_candidates

    def _processor_for(self, surface: str) -> RecoveryProcessor | None:
        if surface == "object":
            return self.object_processor
        return self.review_processor

    def _next_candidate(
        self,
        surface: str,
        now: float,
        start: float,
        end: float,
        *,
        origin: str,
        retry_after_exhaustion: bool,
    ) -> tuple[Any, tuple[str, Any]] | None:
        if start >= end:
            return None

        try:
            cursor_key = (origin, surface)
            with self._state_lock:
                cursor = self._scan_cursors.get(cursor_key)
            candidates = list(
                self._source_for(surface)(
                    start,
                    end,
                    DEFAULT_SCAN_LIMIT,
                    cursor,
                )
            )
            if not candidates and cursor is not None:
                with self._state_lock:
                    self._scan_cursors.pop(cursor_key, None)
                candidates = list(
                    self._source_for(surface)(
                        start,
                        end,
                        DEFAULT_SCAN_LIMIT,
                        None,
                    )
                )
            for model in candidates:
                ended = _as_epoch(getattr(model, "end_time", None))
                if ended is None or ended < start or ended > end:
                    continue
                model_id = getattr(model, "id", None)
                if model_id is None:
                    continue
                key = self._candidate_key(surface, model)
                with self._state_lock:
                    self._scan_cursors[cursor_key] = (ended, str(model_id))
                    if key in self._completed or key in self._inflight:
                        continue
                    if not self._candidate_missing(surface, model):
                        self._mark_completed(key)
                        continue
                    retry_at = self._retry_after.get(key)
                    if retry_at is not None and now < retry_at:
                        continue
                    if self._admissions.get(key, 0) >= MAX_ADMISSIONS_PER_CANDIDATE:
                        if retry_after_exhaustion:
                            self._admissions.pop(key, None)
                            self._retry_after[key] = now + RECOVERY_RETRY_DELAY_SECONDS
                            self._touch_tracking(key)
                        else:
                            self._mark_completed(key)
                        continue
                return (model, key)
        except Exception as error:
            self._logger.warning(
                "GenAI recovery candidate selection failed for %s (%s)",
                surface,
                type(error).__name__,
            )
        return None

    def _recent_window(self) -> tuple[float, float] | None:
        """Return the rolling durable-repair window behind a media grace period."""

        end = self._wall_clock() - RECENT_SCAN_GRACE_SECONDS
        if end <= 0:
            return None
        if self.end is None:
            return None
        start = max(
            0.0,
            end - RECOVERY_LOOKBACK_SECONDS,
            math.nextafter(self.end, math.inf),
        )
        return (start, end) if start < end else None

    def tick(self) -> bool:
        """Attempt one nonblocking recovery admission.

        Returns ``True`` only when a processor accepts the selected candidate.
        """
        with self._state_lock:
            budget_exhausted = self._attempted_count >= self.max_items
            recovery_inflight = bool(self._inflight)
            stopped = self._stopped
        if stopped or recovery_inflight or not self.enabled or budget_exhausted:
            return False

        now = self._clock()
        if self._next_tick_at is not None and now < self._next_tick_at:
            return False
        self._next_tick_at = now + self.interval

        live_selected = self._next_live_candidate(now)
        origin = "live" if live_selected is not None else ""
        selected_surface: str | None
        recent_window: tuple[float, float] | None = None
        if live_selected is not None:
            selected_surface, model, key = live_selected
        else:
            preferred = self._next_surface
            surfaces = (preferred, "review" if preferred == "object" else "object")

            selected_surface = None
            selected: tuple[Any, tuple[str, Any]] | None = None
            recent_window = self._recent_window()
            if recent_window is not None:
                recent_start, recent_end = recent_window
                for surface in surfaces:
                    selected = self._next_candidate(
                        surface,
                        now,
                        recent_start,
                        recent_end,
                        origin="recent",
                        retry_after_exhaustion=True,
                    )
                    if selected is not None:
                        selected_surface = surface
                        origin = "recent"
                        break

            if (
                selected is None
                and self.enabled
                and not budget_exhausted
                and self.start is not None
                and self.end is not None
            ):
                for surface in surfaces:
                    selected = self._next_candidate(
                        surface,
                        now,
                        self.start,
                        self.end,
                        origin="historical",
                        retry_after_exhaustion=False,
                    )
                    if selected is not None:
                        selected_surface = surface
                        origin = "historical"
                        break

            if selected is None or selected_surface is None:
                return False

            model, key = selected
            self._next_surface = "review" if selected_surface == "object" else "object"

        if origin == "live":
            valid_candidate = self._candidate_missing(selected_surface, model)
        elif origin == "recent":
            assert recent_window is not None
            valid_candidate = self._candidate_valid_in_window(
                selected_surface,
                model,
                recent_window[0],
                recent_window[1],
            )
        else:
            valid_candidate = self._candidate_valid(selected_surface, model)
        if not valid_candidate:
            with self._state_lock:
                self._mark_completed(key)
            return False

        processor = self._processor_for(selected_surface)
        if processor is None:
            return False

        with self._state_lock:
            self._inflight.add(key)
            self._admissions[key] = self._admissions.get(key, 0) + 1
            self._touch_tracking(key)
            self._attempted_count += 1

        def on_complete(outcome: AttemptOutcome) -> None:
            with self._state_lock:
                self._inflight.discard(key)
                if outcome == AttemptOutcome.success:
                    self._mark_completed(key)
                else:
                    self._retry_after[key] = (
                        self._clock() + RECOVERY_RETRY_DELAY_SECONDS
                    )
                    self._touch_tracking(key)
                    if origin == "live":
                        self._remember_live_failure(key)

        try:
            admission = processor.submit_recovery(
                model,
                on_complete,
            )
        except Exception as error:
            self._logger.warning(
                "GenAI recovery submission failed for %s (%s)",
                selected_surface,
                type(error).__name__,
            )
            admission = RecoveryAdmission.retry_later

        if admission == RecoveryAdmission.accepted:
            with self._state_lock:
                self._submission_retries.pop(key, None)
            return True

        # Nothing entered the queue, so release the provisional reservation
        # and do not consume the inference budget.
        with self._state_lock:
            self._inflight.discard(key)
            self._attempted_count = max(0, self._attempted_count - 1)
            admissions = self._admissions.get(key, 0) - 1
            if admissions > 0:
                self._admissions[key] = admissions
                self._touch_tracking(key)
            else:
                self._admissions.pop(key, None)

        if admission == RecoveryAdmission.terminal_skip:
            with self._state_lock:
                self._mark_completed(key)
            return False

        with self._state_lock:
            retries = self._submission_retries.get(key, 0) + 1
            self._submission_retries[key] = retries
            self._touch_tracking(key)
            if retries >= MAX_SUBMISSION_RETRIES:
                self._submission_retries[key] = 0
                self._retry_after[key] = self._clock() + RECOVERY_RETRY_DELAY_SECONDS
                self._touch_tracking(key)
                if origin == "live" and key in self._live_failures:
                    self._live_failures.pop(key, None)
                    self._live_failures[key] = None
        return False

    def stop(self) -> None:
        """Disable future admissions during maintainer shutdown."""

        with self._state_lock:
            self.enabled = False
            self._stopped = True
