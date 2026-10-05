"""Resolve semantic candidates to reviews with one pass over review JSON."""

from typing import Any

from peewee import SQL, fn


def semantic_review_matches(review_model: Any, event_ids: list[str]) -> Any:
    """Materialize exact object IDs before joining to candidate events.

    Reviewing every archive row for each candidate turns a small semantic
    result into an unindexed nested-loop scan. JSON arrays must still be read,
    but only once. Do not infer membership from event/review time overlap:
    retained review metadata can reference a previously ended object.
    """
    detection_id = SQL("search_detection.value")
    detections = fn.json_each(review_model.data, "$.detections").alias(
        "search_detection"
    )
    return (
        review_model.select(
            review_model.id.alias("review_id"),
            detection_id.alias("event_id"),
            review_model.camera,
            review_model.thumb_path,
        )
        .from_(review_model, detections)
        .where(detection_id.in_(event_ids))
        .distinct()
        .cte("search_review_matches", materialized=True)
    )
