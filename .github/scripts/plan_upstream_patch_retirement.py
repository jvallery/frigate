#!/usr/bin/env python3
"""Detect exact upstream patch matches and prepare a fail-closed ledger update."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def fail(message: str) -> None:
    raise ValueError(message)


def git(repo: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        input=input_text,
        text=True,
    )
    if result.returncode != 0:
        fail(f"git {' '.join(args)} failed")
    return result.stdout.strip()


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", older, newer],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def stable_patch_id(repo: Path, commit: str) -> str | None:
    patch = git(repo, "show", "--pretty=format:", "--binary", commit)
    if not patch:
        return None
    result = git(repo, "patch-id", "--stable", input_text=patch)
    if not result:
        return None
    patch_id = result.split()[0]
    if not SHA_RE.fullmatch(patch_id):
        fail(f"commit {commit} produced an invalid stable patch ID")
    return patch_id


def load_ledger(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("patches"), list):
        fail("patch ledger is not an object with a patches list")
    return document


def plan(repo: Path, ledger_path: Path, upstream_ref: str) -> dict[str, Any]:
    ledger = load_ledger(ledger_path)
    selected = ledger.get("selected_upstream")
    if not isinstance(selected, dict):
        fail("patch ledger has no selected_upstream object")
    selected_sha = selected.get("sha")
    branch = selected.get("branch")
    if not isinstance(selected_sha, str) or not SHA_RE.fullmatch(selected_sha):
        fail("selected upstream SHA is invalid")
    if branch not in {"dev", "0.19"}:
        fail("selected upstream branch is not approved")

    upstream_tip = git(repo, "rev-parse", f"{upstream_ref}^{{commit}}")
    if not SHA_RE.fullmatch(upstream_tip):
        fail("upstream ref did not resolve to a full commit SHA")
    if not is_ancestor(repo, selected_sha, upstream_tip):
        fail("upstream ref rewrites or does not descend from the selected upstream SHA")

    commits = git(repo, "rev-list", "--reverse", f"{selected_sha}..{upstream_tip}")
    commit_list = commits.splitlines() if commits else []
    upstream_by_patch: dict[str, list[str]] = {}
    for commit in commit_list:
        patch_id = stable_patch_id(repo, commit)
        if patch_id is not None:
            upstream_by_patch.setdefault(patch_id, []).append(commit)

    candidates: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for patch in ledger["patches"]:
        if not isinstance(patch, dict):
            fail("patch ledger contains a non-object entry")
        if patch.get("disposition") != "upstreamable" or patch.get("status") in {
            "upstreamed",
            "retired",
        }:
            continue
        patch_id = patch.get("id")
        concern = patch.get("concern")
        local_commits = patch.get("local_commits")
        if not isinstance(patch_id, str) or not isinstance(concern, str):
            fail("upstreamable patch lacks an ID or concern")
        if not isinstance(local_commits, list) or not local_commits:
            fail(f"{patch_id} has no local commits")

        matches: list[dict[str, str]] = []
        complete = True
        for local in local_commits:
            if not isinstance(local, dict):
                fail(f"{patch_id} contains an invalid local commit entry")
            local_patch_id = local.get("patch_id")
            if not isinstance(local_patch_id, str) or not SHA_RE.fullmatch(
                local_patch_id
            ):
                fail(f"{patch_id} contains an invalid stable patch ID")
            upstream_commits = upstream_by_patch.get(local_patch_id, [])
            if len(upstream_commits) > 1:
                fail(
                    f"{patch_id} patch ID {local_patch_id} matches multiple upstream commits"
                )
            if not upstream_commits:
                complete = False
                break
            matches.append(
                {
                    "local_patch_id": local_patch_id,
                    "upstream_commit": upstream_commits[0],
                }
            )
        if complete:
            candidates.append(
                {
                    "patch_id": patch_id,
                    "concern": concern,
                    "prior_status": patch.get("status"),
                    "matches": matches,
                }
            )
        else:
            unmatched.append(patch_id)

    return {
        "schema_version": 1,
        "upstream_branch": branch,
        "selected_sha": selected_sha,
        "upstream_tip": upstream_tip,
        "scanned_commit_count": len(commit_list),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "unmatched_upstreamable_patches": unmatched,
    }


def apply_plan(ledger_path: Path, plan_document: dict[str, Any]) -> None:
    ledger = load_ledger(ledger_path)
    selected = ledger.get("selected_upstream")
    if not isinstance(selected, dict):
        fail("patch ledger has no selected_upstream object")
    if selected.get("sha") != plan_document.get("selected_sha"):
        fail("retirement plan no longer starts at the ledger's selected upstream SHA")
    if selected.get("branch") != plan_document.get("upstream_branch"):
        fail("retirement plan branch differs from the patch ledger")
    upstream_tip = plan_document.get("upstream_tip")
    if not isinstance(upstream_tip, str) or not SHA_RE.fullmatch(upstream_tip):
        fail("retirement plan has an invalid upstream tip")
    candidates = plan_document.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        fail("refusing to apply a retirement plan without exact candidates")
    candidate_ids = [candidate.get("patch_id") for candidate in candidates]
    if any(not isinstance(item, str) for item in candidate_ids) or len(
        candidate_ids
    ) != len(set(candidate_ids)):
        fail("retirement plan candidate IDs are invalid or duplicated")

    seen: set[str] = set()
    for patch in ledger["patches"]:
        if not isinstance(patch, dict):
            fail("patch ledger contains a non-object entry")
        patch["upstream_base_sha"] = upstream_tip
        if patch.get("id") in candidate_ids:
            if patch.get("disposition") != "upstreamable" or patch.get("status") in {
                "upstreamed",
                "retired",
            }:
                fail(f"{patch.get('id')} is not eligible for upstream detection")
            patch["status"] = "upstreamed"
            seen.add(patch["id"])
    missing = set(candidate_ids) - seen
    if missing:
        fail(f"retirement plan references unknown patches: {sorted(missing)}")
    selected["sha"] = upstream_tip
    ledger_path.write_text(
        json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument(
        "--ledger", type=Path, default=Path(".vallery/downstream-patches.json")
    )
    parser.add_argument("--upstream-ref")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--apply-plan", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    ledger_path = args.ledger
    if not ledger_path.is_absolute():
        ledger_path = repo / ledger_path

    if args.apply_plan:
        apply_plan(ledger_path, json.loads(args.apply_plan.read_text(encoding="utf-8")))
        return
    if not args.upstream_ref or not args.output:
        parser.error(
            "--upstream-ref and --output are required when not applying a plan"
        )
    document = plan(repo, ledger_path, args.upstream_ref)
    args.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(document, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"upstream-patch-retirement: {error}") from None
