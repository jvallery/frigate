#!/usr/bin/env python3
"""Validate the public Frigate development desired-state contract."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "deploy/kubernetes/local-dev"
KUSTOMIZATION = BASE / "kustomization.yaml"
STAGED_INGRESS = BASE / "ingress.yaml"

IMAGE_RE = re.compile(r"^[a-z0-9./:-]+@sha256:[0-9a-f]{64}$")
IPV4_RE = re.compile(
    r"(?<![0-9])(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])"
    r"(?:\.(?:25[0-5]|2[0-4][0-9]|1?[0-9]?[0-9])){3}(?![0-9])"
)
EXPECTED_SECRET_KEYS = {
    "FRIGATE_CAMERA_HOST",
    "FRIGATE_CAMERA_USER",
    "FRIGATE_CAMERA_PASSWORD",
}
EXPECTED_LABELS = {
    "app.kubernetes.io/name": "frigate-dev",
    "app.kubernetes.io/part-of": "frigate",
    "app.kubernetes.io/environment": "development",
}
EXPECTED_MIDDLEWARES = (
    "network-infra-apps-strip-auth-headers@kubernetescrd,"
    "network-infra-ak-forwardauth@kubernetescrd,"
    "network-infra-security-headers@kubernetescrd"
)


def fail(message: str) -> NoReturn:
    """Exit with a public-safe contract error."""

    raise SystemExit(f"local-dev-runtime: {message}")


def require(condition: bool, message: str) -> None:
    """Enforce one fail-closed invariant."""

    if not condition:
        fail(message)


def decode_json_stream(body: str) -> list[dict[str, Any]]:
    """Decode the concatenated JSON objects emitted by kubectl."""

    decoder = json.JSONDecoder()
    documents: list[dict[str, Any]] = []
    offset = 0
    while offset < len(body):
        while offset < len(body) and body[offset].isspace():
            offset += 1
        if offset == len(body):
            break
        value, offset = decoder.raw_decode(body, offset)
        require(isinstance(value, dict), "kubectl emitted a non-object document")
        documents.append(value)
    return documents


def kubectl_objects(body: str) -> list[dict[str, Any]]:
    """Use kubectl's Kubernetes decoder without contacting a cluster."""

    result = subprocess.run(
        [
            "kubectl",
            "create",
            "--dry-run=client",
            "--validate=false",
            "-o",
            "json",
            "-f",
            "-",
        ],
        input=body,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0:
        fail("kubectl could not decode desired state")
    return decode_json_stream(result.stdout)


def render() -> tuple[str, list[dict[str, Any]]]:
    """Render the exact Argo source path."""

    result = subprocess.run(
        ["kubectl", "kustomize", str(BASE.relative_to(ROOT))],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        fail("kubectl kustomize failed")
    return result.stdout, kubectl_objects(result.stdout)


def object_by_kind(documents: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    """Return the only object of one expected kind."""

    matches = [document for document in documents if document.get("kind") == kind]
    require(len(matches) == 1, f"expected exactly one {kind}")
    return matches[0]


def validate_public_tree() -> None:
    """Scan public files for private data and production coupling."""

    forbidden_suffixes = {
        ".db",
        ".db-shm",
        ".db-wal",
        ".log",
        ".mp4",
        ".sqlite",
        ".sqlite3",
    }
    forbidden_text = {
        "cameras.vallery.net": "production hostname",
        "frigate-secrets": "production Secret",
        "frigate-ring-secret": "production companion Secret",
        "jvallery/sentinel-frigate": "production image",
        "kubernetes.io/hostname": "private hostname selector",
        "nvidia.com/gpu": "GPU resource",
        "persistentVolumeClaim:": "PVC mount",
        "hostPath:": "host path",
        "envFrom:": "broad Secret import",
        "ring-mqtt": "Ring or MQTT companion",
    }
    for path in BASE.rglob("*"):
        if not path.is_file():
            continue
        lowered_name = path.name.lower()
        require(
            not any(lowered_name.endswith(suffix) for suffix in forbidden_suffixes),
            "runtime database, log, or media file is committed",
        )
        try:
            body = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            fail("binary file is committed in the public runtime tree")
        require(IPV4_RE.search(body) is None, "literal camera address is committed")
        require(
            re.search(r"(?m)^\s*namespace:\s*frigate\s*$", body) is None,
            "production namespace is referenced",
        )
        require(
            re.search(r"(?m)^\s*hostNetwork:\s*true\s*$", body) is None,
            "host networking is enabled",
        )
        require(
            re.search(r"rtsp://(?!\{FRIGATE_CAMERA_USER\})", body) is None,
            "literal camera URL is committed",
        )
        for token, label in forbidden_text.items():
            require(token not in body, f"{label} is referenced")


def validate_common(document: dict[str, Any], name: str) -> None:
    """Validate namespace, identity, and stable public labels."""

    metadata = document.get("metadata") or {}
    require(metadata.get("name") == name, f"{document.get('kind')} name drifted")
    require(metadata.get("namespace") == "frigate-dev", "resource escaped frigate-dev")
    labels = metadata.get("labels") or {}
    for key, value in EXPECTED_LABELS.items():
        require(labels.get(key) == value, f"resource label {key} drifted")


def validate_service_account(document: dict[str, Any]) -> None:
    """Prove the dedicated account never receives a projected API token."""

    validate_common(document, "frigate-dev")
    require(
        document.get("automountServiceAccountToken") is False,
        "ServiceAccount token automount is not disabled",
    )
    require("secrets" not in document, "ServiceAccount imports a Secret")


def validate_deployment(document: dict[str, Any]) -> None:
    """Prove CPU-only, ephemeral, tokenless, digest-only workload shape."""

    validate_common(document, "frigate-dev")
    metadata = document["metadata"]
    annotations = metadata.get("annotations") or {}
    labels = metadata.get("labels") or {}
    require(
        annotations.get("vallery.net/image-variant") == "standard-amd64",
        "Deployment is not anchored to the standard CPU artifact",
    )
    require(
        re.fullmatch(
            r"v[0-9]+\.[0-9]+\.[0-9]+-vallery\.[0-9]{8}\.[1-9][0-9]*",
            str(annotations.get("vallery.net/release-id")),
        )
        is not None,
        "Deployment release identity is invalid",
    )
    release_id = annotations["vallery.net/release-id"]
    require(
        labels.get("app.kubernetes.io/version") == release_id,
        "Deployment version label disagrees with its release",
    )
    source_sha = str(labels.get("vallery.net/source-sha") or "")
    require(
        re.fullmatch(r"[0-9a-f]{40}", source_sha) is not None,
        "Deployment source SHA label is invalid",
    )
    require(
        labels.get("vallery.net/image-variant") == "standard-amd64",
        "Deployment image variant label drifted",
    )
    require(
        labels.get("vallery.net/detector-device") == "cpu",
        "Deployment detector device label drifted",
    )
    require(
        labels.get("vallery.net/semantic-device") == "cpu",
        "Deployment semantic device label drifted",
    )

    spec = document.get("spec") or {}
    require(spec.get("replicas") == 1, "Deployment must have one replica")
    require(spec.get("strategy", {}).get("type") == "Recreate", "strategy drifted")
    selector = spec.get("selector", {}).get("matchLabels") or {}
    require(
        selector == {"app.kubernetes.io/name": "frigate-dev"},
        "Deployment selector is not the private policy contract",
    )

    pod = spec.get("template", {}).get("spec") or {}
    pod_labels = spec.get("template", {}).get("metadata", {}).get("labels") or {}
    for key, value in EXPECTED_LABELS.items():
        require(pod_labels.get(key) == value, f"Pod label {key} drifted")
    for key in (
        "app.kubernetes.io/version",
        "vallery.net/source-sha",
        "vallery.net/image-variant",
        "vallery.net/detector-device",
        "vallery.net/semantic-device",
    ):
        require(
            pod_labels.get(key) == labels.get(key),
            f"Pod identity label {key} disagrees with Deployment",
        )
    require(pod.get("serviceAccountName") == "frigate-dev", "Pod account drifted")
    require(
        pod.get("automountServiceAccountToken") is False,
        "Pod token automount is not disabled",
    )
    require(pod.get("enableServiceLinks") is False, "service links are enabled")
    require(pod.get("hostNetwork") is False, "host networking is not denied")
    require(pod.get("hostPID") is False, "host PID is not denied")
    require(pod.get("hostIPC") is False, "host IPC is not denied")
    require(
        pod.get("nodeSelector") == {"agentfleet.vallery.net/frigate-dev-cpu": "true"},
        "platform CPU node selector drifted",
    )
    require(
        pod.get("imagePullSecrets") == [{"name": "frigate-dev-zot-pull"}],
        "dev-only pull Secret drifted",
    )
    require("runtimeClassName" not in pod, "accelerator runtime class is forbidden")
    require(
        "tolerations" not in pod, "dedicated production taint tolerance is forbidden"
    )

    containers = pod.get("containers") or []
    init_containers = pod.get("initContainers") or []
    require(
        [container.get("name") for container in containers] == ["frigate"],
        "Frigate must be the only application container",
    )
    require(
        [container.get("name") for container in init_containers] == ["seed-config"],
        "only the config seed init container is allowed",
    )
    for container in [*init_containers, *containers]:
        image = str(container.get("image") or "")
        require(
            IMAGE_RE.fullmatch(image) is not None, "container image is not digest-only"
        )
        lowered = image.lower()
        require("tensorrt" not in lowered, "TensorRT image is forbidden")
        require("nvidia" not in lowered, "GPU image is forbidden")
        require("sentinel" not in lowered, "production image anchor is forbidden")
        require("envFrom" not in container, "broad environment import is forbidden")
        resources = container.get("resources") or {}
        for boundary in ("requests", "limits"):
            values = resources.get(boundary) or {}
            require("cpu" in values, f"{container.get('name')} has no CPU {boundary}")
            require(
                "memory" in values, f"{container.get('name')} has no memory {boundary}"
            )
            require(
                "ephemeral-storage" in values,
                f"{container.get('name')} has no ephemeral-storage {boundary}",
            )
            require("nvidia.com/gpu" not in values, "GPU resource is forbidden")

    frigate = containers[0]
    require(
        str(frigate["image"]).startswith(
            "registry.vallery.net/jvallery/frigate@sha256:"
        ),
        "Frigate image repository drifted",
    )
    env = frigate.get("env") or []
    require(
        {entry.get("name") for entry in env} == {*EXPECTED_SECRET_KEYS, "TZ"},
        "container environment is not the declared minimal set",
    )
    refs: dict[str, tuple[str | None, str | None]] = {}
    for entry in env:
        if entry.get("name") not in EXPECTED_SECRET_KEYS:
            continue
        secret_ref = entry.get("valueFrom", {}).get("secretKeyRef") or {}
        refs[entry["name"]] = (secret_ref.get("name"), secret_ref.get("key"))
        require(secret_ref.get("optional") is not True, "camera Secret key is optional")
    require(
        refs == {key: ("frigate-dev-secrets", key) for key in EXPECTED_SECRET_KEYS},
        "camera keys are not individually mapped from the dev Secret",
    )

    volumes = pod.get("volumes") or []
    require(
        {volume.get("name") for volume in volumes}
        == {"config", "data", "media", "config-source", "shm"},
        "ephemeral volume set drifted",
    )
    for volume in volumes:
        keys = set(volume) - {"name"}
        require(
            keys <= {"emptyDir", "configMap"}, "persistent or host volume is forbidden"
        )
        if "emptyDir" in volume:
            require(
                bool((volume.get("emptyDir") or {}).get("sizeLimit")),
                f"emptyDir {volume.get('name')} is unbounded",
            )
        if "configMap" in volume:
            require(
                str(volume["configMap"].get("name", "")).startswith(
                    "frigate-dev-config-"
                ),
                "config source is not the generated public ConfigMap",
            )


def validate_config(document: dict[str, Any]) -> None:
    """Prove the public fixture is generic, CPU-only, and network-minimal."""

    metadata = document.get("metadata") or {}
    require(
        str(metadata.get("name", "")).startswith("frigate-dev-config-"),
        "generated ConfigMap name drifted",
    )
    require(metadata.get("namespace") == "frigate-dev", "ConfigMap escaped frigate-dev")
    config = str(document.get("data", {}).get("config.yml") or "")
    require(
        "rtsp://{FRIGATE_CAMERA_USER}:{FRIGATE_CAMERA_PASSWORD}@{FRIGATE_CAMERA_HOST}"
        in config,
        "generic camera placeholders drifted",
    )
    for key in EXPECTED_SECRET_KEYS:
        require(config.count(f"{{{key}}}") == 1, f"config placeholder {key} drifted")
    require(
        re.search(r"(?ms)^mqtt:\n\s+enabled:\s+false\s*$", config) is not None,
        "MQTT must be disabled",
    )
    require(
        re.search(r"(?ms)^detectors:\n\s+cpu:\n\s+type:\s+cpu\s*$", config) is not None,
        "CPU detector is not explicit",
    )
    require(
        re.search(
            r"(?ms)^semantic_search:\n\s+enabled:\s+false\n\s+device:\s+CPU\s*$",
            config,
        )
        is not None,
        "semantic search is not explicitly CPU-disabled",
    )
    require("ring" not in config.lower(), "Ring configuration is forbidden")
    require(
        "go2rtc" not in config.lower(), "additional streaming companion is forbidden"
    )


def validate_service(document: dict[str, Any]) -> None:
    """Prove the stable private-policy and staged-route Service contract."""

    validate_common(document, "frigate-dev")
    spec = document.get("spec") or {}
    require(
        spec.get("selector") == {"app.kubernetes.io/name": "frigate-dev"},
        "Service selector drifted",
    )
    ports = {
        port.get("name"): (
            port.get("port"),
            port.get("targetPort"),
            port.get("protocol"),
        )
        for port in spec.get("ports") or []
    }
    require(
        ports
        == {
            "internal": (5000, "internal", "TCP"),
            "external": (8971, "external", "TCP"),
        },
        "Service ports drifted",
    )


def validate_staged_ingress() -> None:
    """Validate the single route artifact while proving it is not rendered."""

    require(STAGED_INGRESS.is_file(), "staged Ingress artifact is missing")
    documents = kubectl_objects(STAGED_INGRESS.read_text(encoding="utf-8"))
    require(len(documents) == 1, "staged route contains extra objects")
    ingress = documents[0]
    require(ingress.get("kind") == "Ingress", "staged route is not an Ingress")
    validate_common(ingress, "frigate-dev")
    metadata = ingress["metadata"]
    require(
        metadata.get("annotations", {}).get(
            "traefik.ingress.kubernetes.io/router.middlewares"
        )
        == EXPECTED_MIDDLEWARES,
        "staged middleware chain drifted",
    )
    spec = ingress.get("spec") or {}
    require(spec.get("ingressClassName") == "traefik-apps", "Ingress class drifted")
    rules = spec.get("rules") or []
    require(len(rules) == 1, "staged route must have one host")
    require(rules[0].get("host") == "cameras-dev.vallery.net", "dev host drifted")
    paths = rules[0].get("http", {}).get("paths") or []
    require(len(paths) == 1, "staged route must have one path")
    backend = paths[0].get("backend", {}).get("service") or {}
    require(backend.get("name") == "frigate-dev", "Ingress backend drifted")
    require(backend.get("port", {}).get("number") == 8971, "Ingress port drifted")


def validate_release_ledger(deployment: dict[str, Any]) -> None:
    """Prove the current public release receipt and exact image inverse."""

    metadata = deployment.get("metadata") or {}
    annotations = metadata.get("annotations") or {}
    labels = metadata.get("labels") or {}
    release_id = str(annotations.get("vallery.net/release-id") or "")
    evidence = BASE / "releases" / release_id
    require(evidence.is_dir(), "current release ledger entry is missing")
    require(
        {path.name for path in evidence.iterdir() if path.is_file()}
        == {
            "desired-state.patch",
            "inverse.patch",
            "promotion.json",
            "release-manifest.json",
        },
        "current release ledger file set drifted",
    )
    try:
        manifest = json.loads(
            (evidence / "release-manifest.json").read_text(encoding="utf-8")
        )
        receipt = json.loads((evidence / "promotion.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        fail("current release ledger JSON is invalid")
    require(
        manifest.get("schema") == "vallery.frigate.release/1", "release schema drifted"
    )
    require(manifest.get("release_id") == release_id, "release manifest ID drifted")
    require(
        manifest.get("source", {}).get("fork_sha")
        == labels.get("vallery.net/source-sha"),
        "release source disagrees with Deployment label",
    )
    digest = manifest.get("artifacts", {}).get("standard-amd64", {}).get("digest")
    image = (
        deployment.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [{}])[0]
        .get("image")
    )
    require(
        image == f"registry.vallery.net/jvallery/frigate@{digest}",
        "release standard digest disagrees with Deployment",
    )
    require(receipt.get("release_id") == release_id, "promotion receipt ID drifted")
    require(receipt.get("image") == image, "promotion receipt image drifted")
    require(
        receipt.get("environment") == "frigate-dev", "promotion environment drifted"
    )
    require(
        receipt.get("schema")
        in {
            "vallery.frigate.dev-promotion/1",
            "vallery.frigate.dev-release-adoption/1",
        },
        "promotion receipt schema drifted",
    )
    for name, reverse in (("inverse.patch", False), ("desired-state.patch", True)):
        patch = evidence / name
        body = patch.read_text(encoding="utf-8")
        require(
            "deploy/kubernetes/local-dev/deployment.yaml" in body,
            "release patch does not name the development Deployment",
        )
        require("sentinel" not in body.lower(), "release patch references production")
        command = ["git", "apply"]
        if reverse:
            command.append("--reverse")
        command.extend(["--check", str(patch)])
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        require(result.returncode == 0, f"{name} does not apply at the current release")


def validate_documents(documents: list[dict[str, Any]]) -> None:
    """Validate the exact rendered object inventory and every owned object."""

    identities = sorted(
        (str(document.get("kind")), str(document.get("metadata", {}).get("name")))
        for document in documents
    )
    require(len(identities) == 4, "base must render exactly four objects")
    require(
        [(kind, name) for kind, name in identities if kind != "ConfigMap"]
        == [
            ("Deployment", "frigate-dev"),
            ("Service", "frigate-dev"),
            ("ServiceAccount", "frigate-dev"),
        ],
        "base rendered an unexpected object",
    )
    forbidden_kinds = {
        "Ingress",
        "Namespace",
        "NetworkPolicy",
        "PersistentVolumeClaim",
        "Role",
        "RoleBinding",
        "Secret",
    }
    require(
        not any(document.get("kind") in forbidden_kinds for document in documents),
        "base rendered a platform-owned or staged object",
    )
    validate_config(object_by_kind(documents, "ConfigMap"))
    validate_service_account(object_by_kind(documents, "ServiceAccount"))
    validate_deployment(object_by_kind(documents, "Deployment"))
    validate_service(object_by_kind(documents, "Service"))


def validate() -> str:
    """Run the complete public runtime contract and return rendered YAML."""

    validate_public_tree()
    rendered, documents = render()
    validate_documents(documents)
    validate_release_ledger(object_by_kind(documents, "Deployment"))
    validate_staged_ingress()
    return rendered


def main() -> int:
    """CLI entry point."""

    validate()
    print("local-dev-runtime: public-safe desired state passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
