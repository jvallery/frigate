#!/usr/bin/env python3
"""Fail closed when the Vallery downstream patch ledger is stale or incomplete."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / ".vallery" / "downstream-patches.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PATCH_ID_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_RE = re.compile(r"^v[^\s]+-vallery\.\d{8}\.\d+$")
REQUIRED_CONCERNS = {
    "sqlite_query_indexes",
    "wal_safe_migration_backup",
    "bounded_explore_queries",
    "bounded_recording_days",
    "bounded_review_summaries",
    "metrics_endpoint_concurrency",
    "chat_image_limits",
    "genai_description_resilience",
    "arcface_cuda_recovery",
    "production_migration_history",
}
REQUIRED_PATCH_FIELDS = {
    "id",
    "concern",
    "title",
    "owner",
    "status",
    "upstream_base_sha",
    "local_commits",
    "disposition",
    "affected_files",
    "tests",
    "first_release",
    "migration_class",
    "rollback_class",
    "upstream",
    "retirement_condition",
}
VALID_STATUSES = {"candidate", "carried", "upstreamed", "retired"}
VALID_DISPOSITIONS = {"upstreamable", "intentionally_local"}


def fail(message: str) -> None:
    raise ValueError(message)


def git(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        input=input_text,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(f"{field} must be a non-empty string")
    return value


def require_string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        fail(f"{field} must be a non-empty list")
    if any(not isinstance(item, str) or not item for item in value):
        fail(f"{field} must contain only non-empty strings")
    if len(value) != len(set(value)):
        fail(f"{field} contains duplicate entries")
    return value


def stable_patch_id(commit: str) -> str:
    diff = git("show", "--pretty=format:", "--binary", commit)
    result = git("patch-id", "--stable", input_text=diff)
    if not result:
        fail(f"commit {commit} has no stable patch ID")
    return result.split()[0]


def validate_path(path: str, field: str, *, must_exist: bool) -> None:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        fail(f"{field} must be a repository-relative path: {path}")
    if must_exist and not (ROOT / candidate).is_file():
        fail(f"{field} does not exist: {path}")


def validate_upstream_links(value: Any, patch_id: str) -> None:
    if not isinstance(value, dict) or set(value) != {
        "issue",
        "discussion",
        "pull_request",
    }:
        fail(f"{patch_id}.upstream must contain issue, discussion, and pull_request")
    for link_type, link in value.items():
        if link is None:
            continue
        if not isinstance(link, str) or not link.startswith(
            "https://github.com/blakeblackshear/frigate/"
        ):
            fail(f"{patch_id}.upstream.{link_type} is not an upstream Frigate URL")


def validate_patch(entry: Any, selected_sha: str) -> tuple[str, str]:
    if not isinstance(entry, dict):
        fail("each patch must be an object")
    missing = REQUIRED_PATCH_FIELDS - set(entry)
    extra = set(entry) - REQUIRED_PATCH_FIELDS
    if missing or extra:
        fail(
            f"patch fields differ from schema; missing={sorted(missing)}, "
            f"extra={sorted(extra)}"
        )

    patch_id = require_string(entry["id"], "patch.id")
    concern = require_string(entry["concern"], f"{patch_id}.concern")
    for field in (
        "title",
        "owner",
        "migration_class",
        "rollback_class",
        "retirement_condition",
    ):
        require_string(entry[field], f"{patch_id}.{field}")

    status = entry["status"]
    if status not in VALID_STATUSES:
        fail(f"{patch_id}.status is invalid: {status}")
    disposition = entry["disposition"]
    if disposition not in VALID_DISPOSITIONS:
        fail(f"{patch_id}.disposition is invalid: {disposition}")
    if entry["upstream_base_sha"] != selected_sha:
        fail(f"{patch_id}.upstream_base_sha differs from selected upstream SHA")
    if not RELEASE_RE.fullmatch(
        require_string(entry["first_release"], "first_release")
    ):
        fail(f"{patch_id}.first_release is not a Vallery release ID")

    active = status != "retired"
    affected_files = require_string_list(
        entry["affected_files"], f"{patch_id}.affected_files"
    )
    tests = require_string_list(entry["tests"], f"{patch_id}.tests")
    for path in affected_files:
        validate_path(path, f"{patch_id}.affected_files", must_exist=active)
    for path in tests:
        validate_path(path, f"{patch_id}.tests", must_exist=active)

    commits = entry["local_commits"]
    if not isinstance(commits, list) or not commits:
        fail(f"{patch_id}.local_commits must be a non-empty list")
    changed_paths: set[str] = set()
    for commit_entry in commits:
        if not isinstance(commit_entry, dict) or set(commit_entry) != {
            "sha",
            "patch_id",
        }:
            fail(f"{patch_id}.local_commits entries require sha and patch_id")
        commit = require_string(commit_entry["sha"], f"{patch_id}.local_commits.sha")
        recorded_patch_id = require_string(
            commit_entry["patch_id"], f"{patch_id}.local_commits.patch_id"
        )
        if not SHA_RE.fullmatch(commit) or not PATCH_ID_RE.fullmatch(recorded_patch_id):
            fail(f"{patch_id} contains a non-40-character commit or patch ID")
        git("cat-file", "-e", f"{commit}^{{commit}}")
        if active:
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
                cwd=ROOT,
                check=True,
                capture_output=True,
            )
        actual_patch_id = stable_patch_id(commit)
        if recorded_patch_id != actual_patch_id:
            fail(
                f"{patch_id} commit {commit} patch ID is stale: "
                f"recorded {recorded_patch_id}, actual {actual_patch_id}"
            )
        changed_paths.update(
            git("diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines()
        )

    unowned_paths = (set(affected_files) | set(tests)) - changed_paths
    if unowned_paths:
        fail(
            f"{patch_id} lists paths not owned by its commits: {sorted(unowned_paths)}"
        )
    validate_upstream_links(entry["upstream"], patch_id)
    return patch_id, concern


def main() -> int:
    document = json.loads(LEDGER.read_text())
    if document.get("schema_version") != 1:
        fail("unsupported downstream patch ledger schema_version")
    if set(document) != {
        "schema_version",
        "ledger_owner",
        "selected_upstream",
        "first_candidate_release",
        "patches",
    }:
        fail("downstream patch ledger has missing or unknown top-level fields")
    require_string(document["ledger_owner"], "ledger_owner")

    selected = document["selected_upstream"]
    if not isinstance(selected, dict) or set(selected) != {
        "repository",
        "branch",
        "sha",
    }:
        fail("selected_upstream must contain repository, branch, and sha")
    if selected["repository"] != "blakeblackshear/frigate":
        fail("selected_upstream.repository is not the authoritative upstream")
    require_string(selected["branch"], "selected_upstream.branch")
    selected_sha = require_string(selected["sha"], "selected_upstream.sha")
    if not SHA_RE.fullmatch(selected_sha):
        fail("selected_upstream.sha must be a complete SHA")
    git("cat-file", "-e", f"{selected_sha}^{{commit}}")
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", selected_sha, "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    first_release = require_string(
        document["first_candidate_release"], "first_candidate_release"
    )
    if not RELEASE_RE.fullmatch(first_release):
        fail("first_candidate_release is not a Vallery release ID")

    patches = document["patches"]
    if not isinstance(patches, list) or not patches:
        fail("patches must be a non-empty list")
    validated = [validate_patch(entry, selected_sha) for entry in patches]
    patch_ids = [item[0] for item in validated]
    concerns = [item[1] for item in validated]
    if len(patch_ids) != len(set(patch_ids)):
        fail("patch IDs must be unique")
    if len(concerns) != len(set(concerns)):
        fail("patch concerns must be unique")
    missing_concerns = REQUIRED_CONCERNS - set(concerns)
    if missing_concerns:
        fail(
            f"initial downstream concerns are unaccounted for: {sorted(missing_concerns)}"
        )

    print(
        f"verified {len(patches)} downstream patches at upstream {selected_sha}; "
        f"first candidate {first_release}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"downstream patch ledger verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
