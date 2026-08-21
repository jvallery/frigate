"""Atomic, WAL-safe SQLite online backup support."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def _sync_directory(path: Path) -> None:
    """Persist an atomic replacement when directory fsync is supported."""
    try:
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def create_consistent_database_backup(
    source_path: str | os.PathLike[str],
    backup_path: str | os.PathLike[str],
) -> None:
    """Atomically back up one pinned read snapshot without blocking WAL writers."""
    source = Path(source_path).resolve()
    backup = Path(backup_path).resolve()
    temporary = backup.with_name(f".{backup.name}.tmp")
    if source == backup or source == temporary:
        raise ValueError("SQLite backup source and destination must be distinct")

    source_connection: sqlite3.Connection | None = None
    try:
        temporary.unlink(missing_ok=True)
        source_connection = sqlite3.connect(
            f"{source.as_uri()}?mode=ro",
            uri=True,
            timeout=60,
        )
        source_connection.execute("PRAGMA busy_timeout = 60000")
        source_connection.execute("BEGIN")
        source_connection.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()

        backup_connection = sqlite3.connect(temporary, timeout=60)
        try:
            source_connection.backup(backup_connection, pages=-1, sleep=0)
        finally:
            backup_connection.close()

        with temporary.open("rb") as backup_file:
            os.fsync(backup_file.fileno())
        os.replace(temporary, backup)
        _sync_directory(backup.parent)
    finally:
        if source_connection is not None:
            if source_connection.in_transaction:
                source_connection.rollback()
            source_connection.close()
        temporary.unlink(missing_ok=True)
