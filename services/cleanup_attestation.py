from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def canonical_attestation_bytes(payload: dict[str, Any]) -> bytes:
    """Return the stable bytes signed by the external cleanup launcher."""
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(unsigned, separators=(",", ":"), sort_keys=True).encode("utf-8")


def verify_attestation_signature(payload: dict[str, Any], public_key_pem: bytes) -> bool:
    """Verify an Ed25519 signature without ever loading a private key."""
    try:
        signature = base64.b64decode(str(payload.get("signature") or ""), validate=True)
        public_key = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(public_key, Ed25519PublicKey):
            return False
        public_key.verify(signature, canonical_attestation_bytes(payload))
    except (TypeError, ValueError, InvalidSignature):
        return False
    return True
