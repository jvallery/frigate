# Frigate release and development delivery contract

The Frigate artifact factory and the development delivery path are independent.
Neither path accepts a deployment-environment selector, dispatches another
repository, or has Kubernetes credentials. Desired state becomes active only
after a normal protected-branch merge and Argo CD reconciliation.

## Environment-neutral release factory

`Vallery immutable artifact release` accepts an exact protected
`vallery/prod` source SHA, its reviewed upstream SHA, and a new release ID. It
calls the protected builder with only the explicitly mapped Zot push-registry
input. The trusted builder produces standard and TensorRT images once, verifies
their digest equality in GHCR and Zot, exports SBOM and provenance, signs the
images and release manifest, and publishes immutable release assets.

The release workflow does not choose development or production and does not
dispatch a delivery workflow. Its release is complete when the annotated tag
and byte-closed public assets are published.

## Development promotion

`Frigate development promotion` is manually dispatched on protected
`vallery/prod` with the exact release ID, source and upstream SHAs, standard
digest, manifest digest, canonical manifest URL, and release workflow URL. Its
public verification job proves all supplied identities agree with:

- the signed `vallery.frigate.release/1` manifest;
- an annotated tag pointing to the exact protected source;
- protected fork and upstream history;
- a successful `Vallery immutable artifact release` run;
- GitHub attestations for the manifest and standard image; and
- the exact standard digest observed in GHCR.

The signed manifest records the same verified digest for the Zot standard
image. Zot remains credential-gated, so the public verification job does not
receive a registry credential. The runtime pulls that exact digest with the
separate read-only `frigate-dev-zot-pull` Secret supplied by the private
platform owner.

Only the PR-writing job enters GitHub Actions Environment
`frigate-dev-promotion`. It receives environment Secrets
`FRIGATE_DEV_PROMOTION_APP_ID` and
`FRIGATE_DEV_PROMOTION_PRIVATE_KEY`. They identify dedicated GitHub App
`frigate-dev-promotion` (slug `vallery-frigate-dev-promotion`) with metadata
read, contents write, and pull request write permissions. The workflow requires
an all-repository metadata-only token to prove the complete installation
inventory equals `jvallery/frigate`. Only after that proof does it mint a
second write token scoped to `frigate`; both tokens explicitly reject access to
`jvallery/sentinel`. The App has no Actions permission,
organization permission, webhook, cluster credential, production runner
profile, or Sentinel installation.

The writer runs only on GitHub-hosted `ubuntu-24.04`. It checks out current
protected `vallery/prod`, calls
`.github/scripts/prepare_local_dev_promotion.py`, and permits exactly these
changes:

- `deploy/kubernetes/local-dev/deployment.yaml`;
- `deploy/kubernetes/local-dev/releases/<release-id>/release-manifest.json`;
- `deploy/kubernetes/local-dev/releases/<release-id>/promotion.json`;
- `deploy/kubernetes/local-dev/releases/<release-id>/desired-state.patch`; and
- `deploy/kubernetes/local-dev/releases/<release-id>/inverse.patch`.

It validates the runtime, render, path set, and inverse before pushing one
idempotent `automation/frigate-dev-<release-id>` branch and opening or updating
a PR to `vallery/prod`. It never merges the PR or invokes Argo CD directly.
Normal repository checks run because the branch and PR use the dedicated App,
not the default workflow token.

## Exact inverse and rollback

Each promotion receipt records the previous release, image, and source. The
generated `inverse.patch` applies to the promoted tree and restores only the
previous Deployment image and public identity labels. Unit tests apply that
patch and compare the restored Deployment byte for byte with its baseline.

A rollback is an operator-reviewed PR containing the recorded inverse. It uses
the same protected checks and Argo reconciliation path as a forward promotion.
It does not restore a database, media, Secret, PVC, or private configuration,
because the development workload has none of those persistent inputs.

## Independent Sentinel production selection

Sentinel selects production artifacts by pulling immutable public release
metadata. It does not receive a dispatch from either Frigate workflow. A
production selector can independently verify:

1. the annotated release tag and exact source/upstream ancestry;
2. `release-manifest.json` against `release-manifest.sha256` and its GitHub
   attestation;
3. the successful protected release workflow recorded by the manifest;
4. the standard and TensorRT GitHub image attestations; and
5. each manifest-recorded GHCR/Zot canonical identity and verified digest.

Sentinel then applies its own private preflight, mirror, promotion, inverse, and
review policy using the TensorRT digest. No Frigate development token, workflow
dispatch, path, or runner participates in that production decision.
