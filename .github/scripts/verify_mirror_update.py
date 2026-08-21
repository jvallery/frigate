#!/usr/bin/env python3
"""Verify that a mirror transition is exact and fast-forward-only."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class MirrorUpdateError(ValueError):
    pass


def verify_update(repo: Path, old_sha: str, new_sha: str) -> str:
    if not FULL_SHA.fullmatch(old_sha) or not FULL_SHA.fullmatch(new_sha):
        raise MirrorUpdateError("mirror identities must be complete lowercase 40-character SHAs")
    if old_sha == new_sha:
        return "already-current"
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", old_sha, new_sha],
        check=False,
    )
    if result.returncode == 0:
        return "verified-fast-forward"
    if result.returncode == 1:
        raise MirrorUpdateError(f"rewrite-or-divergence:{old_sha}:{new_sha}")
    raise MirrorUpdateError("unable to determine mirror ancestry")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    args = parser.parse_args()
    try:
        print(verify_update(args.repo, args.old, args.new))
    except MirrorUpdateError as error:
        print(error)
        return 20
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
