from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("classify_upstream_changes.py")
SPEC = importlib.util.spec_from_file_location("classify_upstream_changes", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ClassifyUpstreamChangesTest(unittest.TestCase):
    def test_risk_paths_are_explicit_and_can_overlap(self) -> None:
        report = MODULE.build_report(
            [
                "docker/main/requirements.txt",
                "frigate/api/review.py",
                "migrations/036_add_query_indexes.py",
                "web/src/components/ReviewCard.tsx",
            ],
            ["frigate/api/review.py", "migrations/**"],
            "a" * 40,
            "b" * 40,
        )
        self.assertTrue(report["safe_to_dispatch"])
        self.assertEqual(report["unclassified"], [])
        self.assertIn("dependency", report["risk_categories"])
        self.assertIn("docker", report["risk_categories"])
        self.assertIn("migration", report["risk_categories"])
        self.assertIn("patch-sensitive", report["risk_categories"])
        self.assertIn("frigate/api/review.py", report["categories"]["source"])

    def test_unknown_path_fails_closed(self) -> None:
        report = MODULE.build_report(["unexpected.binary"], [], "a" * 40, "b" * 40)
        self.assertFalse(report["safe_to_dispatch"])
        self.assertEqual(report["unclassified"], ["unexpected.binary"])


if __name__ == "__main__":
    unittest.main()
