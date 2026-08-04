"""Attest and start one pre-created isolated Compose cleanup runtime.

This is a host-only launcher. Docker Compose creates the stopped container;
this process reads Docker's record before signing so the attestation binds the
actual container ID and Compose-owned labels. The signing key is read only by
this process and is never included in a Docker command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
_IMMUTABLE_IMAGE = re.compile(r".+@sha256:[0-9a-f]{64}\Z")
_ARTIFACT_FILES = (
    "capability",
    "deployment-nonce",
    "attestation.json",
    "public-key.pem",
)


def _run(command: list[str]) -> str:
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _identity(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "instance_id", "role", "storage_root", "compose_project", "container_name",
        "service_port", "image_digest", "marker_sha256", "trusted_destination_scope",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise SystemExit("identity file does not have the required isolated-runtime fields")
    if payload.get("role") != "isolated-development":
        raise SystemExit("identity role must be isolated-development")
    if not _SHA256.fullmatch(str(payload.get("image_digest") or "").lower()):
        raise SystemExit("identity image_digest must be a lowercase sha256 digest")
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("marker_sha256") or "").lower()):
        raise SystemExit("identity marker_sha256 must be a lowercase SHA-256")
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("trusted_destination_scope") or "").lower()):
        raise SystemExit("identity trusted_destination_scope must be a lowercase SHA-256")
    if not isinstance(payload.get("service_port"), int) or not 1 <= payload["service_port"] <= 65535:
        raise SystemExit("identity service_port is invalid")
    return payload


def _write_marker(storage_parent: Path, storage_root: PurePosixPath, identity: dict[str, object]) -> None:
    marker = storage_parent / ".genbox-isolated-cleanup"
    if marker.exists():
        raise SystemExit("isolated marker already exists; use a new storage parent")
    storage_parent.mkdir(parents=True, exist_ok=True)
    host_images = storage_parent / storage_root.name
    host_images.mkdir(exist_ok=True)
    marker_payload = {
        "version": 1,
        "instance_id": identity["instance_id"],
        "role": identity["role"],
        "storage_root": str(storage_root),
        "compose_project": identity["compose_project"],
        "container_name": identity["container_name"],
        "service_port": identity["service_port"],
        "image_digest": identity["image_digest"],
    }
    marker_bytes = json.dumps(marker_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if hashlib.sha256(marker_bytes).hexdigest() != identity["marker_sha256"]:
        raise SystemExit("identity marker_sha256 does not match the host-created marker")
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as handle:
        handle.write(marker_bytes)
        handle.flush()
        os.fsync(handle.fileno())


def _prepared_artifact_directory(path: Path) -> Path:
    """Accept the empty bind source that Compose created, never a prior artifact set."""
    candidate = path.absolute()
    try:
        details = candidate.lstat()
    except FileNotFoundError as exc:
        raise SystemExit("artifact directory must be pre-created by Docker Compose") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise SystemExit("artifact directory must be an ordinary directory")
    artifact_dir = candidate.resolve(strict=True)
    try:
        entries = list(artifact_dir.iterdir())
    except OSError as exc:
        raise SystemExit("artifact directory cannot be inspected") from exc
    if entries:
        raise SystemExit("artifact directory must be empty before attestation")
    return artifact_dir


def _remove_issued_artifacts(artifact_dir: Path) -> None:
    """Remove only launcher-owned files; the bind source directory belongs to Compose."""
    for name in _ARTIFACT_FILES:
        candidate = artifact_dir / name
        try:
            details = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
            candidate.unlink(missing_ok=True)


def _container_inspection(command: list[str], container_id: str) -> dict[str, object]:
    """Read the Docker-owned container record; reject partial format output."""
    try:
        inspection = json.loads(_run([*command, "inspect", "--format", "{{json .}}", container_id]))
    except (json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        raise SystemExit("Docker container inspection is unavailable or malformed") from exc
    if not isinstance(inspection, dict):
        raise SystemExit("Docker container inspection is malformed")
    return inspection


def _require_container_contract(
    inspection: dict[str, object],
    *,
    container_id: str,
    image: str,
    image_id: str,
    compose_project: object,
    container_name: object,
    artifact_dir: Path,
    artifact_target: str,
    storage_parent: Path,
    storage_target: PurePosixPath,
    service_port: object,
    required_environment: dict[str, str],
) -> None:
    """Fail closed unless Docker reports the exact runtime contract we created."""
    inspected_id = str(inspection.get("Id") or "").lower()
    if not _CONTAINER_ID.fullmatch(inspected_id) or inspected_id != container_id.lower():
        raise SystemExit("Docker did not return a stable 64-character container ID")

    config = inspection.get("Config")
    if not isinstance(config, dict):
        raise SystemExit("Docker container configuration is malformed")
    if str(config.get("Image") or "").lower() != image.lower():
        raise SystemExit("Docker container image reference does not match the signed identity")
    if str(inspection.get("Image") or "").lower() != image_id.lower():
        raise SystemExit("Docker container image content does not match the inspected immutable image")
    if str(inspection.get("Name") or "") != f"/{container_name}":
        raise SystemExit("Docker container name does not match the signed identity")

    state = inspection.get("State")
    if not isinstance(state, dict) or state.get("Running") is not False or state.get("Status") != "created":
        raise SystemExit("Docker Compose container must be stopped and created before attestation")

    labels = config.get("Labels")
    if not isinstance(labels, dict) or labels.get("com.docker.compose.project") != compose_project:
        raise SystemExit("Docker Compose project label does not match the signed identity")

    host_config = inspection.get("HostConfig")
    if not isinstance(host_config, dict) or host_config.get("CgroupnsMode") != "host":
        raise SystemExit("Docker Compose runtime must expose the host cgroup namespace")

    environment = config.get("Env")
    if not isinstance(environment, list) or any(not isinstance(item, str) for item in environment):
        raise SystemExit("Docker container environment is malformed")
    for key, value in required_environment.items():
        if environment.count(f"{key}={value}") != 1:
            raise SystemExit("Docker Compose environment does not match the isolated runtime")

    port_bindings_by_container_port = host_config.get("PortBindings")
    if not isinstance(port_bindings_by_container_port, dict) or set(port_bindings_by_container_port) != {"80/tcp"}:
        raise SystemExit("Docker container port bindings are malformed")
    port_bindings = port_bindings_by_container_port.get("80/tcp")
    if not isinstance(port_bindings, list) or len(port_bindings) != 1:
        raise SystemExit("Docker container port bindings do not match the signed identity")
    binding = port_bindings[0]
    if not isinstance(binding, dict) or binding.get("HostPort") != str(service_port):
        raise SystemExit("Docker container port bindings do not match the signed identity")

    mounts = inspection.get("Mounts")
    if not isinstance(mounts, list):
        raise SystemExit("Docker container mounts are malformed")
    expected = {
        (str(artifact_dir), artifact_target): False,
        (str(storage_parent), str(storage_target)): True,
    }
    actual: dict[tuple[str, str], bool] = {}
    for mount in mounts:
        if not isinstance(mount, dict):
            raise SystemExit("Docker container mount is malformed")
        source = str(mount.get("Source") or "")
        destination = str(mount.get("Destination") or "")
        if mount.get("Type") != "bind" or (source, destination) not in expected:
            raise SystemExit("Docker container mount contract does not match the isolated runtime")
        if (source, destination) in actual:
            raise SystemExit("Docker container mount contract has duplicate mounts")
        actual[(source, destination)] = bool(mount.get("RW"))
    if actual != expected:
        raise SystemExit("Docker container mount contract does not match the isolated runtime")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--identity-file", required=True, type=Path)
    parser.add_argument("--private-key-file", required=True, type=Path)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--storage-parent-host", required=True, type=Path)
    parser.add_argument("--trusted-destination-kind", required=True, choices=("http", "https"))
    parser.add_argument("--trusted-destination-url", required=True)
    parser.add_argument("--trusted-private-host", required=True)
    parser.add_argument(
        "--docker-command", nargs="+", default=["docker"],
        help="Docker executable command; defaults to 'docker' and is host-only configuration.",
    )
    args = parser.parse_args()

    identity = _identity(args.identity_file)
    if not _IMMUTABLE_IMAGE.fullmatch(args.image):
        raise SystemExit("image must be an immutable repository@sha256 reference")
    storage_root = PurePosixPath(str(identity["storage_root"]))
    if not storage_root.is_absolute() or storage_root.name in {"", ".", ".."}:
        raise SystemExit("identity storage_root must be an absolute container directory")
    private_key = args.private_key_file.resolve(strict=True)
    if not private_key.is_file():
        raise SystemExit("private key file is missing")
    storage_parent = args.storage_parent_host.resolve()
    if private_key.is_relative_to(storage_parent):
        raise SystemExit("private key must not be inside a directory mounted into the application")
    supplied_image = args.image.lower()
    _, separator, supplied_digest = supplied_image.rpartition("@")
    repository_digests = json.loads(_run([
        *args.docker_command, "image", "inspect", "--format", "{{json .RepoDigests}}", args.image,
    ]))
    if (
        not isinstance(repository_digests, list)
        or supplied_image not in {str(value).lower() for value in repository_digests}
        or not separator
        or not _SHA256.fullmatch(supplied_digest)
        or supplied_digest != identity["image_digest"]
    ):
        raise SystemExit("Docker image identity does not match the signed identity")
    artifact_dir = _prepared_artifact_directory(args.artifact_dir)
    if private_key.is_relative_to(artifact_dir):
        raise SystemExit("private key must not be inside a directory mounted into the application")

    marker = storage_parent / ".genbox-isolated-cleanup"
    marker_created = False
    try:
        _write_marker(storage_parent, storage_root, identity)
        marker_created = True
        artifact_target = "/run/genbox-cleanup"
        required_environment = {
            "CHATGPT2API_CLEANUP_ENVIRONMENT": "isolated-vps",
            "CHATGPT2API_CLEANUP_EXECUTE": "1",
            "CHATGPT2API_CLEANUP_INSTANCE_ID": str(identity["instance_id"]),
            "CHATGPT2API_CLEANUP_INSTANCE_ROLE": str(identity["role"]),
            "CHATGPT2API_CLEANUP_STORAGE_ROOT": str(storage_root),
            "CHATGPT2API_CLEANUP_COMPOSE_PROJECT": str(identity["compose_project"]),
            "CHATGPT2API_CLEANUP_CONTAINER_NAME": str(identity["container_name"]),
            "CHATGPT2API_CLEANUP_SERVICE_PORT": str(identity["service_port"]),
            "CHATGPT2API_CLEANUP_IMAGE_DIGEST": str(identity["image_digest"]),
            "CHATGPT2API_CLEANUP_MARKER_SHA256": str(identity["marker_sha256"]),
            "CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_SCOPE": str(identity["trusted_destination_scope"]),
            "CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_KIND": args.trusted_destination_kind,
            "CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_URL": args.trusted_destination_url,
            "CHATGPT2API_CLEANUP_TRUSTED_PRIVATE_HOST": args.trusted_private_host,
            "CHATGPT2API_CLEANUP_CAPABILITY_FILE": f"{artifact_target}/capability",
            "CHATGPT2API_CLEANUP_DEPLOYMENT_NONCE_FILE": f"{artifact_target}/deployment-nonce",
            "CHATGPT2API_CLEANUP_ATTESTATION_FILE": f"{artifact_target}/attestation.json",
            "CHATGPT2API_CLEANUP_ATTESTATION_PUBLIC_KEY_FILE": f"{artifact_target}/public-key.pem",
        }
        container_id = _run([*args.docker_command, "inspect", "--format", "{{.Id}}", str(identity["container_name"])]).lower()
        if not _CONTAINER_ID.fullmatch(container_id):
            raise SystemExit("Docker did not return a stable 64-character container ID")
        image_id = _run([
            *args.docker_command, "image", "inspect", "--format", "{{.Id}}", args.image,
        ]).lower()
        if not _SHA256.fullmatch(image_id):
            raise SystemExit("Docker immutable image content ID is malformed")
        inspection = _container_inspection(args.docker_command, container_id)
        _require_container_contract(
            inspection,
            container_id=container_id,
            image=args.image,
            image_id=image_id,
            compose_project=identity["compose_project"],
            container_name=identity["container_name"],
            artifact_dir=artifact_dir,
            artifact_target=artifact_target,
            storage_parent=storage_parent,
            storage_target=storage_root.parent,
            service_port=identity["service_port"],
            required_environment=required_environment,
        )
        issuer = Path(__file__).with_name("issue_cleanup_attestation.py")
        _run([
            sys.executable, str(issuer), "--identity-file", str(args.identity_file),
            "--capability-file", str(artifact_dir / "capability"),
            "--deployment-nonce-file", str(artifact_dir / "deployment-nonce"),
            "--private-key-file", str(private_key),
            "--public-key-file", str(artifact_dir / "public-key.pem"),
            "--output", str(artifact_dir / "attestation.json"),
            "--container-runtime-id", container_id.lower(),
        ])
        _run([*args.docker_command, "start", container_id])
    except BaseException:
        _remove_issued_artifacts(artifact_dir)
        if marker_created:
            marker.unlink(missing_ok=True)
        raise
    print(container_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
