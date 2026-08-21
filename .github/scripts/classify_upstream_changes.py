#!/usr/bin/env python3
"""Classify an exact upstream Git range without executing fetched source."""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
from pathlib import Path


ROOT_METADATA = {
    ".dockerignore",
    ".editorconfig",
    ".gitignore",
    ".pre-commit-config.yaml",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "Makefile",
    "README.md",
    "SECURITY.md",
}
DEPENDENCY_NAMES = {
    "package.json",
    "package-lock.json",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "poetry.lock",
    "uv.lock",
}


def git_changed_paths(repo: Path, base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=ACDMRTUXB", base, head],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def classify_path(path: str, patch_sensitive: list[str]) -> set[str]:
    categories: set[str] = set()
    name = Path(path).name

    if path.startswith("migrations/"):
        categories.add("migration")
    if name in DEPENDENCY_NAMES or name.startswith("requirements-"):
        categories.add("dependency")
    if path.startswith("docker/") or name.startswith("Dockerfile"):
        categories.add("docker")
    if (
        path.startswith(("frigate/config/", "config/", ".devcontainer/"))
        or name.endswith((".example", ".schema.json"))
    ):
        categories.add("configuration")
    if path.startswith(".github/"):
        categories.add("workflow")
    if matches_any(path, patch_sensitive):
        categories.add("patch-sensitive")
    if path.startswith(("frigate/test/", "web/src/__tests__/", "web/e2e/")) or name.startswith("test_"):
        categories.add("test")
    if path.startswith(("docs/", "web/public/locales/")) or name.endswith((".md", ".mdx")):
        categories.add("documentation")
    if path.startswith(("frigate/", "web/src/", "go2rtc/")) or name.endswith(
        (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".c", ".cc", ".h", ".sh")
    ):
        categories.add("source")
    if path.startswith(("web/public/", "docker/main/rootfs/")):
        categories.add("asset")
    if path in ROOT_METADATA or path.startswith(("scripts/", "docker-compose", "web/")):
        categories.add("project-metadata")

    return categories


def build_report(paths: list[str], patch_sensitive: list[str], base: str, head: str) -> dict:
    by_category: dict[str, list[str]] = {}
    unclassified: list[str] = []
    for path in paths:
        categories = classify_path(path, patch_sensitive)
        if not categories:
            unclassified.append(path)
            continue
        for category in sorted(categories):
            by_category.setdefault(category, []).append(path)

    risk_categories = [
        category
        for category in ("migration", "dependency", "docker", "configuration", "patch-sensitive")
        if category in by_category
    ]
    return {
        "schema_version": 1,
        "base_sha": base,
        "head_sha": head,
        "changed_path_count": len(paths),
        "categories": dict(sorted(by_category.items())),
        "risk_categories": risk_categories,
        "unclassified": unclassified,
        "safe_to_dispatch": not unclassified,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--policy", type=Path, default=Path(".vallery/upstream-policy.json"))
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    policy = json.loads(args.policy.read_text(encoding="utf-8"))
    paths = git_changed_paths(args.repo, args.base, args.head)
    report = build_report(paths, policy["patch_sensitive_paths"], args.base, args.head)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["safe_to_dispatch"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
