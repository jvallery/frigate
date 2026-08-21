#!/usr/bin/env python3
"""Create the public, value-blind manifest for one exact Vallery Frigate build."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RELEASE_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+-vallery\.[0-9]{8}\.[1-9][0-9]*$")


def fail(message: str) -> None:
    raise SystemExit(f"release-manifest: {message}")


def target_digest(metadata: dict, target: str) -> str:
    record = metadata.get(target)
    if not isinstance(record, dict):
        fail(f"build metadata is missing target {target!r}")
    digest = record.get("containerimage.digest")
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
        fail(f"target {target!r} has no valid container image digest")
    return digest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def active_migrations(ledger: dict) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for patch in ledger.get("patches") or []:
        if patch.get("status") == "retired":
            continue
        migration_class = patch.get("migration_class")
        rollback_class = patch.get("rollback_class")
        for path in patch.get("affected_files") or []:
            if not re.fullmatch(r"migrations/[0-9]{3}_[a-z0-9_]+\.py", str(path)):
                continue
            if not isinstance(migration_class, str) or not migration_class:
                fail(f"migration patch {patch.get('id')!r} has no migration class")
            if not isinstance(rollback_class, str) or not rollback_class:
                fail(f"migration patch {patch.get('id')!r} has no rollback class")
            records.append(
                {
                    "name": Path(path).stem,
                    "path": path,
                    "migration_class": migration_class,
                    "rollback_class": rollback_class,
                }
            )
    records.sort(key=lambda record: record["path"])
    if not records:
        fail("active patch ledger contains no migration inventory")
    if len({record["path"] for record in records}) != len(records):
        fail("active patch ledger contains a duplicate migration path")
    return records


def create(args: argparse.Namespace) -> dict:
    for label, value in (("source", args.source_sha), ("upstream", args.upstream_sha)):
        if not SHA_RE.fullmatch(value):
            fail(f"{label} SHA must be 40 lowercase hexadecimal characters")
    if not RELEASE_RE.fullmatch(args.release_id):
        fail("release ID has an invalid format")

    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    patch_ledger = json.loads(args.patch_ledger.read_text(encoding="utf-8"))
    standard_digest = target_digest(metadata, "vallery-standard")
    tensorrt_digest = target_digest(metadata, "tensorrt")
    if args.standard_digest != standard_digest:
        fail("verified standard digest disagrees with Buildx metadata")
    if args.tensorrt_digest != tensorrt_digest:
        fail("verified TensorRT digest disagrees with Buildx metadata")

    artifacts: dict[str, dict] = {}
    for variant, digest in (
        ("standard-amd64", standard_digest),
        ("tensorrt-amd64", tensorrt_digest),
    ):
        tag_suffix = "standard" if variant == "standard-amd64" else "tensorrt"
        artifacts[variant] = {
            "digest": digest,
            "ghcr": {
                "tag": f"ghcr.io/jvallery/frigate:{args.release_id}-{tag_suffix}",
                "canonical": f"ghcr.io/jvallery/frigate@{digest}",
                "verified_digest": digest,
            },
            "zot": {
                "tag": f"registry.vallery.net/jvallery/frigate:{args.release_id}-{tag_suffix}",
                "canonical": f"registry.vallery.net/jvallery/frigate@{digest}",
                "verified_digest": digest,
            },
            "sbom": f"{tag_suffix}.sbom.spdx.json",
            "buildkit_provenance": f"{tag_suffix}.provenance.json",
        }

    return {
        "schema": "vallery.frigate.release/1",
        "release_id": args.release_id,
        "created_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "source": {
            "upstream_repository": "blakeblackshear/frigate",
            "upstream_branch": args.upstream_branch,
            "upstream_sha": args.upstream_sha,
            "fork_repository": "jvallery/frigate",
            "fork_branch": "vallery/prod",
            "fork_sha": args.source_sha,
        },
        "build": {
            "workflow_run": args.workflow_url,
            "runner_profile": "build-trusted",
            "platform": "linux/amd64",
            "buildkit": "v0.31.2-rootless",
            "buildx": "v0.36.1",
            "docker_cli": "29.7.2",
            "source_date_epoch": args.source_date_epoch,
            "cache_namespace": "jvallery/frigate/arc/cache",
        },
        "artifacts": artifacts,
        "patch_ledger": {
            "path": ".vallery/downstream-patches.json",
            "sha256": sha256_file(args.patch_ledger),
        },
        "migrations": active_migrations(patch_ledger),
        "promotion": {
            "release_class": "additive-schema",
            "rollback_class": "image-rollback-safe-indexes-remain",
            "required_soaks": ["immediate", "15m", "24h"],
            "cluster_mutated_by_build": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--patch-ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--upstream-sha", required=True)
    parser.add_argument("--upstream-branch", choices=("dev", "0.19"), required=True)
    parser.add_argument("--standard-digest", required=True)
    parser.add_argument("--tensorrt-digest", required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--workflow-url", required=True)
    args = parser.parse_args()
    manifest = create(args)
    args.output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
