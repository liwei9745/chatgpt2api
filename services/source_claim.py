from __future__ import annotations

import hashlib
from pathlib import Path

from services.config import DATA_DIR
from services.process_file_lock import ProcessFileLock


def source_claim_path(relative_path: str, source_sha256: str, runtime_identity_digest: str = "") -> Path:
    """Return the process-shared claim file for one source identity."""
    identity = f"{runtime_identity_digest}\n{relative_path}\n{source_sha256}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return DATA_DIR / f".genbox_source_claim.{digest}.lock"


def source_claim(relative_path: str, source_sha256: str, runtime_identity_digest: str = "") -> ProcessFileLock:
    return ProcessFileLock(source_claim_path(relative_path, source_sha256, runtime_identity_digest))
