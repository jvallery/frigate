#!/usr/bin/env python3
"""Verify exact public inputs for one Frigate development promotion."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from typing import Any, NoReturn

from prepare_local_dev_promotion import load_manifest

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RUN_RE = re.compile(
    r"^https://github\.com/jvallery/frigate/actions/runs/[1-9][0-9]*$"
)


def fail(message: str) -> NoReturn:
    """Exit with one value-blind verification error."""

    raise SystemExit(f"local-dev-release: {message}")


def require(condition: bool, message: str) -> None:
    """Enforce one exact release identity invariant."""

    if not condition:
        fail(message)


def verify(
    manifest_path: Path,
    release_id: str,
    source_sha: str,
    upstream_sha: str,
    standard_digest: str,
    manifest_sha256: str,
    manifest_url: str,
    run_url: str,
) -> dict[str, Any]:
    """Return a manifest only when every externally supplied identity agrees."""

    require(SHA_RE.fullmatch(source_sha) is not None, "source SHA is invalid")
    require(SHA_RE.fullmatch(upstream_sha) is not None, "upstream SHA is invalid")
    require(
        DIGEST_RE.fullmatch(standard_digest) is not None,
        "standard digest is invalid",
    )
    require(
        DIGEST_RE.fullmatch(manifest_sha256) is not None,
        "manifest digest is invalid",
    )
    require(RUN_RE.fullmatch(run_url) is not None, "release run URL is invalid")
    require(
        manifest_url
        == (
            "https://github.com/jvallery/frigate/releases/download/"
            f"{release_id}/release-manifest.json"
        ),
        "manifest URL is not the canonical Frigate release asset",
    )

    try:
        body = manifest_path.read_bytes()
    except OSError:
        fail("cannot read the public release manifest")
    observed_hash = f"sha256:{hashlib.sha256(body).hexdigest()}"
    require(observed_hash == manifest_sha256, "manifest digest disagrees")

    manifest = load_manifest(manifest_path)
    require(manifest["release_id"] == release_id, "release ID disagrees")
    require(manifest["source"]["fork_sha"] == source_sha, "source SHA disagrees")
    require(
        manifest["source"]["upstream_sha"] == upstream_sha,
        "upstream SHA disagrees",
    )
    require(
        manifest["artifacts"]["standard-amd64"]["digest"] == standard_digest,
        "standard digest disagrees",
    )
    require(manifest["build"]["workflow_run"] == run_url, "release run disagrees")
    return manifest


def main() -> int:
    """Verify one release manifest from command-line identities."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--upstream-sha", required=True)
    parser.add_argument("--standard-digest", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--manifest-url", required=True)
    parser.add_argument("--run-url", required=True)
    args = parser.parse_args()
    verify(
        args.manifest,
        args.release_id,
        args.source_sha,
        args.upstream_sha,
        args.standard_digest,
        args.manifest_sha256,
        args.manifest_url,
        args.run_url,
    )
    print("local-dev-release: exact public release verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
