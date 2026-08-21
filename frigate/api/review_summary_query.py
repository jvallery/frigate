"""Bounded queries for Review summary counts."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from functools import reduce
from typing import Any

from peewee import Case, fn, operator

DAY_SECONDS = 86_400


def _camera_scope(requested: str, allowed_cameras: list[str]) -> list[str] | None:
    if requested == "all":
        return list(allowed_cameras)

    filtered = set(requested.split(",")).intersection(allowed_cameras)
    return list(filtered) if filtered else None


def _review_clauses(
    review_model: Any,
    camera_list: list[str],
    labels: str,
    zones: str,
    *,
    include_audio_labels: bool,
    include_zones: bool,
) -> list[Any]:
    clauses: list[Any] = [review_model.camera << camera_list]

    if labels != "all":
        label_clauses = []
        for label in labels.split(","):
            object_match = review_model.data["objects"].cast("text") % f'*"{label}"*'
            if include_audio_labels:
                audio_match = review_model.data["audio"].cast("text") % f'*"{label}"*'
                label_clauses.append(object_match | audio_match)
            else:
                label_clauses.append(object_match)
        clauses.append(reduce(operator.or_, label_clauses))

    if include_zones and zones != "all":
        zone_clauses = [
            review_model.data["zones"].cast("text") % f'*"{zone}"*'
            for zone in zones.split(",")
        ]
        clauses.append(reduce(operator.or_, zone_clauses))

    return clauses


def _sum_matches(condition: Any, alias: str) -> Any:
    return fn.SUM(Case(None, [(condition, 1)], 0)).alias(alias)


def _totals_columns(review_model: Any, severity_enum: Any) -> tuple[Any, Any]:
    return (
        _sum_matches(
            review_model.severity == severity_enum.alert,
            "total_alert",
        ),
        _sum_matches(
            review_model.severity == severity_enum.detection,
            "total_detection",
        ),
    )


def _reviewed_columns(review_model: Any, severity_enum: Any) -> tuple[Any, Any]:
    return (
        _sum_matches(
            review_model.severity == severity_enum.alert,
            "reviewed_alert",
        ),
        _sum_matches(
            review_model.severity == severity_enum.detection,
            "reviewed_detection",
        ),
    )


def _ungrouped_counts(
    review_model: Any,
    status_model: Any,
    clauses: list[Any],
    user_id: str,
    severity_enum: Any,
) -> dict[str, int | None]:
    totals = (
        review_model.select(*_totals_columns(review_model, severity_enum))
        .where(reduce(operator.and_, clauses))
        .dicts()
        .get()
    )
    reviewed = (
        status_model.select(*_reviewed_columns(review_model, severity_enum))
        .join(
            review_model,
            on=(status_model.review_segment == review_model.id),
        )
        .where(
            (status_model.user_id == user_id)
            & (status_model.has_been_reviewed == True)  # noqa: E712
            & reduce(operator.and_, clauses)
        )
        .dicts()
        .get()
    )

    has_reviews = any(value is not None for value in totals.values())
    return {
        "reviewed_alert": (reviewed["reviewed_alert"] or 0) if has_reviews else None,
        "reviewed_detection": (
            (reviewed["reviewed_detection"] or 0) if has_reviews else None
        ),
        "total_alert": totals["total_alert"],
        "total_detection": totals["total_detection"],
    }


def _grouped_counts(
    review_model: Any,
    status_model: Any,
    clauses: list[Any],
    user_id: str,
    severity_enum: Any,
    day_expression: Any,
    day_group: Any,
) -> list[dict[str, Any]]:
    totals_query = (
        review_model.select(
            day_expression.alias("day"),
            *_totals_columns(review_model, severity_enum),
        )
        .where(reduce(operator.and_, clauses))
        .group_by(day_group)
        .order_by(review_model.start_time.desc())
        .dicts()
    )
    grouped = {
        row["day"]: {
            "day": row["day"],
            "reviewed_alert": 0,
            "reviewed_detection": 0,
            "total_alert": row["total_alert"],
            "total_detection": row["total_detection"],
        }
        for row in totals_query.iterator()
    }

    reviewed_query = (
        status_model.select(
            day_expression.alias("day"),
            *_reviewed_columns(review_model, severity_enum),
        )
        .join(
            review_model,
            on=(status_model.review_segment == review_model.id),
        )
        .where(
            (status_model.user_id == user_id)
            & (status_model.has_been_reviewed == True)  # noqa: E712
            & reduce(operator.and_, clauses)
        )
        .group_by(day_group)
        .dicts()
    )
    for row in reviewed_query.iterator():
        if row["day"] not in grouped:
            continue
        grouped[row["day"]]["reviewed_alert"] = row["reviewed_alert"] or 0
        grouped[row["day"]]["reviewed_detection"] = row["reviewed_detection"] or 0

    return list(grouped.values())


def query_review_summary(
    params: Any,
    user_id: str,
    allowed_cameras: list[str],
    review_model: Any,
    status_model: Any,
    severity_enum: Any,
    get_dst_transitions: Callable[
        [str, float, float], list[tuple[float, float, float]]
    ],
    *,
    now_timestamp: float | None = None,
) -> dict[str, Any]:
    """Return the Review summary without an all-row user-status join."""
    camera_list = _camera_scope(params.cameras, allowed_cameras)
    if camera_list is None:
        return {}

    if now_timestamp is None:
        now_timestamp = datetime.datetime.now().timestamp()
    day_ago = now_timestamp - DAY_SECONDS

    recent_clauses = _review_clauses(
        review_model,
        camera_list,
        params.labels,
        params.zones,
        include_audio_labels=True,
        include_zones=True,
    )
    recent_clauses.append(review_model.start_time > day_ago)
    recent = _ungrouped_counts(
        review_model,
        status_model,
        recent_clauses,
        user_id,
        severity_enum,
    )

    # Preserve current API semantics: historical daily totals apply camera and
    # object-label filters, while zones/audio only affect the last-24-hour card.
    historical_clauses = _review_clauses(
        review_model,
        camera_list,
        params.labels,
        params.zones,
        include_audio_labels=False,
        include_zones=False,
    )
    time_range = (
        review_model.select(
            fn.MIN(review_model.start_time).alias("min_time"),
            fn.MAX(review_model.start_time).alias("max_time"),
        )
        .where(reduce(operator.and_, historical_clauses))
        .dicts()
        .get()
    )

    data: dict[str, Any] = {"last24Hours": recent}
    min_time = time_range["min_time"]
    max_time = time_range["max_time"]
    if min_time is None or max_time is None:
        return data

    for period_start, period_end, period_offset in get_dst_transitions(
        params.timezone, min_time, max_time
    ):
        hours_offset = int(period_offset / 3600)
        minutes_offset = int(period_offset / 60 - hours_offset * 60)
        day_expression = fn.strftime(
            "%Y-%m-%d",
            fn.datetime(
                review_model.start_time,
                "unixepoch",
                f"{hours_offset} hour",
                f"{minutes_offset} minute",
            ),
        )
        day_group = (review_model.start_time + period_offset).cast("int") / DAY_SECONDS
        period_clauses = [
            *historical_clauses,
            (review_model.start_time >= period_start)
            & (review_model.start_time <= period_end),
        ]
        for row in _grouped_counts(
            review_model,
            status_model,
            period_clauses,
            user_id,
            severity_enum,
            day_expression,
            day_group,
        ):
            day = row["day"]
            if day in data:
                for key in (
                    "reviewed_alert",
                    "reviewed_detection",
                    "total_alert",
                    "total_detection",
                ):
                    data[day][key] += row[key] or 0
            else:
                data[day] = row

    return data
