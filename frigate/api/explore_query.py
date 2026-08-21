"""Bound Explore loading without a full-table window sort or N+1 queries."""

from __future__ import annotations

from typing import Any

EXPLORE_EVENT_FIELDS = (
    "id",
    "camera",
    "label",
    "zones",
    "start_time",
    "end_time",
    "has_clip",
    "has_snapshot",
    "plus_id",
    "retain_indefinitely",
    "sub_label",
    "top_score",
    "false_positive",
    "box",
    "data",
)
EXPLORE_DATA_FIELDS = frozenset(
    {
        "type",
        "score",
        "top_score",
        "description",
        "sub_label_score",
        "average_estimated_speed",
        "velocity_angle",
        "path_data",
        "recognized_license_plate",
        "recognized_license_plate_score",
    }
)
MAX_LABELS_PER_RECENT_QUERY = 200


def _event_fields(event_model: Any) -> dict[str, Any]:
    metadata = event_model._meta
    if metadata.table_name != "event":
        raise RuntimeError(f"unexpected Frigate Event table: {metadata.table_name!r}")
    missing = [name for name in EXPLORE_EVENT_FIELDS if name not in metadata.fields]
    if missing:
        raise RuntimeError(
            f"Frigate Event model is missing Explore fields: {', '.join(missing)}"
        )
    fields = {name: metadata.fields[name] for name in EXPLORE_EVENT_FIELDS}
    renamed = [
        name
        for name, field in fields.items()
        if getattr(field, "column_name", None) != name
    ]
    if renamed:
        raise RuntimeError(
            "Frigate Event columns drifted for Explore fields: " + ", ".join(renamed)
        )
    return fields


def _label_counts_sql(camera_count: int) -> str:
    if camera_count < 1:
        raise ValueError("camera_count must be positive")
    placeholders = ", ".join("?" for _ in range(camera_count))
    return f"""SELECT "label", COUNT(*) AS "event_count"
FROM "event" INDEXED BY "event_label_start_time"
WHERE "camera" IN ({placeholders})
GROUP BY "label"
ORDER BY "label" ASC
"""


def _recent_events_sql(camera_count: int, label_count: int) -> str:
    if camera_count < 1:
        raise ValueError("camera_count must be positive")
    if label_count < 1 or label_count > MAX_LABELS_PER_RECENT_QUERY:
        raise ValueError(
            f"label_count must be between 1 and {MAX_LABELS_PER_RECENT_QUERY}"
        )
    camera_values = ", ".join("(?)" for _ in range(camera_count))
    selected_fields = ",\n        ".join(
        f'"selected_event"."{name}"' for name in EXPLORE_EVENT_FIELDS
    )
    branches = []
    for branch_index in range(label_count):
        branches.append(
            f"""    SELECT
        {selected_fields},
        ? AS "event_count"
    FROM (
        SELECT "id"
        FROM "event" INDEXED BY "event_label_start_time"
        WHERE "label" = ?
          AND "camera" IN (SELECT "camera" FROM "allowed_cameras")
        ORDER BY "start_time" DESC
        LIMIT ?
    ) AS "recent_ids_{branch_index}"
    JOIN "event" AS "selected_event"
      ON "selected_event"."id" = "recent_ids_{branch_index}"."id"
"""
        )
    compound_query = "\nUNION ALL\n".join(branches)
    return f"""WITH "allowed_cameras"("camera") AS (
    VALUES {camera_values}
)
SELECT *
FROM (
{compound_query}
) AS "explore_events"
ORDER BY "label" ASC, "start_time" DESC
"""


def query_explore_events(
    event_model: Any,
    allowed_cameras: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Return authorized per-label recent events in bounded DB round trips."""
    fields = _event_fields(event_model)
    cameras = tuple(allowed_cameras)
    if not cameras or limit == 0:
        return []

    database = event_model._meta.database
    counts_cursor = database.execute_sql(_label_counts_sql(len(cameras)), list(cameras))
    try:
        label_counts = [
            (str(label), int(event_count)) for label, event_count in counts_cursor
        ]
    finally:
        counts_cursor.close()

    processed_events: list[dict[str, Any]] = []
    expected_columns = len(EXPLORE_EVENT_FIELDS) + 1
    for offset in range(0, len(label_counts), MAX_LABELS_PER_RECENT_QUERY):
        count_chunk = label_counts[offset : offset + MAX_LABELS_PER_RECENT_QUERY]
        sql = _recent_events_sql(len(cameras), len(count_chunk))
        parameters: list[Any] = list(cameras)
        for label, event_count in count_chunk:
            parameters.extend((event_count, label, limit))
        cursor = database.execute_sql(sql, parameters)
        try:
            for row in cursor:
                if len(row) != expected_columns:
                    raise RuntimeError(
                        "unexpected Frigate Explore query projection: "
                        f"{len(row)} columns, expected {expected_columns}"
                    )
                event = {
                    name: fields[name].python_value(value)
                    for name, value in zip(EXPLORE_EVENT_FIELDS, row[:-1], strict=True)
                }
                event["data"] = {
                    key: value
                    for key, value in event["data"].items()
                    if key in EXPLORE_DATA_FIELDS
                }
                event["event_count"] = int(row[-1])
                processed_events.append(event)
        finally:
            cursor.close()

    processed_events.sort(
        key=lambda event: (event["event_count"], event["start_time"]),
        reverse=True,
    )
    return processed_events
