# Agent Instructions

Fix forward to the requested, verified outcome with the smallest complete change. Ask only about material ambiguity; add no approval, planning, or issue/PR ceremony unless requested.

- Use focused checks or direct probes. Docs-only edits need diff/readback. Add tests only for meaningful risks to critical behavior, security, data integrity, or subtle logic.
- Reuse CI/CD; honor enforced checks without adding pipelines or gates unnecessarily. Broaden checks only for affected behavior; stop rechecking once verified.
- Search narrowly, reuse current evidence, avoid repeated polling/replanning, and report results briefly.
- Preserve unrelated work and secrets. Use owning sources/generators and practical recovery options for destructive work.
- For GitOps changes, commit declarative state, let ArgoCD reconcile, and verify the affected runtime.

## Frigate context

Frigate is a local NVR with Python 3.13+ / FastAPI, a React / TypeScript frontend, and multiprocessing, ZMQ, and MQTT. Preserve camera performance and access controls.

- Backend: `frigate/` (`api/`, `config/`, `detectors/`, `events/`, `util/`); tests: `frigate/test/`; migrations: `migrations/`.
- Frontend: `web/src/`; English translations: `web/public/locales/en/`; Docker: `docker/`; docs: `docs/`.
- Match nearby patterns and let Ruff, ESLint, and Prettier enforce style. Use module-level lazy logging without secret values and avoid exposing exception details in API responses. Keep blocking I/O out of async functions.
- Use `react-i18next` for user-visible strings and the existing Radix/Tailwind components.
- New outbound WebSocket topics must be classified in `frigate/comms/ws.py`: unknown topics are dropped by the fail-closed camera-access classifier.

## Focused commands

Run only commands relevant to the change:

- Python test module: `python3 -u -m unittest frigate.test.test_ffmpeg_presets` (substitute the affected module). Full suite: `python3 -u -m unittest` when warranted.
- Python lint/format: `ruff check <changed-paths>` and `ruff format --check <changed-paths>`; type check: `python3 -u -m mypy --config-file frigate/mypy.ini frigate` when types are affected.
- From `web/`: `npm run build`, `npm run lint`; focused browser check: `npx playwright test --config e2e/playwright.config.ts e2e/specs/live.spec.ts` (select the relevant spec). Use `npm run e2e:build` when the browser tests need a fresh build; first-time setup uses `npm install` and `npx playwright install chromium`.
- After API endpoint or auth dependency changes, run `python3 generate_api_auth_spec.py`, then its `--check` variant. Do not hand-edit `docs/static/frigate-api.yaml`.
- After Pydantic config title/description changes, run `python3 generate_config_translations.py`; do not hand-edit generated `web/public/locales/en/config/{global,cameras}.json`.
- After new translation calls, run `npm run i18n:extract` from `web/`; `npm run i18n:extract:ci` checks synchronization.
- If backend model changes affect browser fixtures: `PYTHONPATH=. python3 web/e2e/fixtures/mock-data/generate-mock-data.py` from the root.
- Start `npm run dev` (in `web/`) or Docker builds (`make local`, `make debug`) only when the task needs them; avoid unnecessary long-running processes or full image builds.
