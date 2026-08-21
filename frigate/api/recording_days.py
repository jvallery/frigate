"""Bounded queries for the recording-day summary API."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable
from typing import Any

RECORDING_INDEX = "recordings_camera_start_time_end_time"
SECONDS_PER_DAY = 86_400


def _validate_recordings_model(recordings_model: Any) -> None:
    metadata = recordings_model._meta
    if metadata.table_name != "recordings":
        raise RuntimeError(
            f"unexpected Frigate Recordings table: {metadata.table_name!r}"
        )

    required = {"camera", "start_time", "end_time"}
    missing = sorted(required.difference(metadata.fields))
    if missing:
        raise RuntimeError(
            f"Frigate Recordings model is missing fields: {', '.join(missing)}"
        )


def _first_value(database: Any, sql: str, parameters: tuple[Any, ...]) -> Any:
    cursor = database.execute_sql(sql, parameters)
    try:
        row = cursor.fetchone()
        return None if row is None else row[0]
    finally:
        cursor.close()


def _camera_bound(database: Any, camera: str, *, newest: bool) -> float | None:
    direction = "DESC" if newest else "ASC"
    value = _first_value(
        database,
        f'''\
SELECT "start_time"
FROM "recordings" INDEXED BY "{RECORDING_INDEX}"
WHERE "camera" = ?
ORDER BY "start_time" {direction}
LIMIT 1
''',
        (camera,),
    )
    return None if value is None else float(value)


def _camera_has_recording(
    database: Any,
    camera: str,
    day_start: float,
    day_end: float,
    period_start: float,
    period_end: float,
) -> bool:
    value = _first_value(
        database,
        f'''\
SELECT 1
FROM "recordings" INDEXED BY "{RECORDING_INDEX}"
WHERE
    "camera" = ?
    AND "start_time" >= ?
    AND "start_time" < ?
    AND "end_time" >= ?
    AND "start_time" <= ?
LIMIT 1
''',
        (camera, day_start, day_end, period_start, period_end),
    )
    return value is not None


def query_recording_days(
    recordings_model: Any,
    allowed_cameras: list[str],
    timezone: str,
    get_dst_transitions: Callable[
        [str, float, float], list[tuple[float, float, float]]
    ],
) -> dict[str, bool]:
    """Return exact authorized recording days using bounded index probes."""
    _validate_recordings_model(recordings_model)
    cameras = tuple(dict.fromkeys(allowed_cameras))
    if not cameras:
        return {}

    database = recordings_model._meta.database
    oldest: list[float] = []
    newest: list[float] = []
    for camera in cameras:
        camera_oldest = _camera_bound(database, camera, newest=False)
        camera_newest = _camera_bound(database, camera, newest=True)
        if camera_oldest is not None:
            oldest.append(camera_oldest)
        if camera_newest is not None:
            newest.append(camera_newest)

    if not oldest or not newest:
        return {}

    min_time = min(oldest)
    max_time = max(newest)
    days: dict[str, bool] = {}
    epoch = dt.date(1970, 1, 1)

    for period_start, period_end, period_offset in get_dst_transitions(
        timezone, min_time, max_time
    ):
        first_day = math.floor((min_time + period_offset) / SECONDS_PER_DAY)
        last_day = math.floor((max_time + period_offset) / SECONDS_PER_DAY)
        for day_index in range(first_day, last_day + 1):
            day_start = day_index * SECONDS_PER_DAY - period_offset
            day_end = (day_index + 1) * SECONDS_PER_DAY - period_offset
            if any(
                _camera_has_recording(
                    database,
                    camera,
                    day_start,
                    day_end,
                    period_start,
                    period_end,
                )
                for camera in cameras
            ):
                days[(epoch + dt.timedelta(days=day_index)).isoformat()] = True

    return dict(sorted(days.items()))
