"""Bound and serialize ArcFace GPU session creation and inference.

It deliberately reports exception *types* only:
provider exceptions can include host paths, model paths, or runtime details
that do not belong in production logs.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import TypeVar

MAX_ARCFACE_GPU_ATTEMPTS = 3
ARCFACE_GPU_RETRY_DELAY_SECONDS = 1.0
_CUDA_TRANSIENT_MARKERS = (
    "status 901",
    "cuda error 901",
    "cuda failure 901",
    "cudaerrorstreamcapture",
    "stream capture",
    "stream is capturing",
)
_ORT_TRANSIENT_ERROR_TYPES = frozenset({"RuntimeException", "EPFail"})
CUDA_EXECUTION_PROVIDER = "CUDAExecutionProvider"

Result = TypeVar("Result")


def require_arcface_cuda_provider(
    providers: list[str],
    options: list[dict],
) -> tuple[list[str], list[dict]]:
    """Return the matched CUDA provider pair or fail without a CPU fallback."""
    if len(providers) != len(options):
        raise RuntimeError("ArcFace execution-provider options are inconsistent")

    try:
        cuda_index = providers.index(CUDA_EXECUTION_PROVIDER)
    except ValueError:
        raise RuntimeError(
            "ArcFace requires CUDAExecutionProvider; CPU fallback is disabled"
        ) from None

    return ([providers[cuda_index]], [options[cuda_index]])


def is_retryable_arcface_gpu_error(error: Exception) -> bool:
    """Recognize only the observed CUDA stream-capture/status-901 failure family."""
    error_type = type(error)
    is_supported_type = isinstance(error, RuntimeError) or (
        error_type.__module__.startswith("onnxruntime.")
        and error_type.__name__ in _ORT_TRANSIENT_ERROR_TYPES
    )
    if not is_supported_type:
        return False

    details: list[str] = []
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        details.append(str(current).lower())
        current = current.__cause__ or current.__context__

    combined = " ".join(details)
    return any(marker in combined for marker in _CUDA_TRANSIENT_MARKERS)


class ArcFaceRetryExhausted(RuntimeError):
    """Raised after the bounded ArcFace GPU attempt budget is exhausted."""

    def __init__(self, error_type: str, attempts: int) -> None:
        self.error_type = error_type
        self.attempts = attempts
        super().__init__(
            f"ArcFace GPU operation exhausted {attempts} attempts ({error_type})"
        )


class ArcFaceRetryController:
    """Serialize one ArcFace runner and retry failures a fixed number of times."""

    def __init__(
        self,
        *,
        attempts: int = MAX_ARCFACE_GPU_ATTEMPTS,
        delay_seconds: float = ARCFACE_GPU_RETRY_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
        retryable: Callable[[Exception], bool] = is_retryable_arcface_gpu_error,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least one")
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")

        self._attempts = attempts
        self._delay_seconds = delay_seconds
        self._sleep = sleep
        self._logger = logger or logging.getLogger(__name__)
        self._retryable = retryable
        self._lock = threading.Lock()
        self._exhausted_error_type: str | None = None

    def reset_exhaustion(self) -> None:
        """Explicitly re-arm the controller after an operator reset."""
        with self._lock:
            self._exhausted_error_type = None

    def run(
        self,
        operation: Callable[[], Result],
        reset_runner: Callable[[], None],
    ) -> Result:
        """Run an ArcFace operation with serialized, bounded retry semantics."""
        with self._lock:
            if self._exhausted_error_type is not None:
                raise ArcFaceRetryExhausted(
                    self._exhausted_error_type, self._attempts
                ) from None

            for attempt in range(1, self._attempts + 1):
                try:
                    return operation()
                except Exception as error:
                    if not self._retryable(error):
                        raise

                    error_type = type(error).__name__
                    reset_error_type: str | None = None
                    try:
                        reset_runner()
                    except Exception as reset_error:
                        reset_error_type = type(reset_error).__name__

                    self._logger.warning(
                        "ArcFace GPU attempt %d/%d failed (%s)",
                        attempt,
                        self._attempts,
                        error_type,
                    )
                    if reset_error_type is not None:
                        self._logger.warning(
                            "ArcFace GPU runner reset failed (%s)",
                            reset_error_type,
                        )

                    if attempt == self._attempts:
                        self._exhausted_error_type = error_type
                        raise ArcFaceRetryExhausted(
                            error_type, self._attempts
                        ) from None

                    self._sleep(self._delay_seconds)

        raise AssertionError("unreachable ArcFace retry state")
