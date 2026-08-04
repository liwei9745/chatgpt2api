"""Host-only issuer for a signed GenBox cleanup attestation.

Run this outside the chatgpt2api container. The private key must remain under
host/launcher control and must never be mounted into the application.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def canonical_attestation_bytes(payload: dict[str, object]) -> bytes:
    """Keep the host-only issuer independent from the application package."""
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(unsigned, separators=(",", ":"), sort_keys=True).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity-file", required=True, type=Path)
    parser.add_argument("--capability-file", required=True, type=Path)
    parser.add_argument("--deployment-nonce-file", required=True, type=Path)
    parser.add_argument("--private-key-file", required=True, type=Path)
    parser.add_argument("--public-key-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--container-runtime-id", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.ttl_seconds < 30 or args.ttl_seconds > 3600:
        raise SystemExit("ttl must be between 30 and 3600 seconds")
    identity = json.loads(args.identity_file.read_text(encoding="utf-8"))
    if not isinstance(identity, dict):
        raise SystemExit("identity file must contain an object")
    if args.capability_file.exists():
        raise SystemExit("capability file already exists; start a new isolated runtime instead")
    if args.deployment_nonce_file.exists():
        raise SystemExit("deployment nonce file already exists; start a new isolated runtime instead")
    if args.public_key_file.exists():
        raise SystemExit("public key file already exists; start a new isolated runtime instead")
    runtime_identity = args.container_runtime_id.strip().lower()
    if len(runtime_identity) != 64 or any(char not in "0123456789abcdef" for char in runtime_identity):
        raise SystemExit("container runtime ID must be a 64-character lowercase SHA-256 value")
    args.capability_file.parent.mkdir(parents=True, exist_ok=True)
    capability = secrets.token_urlsafe(48)
    descriptor = os.open(args.capability_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", closefd=True) as handle:
            handle.write(capability + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            args.capability_file.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        args.capability_file.chmod(0o600)
    except OSError:
        pass
    if len(capability) < 32:
        raise SystemExit("capability is too short")
    args.deployment_nonce_file.parent.mkdir(parents=True, exist_ok=True)
    nonce_descriptor = os.open(args.deployment_nonce_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        deployment_nonce = secrets.token_bytes(32)
        with os.fdopen(nonce_descriptor, "wb", closefd=True) as handle:
            handle.write(deployment_nonce)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            args.deployment_nonce_file.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        args.deployment_nonce_file.chmod(0o600)
    except OSError:
        pass
    now = int(time.time())
    payload = {
        **identity,
        "version": 3,
        "runtime_binding": runtime_identity,
        "deployment_nonce_sha256": hashlib.sha256(deployment_nonce).hexdigest(),
        "capability_sha256": hashlib.sha256(capability.encode("utf-8")).hexdigest(),
        "not_before": now,
        "expires_at": now + args.ttl_seconds,
    }
    private_key = serialization.load_pem_private_key(args.private_key_file.read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise SystemExit("private key must be Ed25519")
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_descriptor = os.open(args.public_key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(public_descriptor, "wb", closefd=True) as handle:
            handle.write(public_key)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            args.public_key_file.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        args.public_key_file.chmod(0o600)
    except OSError:
        pass
    payload["signature"] = base64.b64encode(private_key.sign(canonical_attestation_bytes(payload))).decode("ascii")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")
    temp.chmod(0o600)
    os.replace(temp, args.output)
    args.output.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
