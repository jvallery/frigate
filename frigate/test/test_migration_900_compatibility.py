"""Contract tests for the recorded production migration-history marker."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MIGRATION_PATH = Path(__file__).parents[2] / "migrations" / "900_performance_indexes.py"
SPEC = importlib.util.spec_from_file_location("migration_900", MIGRATION_PATH)
assert SPEC and SPEC.loader
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class RejectingMigrator:
    def sql(self, statement: str) -> None:
        raise AssertionError(f"compatibility marker attempted SQL: {statement}")

    def run(self, function) -> None:
        raise AssertionError(f"compatibility marker attempted callback: {function}")


class Migration900CompatibilityTest(unittest.TestCase):
    def test_migrate_and_rollback_are_deliberate_noops(self) -> None:
        migrator = RejectingMigrator()
        database = object()
        self.assertIsNone(MIGRATION.migrate(migrator, database))
        self.assertIsNone(MIGRATION.rollback(migrator, database))

    def test_marker_name_matches_production_history_exactly(self) -> None:
        self.assertEqual(MIGRATION_PATH.stem, "900_performance_indexes")


if __name__ == "__main__":
    unittest.main()
