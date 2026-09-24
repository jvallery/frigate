#!/usr/bin/env python3
"""Regression tests for exact downstream patch path ownership."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("verify_downstream_patch_ledger.py")
SPEC = importlib.util.spec_from_file_location(
    "verify_downstream_patch_ledger", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot load {MODULE_PATH}")
LEDGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LEDGER)


class PathOwnershipTest(unittest.TestCase):
    def test_exact_path_set_passes(self) -> None:
        LEDGER.validate_path_ownership(
            "VLY-TEST",
            {"frigate/example.py", "frigate/test/test_example.py"},
            {"frigate/example.py", "frigate/test/test_example.py"},
        )

    def test_declared_path_missing_from_commit_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "lists paths not owned"):
            LEDGER.validate_path_ownership(
                "VLY-TEST",
                {"frigate/example.py", "frigate/omitted.py"},
                {"frigate/example.py"},
            )

    def test_commit_path_missing_from_ledger_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "commits change undeclared paths"):
            LEDGER.validate_path_ownership(
                "VLY-TEST",
                {"frigate/example.py"},
                {"frigate/example.py", "frigate/undeclared.py"},
            )


class DownstreamDeletionTest(unittest.TestCase):
    ENTRY = {
        "path": "CLAUDE.md",
        "reason": "estate standard",
        "intake_resolution": "keep deleted",
        "retirement_condition": "upstream removes it",
    }

    @staticmethod
    def tree(head: set[str], upstream: set[str]):
        return lambda ref, path: path in (head if ref == "HEAD" else upstream)

    def test_deleted_upstream_path_passes(self) -> None:
        LEDGER.validate_downstream_deletions(
            [self.ENTRY], "upstream", self.tree(set(), {"CLAUDE.md"})
        )

    def test_reintroduced_path_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "present at HEAD"):
            LEDGER.validate_downstream_deletions(
                [self.ENTRY], "upstream", self.tree({"CLAUDE.md"}, {"CLAUDE.md"})
            )

    def test_path_absent_upstream_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "retire the entry"):
            LEDGER.validate_downstream_deletions(
                [self.ENTRY], "upstream", self.tree(set(), set())
            )


if __name__ == "__main__":
    unittest.main()
