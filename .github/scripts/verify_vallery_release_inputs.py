#!/usr/bin/env python3
"""Fail-closed validation for protected Vallery Frigate release inputs."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_RE = re.compile(
    r"^v(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)"
    r"-vallery\.(?P<date>[0-9]{8})\.(?P<serial>[1-9][0-9]*)$"
)


def fail(message: str) -> None:
    raise SystemExit(f"release-inputs: {message}")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(f"git {' '.join(args)} failed")
    return result.stdout.strip()


def release_key(value: str) -> tuple[int, int, int, int, int]:
    match = RELEASE_RE.fullmatch(value)
    if match is None:
        fail("release ID must match vMAJOR.MINOR.PATCH-vallery.YYYYMMDD.N")
    return tuple(
        int(match.group(name)) for name in ("major", "minor", "patch", "date", "serial")
    )


def validate(repo: Path, source_sha: str, upstream_sha: str, release_id: str) -> dict:
    if not SHA_RE.fullmatch(source_sha):
        fail("source SHA must be 40 lowercase hexadecimal characters")
    if not SHA_RE.fullmatch(upstream_sha):
        fail("upstream SHA must be 40 lowercase hexadecimal characters")
    requested_release = release_key(release_id)

    ledger_path = repo / ".vallery" / "downstream-patches.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    selected = ledger.get("selected_upstream") or {}
    if selected.get("repository") != "blakeblackshear/frigate":
        fail("patch ledger selects an unexpected upstream repository")
    if selected.get("branch") not in {"dev", "0.19"}:
        fail("patch ledger selects an unapproved upstream branch")
    if selected.get("sha") != upstream_sha:
        fail("requested upstream SHA does not equal the reviewed patch-ledger base")
    first_candidate = str(ledger.get("first_candidate_release") or "")
    if requested_release < release_key(first_candidate):
        fail("release ID predates the patch ledger's first candidate release")

    if git(repo, "rev-parse", "HEAD") != source_sha:
        fail("checked-out HEAD does not equal the requested source SHA")
    git(repo, "cat-file", "-e", f"{upstream_sha}^{{commit}}")
    if (
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "merge-base",
                "--is-ancestor",
                upstream_sha,
                source_sha,
            ],
            check=False,
        ).returncode
        != 0
    ):
        fail("selected upstream SHA is not an ancestor of the fork source SHA")

    for patch in ledger.get("patches") or []:
        if patch.get("status") == "retired":
            continue
        patch_first_release = str(patch.get("first_release") or "")
        if release_key(patch_first_release) > requested_release:
            fail(
                f"active patch {patch.get('id')} first appears after the requested release"
            )
        for commit in patch.get("local_commits") or []:
            local_sha = str(commit.get("sha") or "")
            if not SHA_RE.fullmatch(local_sha):
                fail("active patch ledger entry contains an invalid local commit SHA")
            if (
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "merge-base",
                        "--is-ancestor",
                        local_sha,
                        source_sha,
                    ],
                    check=False,
                ).returncode
                != 0
            ):
                fail(
                    f"active patch commit {local_sha} is absent from the release source"
                )

    return {
        "release_id": release_id,
        "source_sha": source_sha,
        "upstream_repository": selected["repository"],
        "upstream_branch": selected["branch"],
        "upstream_sha": upstream_sha,
        "active_patch_count": sum(
            1
            for patch in ledger.get("patches") or []
            if patch.get("status") != "retired"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--upstream-sha", required=True)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            validate(
                args.repo.resolve(), args.source_sha, args.upstream_sha, args.release_id
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
