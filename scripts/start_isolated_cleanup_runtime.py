"""Create and attest one isolated cleanup runtime from a Docker host.

This is a host-only launcher. It deliberately creates the container before
signing so the attestation binds Docker's actual container ID. The signing key
is read only by this process and is never included in a Docker command.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
_IMMUTABLE_IMAGE = re.compile(r".+@sha256:[0-9a-f]{64}\Z")


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
    if args.artifact_dir.exists():
        raise SystemExit("artifact directory already exists; start a new isolated runtime instead")
    private_key = args.private_key_file.resolve(strict=True)
    if not private_key.is_file():
        raise SystemExit("private key file is missing")
    storage_parent = args.storage_parent_host.resolve()
    artifact_dir = args.artifact_dir.resolve()
    if private_key.is_relative_to(storage_parent) or private_key.is_relative_to(artifact_dir):
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

    _write_marker(storage_parent, storage_root, identity)
    artifact_dir.mkdir(mode=0o700)
    container_id = ""
    try:
        artifact_target = "/run/genbox-cleanup"
        container_id = _run([
            *args.docker_command, "create", "--name", str(identity["container_name"]),
            "--mount", f"type=bind,src={artifact_dir},dst={artifact_target},readonly",
            "--mount", f"type=bind,src={storage_parent},dst={storage_root.parent}",
            "--publish", f"{identity['service_port']}:80",
            "--env", "CHATGPT2API_CLEANUP_ENVIRONMENT=isolated-vps",
            "--env", "CHATGPT2API_CLEANUP_EXECUTE=1",
            "--env", f"CHATGPT2API_CLEANUP_INSTANCE_ID={identity['instance_id']}",
            "--env", f"CHATGPT2API_CLEANUP_INSTANCE_ROLE={identity['role']}",
            "--env", f"CHATGPT2API_CLEANUP_STORAGE_ROOT={storage_root}",
            "--env", f"CHATGPT2API_CLEANUP_COMPOSE_PROJECT={identity['compose_project']}",
            "--env", f"CHATGPT2API_CLEANUP_CONTAINER_NAME={identity['container_name']}",
            "--env", f"CHATGPT2API_CLEANUP_SERVICE_PORT={identity['service_port']}",
            "--env", f"CHATGPT2API_CLEANUP_IMAGE_DIGEST={identity['image_digest']}",
            "--env", f"CHATGPT2API_CLEANUP_MARKER_SHA256={identity['marker_sha256']}",
            "--env", f"CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_SCOPE={identity['trusted_destination_scope']}",
            "--env", f"CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_KIND={args.trusted_destination_kind}",
            "--env", f"CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_URL={args.trusted_destination_url}",
            "--env", f"CHATGPT2API_CLEANUP_TRUSTED_PRIVATE_HOST={args.trusted_private_host}",
            "--env", f"CHATGPT2API_CLEANUP_CAPABILITY_FILE={artifact_target}/capability",
            "--env", f"CHATGPT2API_CLEANUP_DEPLOYMENT_NONCE_FILE={artifact_target}/deployment-nonce",
            "--env", f"CHATGPT2API_CLEANUP_ATTESTATION_FILE={artifact_target}/attestation.json",
            "--env", f"CHATGPT2API_CLEANUP_ATTESTATION_PUBLIC_KEY_FILE={artifact_target}/public-key.pem",
            args.image,
        ])
        inspected_id = _run([*args.docker_command, "inspect", "--format", "{{.Id}}", container_id]).lower()
        if not _CONTAINER_ID.fullmatch(inspected_id) or inspected_id != container_id.lower():
            raise SystemExit("Docker did not return a stable 64-character container ID")
        issuer = Path(__file__).with_name("issue_cleanup_attestation.py")
        _run([
            sys.executable, str(issuer), "--identity-file", str(args.identity_file),
            "--capability-file", str(artifact_dir / "capability"),
            "--deployment-nonce-file", str(artifact_dir / "deployment-nonce"),
            "--private-key-file", str(private_key),
            "--public-key-file", str(artifact_dir / "public-key.pem"),
            "--output", str(artifact_dir / "attestation.json"),
            "--container-runtime-id", inspected_id,
        ])
        _run([*args.docker_command, "start", container_id])
    except Exception:
        if container_id:
            subprocess.run([*args.docker_command, "rm", "-f", container_id], check=False, capture_output=True)
        shutil.rmtree(artifact_dir, ignore_errors=True)
        raise
    print(container_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
