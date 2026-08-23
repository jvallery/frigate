#!/usr/bin/env python3
"""Unit tests for exact Frigate development release verification."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERIFIER_PATH = ROOT / ".github/scripts/verify_local_dev_release.py"


def load_module(name: str, path: Path):
    """Load one repository script as a test module."""

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFIER = load_module("verify_local_dev_release", VERIFIER_PATH)
RELEASE_ID = "v0.18.0-vallery.20260824.1"
SOURCE_SHA = "1" * 40
UPSTREAM_SHA = "2" * 40
STANDARD_DIGEST = "sha256:" + "a" * 64
TENSORRT_DIGEST = "sha256:" + "b" * 64
RUN_URL = "https://github.com/jvallery/frigate/actions/runs/123456"
MANIFEST_URL = (
    "https://github.com/jvallery/frigate/releases/download/"
    f"{RELEASE_ID}/release-manifest.json"
)


def release_manifest() -> dict:
    """Return one minimal public release accepted by the runtime helper."""

    def artifact(digest: str) -> dict:
        return {
            "digest": digest,
            "ghcr": {
                "canonical": f"ghcr.io/jvallery/frigate@{digest}",
                "verified_digest": digest,
            },
            "zot": {
                "canonical": f"registry.vallery.net/jvallery/frigate@{digest}",
                "verified_digest": digest,
            },
        }

    return {
        "schema": "vallery.frigate.release/1",
        "release_id": RELEASE_ID,
        "source": {
            "fork_repository": "jvallery/frigate",
            "fork_branch": "vallery/prod",
            "fork_sha": SOURCE_SHA,
            "upstream_sha": UPSTREAM_SHA,
        },
        "build": {"workflow_run": RUN_URL},
        "artifacts": {
            "standard-amd64": artifact(STANDARD_DIGEST),
            "tensorrt-amd64": artifact(TENSORRT_DIGEST),
        },
    }


class LocalDevReleaseVerificationTest(unittest.TestCase):
    """Prove exact agreement and negative repository boundaries."""

    def write_manifest(self, root: Path, manifest: dict | None = None) -> tuple[Path, str]:
        """Write deterministic public input and return its digest."""

        path = root / "release-manifest.json"
        path.write_text(
            json.dumps(manifest if manifest is not None else release_manifest())
            + "\n",
            encoding="utf-8",
        )
        digest = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        return path, digest

    def verify(self, path: Path, digest: str, **overrides):
        """Call the verifier with exact defaults and optional mutations."""

        values = {
            "release_id": RELEASE_ID,
            "source_sha": SOURCE_SHA,
            "upstream_sha": UPSTREAM_SHA,
            "standard_digest": STANDARD_DIGEST,
            "manifest_sha256": digest,
            "manifest_url": MANIFEST_URL,
            "run_url": RUN_URL,
        }
        values.update(overrides)
        return VERIFIER.verify(path, **values)

    def test_exact_public_release_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path, digest = self.write_manifest(Path(temp))
            manifest = self.verify(path, digest)
        self.assertEqual(STANDARD_DIGEST, manifest["artifacts"]["standard-amd64"]["digest"])

    def test_cross_repository_manifest_url_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path, digest = self.write_manifest(Path(temp))
            with self.assertRaisesRegex(SystemExit, "canonical Frigate release asset"):
                self.verify(
                    path,
                    digest,
                    manifest_url=(
                        "https://github.com/jvallery/sentinel/releases/download/"
                        f"{RELEASE_ID}/release-manifest.json"
                    ),
                )

    def test_cross_repository_workflow_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = release_manifest()
            manifest["build"]["workflow_run"] = (
                "https://github.com/jvallery/sentinel/actions/runs/123456"
            )
            path, digest = self.write_manifest(Path(temp), manifest)
            with self.assertRaisesRegex(SystemExit, "build workflow identity"):
                self.verify(path, digest)

    def test_wrong_standard_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path, digest = self.write_manifest(Path(temp))
            with self.assertRaisesRegex(SystemExit, "standard digest disagrees"):
                self.verify(path, digest, standard_digest="sha256:" + "c" * 64)

    def test_changed_manifest_bytes_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path, digest = self.write_manifest(Path(temp))
            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "manifest digest disagrees"):
                self.verify(path, digest)


if __name__ == "__main__":
    unittest.main()
