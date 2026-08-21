"""Bound image-bearing GenAI requests without changing text or tool history."""

from __future__ import annotations

from typing import Any

MAX_PROVIDER_IMAGES = 10
MAX_CHAT_IMAGES = 10
OMITTED_IMAGE_TEXT = "[Older live image omitted to enforce the 10-image history limit.]"


def _is_image_part(part: Any) -> bool:
    return isinstance(part, dict) and part.get("type") == "image_url"


def count_chat_images(messages: list[dict[str, Any]]) -> int:
    """Return the number of OpenAI-style image parts in a conversation."""
    count = 0
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        count += sum(1 for part in content if _is_image_part(part))
    return count


def limit_description_images(
    images: list[bytes],
    max_images: int = MAX_PROVIDER_IMAGES,
) -> list[bytes]:
    """Sample at most ``max_images`` frames across the full sequence."""
    if max_images < 0:
        raise ValueError("max_images must be non-negative")
    if len(images) <= max_images:
        return images
    if max_images == 0:
        return []
    if max_images == 1:
        return [images[-1]]

    last_index = len(images) - 1
    return [
        images[(sample_index * last_index) // (max_images - 1)]
        for sample_index in range(max_images)
    ]


def enforce_chat_image_limit(
    messages: list[dict[str, Any]],
    max_images: int = MAX_CHAT_IMAGES,
) -> list[dict[str, Any]]:
    """Return a conversation containing at most the newest ``max_images``."""
    if max_images < 0:
        raise ValueError("max_images must be non-negative")

    images_to_remove = max(0, count_chat_images(messages) - max_images)
    if images_to_remove == 0:
        return messages

    bounded: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list) or images_to_remove == 0:
            bounded.append(message)
            continue

        retained: list[Any] = []
        changed = False
        omission_marker_added = False
        for part in content:
            if images_to_remove and _is_image_part(part):
                images_to_remove -= 1
                changed = True
                if not omission_marker_added:
                    retained.append({"type": "text", "text": OMITTED_IMAGE_TEXT})
                    omission_marker_added = True
                continue
            retained.append(part)

        if not changed:
            bounded.append(message)
            continue

        replacement = dict(message)
        replacement["content"] = retained
        bounded.append(replacement)

    return bounded
