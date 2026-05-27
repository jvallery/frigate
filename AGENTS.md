# Frigate Fork Instructions

This repository is an active fork of Frigate for Sentinel surveillance
evaluation. Treat it as an upstream-derived codebase with local customization
boundaries, not as a generic Agent Fleet project scaffold.

## Boundaries

- Preserve the upstream fork relationship and avoid broad rewrites that make
  future upstream comparison or cherry-picking harder.
- Do not add Agent Fleet `project.yaml` or reusable CI unless a fork-specific
  rationale is approved. Issue `jvallery/agents#218` records the current waiver.
- Do not commit secrets, camera credentials, stream URLs, home network details,
  `.env` values, private keys, or runtime NVR data.
- Keep Sentinel deployment decisions in the Sentinel/control-plane issues, not
  hidden in this fork.

## Workflow

- Use GitHub issues and pull requests for durable local fork changes.
- Keep changes scoped to the issue or upstream patch being evaluated.
- Prefer upstream-compatible validation commands already present in this repo.
- Before handoff, run `git diff --check` and the narrowest relevant existing
  Frigate checks for touched code.

## Coordinator Context

The repo coordinator for this repository is `frigate-1-coordinator` in the
Agent Deck `coding` profile. Control-plane workflow and waiver decisions are
tracked in `jvallery/agents#215` and `jvallery/agents#218`.
