#!/usr/bin/env python3
"""Regression tests for exact, fail-closed upstream patch retirement detection."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("plan_upstream_patch_retirement.py")
SPEC = importlib.util.spec_from_file_location(
    "plan_upstream_patch_retirement", MODULE_PATH
)
assert SPEC and SPEC.loader
RETIREMENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RETIREMENT)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class RetirementPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        git(self.repo, "init", "-b", "dev")
        git(self.repo, "config", "user.name", "Vallery Test")
        git(self.repo, "config", "user.email", "test@vallery.net")
        (self.repo / "source.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "source.txt")
        git(self.repo, "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")

        git(self.repo, "switch", "-c", "local")
        (self.repo / "source.txt").write_text("base\nexact patch\n", encoding="utf-8")
        git(self.repo, "commit", "-am", "local patch")
        self.local_commit = git(self.repo, "rev-parse", "HEAD")
        self.patch_id = RETIREMENT.stable_patch_id(self.repo, self.local_commit)
        assert self.patch_id

        git(self.repo, "switch", "dev")
        (self.repo / "source.txt").write_text("base\nexact patch\n", encoding="utf-8")
        git(self.repo, "commit", "-am", "upstream patch")
        self.upstream_commit = git(self.repo, "rev-parse", "HEAD")
        self.ledger = self.repo / "ledger.json"
        self.ledger.write_text(
            json.dumps(
                {
                    "selected_upstream": {
                        "repository": "blakeblackshear/frigate",
                        "branch": "dev",
                        "sha": self.base,
                    },
                    "patches": [
                        {
                            "id": "VLY-TEST-001",
                            "concern": "exact_patch",
                            "status": "candidate",
                            "disposition": "upstreamable",
                            "upstream_base_sha": self.base,
                            "local_commits": [
                                {"sha": self.local_commit, "patch_id": self.patch_id}
                            ],
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_patch_id_becomes_upstreamed_but_not_retired(self) -> None:
        plan = RETIREMENT.plan(self.repo, self.ledger, "dev")
        self.assertEqual(plan["candidate_count"], 1)
        self.assertEqual(
            plan["candidates"][0]["matches"][0]["upstream_commit"],
            self.upstream_commit,
        )
        RETIREMENT.apply_plan(self.ledger, plan)
        updated = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(updated["selected_upstream"]["sha"], self.upstream_commit)
        self.assertEqual(updated["patches"][0]["status"], "upstreamed")
        self.assertNotEqual(updated["patches"][0]["status"], "retired")

    def test_non_descendant_upstream_is_rejected(self) -> None:
        git(self.repo, "switch", "--orphan", "rewritten")
        (self.repo / "source.txt").unlink(missing_ok=True)
        (self.repo / "other.txt").write_text("rewrite\n", encoding="utf-8")
        git(self.repo, "add", "other.txt")
        git(self.repo, "commit", "-m", "rewritten root")
        with self.assertRaisesRegex(ValueError, "rewrites or does not descend"):
            RETIREMENT.plan(self.repo, self.ledger, "rewritten")


if __name__ == "__main__":
    unittest.main()
