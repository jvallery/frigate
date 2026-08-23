#!/usr/bin/env python3
"""Unit and mutation tests for the isolated development runtime."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PATH = ROOT / ".github/scripts/validate_local_dev_runtime.py"
PROMOTION_PATH = ROOT / ".github/scripts/prepare_local_dev_promotion.py"


def load_module(name: str, path: Path):
    """Load a repository script as a test module."""

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_module("validate_local_dev_runtime", VALIDATOR_PATH)
PROMOTION = load_module("prepare_local_dev_promotion", PROMOTION_PATH)

RELEASE_ID = "v0.18.0-vallery.20260824.1"
STANDARD_DIGEST = "sha256:" + "a" * 64
TENSORRT_DIGEST = "sha256:" + "b" * 64
PROMOTION_RUN = "https://github.com/jvallery/frigate/actions/runs/123456"


def release_manifest() -> dict:
    """Return one minimal, public release manifest accepted by the helper."""

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
            "fork_sha": "1" * 40,
            "upstream_sha": "2" * 40,
        },
        "build": {
            "workflow_run": "https://github.com/jvallery/frigate/actions/runs/654321"
        },
        "artifacts": {
            "standard-amd64": artifact(STANDARD_DIGEST),
            "tensorrt-amd64": artifact(TENSORRT_DIGEST),
        },
    }


class LocalDevRuntimeContractTest(unittest.TestCase):
    """Prove the checked-in runtime and its fail-closed guards."""

    @classmethod
    def setUpClass(cls) -> None:
        _, cls.documents = VALIDATOR.render()

    def deployment(self) -> dict:
        """Return a mutable copy of the rendered Deployment."""

        return copy.deepcopy(VALIDATOR.object_by_kind(self.documents, "Deployment"))

    def documents_with_deployment(self, deployment: dict) -> list[dict]:
        """Replace the Deployment in one mutable render copy."""

        return [
            deployment
            if document.get("kind") == "Deployment"
            else copy.deepcopy(document)
            for document in self.documents
        ]

    def assert_deployment_rejected(self, deployment: dict, message: str) -> None:
        """Assert one mutated workload violates the contract."""

        with self.assertRaisesRegex(SystemExit, message):
            VALIDATOR.validate_documents(self.documents_with_deployment(deployment))

    def test_checked_in_public_runtime_contract_passes(self) -> None:
        rendered = VALIDATOR.validate()
        self.assertNotIn("kind: Ingress", rendered)
        self.assertNotIn("kind: Secret", rendered)
        self.assertNotIn("kind: PersistentVolumeClaim", rendered)

    def test_guard_rejects_service_account_token_automount(self) -> None:
        deployment = self.deployment()
        deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] = True
        self.assert_deployment_rejected(deployment, "token automount")

    def test_guard_rejects_host_network(self) -> None:
        deployment = self.deployment()
        deployment["spec"]["template"]["spec"]["hostNetwork"] = True
        self.assert_deployment_rejected(deployment, "host networking")

    def test_guard_rejects_broad_secret_import(self) -> None:
        deployment = self.deployment()
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        container["envFrom"] = [{"secretRef": {"name": "foreign"}}]
        self.assert_deployment_rejected(deployment, "broad environment import")

    def test_guard_rejects_persistent_storage(self) -> None:
        deployment = self.deployment()
        deployment["spec"]["template"]["spec"]["volumes"][0] = {
            "name": "config",
            "persistentVolumeClaim": {"claimName": "foreign"},
        }
        self.assert_deployment_rejected(deployment, "persistent or host volume")

    def test_guard_rejects_gpu_resource(self) -> None:
        deployment = self.deployment()
        resources = deployment["spec"]["template"]["spec"]["containers"][0]["resources"]
        resources["limits"]["nvidia.com/gpu"] = "1"
        self.assert_deployment_rejected(deployment, "GPU resource")

    def test_guard_rejects_gpu_device_identity(self) -> None:
        deployment = self.deployment()
        deployment["metadata"]["labels"]["vallery.net/detector-device"] = "gpu"
        self.assert_deployment_rejected(deployment, "detector device label")

    def test_guard_rejects_companion_container(self) -> None:
        deployment = self.deployment()
        deployment["spec"]["template"]["spec"]["containers"].append(
            {
                "name": "companion",
                "image": "example.invalid/companion@sha256:" + "c" * 64,
            }
        )
        self.assert_deployment_rejected(deployment, "only application container")

    def test_guard_rejects_secret_or_route_in_base(self) -> None:
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "forbidden", "namespace": "frigate-dev"},
        }
        with self.assertRaisesRegex(SystemExit, "exactly four objects"):
            VALIDATOR.validate_documents([*copy.deepcopy(self.documents), secret])


class LocalDevPromotionTest(unittest.TestCase):
    """Prove digest selection, path bounds, public receipts, and inverse patches."""

    def initialize_repo(self, root: Path) -> Path:
        """Create an isolated promotion fixture with the checked-in Deployment."""

        deployment = root / "deploy/kubernetes/local-dev/deployment.yaml"
        deployment.parent.mkdir(parents=True)
        deployment.write_text(
            (ROOT / "deploy/kubernetes/local-dev/deployment.yaml").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)
        return deployment

    def write_manifest(self, root: Path, value: dict | None = None) -> Path:
        """Write one external public manifest fixture."""

        path = root / "public-release.json"
        path.write_text(
            json.dumps(value if value is not None else release_manifest()) + "\n",
            encoding="utf-8",
        )
        return path

    def test_prepare_changes_only_standard_image_and_generates_exact_inverse(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            deployment = self.initialize_repo(root)
            manifest_path = self.write_manifest(root)
            previous = deployment.read_text(encoding="utf-8")

            evidence = PROMOTION.prepare(root, manifest_path, PROMOTION_RUN)

            candidate = f"registry.vallery.net/jvallery/frigate@{STANDARD_DIGEST}"
            updated = deployment.read_text(encoding="utf-8")
            self.assertIn(candidate, updated)
            self.assertIn(f"vallery.net/release-id: {RELEASE_ID}", updated)
            self.assertEqual(
                2, updated.count(f"app.kubernetes.io/version: {RELEASE_ID}")
            )
            self.assertEqual(2, updated.count(f"vallery.net/source-sha: {'1' * 40}"))
            self.assertNotIn(TENSORRT_DIGEST, updated)
            self.assertEqual(
                {
                    "desired-state.patch",
                    "inverse.patch",
                    "promotion.json",
                    "release-manifest.json",
                },
                {path.name for path in evidence.iterdir()},
            )
            receipt = json.loads(
                (evidence / "promotion.json").read_text(encoding="utf-8")
            )
            self.assertEqual("frigate-dev", receipt["environment"])
            self.assertEqual(candidate, receipt["image"])
            self.assertEqual(PROMOTION_RUN, receipt["promotion_run"])
            self.assertNotIn("private", json.dumps(receipt).lower())
            inverse = evidence / "inverse.patch"
            self.assertEqual(
                0,
                subprocess.run(
                    ["git", "apply", "--check", str(inverse)], cwd=root
                ).returncode,
            )
            subprocess.run(["git", "apply", str(inverse)], cwd=root, check=True)
            self.assertEqual(previous, deployment.read_text(encoding="utf-8"))
            for patch_name in ("desired-state.patch", "inverse.patch"):
                patch = (evidence / patch_name).read_text(encoding="utf-8")
                self.assertIn("deploy/kubernetes/local-dev/deployment.yaml", patch)
                self.assertNotIn("sentinel", patch.lower())
                self.assertNotIn("application.yaml", patch.lower())

    def test_prepare_rejects_nonpublic_release_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.initialize_repo(root)
            manifest = release_manifest()
            manifest["camera"] = "rtsp://example.invalid/stream"
            with self.assertRaisesRegex(SystemExit, "public-safe"):
                PROMOTION.prepare(
                    root,
                    self.write_manifest(root, manifest),
                    PROMOTION_RUN,
                )

    def test_prepare_rejects_nested_sensitive_release_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.initialize_repo(root)
            manifest = release_manifest()
            manifest["build"]["password"] = "redacted-test-value"
            with self.assertRaisesRegex(SystemExit, "forbidden field"):
                PROMOTION.prepare(
                    root,
                    self.write_manifest(root, manifest),
                    PROMOTION_RUN,
                )

    def test_prepare_rejects_staged_deployment_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            deployment = self.initialize_repo(root)
            manifest_path = self.write_manifest(root)
            deployment.write_text(
                deployment.read_text(encoding="utf-8").replace(
                    "replicas: 1", "replicas: 2"
                ),
                encoding="utf-8",
            )
            subprocess.run(["git", "add", str(deployment)], cwd=root, check=True)

            with self.assertRaisesRegex(SystemExit, "must be clean"):
                PROMOTION.prepare(root, manifest_path, PROMOTION_RUN)

            self.assertFalse((root / "deploy/kubernetes/local-dev/releases").exists())

    def test_checked_in_inverse_targets_valid_adopted_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            deployment_path = self.initialize_repo(root)
            inverse = (
                ROOT
                / "deploy/kubernetes/local-dev/releases"
                / "v0.18.0-vallery.20260823.5/inverse.patch"
            )
            subprocess.run(["git", "apply", str(inverse)], cwd=root, check=True)
            deployment = VALIDATOR.yaml_objects(
                deployment_path.read_text(encoding="utf-8")
            )[0]

            VALIDATOR.validate_release_ledger(deployment)

            deployment["spec"]["template"]["spec"]["containers"][0]["image"] = (
                "registry.vallery.net/jvallery/frigate@sha256:" + "0" * 64
            )
            with self.assertRaisesRegex(SystemExit, "baseline identity drifted"):
                VALIDATOR.validate_release_ledger(deployment)

    def test_prepare_rejects_cross_repository_controller(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.initialize_repo(root)
            with self.assertRaisesRegex(SystemExit, "workflow identity"):
                PROMOTION.prepare(
                    root,
                    self.write_manifest(root),
                    "https://github.com/jvallery/sentinel/actions/runs/123456",
                )


if __name__ == "__main__":
    unittest.main()
