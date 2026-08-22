#!/usr/bin/env python3
"""Static and data-contract tests for the protected Vallery release path."""

from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUILD_WORKFLOW = ROOT / ".github/workflows/vallery-tensorrt-build.yml"
RELEASE_WORKFLOW = ROOT / ".github/workflows/vallery-release.yml"
RETIREMENT_WORKFLOW = ROOT / ".github/workflows/upstream-patch-retirement.yml"
INTEGRATION_WORKFLOW = ROOT / ".github/workflows/vallery-integration.yml"
BAKE = ROOT / ".vallery/release-build.hcl"
SCHEMA = ROOT / ".vallery/release-manifest.schema.json"
MAIN_DOCKERFILE = ROOT / "docker/main/Dockerfile"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MANIFEST = load_module(
    "create_vallery_release_manifest",
    ROOT / ".github/scripts/create_vallery_release_manifest.py",
)
INPUTS = load_module(
    "verify_vallery_release_inputs",
    ROOT / ".github/scripts/verify_vallery_release_inputs.py",
)


class ReleaseWorkflowContractTest(unittest.TestCase):
    def test_release_order_allows_later_controlled_releases(self) -> None:
        first = INPUTS.release_key("v0.18.0-vallery.20260820.1")
        second = INPUTS.release_key("v0.18.0-vallery.20260821.2")
        next_upstream = INPUTS.release_key("v0.19.0-vallery.20260901.1")
        self.assertLess(first, second)
        self.assertLess(second, next_upstream)
        with self.assertRaises(SystemExit):
            INPUTS.release_key("v0.18.0-vallery.20260820.0")

    def test_privileged_workflow_is_reusable_call_only_and_protected(self) -> None:
        body = BUILD_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_call:", body)
        self.assertNotIn("workflow_dispatch:", body)
        self.assertNotIn("pull_request_target", body.split("permissions:", 1)[0])
        self.assertNotIn("\n  pull_request:", body)
        self.assertEqual(body.count("runs-on: build-trusted"), 1)
        self.assertIn("timeout-minutes: 360", body)
        self.assertNotIn("timeout-minutes: 480", body)
        self.assertIn('test "${GITHUB_REF}" = "refs/heads/vallery/prod"', body)
        self.assertIn('test "${REF_PROTECTED}" = "true"', body)
        self.assertIn('test "${GITHUB_SHA}" = "${SOURCE_SHA}"', body)
        self.assertIn("test ! -S /var/run/docker.sock", body)
        self.assertIn('test -z "${KUBECONFIG:-}"', body)

    def test_build_uses_one_rootless_remote_bake_for_both_registries(self) -> None:
        body = BUILD_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("--driver remote", body)
        self.assertNotIn("--buildkitd-config", body)
        self.assertIn("inspect_zot_push_digest", body)
        self.assertIn("http.client.HTTPConnection(registry, timeout=30)", body)
        self.assertIn('connection.request("HEAD", path, headers=headers)', body)
        self.assertIn('"Authorization": f"Basic {encoded_auth}"', body)
        self.assertIn("BUILDKIT_HOST: tcp://127.0.0.1:1234", body)
        self.assertIn("docker --version", body)
        self.assertNotIn("docker version --format", body)
        self.assertIn("--file docker/tensorrt/trt.hcl", body)
        self.assertIn("--file .vallery/release-build.hcl", body)
        self.assertIn("--provenance mode=max", body)
        self.assertIn("--sbom true", body)
        self.assertIn("vallery-standard.tags=${standard_ghcr}", body)
        self.assertIn("vallery-standard.tags+=${standard_zot}", body)
        self.assertIn("tensorrt.tags=${tensorrt_ghcr}", body)
        self.assertIn("tensorrt.tags+=${tensorrt_zot}", body)
        self.assertNotIn("tags=${standard_ghcr},${standard_zot}", body)
        self.assertNotIn("tags=${tensorrt_ghcr},${tensorrt_zot}", body)
        self.assertIn("jvallery/frigate/arc/cache/standard", body)
        self.assertIn("jvallery/frigate/arc/cache/tensorrt", body)
        self.assertNotIn("registry-origin", body)
        self.assertIn('default_config = Path.home() / ".docker" / "config.json"', body)
        self.assertIn("default_config.symlink_to(target)", body)
        self.assertIn('test "$(readlink "${default_config}")" = "${DOCKER_CONFIG}/config.json"', body)

    def test_untrusted_workflow_cannot_receive_private_authorities(self) -> None:
        integration = INTEGRATION_WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("\n  pull_request_target:", integration)
        self.assertIn('test -z "${KUBECONFIG:-}"', integration)
        self.assertIn('test -z "${DOCKER_CONFIG:-}"', integration)
        self.assertNotIn("VALLERY_ZOT_PUSH_REGISTRY", integration)

    def test_every_referenced_action_is_immutable(self) -> None:
        for workflow in (
            BUILD_WORKFLOW,
            RELEASE_WORKFLOW,
            RETIREMENT_WORKFLOW,
            INTEGRATION_WORKFLOW,
        ):
            for line in workflow.read_text(encoding="utf-8").splitlines():
                match = re.search(r"\buses:\s*([^\s]+)", line)
                if not match or match.group(1).startswith("./"):
                    continue
                self.assertRegex(
                    match.group(1),
                    r"^[^@]+@[0-9a-f]{40}$",
                    f"{workflow.name}: unpinned action {match.group(1)}",
                )

    def test_release_dispatch_is_sentinel_scoped(self) -> None:
        body = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("repositories: sentinel", body)
        self.assertIn("permission-actions: write", body)
        self.assertIn("permission-contents: read", body)
        self.assertIn("/installation/repositories", body)
        self.assertIn('test "${repositories}" = "jvallery/sentinel"', body)
        self.assertIn(
            "repos/jvallery/sentinel/actions/workflows/frigate-promotion.yml/dispatches",
            body,
        )
        self.assertNotIn("repos/jvallery/sentinel/dispatches", body)
        self.assertNotIn('event_type:"frigate-release"', body)
        self.assertNotIn("KUBECONFIG", body)
        self.assertNotIn("kubectl", body)

    def test_failed_dispatch_can_reuse_only_byte_identical_release_assets(self) -> None:
        body = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("reusing exact immutable release assets", body)
        self.assertIn('test "${actual}" = "${expected}"', body)
        self.assertIn('cmp "release-evidence/${name}"', body)
        self.assertNotIn("--clobber", body)

    def test_release_ids_are_immutable_before_build_or_asset_publication(self) -> None:
        body = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Refuse mutable release replacement", body)
        self.assertIn("needs: preflight", body)
        self.assertNotIn("--clobber", body)
        self.assertIn("sha256sum --check release-manifest.sha256", body)

    def test_build_log_is_redacted_and_source_epoch_reaches_buildkit(self) -> None:
        body = BUILD_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('export SOURCE_DATE_EPOCH="${source_epoch}"', body)
        self.assertIn('raw.replace(private_registry, "<zot-push-registry>")', body)
        self.assertIn('evidence / "build.log"', body)
        self.assertIn('"private_registry_redacted"', body)

    def test_retirement_is_internal_exact_match_and_not_automatic_removal(self) -> None:
        body = RETIREMENT_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("plan_upstream_patch_retirement.py", body)
        self.assertIn("automation/patch-retirement-", body)
        self.assertIn("--apply-plan", body)
        self.assertIn("marks matches as upstreamed, not retired", body)
        self.assertIn("--force-with-lease=", body)
        self.assertIn("--state open", body)
        self.assertNotIn("repos/blakeblackshear/frigate", body)

    def test_bake_group_contains_exact_release_variants(self) -> None:
        body = BAKE.read_text(encoding="utf-8")
        self.assertIn('targets = ["vallery-standard", "tensorrt"]', body)
        self.assertIn('target "vallery-standard"', body)
        self.assertIn('dockerfile = "docker/main/Dockerfile"', body)
        self.assertIn('target     = "frigate"', body)
        self.assertIn('platforms  = ["linux/amd64"]', body)

    def test_model_archive_extracts_without_restoring_foreign_ownership(self) -> None:
        body = MAIN_DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn(
            "tar --no-same-owner -xvf "
            "ssdlite_mobilenet_v2_coco_2018_05_09.tar.gz",
            body,
        )

    def test_manifest_is_public_and_digest_closed(self) -> None:
        digest_a = "sha256:" + "a" * 64
        digest_b = "sha256:" + "b" * 64
        metadata = {
            "vallery-standard": {"containerimage.digest": digest_a},
            "tensorrt": {"containerimage.digest": digest_b},
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            metadata_path = root / "metadata.json"
            ledger_path = root / "ledger.json"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            ledger_path.write_text(
                json.dumps(
                    {
                        "patches": [
                            {
                                "id": "VLY-TEST-001",
                                "status": "candidate",
                                "affected_files": ["migrations/036_add_query_indexes.py"],
                                "migration_class": "additive_schema_indexes",
                                "rollback_class": "image_rollback_safe_indexes_remain",
                            },
                            {
                                "id": "VLY-TEST-002",
                                "status": "candidate",
                                "affected_files": ["migrations/900_performance_indexes.py"],
                                "migration_class": "history_compatibility_noop",
                                "rollback_class": "required_for_existing_history_image_rollback_safe",
                            },
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            manifest = MANIFEST.create(
                Namespace(
                    metadata=metadata_path,
                    patch_ledger=ledger_path,
                    release_id="v0.18.0-vallery.20260820.1",
                    source_sha="1" * 40,
                    upstream_sha="2" * 40,
                    upstream_branch="dev",
                    standard_digest=digest_a,
                    tensorrt_digest=digest_b,
                    source_date_epoch=1,
                    workflow_url="https://github.com/jvallery/frigate/actions/runs/1",
                )
            )
        serialized = json.dumps(manifest)
        self.assertEqual(manifest["artifacts"]["tensorrt-amd64"]["digest"], digest_b)
        self.assertIn(f"ghcr.io/jvallery/frigate@{digest_b}", serialized)
        self.assertIn(f"registry.vallery.net/jvallery/frigate@{digest_b}", serialized)
        self.assertNotIn("registry-origin", serialized)
        self.assertFalse(manifest["promotion"]["cluster_mutated_by_build"])
        self.assertEqual(
            [row["name"] for row in manifest["migrations"]],
            ["036_add_query_indexes", "900_performance_indexes"],
        )

    def test_schema_requires_both_variants(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["source"]["additionalProperties"], False)
        self.assertEqual(schema["properties"]["build"]["additionalProperties"], False)
        self.assertIn("migrations", schema["required"])
        self.assertEqual(schema["properties"]["migrations"]["minItems"], 1)
        self.assertEqual(
            schema["properties"]["artifacts"]["required"],
            ["standard-amd64", "tensorrt-amd64"],
        )


if __name__ == "__main__":
    unittest.main()
