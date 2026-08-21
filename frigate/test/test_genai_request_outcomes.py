"""Outcome classification tests for OpenAI-compatible descriptions."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from frigate.genai.plugins import openai
from frigate.genai.request_outcome import AttemptOutcome, consume_outcome


class OpenAIDescriptionOutcomeTest(unittest.TestCase):
    @staticmethod
    def client(response=None, error: Exception | None = None):
        def create(**_kwargs):
            if error is not None:
                raise error
            return response

        instance = object.__new__(openai.OpenAIClient)
        instance.genai_config = SimpleNamespace(
            model="model",
            runtime_options={},
            provider_options={},
        )
        instance.timeout = 1
        instance.provider = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))
        )
        return instance

    def test_classifies_empty_and_successful_responses(self) -> None:
        empty_results = (
            None,
            SimpleNamespace(choices=[]),
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=None))]
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="   "))]
            ),
        )
        for result in empty_results:
            with self.subTest(result=result):
                self.assertIsNone(self.client(result)._send("prompt", []))
                self.assertEqual(AttemptOutcome.empty, consume_outcome())

        content = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=" answer "))]
        )
        self.assertEqual("answer", self.client(content)._send("prompt", []))
        self.assertEqual(AttemptOutcome.success, consume_outcome())

        reasoning = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        reasoning_content=" recovered ",
                    )
                )
            ]
        )
        self.assertEqual("recovered", self.client(reasoning)._send("prompt", []))
        self.assertEqual(AttemptOutcome.success, consume_outcome())

    def test_classifies_errors_without_logging_private_content(self) -> None:
        private = "private provider response body"
        with self.assertLogs(openai.logger.name, level="WARNING") as timeout_logs:
            self.assertIsNone(
                self.client(error=openai.TimeoutException(private))._send("prompt", [])
            )
        self.assertEqual(AttemptOutcome.timeout, consume_outcome())
        self.assertNotIn(private, "\n".join(timeout_logs.output))

        sdk_timeout = type("APITimeoutError", (RuntimeError,), {})(private)
        with self.assertLogs(openai.logger.name, level="WARNING") as sdk_timeout_logs:
            self.assertIsNone(self.client(error=sdk_timeout)._send("prompt", []))
        self.assertEqual(AttemptOutcome.timeout, consume_outcome())
        self.assertNotIn(private, "\n".join(sdk_timeout_logs.output))

        unavailable = RuntimeError(private)
        unavailable.status_code = 503  # type: ignore[attr-defined]
        with self.assertLogs(openai.logger.name, level="WARNING") as error_logs:
            self.assertIsNone(self.client(error=unavailable)._send("prompt", []))
        self.assertEqual(AttemptOutcome.provider_error, consume_outcome())
        rendered = "\n".join(error_logs.output)
        self.assertIn("RuntimeError", rendered)
        self.assertIn("503", rendered)
        self.assertNotIn(private, rendered)

        rejected = RuntimeError(private)
        rejected.status_code = 401  # type: ignore[attr-defined]
        with self.assertLogs(openai.logger.name, level="WARNING") as rejected_logs:
            self.assertIsNone(self.client(error=rejected)._send("prompt", []))
        self.assertEqual(AttemptOutcome.invalid_input, consume_outcome())
        rendered = "\n".join(rejected_logs.output)
        self.assertIn("401", rendered)
        self.assertNotIn(private, rendered)


if __name__ == "__main__":
    unittest.main()
