#!/usr/bin/env python3
"""Prepare one public, digest-only Frigate development promotion."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any, NoReturn

RELEASE_SCHEMA = "vallery.frigate.release/1"
RECEIPT_SCHEMA = "vallery.frigate.dev-promotion/1"
RELEASE_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+-vallery\.[0-9]{8}\.[1-9][0-9]*$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^registry\.vallery\.net/jvallery/frigate@sha256:[0-9a-f]{64}$")
RUN_RE = re.compile(r"^https://github\.com/jvallery/frigate/actions/runs/[1-9][0-9]*$")
IPV4_RE = re.compile(
    r"(?<![0-9])(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])"
    r"(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}(?![0-9])"
)

DEPLOYMENT = Path("deploy/kubernetes/local-dev/deployment.yaml")
RELEASES = Path("deploy/kubernetes/local-dev/releases")


def fail(message: str) -> NoReturn:
    """Exit with one value-blind promotion error."""

    raise SystemExit(f"local-dev-promotion: {message}")


def load_manifest(path: Path) -> dict[str, Any]:
    """Load and validate the public release fields used by this promotion."""

    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        fail("cannot read the public release manifest")
    if not isinstance(manifest, dict):
        fail("release manifest must be an object")
    if manifest.get("schema") != RELEASE_SCHEMA:
        fail("release manifest schema is invalid")

    release_id = manifest.get("release_id")
    if not isinstance(release_id, str) or RELEASE_RE.fullmatch(release_id) is None:
        fail("release ID is invalid")

    source = manifest.get("source")
    if not isinstance(source, dict):
        fail("release source identity is missing")
    if source.get("fork_repository") != "jvallery/frigate":
        fail("release source repository is invalid")
    if source.get("fork_branch") != "vallery/prod":
        fail("release source branch is invalid")
    for field in ("fork_sha", "upstream_sha"):
        value = source.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
            fail("release source SHA is invalid")

    build = manifest.get("build")
    if (
        not isinstance(build, dict)
        or RUN_RE.fullmatch(str(build.get("workflow_run"))) is None
    ):
        fail("release build workflow identity is invalid")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        fail("release artifacts are missing")
    standard = artifacts.get("standard-amd64")
    if not isinstance(standard, dict):
        fail("standard artifact is missing")
    digest = standard.get("digest")
    if not isinstance(digest, str) or DIGEST_RE.fullmatch(digest) is None:
        fail("standard digest is invalid")
    for registry in ("ghcr", "zot"):
        record = standard.get(registry)
        if not isinstance(record, dict) or record.get("verified_digest") != digest:
            fail("standard registry digest identity is invalid")
    if standard["ghcr"].get("canonical") != f"ghcr.io/jvallery/frigate@{digest}":
        fail("standard GHCR identity is invalid")
    candidate = f"registry.vallery.net/jvallery/frigate@{digest}"
    if standard["zot"].get("canonical") != candidate:
        fail("standard Zot identity is invalid")

    serialized = json.dumps(manifest, sort_keys=True)
    if IPV4_RE.search(serialized) or "rtsp://" in serialized.lower():
        fail("release manifest is not public-safe")
    forbidden_keys = {
        "camera",
        "credential",
        "database",
        "media",
        "password",
        "secret",
    }
    if any(token in str(key).lower() for key in manifest for token in forbidden_keys):
        fail("release manifest contains a forbidden top-level field")
    return manifest


def replace_once(
    text: str, pattern: re.Pattern[str], replacement: str, label: str
) -> str:
    """Replace one protected anchor and reject ambiguous desired state."""

    updated, count = pattern.subn(replacement, text)
    if count != 1:
        fail(f"{label} must have exactly one anchor")
    return updated


def replace_count(
    text: str,
    pattern: re.Pattern[str],
    replacement: str,
    expected: int,
    label: str,
) -> str:
    """Replace an exact number of repeated public identity labels."""

    updated, count = pattern.subn(replacement, text)
    if count != expected:
        fail(f"{label} must have exactly {expected} anchors")
    return updated


def git(repo: Path, *arguments: str, capture: bool = False) -> str:
    """Run one bounded Git query."""

    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=False,
        capture_output=capture,
        text=True,
    )
    if result.returncode != 0:
        fail("Git operation failed")
    return result.stdout if capture else ""


def current_anchors(repo: Path) -> tuple[str, str, str]:
    """Return the tracked release and immutable standard image anchors."""

    deployment_path = repo / DEPLOYMENT
    try:
        body = deployment_path.read_text(encoding="utf-8")
    except OSError:
        fail("development Deployment is missing")
    image_match = re.search(
        r"(?m)^        - name: frigate\n          image: (\S+)$", body
    )
    release_match = re.search(r"(?m)^    vallery\.net/release-id: (\S+)$", body)
    if image_match is None or release_match is None:
        fail("development image or release anchor is missing")
    image = image_match.group(1)
    release_id = release_match.group(1)
    versions = re.findall(r"(?m)^\s+app\.kubernetes\.io/version: (\S+)$", body)
    source_shas = re.findall(r"(?m)^\s+vallery\.net/source-sha: (\S+)$", body)
    if IMAGE_RE.fullmatch(image) is None or RELEASE_RE.fullmatch(release_id) is None:
        fail("existing development anchors are invalid")
    if versions != [release_id, release_id]:
        fail("existing development version labels disagree")
    if len(source_shas) != 2 or source_shas[0] != source_shas[1]:
        fail("existing development source labels disagree")
    if re.fullmatch(r"[0-9a-f]{40}", source_shas[0]) is None:
        fail("existing development source label is invalid")
    return release_id, image, source_shas[0]


def write_text(path: Path, content: str) -> None:
    """Write deterministic generated text."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def create_patch(repo: Path, destination: Path, reverse: bool) -> None:
    """Create and prove one Deployment-only forward or inverse patch."""

    arguments = ["diff"]
    if reverse:
        arguments.append("-R")
    arguments.extend(["--binary", "--", str(DEPLOYMENT)])
    patch = git(repo, *arguments, capture=True)
    if not patch.strip():
        fail("promotion produced no Deployment change")
    if "deploy/kubernetes/local-dev/deployment.yaml" not in patch:
        fail("generated patch is outside the development Deployment")
    write_text(destination, patch)
    check_arguments = ["git", "apply", "--check", str(destination)]
    if not reverse:
        check_arguments.insert(2, "--reverse")
    result = subprocess.run(check_arguments, cwd=repo, capture_output=True, text=True)
    if result.returncode != 0:
        fail("generated patch does not apply at its expected side")


def prepare(
    repo: Path,
    manifest_path: Path,
    promotion_run_url: str,
) -> Path:
    """Prepare the bounded change and return its release evidence directory."""

    repo = repo.resolve()
    if RUN_RE.fullmatch(promotion_run_url) is None:
        fail("promotion workflow identity is invalid")
    manifest = load_manifest(manifest_path)
    release_id = manifest["release_id"]
    digest = manifest["artifacts"]["standard-amd64"]["digest"]
    candidate = f"registry.vallery.net/jvallery/frigate@{digest}"

    if (
        subprocess.run(
            ["git", "diff", "--quiet", "--", str(DEPLOYMENT)], cwd=repo, check=False
        ).returncode
        != 0
    ):
        fail("development Deployment must be clean before promotion")

    previous_release_id, previous_image, previous_source_sha = current_anchors(repo)
    if candidate == previous_image:
        fail("candidate digest is already desired")
    evidence = repo / RELEASES / release_id
    if evidence.exists():
        fail("release ledger entry already exists")

    deployment_path = repo / DEPLOYMENT
    body = deployment_path.read_text(encoding="utf-8")
    body = replace_once(
        body,
        re.compile(r"(?m)^(        - name: frigate\n          image: )\S+$"),
        rf"\g<1>{candidate}",
        "Deployment image",
    )
    body = replace_once(
        body,
        re.compile(r"(?m)^(    vallery\.net/release-id: )\S+$"),
        rf"\g<1>{release_id}",
        "Deployment release",
    )
    body = replace_count(
        body,
        re.compile(r"(?m)^(\s+app\.kubernetes\.io/version: )\S+$"),
        rf"\g<1>{release_id}",
        2,
        "Deployment version label",
    )
    body = replace_count(
        body,
        re.compile(r"(?m)^(\s+vallery\.net/source-sha: )\S+$"),
        rf"\g<1>{manifest['source']['fork_sha']}",
        2,
        "Deployment source label",
    )
    deployment_path.write_text(body, encoding="utf-8")

    evidence.mkdir(parents=True)
    write_text(
        evidence / "release-manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "release_id": release_id,
        "environment": "frigate-dev",
        "source_sha": manifest["source"]["fork_sha"],
        "upstream_sha": manifest["source"]["upstream_sha"],
        "image": candidate,
        "previous_release_id": previous_release_id,
        "previous_image": previous_image,
        "previous_source_sha": previous_source_sha,
        "promotion_run": promotion_run_url,
        "deployment_state": "pending-protected-merge",
    }
    write_text(
        evidence / "promotion.json",
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
    )
    create_patch(repo, evidence / "inverse.patch", reverse=True)
    create_patch(repo, evidence / "desired-state.patch", reverse=False)
    return evidence


def main() -> int:
    """CLI entry point."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--release-manifest", type=Path, required=True)
    parser.add_argument("--promotion-run-url", required=True)
    args = parser.parse_args()
    evidence = prepare(args.repo, args.release_manifest, args.promotion_run_url)
    print(
        "local-dev-promotion: prepared public release ledger "
        f"{evidence.relative_to(args.repo.resolve())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
