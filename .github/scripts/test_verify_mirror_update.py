from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("verify_mirror_update.py")
SPEC = importlib.util.spec_from_file_location("verify_mirror_update", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VerifyMirrorUpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        self.base = self.commit("base")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def commit(self, message: str) -> str:
        path = self.repo / "history.txt"
        prior = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(prior + message + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "history.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", message], check=True)
        return subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()

    def test_accepts_exact_noop_and_fast_forward(self) -> None:
        self.assertEqual(MODULE.verify_update(self.repo, self.base, self.base), "already-current")
        head = self.commit("head")
        self.assertEqual(MODULE.verify_update(self.repo, self.base, head), "verified-fast-forward")

    def test_rejects_rewrite_or_divergence(self) -> None:
        subprocess.run(["git", "-C", str(self.repo), "switch", "-q", "--detach", self.base], check=True)
        divergent = self.commit("divergent")
        subprocess.run(["git", "-C", str(self.repo), "switch", "-q", "--detach", self.base], check=True)
        other = self.commit("other")
        with self.assertRaisesRegex(MODULE.MirrorUpdateError, "rewrite-or-divergence"):
            MODULE.verify_update(self.repo, divergent, other)

    def test_rejects_abbreviated_or_malformed_identity(self) -> None:
        with self.assertRaisesRegex(MODULE.MirrorUpdateError, "complete lowercase"):
            MODULE.verify_update(self.repo, self.base[:7], self.base)


if __name__ == "__main__":
    unittest.main()
