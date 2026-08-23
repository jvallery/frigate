# Development release ledger

Each generated directory contains only public release identity and value-blind
Git patches:

- `release-manifest.json`: the immutable public Frigate release manifest;
- `promotion.json`: source, standard digest, prior digest, and workflow identity;
- `desired-state.patch`: the bounded Deployment image change;
- `inverse.patch`: the exact reverse of that image change.

Runtime configuration, camera identity, addresses, credentials, database data,
logs, media, and private rollout evidence are forbidden here.

The checked-in inverse currently returns to the exact adopted
`v0.18.0-vallery.20260823.4` Deployment identity. That release predates this
ledger, so the validator recognizes only its immutable release ID, source SHA,
and image digest tuple as a valid rollback baseline; any partial or altered
baseline is rejected.
