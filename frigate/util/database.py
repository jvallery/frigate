"""Database utilities."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path


def create_database_backup(
    source_path: str | os.PathLike[str],
    backup_path: str | os.PathLike[str],
) -> None:
    """Atomically back up a consistent snapshot without blocking WAL writers.

    Pinning the read snapshot before copying includes committed WAL pages, and
    copying all pages in one step prevents continuous writes from restarting a
    large backup.
    """
    source = Path(source_path).resolve()
    backup = Path(backup_path).resolve()
    temporary = backup.with_name(f".{backup.name}.tmp")

    if source == backup:
        raise ValueError("SQLite backup source and destination must be distinct")

    try:
        temporary.unlink(missing_ok=True)
        with closing(
            sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=60)
        ) as source_connection:
            source_connection.execute("BEGIN")
            source_connection.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()

            with closing(sqlite3.connect(temporary, timeout=60)) as backup_connection:
                source_connection.backup(backup_connection, pages=-1, sleep=0)

        os.replace(temporary, backup)
    finally:
        temporary.unlink(missing_ok=True)
