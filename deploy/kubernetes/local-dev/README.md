# Frigate development runtime

This directory is the public, disposable desired state for the isolated Frigate
development environment. The protected `vallery/prod` branch is reconciled from
`deploy/kubernetes/local-dev` into namespace `frigate-dev`. Argo CD is the only
deployment mechanism.

`kubectl kustomize deploy/kubernetes/local-dev` intentionally renders exactly
one ConfigMap, Deployment, Service, and ServiceAccount. It does not render a
Namespace, Secret, PVC, RBAC object, NetworkPolicy, or Ingress.

## Private platform contract

The private platform repository supplies, without granting this public tree
ownership of them:

- namespace `frigate-dev`;
- AppProject `repo-pod-jvallery-frigate` and Application `frigate-local-dev`;
- pull Secret `frigate-dev-zot-pull` with read-only access to the Frigate image;
- Secret `frigate-dev-secrets` with exactly these keys:
  `FRIGATE_CAMERA_HOST`, `FRIGATE_CAMERA_USER`, and
  `FRIGATE_CAMERA_PASSWORD`;
- the node label `agentfleet.vallery.net/frigate-dev-cpu=true` on the selected
  non-GPU development worker.

The Deployment maps the three camera keys individually. It never uses
`envFrom`. `FRIGATE_CAMERA_HOST` is the host, optional port, and stream-path
portion that follows `@` in an RTSP URL. It does not contain a scheme or
credentials. No Secret values belong in this repository, generated manifests,
release receipts, CI output, or issue evidence.

ServiceAccount `frigate-dev` has no RBAC and disables token automount at both
the account and Pod levels. The Pod is CPU-only, uses the standard image by
immutable digest, and mounts only bounded `emptyDir` volumes. Configuration,
database, and media disappear with the Pod lifecycle.

## Private network and route contract

Private network policy selects only
`app.kubernetes.io/name=frigate-dev` in namespace `frigate-dev`. The private
policy owner supplies default deny plus the narrowly selected DNS, camera, and
Traefik allowances. It also allows only Prometheus Pods labeled
`app.kubernetes.io/name=prometheus` in namespace `observability` to reach TCP
port `5000`. Camera address ranges cannot be expressed safely in this public
repository, so this tree does not render a NetworkPolicy.

Service `frigate-dev` exposes TCP port `8971` as `external`. `ingress.yaml` is a
tested but staged artifact and is deliberately absent from `kustomization.yaml`.
During the ownership cutover, the previous route must first be pruned through
its owning Argo Application. Only then may a Frigate PR add `ingress.yaml` to
the base resources. There must never be two active routers for the development
hostname.

Prometheus scrapes `/api/metrics` through the Service's named `internal` port,
TCP `5000`. Kube-state-metrics can join that scrape to the Deployment and Pod
through these public labels: `app.kubernetes.io/name=frigate-dev`,
`app.kubernetes.io/environment=development`,
`app.kubernetes.io/version=<release-id>`,
`vallery.net/source-sha=<public-release-source>`,
`vallery.net/image-variant=standard-amd64`,
`vallery.net/detector-device=cpu`, and
`vallery.net/semantic-device=cpu`. The promotion helper updates version and
source identity atomically with the image digest.

## Promotion and inverse contract

`.github/scripts/prepare_local_dev_promotion.py` accepts one public Frigate
release manifest and one Frigate Actions run URL. It selects only the
`standard-amd64` digest, changes only the Deployment image, and records a
value-blind manifest, receipt, forward patch, and exact inverse below
`releases/<release-id>/`.

Private runtime evidence remains in its private owning repository. Image
inverses never restore database or media state. Promotions and inverses become
effective only after a reviewed merge and Argo reconciliation; never apply this
tree directly.
