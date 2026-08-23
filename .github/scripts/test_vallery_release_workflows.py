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
DEV_PROMOTION_WORKFLOW = ROOT / ".github/workflows/frigate-dev-promotion.yml"
RETIREMENT_WORKFLOW = ROOT / ".github/workflows/upstream-patch-retirement.yml"
INTEGRATION_WORKFLOW = ROOT / ".github/workflows/vallery-integration.yml"
SYNC_WORKFLOW = ROOT / ".github/workflows/vallery-upstream-sync.yml"
BAKE = ROOT / ".vallery/release-build.hcl"
SCHEMA = ROOT / ".vallery/release-manifest.schema.json"
MAIN_DOCKERFILE = ROOT / "docker/main/Dockerfile"
DEV_DELIVERY_CONTRACT = ROOT / "deploy/kubernetes/local-dev/DELIVERY.md"


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
            DEV_PROMOTION_WORKFLOW,
            RETIREMENT_WORKFLOW,
            INTEGRATION_WORKFLOW,
            SYNC_WORKFLOW,
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

    def test_release_factory_is_environment_neutral_and_publish_only(self) -> None:
        body = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("name: Vallery immutable artifact release", body)
        self.assertIn("group: vallery-artifact-release-${{ inputs.release_id }}", body)
        self.assertNotIn("local-dev", body)
        self.assertNotIn("local-prod", body)
        self.assertNotIn("TARGET_ENVIRONMENT", body)
        self.assertNotIn("SENTINEL_PROMOTION", body)
        self.assertNotIn("jvallery/sentinel", body)
        self.assertNotIn("/dispatches", body)
        self.assertNotIn("secrets: inherit", body)
        self.assertIn("zot_push_registry: ${{ secrets.VALLERY_ZOT_PUSH_REGISTRY }}", body)
        self.assertIn("Publish immutable annotated release", body)
        self.assertNotIn("--clobber", body)

    def test_no_workflow_accepts_an_environment_selector(self) -> None:
        for workflow in sorted((ROOT / ".github/workflows").glob("*.yml")):
            body = workflow.read_text(encoding="utf-8")
            self.assertNotIn(
                "\n      environment:\n",
                body,
                f"{workflow.name} accepts an environment input",
            )

    def test_reusable_build_receives_only_explicit_zot_input(self) -> None:
        body = BUILD_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("    secrets:\n      zot_push_registry:", body)
        self.assertIn("required: true", body)
        self.assertIn("ZOT_PUSH_REGISTRY: ${{ secrets.zot_push_registry }}", body)
        self.assertNotIn("secrets.VALLERY_ZOT_PUSH_REGISTRY", body)
        self.assertNotIn("SENTINEL", body)

    def test_dev_promotion_has_a_frigate_only_writer_boundary(self) -> None:
        body = DEV_PROMOTION_WORKFLOW.read_text(encoding="utf-8")
        writer = body.split("  prepare-pr:\n", 1)[1]
        self.assertIn("name: Frigate development promotion", body)
        self.assertIn("group: frigate-dev-promotion\n", body)
        self.assertIn("environment: frigate-dev-promotion", body)
        self.assertIn("secrets.FRIGATE_DEV_PROMOTION_APP_ID", body)
        self.assertIn("secrets.FRIGATE_DEV_PROMOTION_PRIVATE_KEY", body)
        self.assertIn("repositories: frigate", body)
        self.assertIn("permission-metadata: read", body)
        self.assertIn("permission-contents: write", body)
        self.assertIn("permission-pull-requests: write", body)
        self.assertNotIn("permission-actions: write", body)
        self.assertIn("/installation/repositories", body)
        self.assertIn('test "${repositories}" = "jvallery/frigate"', body)
        self.assertIn(
            'test "${APP_SLUG}" = "vallery-frigate-dev-promotion"', body
        )
        self.assertIn(
            'test "${WRITER_INSTALLATION}" = "${INVENTORY_INSTALLATION}"', body
        )
        self.assertIn("if gh api repos/jvallery/sentinel", body)
        self.assertNotIn("SENTINEL_PROMOTION", body)
        self.assertNotIn("sentinel-private-preflight", body)
        self.assertNotIn("build-trusted", body)
        self.assertNotIn("/run/secrets", body)
        self.assertEqual(body.count("runs-on: ubuntu-24.04"), 2)
        self.assertEqual(
            set(re.findall(r"secrets\.([A-Z0-9_]+)", writer)),
            {
                "FRIGATE_DEV_PROMOTION_APP_ID",
                "FRIGATE_DEV_PROMOTION_PRIVATE_KEY",
            },
        )
        self.assertNotIn("github.token", writer)
        self.assertEqual(
            body.count("actions/create-github-app-token@"),
            2,
            "inventory and writer tokens must be independently scoped",
        )

    def test_dev_promotion_is_cross_repository_and_cross_path_closed(self) -> None:
        body = DEV_PROMOTION_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            'test "${WORKFLOW_REF}" = "jvallery/frigate/.github/workflows/'
            'frigate-dev-promotion.yml@refs/heads/vallery/prod"',
            body,
        )
        self.assertIn("prepare_local_dev_promotion.py", body)
        self.assertIn('expected="$(printf \'%s\\n\' \\', body)
        self.assertIn("deploy/kubernetes/local-dev/deployment.yaml", body)
        self.assertIn('"${evidence}/inverse.patch"', body)
        self.assertIn('test "${actual}" = "${expected}"', body)
        self.assertNotIn("platform/kubernetes/frigate", body)
        self.assertNotIn("deploy/argocd", body)
        self.assertNotIn("gh pr merge", body)
        self.assertNotIn("kubectl apply", body)

    def test_dev_promotion_verifies_only_the_standard_variant(self) -> None:
        body = DEV_PROMOTION_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Verify public standard release", body)
        self.assertIn("--standard-digest", body)
        self.assertIn("ghcr.io/jvallery/frigate@${STANDARD_DIGEST}", body)
        self.assertNotIn("TENSORRT_DIGEST", body)
        self.assertNotIn("tensorrt_digest:", body)
        self.assertIn("gh attestation verify", body)
        self.assertIn("vallery-tensorrt-build.yml", body)

    def test_production_selection_contract_is_pull_based_and_independent(self) -> None:
        body = DEV_DELIVERY_CONTRACT.read_text(encoding="utf-8")
        self.assertIn("Independent Sentinel production selection", body)
        self.assertIn("pulling immutable public release", body)
        self.assertIn("does not receive a dispatch", body)
        self.assertIn("release-manifest.sha256", body)
        self.assertIn("standard and TensorRT GitHub image attestations", body)
        self.assertIn("No Frigate development token", body)

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

    def test_build_generates_runtime_version_from_exact_source(self) -> None:
        body = BUILD_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Generate and prove runtime version assets", body)
        self.assertNotIn("make version", body)
        self.assertIn('["git", "log", "-1", "--pretty=format:%h", source_sha]', body)
        self.assertIn("path.write_text", body)
        self.assertIn('Path("web/.env").write_text', body)
        self.assertIn("module.VERSION != runtime_version", body)

    def test_retirement_is_internal_exact_match_and_not_automatic_removal(self) -> None:
        body = RETIREMENT_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("plan_upstream_patch_retirement.py", body)
        self.assertIn("automation/patch-retirement-", body)
        self.assertIn("--apply-plan", body)
        self.assertIn("marks matches as upstreamed, not retired", body)
        self.assertIn("--force-with-lease=", body)
        self.assertIn("--state open", body)
        self.assertNotIn("repos/blakeblackshear/frigate", body)

    def test_verified_dev_intake_survives_later_branch_failure(self) -> None:
        body = SYNC_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            'moved_dev="${new_sha}"\n'
            '              echo "moved_dev=${moved_dev}" >> "${GITHUB_OUTPUT}"',
            body,
        )
        self.assertIn(
            "if: always() && steps.mirrors.outputs.moved_dev != '' "
            "&& inputs.dry_run != true",
            body,
        )

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
