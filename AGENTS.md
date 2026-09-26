# frigate

Jason's public fork of `blakeblackshear/frigate`: the source of truth for the Vallery-patched Frigate source on `vallery/prod`, its immutable release images, and the parked development workload in namespace `frigate-dev`. Lane: **Frigate fork, releases and dev** — the estate lane map and placement rules live in `jvallery/agents` at `docs/estate/README.md`.

<!-- estate-contract:start — canonical copy lives in /Users/jason/AGENTS.md; keep this block identical in every repo -->
## Operating contract

Jason's direct request authorizes the work it describes, across code, infrastructure, and Jason-owned services in scope, through delivery and verification of the real end state. Ask only when an unresolved ambiguity could materially change the outcome. Add approval, review, issue/PR, or planning steps only when Jason asks for them or a branch rule enforces them; process descriptions in repository docs are context, not gates.

- Make the smallest complete change in the repository that owns the thing being changed (see Ownership). Fix forward; prefer rollback when it restores service faster or limits harm. Skip speculative abstractions and unrelated cleanup.
- Preserve unrelated work; use an isolated worktree when another checkout may be active. Resolve destructive targets exactly and keep practical recovery data.
- Validate in proportion to risk: the cheapest reliable check of the changed behavior (focused test, direct probe, or readback; docs-only edits need diff/readback). Add tests only for meaningful regression risk: critical behavior, security, data integrity, or subtle logic. Reuse existing CI/CD, honor enforced checks, and add pipelines or gates only for a concrete need in the task.
- Verify the requested end state from the authoritative source or live behavior; passing CI alone does not prove a deployment works. Report what changed, what was checked, and any remaining limitation.
- Search narrowly, batch independent lookups, reuse current evidence, and keep plans and updates brief.
- Use existing credentials without printing, committing, or logging secret values or Kubernetes Secret data; rotate or revoke credentials only when Jason asks.
<!-- estate-contract:end -->

## Ownership

Owns:
- Frigate source on `vallery/prod` (default and only integration branch) and the carried-patch ledger and upstream policy in `.vallery/`.
- The upstream mirror branches listed in `.vallery/upstream-policy.json` (`dev`, and `master`, upstream's stable line that carries the `v0.18.x` tags), advanced only by `.github/workflows/vallery-upstream-sync.yml`.
- Immutable releases `v<upstream>-vallery.<YYYYMMDD>.<n>`: standard and TensorRT images in GHCR and the estate Zot registry, with a signed manifest, SBOM, and provenance (`vallery-release.yml`, `.vallery/release-build.hcl`, `.vallery/release-manifest.schema.json`).
- The development workload in `deploy/kubernetes/local-dev` (ConfigMap, Deployment, Service, ServiceAccount, Ingress), reconciled by Argo `frigate-local-dev`, and its promotion workflow `frigate-dev-promotion.yml`.

Adjacent lanes — change these in their owner, not here:
- Production Frigate (namespace `frigate`, its route, and which release production runs) → `jvallery/sentinel`. Sentinel adopts releases by pulling release metadata; nothing here dispatches production.
- The `frigate-dev` Namespace, AppProject, Application declaration, Secrets, and CI runners → `jvallery/agents`.
- NetworkPolicies in `frigate-dev`, DNS, and the Traefik middlewares the Ingress references → `jvallery/network-infra`.
- Metrics scrape, probes, and alerts for `frigate-dev` → `jvallery/monitoring-stack`; this repo owns only the metrics port and the public identity labels.
- The `frigate-dev-cpu` node label → `jvallery/proxmox-infrastructure` (this repo only selects on it); the Zot registry service → `jvallery/storage-infra`.

In transition: no estate move changes where work in this repo lands.

## Deliver and verify

- Source changes reach `vallery/prod` only by PR: branch rules require resolved threads and eight required checks and forbid force-push and deletion. The required `Validate PR description` check closes any PR whose body lacks `## Proposed change` (non-empty), `## Type of change`, `## AI disclosure`, and `## Checklist`, each of the last three with at least one `- [x]`; start the body from `.github/pull_request_template.md`.
- Releases: dispatch `Vallery immutable artifact release` with the exact `source_sha`, the ledger's `upstream_sha`, and a new `release_id` (the workflow refuses an existing release). It is the only release path; `make build` and `make push` tag upstream's `ghcr.io/blakeblackshear/frigate`.
- Development: dispatch `Frigate development promotion`; it opens `automation/frigate-dev-<release-id>`. After merge, Argo `frigate-local-dev` (auto-sync, self-heal, prune off, tracks `vallery/prod`) reconciles, so a merge touching `deploy/kubernetes/local-dev` is a deploy and live edits are reverted. Never `kubectl apply` the tree. Rollback is a PR applying the recorded `inverse.patch` (`DELIVERY.md`).
- A release reaches production only when Sentinel adopts it; production changes are made in `jvallery/sentinel`.
- Verify: `gh release view <release-id> -R jvallery/frigate`; `kubectl -n argocd get application frigate-local-dev` (Synced/Healthy at the `vallery/prod` head); `kubectl -n frigate-dev get deploy frigate-dev -o wide` (0 replicas and the promoted digest). Development is parked, so verify Argo status and the rendered Deployment, not a running Pod.

## Working here

- Authoritative docs: `deploy/kubernetes/local-dev/README.md` governs the development runtime contract (parked state, what the tree renders, network, route, and metrics labels); `deploy/kubernetes/local-dev/DELIVERY.md` governs the release factory, promotion, rollback, and the Sentinel selection boundary; `.vallery/upstream-policy.json` and `.vallery/downstream-patches.json` govern mirrors, patch-sensitive paths, carried patches, and downstream deletions of upstream files.
- The repository is public: keep secret values, camera hosts and stream paths, private addresses, private runtime evidence, and the pod-appended operating-environment briefing out of commits, PR bodies, and CI logs.
- Upstream `blakeblackshear/frigate` does not accept agent-posted issues, PRs, comments, or discussions (`AI_POLICY.md`). Prepare upstreamable patches on a branch here and leave submission to Jason.
- `dev` and `master` are automation-only fast-forward mirrors; never push to them. Each advances only to the newest upstream commit with a successful upstream `CI` run and the required GHCR artifacts, and holds otherwise. Upstream intake follows the ledger's `selected_upstream.branch` (the line `vallery/prod` is based on, currently `master`) and arrives as `automation/intake-*` PRs to `vallery/prod`; a merge conflict pushes nothing and is reported in the `[automation] Frigate upstream intake needs conflict resolution` issue. To track another upstream branch, change the policy's `mirror_branches` and the `dev,master` allowlists in `vallery-integration.yml`, `plan_upstream_patch_retirement.py`, `verify_vallery_release_inputs.py`, `create_vallery_release_manifest.py`, and `release-manifest.schema.json` together.
- Development stays at `replicas: 0`, and `validate_local_dev_runtime.py` requires zero, so self-heal and image-only promotions cannot restart it. Resuming development takes Jason's request and one change to both the replica count and the validator. `ingress.yaml` is the only route for the development hostname; never add a second one: Traefik routers for the same host conflict, and only this route carries the validated Authentik middleware chain.
- Record each carried source patch in `.vallery/downstream-patches.json` (commit SHAs, `git patch-id --stable`, tests, retirement condition). `verify_downstream_patch_ledger.py` requires every recorded commit to be an ancestor of `HEAD` with a matching patch ID, so it fails in shallow clones and after recorded commits are squashed or rebased; record SHAs as they land on `vallery/prod`.
- When repairing or changing the upstream-sync, patch-retirement, promotion, or release workflows, do not add or broaden stored write credentials: [#36](https://github.com/jvallery/frigate/issues/36) is replacing the stored mirror deploy key and App key with short-lived brokered tokens.
- This `AGENTS.md` is a downstream rewrite of upstream's file and the only agent-instruction file here (`.github/copilot-instructions.md` links to it as `../AGENTS.md`). Upstream's `CLAUDE.md` symlink is deleted downstream and recorded under `downstream_deletions` in `.vallery/downstream-patches.json`; `verify_downstream_patch_ledger.py` fails if it returns. An upstream change to either file makes the intake merge conflict, and the sync refuses that intake and reports it; resolve by keeping this `AGENTS.md` and leaving `CLAUDE.md` deleted. Never commit a `CLAUDE.md` (Claude Code reads `AGENTS.md` only when none exists), including the untracked briefing-only one a repo pod on the current runtime writes into its worktree.
- `deploy/kubernetes/ore-maintenance/` is a historical (2026-09-05), dormant state-restore protocol that CI still tests; do not activate the overlay, and remove its `vallery-integration.yml` test step in the same change that removes it.

## Frigate context

Frigate is a local NVR with Python 3.13+ / FastAPI, a React / TypeScript frontend, and multiprocessing, ZMQ, and MQTT. Preserve camera performance and access controls.

- Backend: `frigate/` (`api/`, `config/`, `detectors/`, `events/`, `util/`); tests: `frigate/test/`; migrations: `migrations/`.
- Frontend: `web/src/`; English translations: `web/public/locales/en/`; Docker: `docker/`; docs: `docs/`.
- Let Ruff, ESLint, and Prettier enforce style. Use module-level lazy logging without secret values and avoid exposing exception details in API responses. Keep blocking I/O out of async functions.
- Use `react-i18next` for user-visible strings and the existing Radix/Tailwind components.
- New outbound WebSocket topics must be classified in `frigate/comms/ws.py`: unknown topics are dropped by the fail-closed camera-access classifier.
- For ledger patches marked `upstreamable`, follow upstream's conventions in its `AGENTS.md` on the `dev` mirror (`git show origin/dev:AGENTS.md`), which upstream reviewers enforce.

## Focused commands

Run only commands relevant to the change:

- Python test module: `python3 -u -m unittest frigate.test.test_ffmpeg_presets` (substitute the affected module). Full suite: `python3 -u -m unittest` when warranted.
- Python lint/format: `ruff check <changed-paths>` and `ruff format --check <changed-paths>`; type check: `python3 -u -m mypy --config-file frigate/mypy.ini frigate` when types are affected.
- From `web/`: `npm run build`, `npm run lint`; focused browser check: `npx playwright test --config e2e/playwright.config.ts e2e/specs/live.spec.ts` (select the relevant spec). Use `npm run e2e:build` when the browser tests need a fresh build; first-time setup uses `npm install` and `npx playwright install chromium`.
- After API endpoint or auth dependency changes, run `python3 generate_api_auth_spec.py`, then its `--check` variant. Do not hand-edit `docs/static/frigate-api.yaml`.
- After Pydantic config title/description changes, run `python3 generate_config_translations.py`; do not hand-edit generated `web/public/locales/en/config/{global,cameras}.json`.
- After new translation calls, run `npm run i18n:extract` from `web/`; `npm run i18n:extract:ci` checks synchronization.
- If backend model changes affect browser fixtures: `PYTHONPATH=. python3 web/e2e/fixtures/mock-data/generate-mock-data.py` from the root.
- Vallery contracts run as `python3 .github/scripts/<script>` from the root:
  - Upstream intake and ledger: `test_classify_upstream_changes.py`, `test_verify_mirror_update.py`, `test_verify_downstream_patch_ledger.py`, then `verify_downstream_patch_ledger.py` (needs full history).
  - Development desired state (needs `pyyaml`): `validate_local_dev_runtime.py` and `test_local_dev_runtime.py`, then `kubectl kustomize deploy/kubernetes/local-dev`.
  - Release and delivery: `test_vallery_release_workflows.py`, `test_local_dev_delivery.py`, `test_plan_upstream_patch_retirement.py`.
- Start `npm run dev` (in `web/`) or local Docker builds (`make local`, `make debug`) only when the task needs them; avoid unnecessary long-running processes or full image builds.
