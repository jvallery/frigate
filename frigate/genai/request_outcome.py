"""Thread-local outcomes for one Frigate description-provider attempt.

The OpenAI adapter and the bounded description worker execute on the same
worker thread.  This tiny helper lets the adapter distinguish an HTTP-success
response with no usable content from timeouts and provider failures without
changing Frigate's existing ``str | None`` provider interface.
"""

from __future__ import annotations

import threading
from enum import StrEnum


class AttemptOutcome(StrEnum):
    """Privacy-safe result classes for one description attempt."""

    success = "success"
    empty = "empty"
    timeout = "timeout"
    provider_error = "provider_error"
    provider_unavailable = "provider_unavailable"
    invalid_response = "invalid_response"
    persistence_error = "persistence_error"
    invalid_input = "invalid_input"
    internal_error = "internal_error"


_state = threading.local()


def reset_outcome() -> None:
    """Clear the outcome recorded on the current thread."""

    _state.outcome = None


def set_outcome(outcome: AttemptOutcome) -> None:
    """Record ``outcome`` for the current thread."""

    if not isinstance(outcome, AttemptOutcome):
        raise TypeError("outcome must be an AttemptOutcome")
    _state.outcome = outcome


def consume_outcome(
    default: AttemptOutcome = AttemptOutcome.provider_error,
) -> AttemptOutcome:
    """Return and clear the current thread's outcome.

    ``default`` deliberately represents a provider failure: a caller that
    forgot to record an outcome must fail closed instead of counting success.
    """

    if not isinstance(default, AttemptOutcome):
        raise TypeError("default must be an AttemptOutcome")

    outcome = getattr(_state, "outcome", None)
    _state.outcome = None
    return outcome if isinstance(outcome, AttemptOutcome) else default
