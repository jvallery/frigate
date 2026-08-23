# Development release ledger

Each generated directory contains only public release identity and value-blind
Git patches:

- `release-manifest.json`: the immutable public Frigate release manifest;
- `promotion.json`: source, standard digest, prior digest, and workflow identity;
- `desired-state.patch`: the bounded Deployment image change;
- `inverse.patch`: the exact reverse of that image change.

Runtime configuration, camera identity, addresses, credentials, database data,
logs, media, and private rollout evidence are forbidden here.
