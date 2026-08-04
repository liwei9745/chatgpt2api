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

from services.cleanup_attestation import canonical_attestation_bytes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--identity-file", required=True, type=Path)
    parser.add_argument("--capability-file", required=True, type=Path)
    parser.add_argument("--private-key-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--runtime-binding", required=True)
    parser.add_argument("--ttl-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.ttl_seconds < 30 or args.ttl_seconds > 3600:
        raise SystemExit("ttl must be between 30 and 3600 seconds")
    identity = json.loads(args.identity_file.read_text(encoding="utf-8"))
    if not isinstance(identity, dict):
        raise SystemExit("identity file must contain an object")
    if args.capability_file.exists():
        raise SystemExit("capability file already exists; start a new isolated runtime instead")
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
    now = int(time.time())
    payload = {
        **identity,
        "version": 2,
        "runtime_binding": args.runtime_binding,
        "capability_sha256": hashlib.sha256(capability.encode("utf-8")).hexdigest(),
        "not_before": now,
        "expires_at": now + args.ttl_seconds,
    }
    private_key = serialization.load_pem_private_key(args.private_key_file.read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise SystemExit("private key must be Ed25519")
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
