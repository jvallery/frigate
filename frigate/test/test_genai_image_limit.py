"""Regression tests for provider and Chat image ceilings."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "genai" / "image_limit.py"
SPEC = importlib.util.spec_from_file_location("genai_image_limit", MODULE_PATH)
assert SPEC and SPEC.loader
image_limit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(image_limit)


def image(label: str) -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{label}"},
    }


class GenAIImageLimitTest(unittest.TestCase):
    def test_keeps_newest_ten_images_without_mutating_input(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"frame {index}"},
                    image(str(index)),
                ],
            }
            for index in range(25)
        ]

        bounded = image_limit.enforce_chat_image_limit(messages)

        self.assertEqual(10, image_limit.count_chat_images(bounded))
        self.assertEqual(25, image_limit.count_chat_images(messages))
        retained_urls = [
            part["image_url"]["url"]
            for message in bounded
            for part in message["content"]
            if part.get("type") == "image_url"
        ]
        self.assertEqual(
            [f"data:image/jpeg;base64,{index}" for index in range(15, 25)],
            retained_urls,
        )
        self.assertEqual("frame 0", bounded[0]["content"][0]["text"])
        self.assertEqual(
            image_limit.OMITTED_IMAGE_TEXT, bounded[0]["content"][1]["text"]
        )

    def test_preserves_image_only_messages_and_tool_metadata(self) -> None:
        messages = [
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "get_live_context",
                "content": [image("old")],
            },
            *[{"role": "user", "content": [image(str(index))]} for index in range(10)],
        ]

        bounded = image_limit.enforce_chat_image_limit(messages)

        self.assertEqual(10, image_limit.count_chat_images(bounded))
        self.assertEqual("call-1", bounded[0]["tool_call_id"])
        self.assertEqual("get_live_context", bounded[0]["name"])
        self.assertEqual(
            image_limit.OMITTED_IMAGE_TEXT, bounded[0]["content"][0]["text"]
        )

    def test_returns_original_conversation_when_already_bounded(self) -> None:
        messages = [{"role": "user", "content": [image("one")]}]
        self.assertIs(messages, image_limit.enforce_chat_image_limit(messages))

    def test_rejects_negative_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-negative"):
            image_limit.enforce_chat_image_limit([], -1)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            image_limit.limit_description_images([], -1)

    def test_samples_description_sequence_across_full_span(self) -> None:
        images = [str(index).encode() for index in range(20)]

        bounded = image_limit.limit_description_images(images)

        self.assertEqual(10, len(bounded))
        self.assertEqual(b"0", bounded[0])
        self.assertEqual(b"19", bounded[-1])
        self.assertEqual(10, len(set(bounded)))
        self.assertEqual(20, len(images))

    def test_description_sequence_boundary_cases(self) -> None:
        images = [b"old", b"new"]
        self.assertIs(images, image_limit.limit_description_images(images))
        self.assertEqual([b"new"], image_limit.limit_description_images(images, 1))
        self.assertEqual([], image_limit.limit_description_images(images, 0))


if __name__ == "__main__":
    unittest.main()
