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


if __name__ == "__main__":
    unittest.main()
