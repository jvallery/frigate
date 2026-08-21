"""Regression tests for bounded ArcFace GPU retries."""

from __future__ import annotations

import importlib.util
import logging
import threading
import time
import unittest
from pathlib import Path


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).parents[1]
retry = load_module(
    "arcface_gpu_retry",
    ROOT / "embeddings" / "onnx" / "arcface_gpu_retry.py",
)


class ArcFaceCudaProviderTest(unittest.TestCase):
    def test_keeps_only_the_matched_cuda_provider_option_pair(self) -> None:
        cuda_options = {"device_id": 0, "use_ep_level_unified_stream": True}
        providers = ["CPUExecutionProvider", "CUDAExecutionProvider", "OtherEP"]
        options = [{"cpu": True}, cuda_options, {"other": True}]

        selected_providers, selected_options = retry.require_arcface_cuda_provider(
            providers, options
        )

        self.assertEqual(["CUDAExecutionProvider"], selected_providers)
        self.assertEqual([cuda_options], selected_options)
        self.assertIs(cuda_options, selected_options[0])
        self.assertEqual(
            ["CPUExecutionProvider", "CUDAExecutionProvider", "OtherEP"],
            providers,
        )

    def test_fails_when_cuda_is_absent_or_provider_options_are_unpaired(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "CPU fallback is disabled"):
            retry.require_arcface_cuda_provider(
                ["CPUExecutionProvider"], [{"cpu": True}]
            )

        with self.assertRaisesRegex(RuntimeError, "options are inconsistent"):
            retry.require_arcface_cuda_provider(
                ["CUDAExecutionProvider", "CPUExecutionProvider"],
                [{"device_id": 0}],
            )


class ArcFaceRetryControllerTest(unittest.TestCase):
    def test_succeeds_on_third_attempt_with_reset_and_delay_between_failures(
        self,
    ) -> None:
        attempts = 0
        resets = 0
        delays: list[float] = []

        def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError(
                    "CUDA failure 901: provider detail must not be logged"
                )
            return "ready"

        def reset_runner() -> None:
            nonlocal resets
            resets += 1

        controller = retry.ArcFaceRetryController(
            delay_seconds=0.25,
            sleep=delays.append,
            logger=logging.getLogger("arcface-retry-success-test"),
        )

        with self.assertLogs("arcface-retry-success-test", level="WARNING"):
            result = controller.run(operation, reset_runner)

        self.assertEqual("ready", result)
        self.assertEqual(3, attempts)
        self.assertEqual(2, resets)
        self.assertEqual([0.25, 0.25], delays)

    def test_exhausts_after_three_attempts_without_a_final_delay(self) -> None:
        attempts = 0
        resets = 0
        delays: list[float] = []
        log_name = "arcface-retry-exhaustion-test"

        def operation() -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("CUDA status 901: sensitive provider detail")

        def reset_runner() -> None:
            nonlocal resets
            resets += 1

        controller = retry.ArcFaceRetryController(
            delay_seconds=0.5,
            sleep=delays.append,
            logger=logging.getLogger(log_name),
        )

        with self.assertLogs(log_name, level="WARNING") as captured:
            with self.assertRaises(retry.ArcFaceRetryExhausted) as raised:
                controller.run(operation, reset_runner)

        self.assertEqual(3, attempts)
        self.assertEqual(3, resets)
        self.assertEqual([0.5, 0.5], delays)
        self.assertEqual("RuntimeError", raised.exception.error_type)
        self.assertEqual(3, raised.exception.attempts)
        rendered = "\n".join(captured.output) + str(raised.exception)
        self.assertIn("RuntimeError", rendered)
        self.assertNotIn("sensitive provider detail", rendered)

        with self.assertRaises(retry.ArcFaceRetryExhausted):
            controller.run(operation, reset_runner)
        self.assertEqual(3, attempts)
        self.assertEqual(3, resets)
        self.assertEqual([0.5, 0.5], delays)

        controller.reset_exhaustion()
        self.assertEqual("recovered", controller.run(lambda: "recovered", reset_runner))

    def test_reset_errors_are_redacted_and_do_not_expand_attempt_budget(self) -> None:
        attempts = 0
        log_name = "arcface-reset-error-test"

        def operation() -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("stream capture failed: operation detail")

        def reset_runner() -> None:
            raise OSError("reset detail")

        controller = retry.ArcFaceRetryController(
            attempts=2,
            delay_seconds=0,
            sleep=lambda _delay: None,
            logger=logging.getLogger(log_name),
        )

        with self.assertLogs(log_name, level="WARNING") as captured:
            with self.assertRaises(retry.ArcFaceRetryExhausted):
                controller.run(operation, reset_runner)

        self.assertEqual(2, attempts)
        rendered = "\n".join(captured.output)
        self.assertIn("OSError", rendered)
        self.assertNotIn("operation detail", rendered)
        self.assertNotIn("reset detail", rendered)

    def test_does_not_retry_non_gpu_or_deterministic_errors(self) -> None:
        attempts = 0
        resets = 0
        delays: list[float] = []

        def operation() -> None:
            nonlocal attempts
            attempts += 1
            raise ValueError("invalid deterministic input")

        def reset_runner() -> None:
            nonlocal resets
            resets += 1

        controller = retry.ArcFaceRetryController(
            delay_seconds=0.25,
            sleep=delays.append,
        )

        with self.assertRaisesRegex(ValueError, "invalid deterministic input"):
            controller.run(operation, reset_runner)

        self.assertEqual(1, attempts)
        self.assertEqual(0, resets)
        self.assertEqual([], delays)

    def test_recognizes_onnx_runtime_stream_capture_exception(self) -> None:
        ort_runtime_exception = type(
            "RuntimeException",
            (Exception,),
            {"__module__": "onnxruntime.capi.onnxruntime_pybind11_state"},
        )
        attempts = 0
        resets = 0

        def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ort_runtime_exception("stream is capturing")
            return "ready"

        def reset_runner() -> None:
            nonlocal resets
            resets += 1

        controller = retry.ArcFaceRetryController(
            delay_seconds=0,
            sleep=lambda _delay: None,
            logger=logging.getLogger("arcface-ort-retry-test"),
        )

        with self.assertLogs("arcface-ort-retry-test", level="WARNING"):
            result = controller.run(operation, reset_runner)

        self.assertEqual("ready", result)
        self.assertEqual(2, attempts)
        self.assertEqual(1, resets)

    def test_serializes_concurrent_operations(self) -> None:
        controller = retry.ArcFaceRetryController(
            attempts=1,
            delay_seconds=0,
            sleep=lambda _delay: None,
        )
        active = 0
        maximum_active = 0
        state_lock = threading.Lock()
        results: list[int] = []

        def operation() -> int:
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.01)
            with state_lock:
                active -= 1
            return 1

        def invoke() -> None:
            results.append(controller.run(operation, lambda: None))

        threads = [threading.Thread(target=invoke) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([1, 1, 1, 1], results)
        self.assertEqual(1, maximum_active)


if __name__ == "__main__":
    unittest.main()
