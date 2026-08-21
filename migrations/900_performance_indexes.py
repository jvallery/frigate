"""Compatibility marker for the pre-source-native Vallery performance migration.

Production databases created by Sentinel's transitional patch image already record
``900_performance_indexes`` in ``migratehistory``. Migration 036 now owns the
index definitions in source. This file must remain a no-op until that production
history is explicitly normalized; removing it would make a previously applied
migration appear to have vanished from the source set.
"""


def migrate(migrator, database, fake=False, **kwargs):
    """Preserve the recorded migration identity without repeating DDL."""
    del migrator, database, fake, kwargs


def rollback(migrator, database, fake=False, **kwargs):
    """Never remove indexes owned by migration 036 during compatibility rollback."""
    del migrator, database, fake, kwargs
