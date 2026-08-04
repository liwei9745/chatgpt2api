from __future__ import annotations

import json
import hashlib
import hmac
import ipaddress
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from services.config import DATA_DIR
from services.cleanup_attestation import verify_attestation_signature
from services.cleanup_attestation_anchor import CLEANUP_ATTESTATION_PUBLIC_KEY_SHA256
from services.image_storage_service import ImageStorageService, _is_filesystem_alias, image_storage_service
from services.json_file import read_json_object
from services.process_file_lock import ProcessFileLock, ProcessReentrantLock
from services.source_claim import source_claim
from utils.timezone import beijing_now_str


CLEANUP_STATE_FILE = DATA_DIR / "genbox_push_cleanup.json"
CLEANUP_AUDIT_FILE = DATA_DIR / "genbox_push_cleanup_audit.json"
CLEANUP_RECORD_VERSION = 1
MAX_AUDIT_EVENTS = 2000
VALID_RECEIPT_STATUSES = {"imported", "already-imported", "duplicate-local"}
TERMINAL_CLEANUP_STATUSES = {"deleted", "retained", "delete_failed", "delete_unknown"}
ISOLATED_MARKER_VERSION = 1
ISOLATED_MARKER_FIELDS = {
    "version",
    "instance_id",
    "role",
    "storage_root",
    "compose_project",
    "container_name",
    "service_port",
    "image_digest",
}
RUNTIME_ATTESTATION_VERSION = 3
RUNTIME_ATTESTATION_FIELDS = {
    "version",
    "instance_id",
    "role",
    "storage_root",
    "compose_project",
    "container_name",
    "service_port",
    "image_digest",
    "marker_sha256",
    "trusted_destination_scope",
    "capability_sha256",
    "runtime_binding",
    "deployment_nonce_sha256",
    "not_before",
    "expires_at",
    "signature",
}


def _clean(value: object) -> str:
    return str(value or "").strip()


def _strict_relative_path(value: object) -> str:
    path = _clean(value)
    if not path or "\\" in path or "\x00" in path or path.startswith("/"):
        raise ValueError("cleanup path must be a normalized relative path")
    parts = path.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("cleanup path must be a normalized relative path")
    if Path(path).is_absolute() or Path(path).as_posix() != path:
        raise ValueError("cleanup path must be a normalized relative path")
    return path


def _valid_digest(value: object) -> str:
    digest = _clean(value).lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("cleanup source hash is invalid")
    return digest


def _bool_env(value: object) -> bool:
    return _clean(value).lower() in {"1", "true", "yes", "on"}


def _valid_runtime_name(value: object, *, max_length: int = 128) -> str:
    name = _clean(value)
    if not name or len(name) > max_length or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) is None:
        return ""
    return name


def _valid_service_port(value: object) -> int:
    try:
        port = int(_clean(value))
    except (TypeError, ValueError):
        return 0
    return port if 1 <= port <= 65535 else 0


def _valid_image_digest(value: object) -> str:
    digest = _clean(value).lower()
    if not digest.startswith("sha256:"):
        return ""
    hex_digest = digest.removeprefix("sha256:")
    if len(hex_digest) != 64 or any(char not in "0123456789abcdef" for char in hex_digest):
        return ""
    return digest


def _container_runtime_identity_from_cgroup(cgroup_data: str) -> str:
    """Accept one distinct Docker ID; repeated cgroup-v1 controller rows are normal."""
    matches = set(re.findall(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", cgroup_data.lower()))
    return next(iter(matches)) if len(matches) == 1 else ""


def _container_runtime_identity() -> str:
    """Return a Linux container ID from kernel cgroup metadata, or fail closed."""
    if sys.platform != "linux":
        return ""
    try:
        cgroup_data = Path("/proc/self/cgroup").read_text(encoding="utf-8", errors="strict")
    except OSError:
        return ""
    return _container_runtime_identity_from_cgroup(cgroup_data)


def settings_coordination_lock_path(settings_file: Path) -> Path:
    """Return the process-shared lock used by settings writers and cleanup."""
    return settings_file.with_name(f".{settings_file.name}.coord.lock")


def _read_external_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    """Read a launcher-owned file without following aliases or stale paths."""
    stat_result = path.lstat()
    if (
        not path.is_file()
        or _is_filesystem_alias(stat_result)
        or int(getattr(stat_result, "st_nlink", 1) or 1) != 1
        or stat_result.st_size < 1
        or stat_result.st_size > maximum_bytes
    ):
        raise ValueError("external cleanup file is invalid")
    descriptor = os.open(path, os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0) or 0))
    try:
        opened_stat = os.fstat(descriptor)
        if (
            opened_stat.st_dev != stat_result.st_dev
            or opened_stat.st_ino != stat_result.st_ino
            or opened_stat.st_size != stat_result.st_size
        ):
            raise ValueError("external cleanup file changed while opening")
        with os.fdopen(os.dup(descriptor), "rb", closefd=True) as handle:
            content = handle.read(maximum_bytes + 1)
    finally:
        os.close(descriptor)
    if len(content) != stat_result.st_size or len(content) > maximum_bytes:
        raise ValueError("external cleanup file changed while reading")
    return content


class CleanupEnvironmentGate:
    """Server-side capability gate for destructive cleanup.

    Only an explicitly classified isolated-vps runtime with an explicit
    execute switch can unlink. Missing or unknown classification is disabled.
    Browser payloads never reach this class.
    """

    def __init__(
        self,
        environ: dict[str, str] | None = None,
        *,
        capability: str | None = None,
        attestation_file: Path | None = None,
        public_key_file: Path | None = None,
        trusted_public_key_sha256: str | None = None,
        deployment_nonce_file: Path | None = None,
        runtime_binding_provider: Callable[[], str] | None = None,
    ) -> None:
        self.environ = environ if environ is not None else os.environ
        # The capability is injected by the service launcher and is never
        # accepted from HTTP or recovered from durable cleanup state.
        self._capability = capability or ""
        configured_attestation = _clean(self.environ.get("CHATGPT2API_CLEANUP_ATTESTATION_FILE"))
        self._attestation_file = (
            attestation_file
            if attestation_file is not None
            else Path(configured_attestation)
            if configured_attestation
            else None
        )
        configured_public_key = _clean(self.environ.get("CHATGPT2API_CLEANUP_ATTESTATION_PUBLIC_KEY_FILE"))
        self._public_key_file = public_key_file or (Path(configured_public_key) if configured_public_key else None)
        self._trusted_public_key_sha256 = _clean(
            trusted_public_key_sha256 if trusted_public_key_sha256 is not None
            else CLEANUP_ATTESTATION_PUBLIC_KEY_SHA256
        ).lower()
        configured_nonce = _clean(self.environ.get("CHATGPT2API_CLEANUP_DEPLOYMENT_NONCE_FILE"))
        self._deployment_nonce_file = (
            deployment_nonce_file
            if deployment_nonce_file is not None
            else Path(configured_nonce)
            if configured_nonce
            else None
        )
        self._runtime_binding_provider = runtime_binding_provider or _container_runtime_identity
        self._storage_root_provider: Callable[[], Path] | None = None

    def bind_storage_root(self, provider: Callable[[], Path]) -> None:
        self._storage_root_provider = provider

    def environment_class(self) -> str:
        return _clean(self.environ.get("CHATGPT2API_CLEANUP_ENVIRONMENT")).lower()

    def _runtime_identity_matches(self) -> bool:
        configured_root = _clean(self.environ.get("CHATGPT2API_CLEANUP_STORAGE_ROOT"))
        if not configured_root or self._storage_root_provider is None:
            return False
        instance_id = _clean(self.environ.get("CHATGPT2API_CLEANUP_INSTANCE_ID"))
        role = _clean(self.environ.get("CHATGPT2API_CLEANUP_INSTANCE_ROLE"))
        compose_project = _valid_runtime_name(self.environ.get("CHATGPT2API_CLEANUP_COMPOSE_PROJECT"))
        container_name = _valid_runtime_name(self.environ.get("CHATGPT2API_CLEANUP_CONTAINER_NAME"))
        service_port = _valid_service_port(self.environ.get("CHATGPT2API_CLEANUP_SERVICE_PORT"))
        image_digest = _valid_image_digest(self.environ.get("CHATGPT2API_CLEANUP_IMAGE_DIGEST"))
        attestation_matches = False
        try:
            configured_path = Path(configured_root)
            configured_stat = configured_path.lstat()
            if not configured_path.is_dir() or _is_filesystem_alias(configured_stat):
                return False
            provider_path = Path(self._storage_root_provider())
            provider_stat = provider_path.lstat()
            if not provider_path.is_dir() or _is_filesystem_alias(provider_stat):
                return False
            configured_resolved = configured_path.resolve()
            roots_match = configured_resolved == provider_path.resolve()
            marker_path = configured_resolved.parent / ".genbox-isolated-cleanup"
            marker_stat = marker_path.lstat()
            if (
                not marker_path.is_file()
                or _is_filesystem_alias(marker_stat)
                or int(getattr(marker_stat, "st_nlink", 1) or 1) != 1
            ):
                return False
            nofollow = int(getattr(os, "O_NOFOLLOW", 0) or 0)
            descriptor = os.open(marker_path, os.O_RDONLY | nofollow)
            try:
                opened_stat = os.fstat(descriptor)
                if (
                    opened_stat.st_dev != marker_stat.st_dev
                    or opened_stat.st_ino != marker_stat.st_ino
                    or opened_stat.st_size != marker_stat.st_size
                ):
                    return False
                with os.fdopen(os.dup(descriptor), "rb", closefd=True) as handle:
                    marker_bytes = handle.read(4096)
            finally:
                os.close(descriptor)
            if len(marker_bytes) != marker_stat.st_size:
                return False
            marker = json.loads(marker_bytes.decode("utf-8"))
            if not isinstance(marker, dict) or set(marker) != ISOLATED_MARKER_FIELDS:
                return False
            expected_marker_hash = _clean(self.environ.get("CHATGPT2API_CLEANUP_MARKER_SHA256")).lower()
            if (
                len(expected_marker_hash) != 64
                or any(char not in "0123456789abcdef" for char in expected_marker_hash)
                or hashlib.sha256(marker_bytes).hexdigest() != expected_marker_hash
            ):
                return False
            marker_matches = marker == {
                "version": ISOLATED_MARKER_VERSION,
                "instance_id": instance_id,
                "role": role,
                "storage_root": str(configured_resolved),
                "compose_project": compose_project,
                "container_name": container_name,
                "service_port": service_port,
                "image_digest": image_digest,
            }
            if (
                not marker_matches
                or self._attestation_file is None
                or self._public_key_file is None
                or self._deployment_nonce_file is None
                or not self._capability
            ):
                return False
            attestation = json.loads(_read_external_regular_file(
                self._attestation_file, maximum_bytes=16 * 1024,
            ).decode("utf-8"))
            if not isinstance(attestation, dict):
                return False
            identity = {
                "instance_id": instance_id,
                "role": role,
                "storage_root": str(configured_resolved),
                "compose_project": compose_project,
                "container_name": container_name,
                "service_port": service_port,
                "image_digest": image_digest,
                "marker_sha256": expected_marker_hash,
                "trusted_destination_scope": _clean(self.environ.get("CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_SCOPE")),
            }
            runtime_binding = _clean(self._runtime_binding_provider())
            if (
                len(runtime_binding) != 64
                or any(char not in "0123456789abcdef" for char in runtime_binding)
                or attestation.get("runtime_binding") != runtime_binding
            ):
                return False
            deployment_nonce = _read_external_regular_file(self._deployment_nonce_file, maximum_bytes=1024)
            if len(deployment_nonce) < 32 or not hmac.compare_digest(
                str(attestation.get("deployment_nonce_sha256") or ""),
                hashlib.sha256(deployment_nonce).hexdigest(),
            ):
                return False
            now = int(time.time())
            if (
                not isinstance(attestation.get("not_before"), int)
                or not isinstance(attestation.get("expires_at"), int)
                or attestation["not_before"] > now
                or attestation["expires_at"] <= now
                or attestation["expires_at"] - attestation["not_before"] > 3600
                or set(attestation) != RUNTIME_ATTESTATION_FIELDS
            ):
                return False
            public_key = _read_external_regular_file(self._public_key_file, maximum_bytes=16 * 1024)
            attestation_matches = (
                attestation.get("version") == RUNTIME_ATTESTATION_VERSION
                and len(self._trusted_public_key_sha256) == 64
                and all(char in "0123456789abcdef" for char in self._trusted_public_key_sha256)
                and hmac.compare_digest(
                    hashlib.sha256(public_key).hexdigest(),
                    self._trusted_public_key_sha256,
                )
                and all(
                    attestation.get(key) == value for key, value in identity.items()
                )
                and hmac.compare_digest(
                    str(attestation.get("capability_sha256") or ""),
                    hashlib.sha256(self._capability.encode("utf-8")).hexdigest(),
                )
                and verify_attestation_signature(attestation, public_key)
            )
        except (OSError, RuntimeError, TypeError, UnicodeError, ValueError):
            roots_match = False
            marker_matches = False
        return bool(
            role == "isolated-development"
            and _valid_runtime_name(instance_id)
            and compose_project
            and container_name
            and service_port
            and image_digest
            and len(self._capability) >= 32
            and roots_match
            and marker_matches
            and attestation_matches
        )

    def runtime_identity_digest(self) -> str:
        if not self._runtime_identity_matches():
            return ""
        try:
            attestation = json.loads(
                _read_external_regular_file(self._attestation_file, maximum_bytes=16 * 1024).decode("utf-8")
            ) if self._attestation_file else {}
            digest = hashlib.sha256(
                json.dumps(attestation, separators=(",", ":"), sort_keys=True).encode("utf-8")
            ).hexdigest()
        except (OSError, TypeError, ValueError, UnicodeError):
            return ""
        return digest if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest) else ""

    def destination_trusted(self, destination_scope: str, base_url: str) -> bool:
        trusted = _clean(self.environ.get("CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_SCOPE"))
        trust_kind = _clean(self.environ.get("CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_KIND"))
        trusted_url = _clean(self.environ.get("CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_URL")).rstrip("/")
        configured_url = _clean(base_url).rstrip("/")
        if not trusted or not trusted_url or not configured_url:
            return False
        try:
            parsed_trusted = urlparse(trusted_url)
            parsed_configured = urlparse(configured_url)
        except ValueError:
            return False
        if (
            parsed_trusted.scheme != "https"
            or parsed_configured.scheme != "https"
            or parsed_trusted.username
            or parsed_trusted.password
            or parsed_configured.username
            or parsed_configured.password
            or parsed_trusted.query
            or parsed_trusted.fragment
            or parsed_configured.query
            or parsed_configured.fragment
            or parsed_trusted.netloc != parsed_configured.netloc
            or (parsed_trusted.path or "").rstrip("/") != (parsed_configured.path or "").rstrip("/")
        ):
            return False
        if trust_kind == "private-verified":
            host = parsed_configured.hostname or ""
            verified_host = _clean(self.environ.get("CHATGPT2API_CLEANUP_TRUSTED_PRIVATE_HOST")).lower()
            if not verified_host or verified_host != host.lower():
                return False
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                return False
            private_cgnat = ipaddress.ip_network("100.64.0.0/10")
            if (
                address.is_loopback
                or address.is_unspecified
                or address.is_reserved
                or address.is_multicast
                or not (address.is_private or address.is_link_local or address in private_cgnat)
            ):
                return False
        else:
            host = parsed_configured.hostname or ""
            try:
                address = ipaddress.ip_address(host)
                if address.is_loopback or address.is_unspecified or address.is_reserved or address.is_multicast:
                    return False
            except ValueError:
                if not host:
                    return False
        return hmac.compare_digest(trusted, _clean(destination_scope))

    def can_execute(self) -> bool:
        return (
            self.environment_class() == "isolated-vps"
            and _bool_env(self.environ.get("CHATGPT2API_CLEANUP_EXECUTE"))
            and self._runtime_identity_matches()
        )

    def reason(self) -> str:
        if self.environment_class() != "isolated-vps":
            return "development-disabled"
        if not self._runtime_identity_matches():
            return "runtime-identity-unverified"
        return "cleanup-execute-disabled"


def _atomic_json_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        directory_fd = None
    if directory_fd is not None:
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)
    try:
        path.chmod(0o600)
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(content, encoding="utf-8")
        backup.chmod(0o600)
    except OSError:
        pass


class GenBoxPushCleanupService:
    """Durable receipt-gated cleanup evaluator and executor."""

    def __init__(
        self,
        *,
        state_file: Path = CLEANUP_STATE_FILE,
        audit_file: Path = CLEANUP_AUDIT_FILE,
        settings_file: Path = DATA_DIR / "genbox_push_settings.json",
        image_storage: ImageStorageService = image_storage_service,
        environment_gate: CleanupEnvironmentGate | None = None,
        now: Callable[[], str] = beijing_now_str,
    ) -> None:
        self.state_file = state_file
        self.audit_file = audit_file
        self.settings_file = settings_file
        self.image_storage = image_storage
        self.environment_gate = environment_gate or CleanupEnvironmentGate()
        self.now = now
        self._lock = ProcessReentrantLock(state_file.with_suffix(state_file.suffix + ".state.lock"))
        # Hold this lock from the final policy/scope check through unlink and
        # terminal audit persistence. Settings writers use the same lock.
        self._settings_lock = ProcessReentrantLock(settings_coordination_lock_path(self.settings_file))
        self.environment_gate.bind_storage_root(lambda: self.image_storage_root())

    def initialize_runtime_capability(self) -> bool:
        """Load an externally issued capability; application code never issues it."""
        gate = self.environment_gate
        if gate.environment_class() != "isolated-vps" or not _bool_env(gate.environ.get("CHATGPT2API_CLEANUP_EXECUTE")):
            return False
        try:
            capability_file_value = _clean(gate.environ.get("CHATGPT2API_CLEANUP_CAPABILITY_FILE"))
            if not capability_file_value or gate._attestation_file is None or gate._public_key_file is None:
                return False
            capability = _read_external_regular_file(Path(capability_file_value), maximum_bytes=1024).decode("ascii").strip()
            if len(capability) < 32:
                return False
        except (OSError, ValueError, UnicodeError):
            return False
        self.environment_gate = CleanupEnvironmentGate(
            gate.environ,
            capability=capability,
            attestation_file=gate._attestation_file,
            public_key_file=gate._public_key_file,
            trusted_public_key_sha256=gate._trusted_public_key_sha256,
            deployment_nonce_file=gate._deployment_nonce_file,
            runtime_binding_provider=gate._runtime_binding_provider,
        )
        self.environment_gate.bind_storage_root(lambda: self.image_storage_root())
        return self.environment_gate.can_execute()

    def image_storage_root(self) -> Path:
        provider = getattr(self.image_storage, "local_root", None)
        if callable(provider):
            return Path(provider())
        from services.config import config

        return config.images_dir

    def _load_state_locked(self) -> dict[str, dict[str, Any]]:
        raw = read_json_object(self.state_file, name=self.state_file.name)
        records = raw.get("records") if isinstance(raw.get("records"), dict) else {}
        return {
            str(key): dict(value)
            for key, value in records.items()
            if isinstance(value, dict) and int(value.get("record_version") or 0) == CLEANUP_RECORD_VERSION
        }

    def _save_state_locked(self, records: dict[str, dict[str, Any]]) -> None:
        _atomic_json_write(self.state_file, {"record_version": CLEANUP_RECORD_VERSION, "records": records})

    def _save_recovery_state_locked(self, records: dict[str, dict[str, Any]]) -> None:
        """Persist recovery results even when a test or wrapper breaks the normal saver."""
        try:
            self._save_state_locked(records)
        except Exception:
            _atomic_json_write(
                self.state_file,
                {"record_version": CLEANUP_RECORD_VERSION, "records": records},
            )

    def _load_audit_locked(self) -> list[dict[str, object]]:
        raw = read_json_object(self.audit_file, name=self.audit_file.name)
        events = raw.get("events") if isinstance(raw.get("events"), list) else []
        return [dict(event) for event in events if isinstance(event, dict)][-MAX_AUDIT_EVENTS:]

    def _append_audit_locked(self, event: dict[str, object]) -> None:
        events = self._load_audit_locked()
        events.append(event)
        _atomic_json_write(self.audit_file, {"events": events[-MAX_AUDIT_EVENTS:]})

    def policy_enabled(self) -> bool:
        raw = read_json_object(self.settings_file, name=self.settings_file.name)
        return raw.get("cleanup_enabled") is True

    def _current_destination(self) -> tuple[str, str]:
        raw = read_json_object(self.settings_file, name=self.settings_file.name)
        base_url = _clean(raw.get("base_url")).rstrip("/")
        source_id = _clean(raw.get("source_id"))
        push_key = _clean(raw.get("push_key"))
        if not base_url or not source_id or not push_key:
            return "", base_url
        return hashlib.sha256(f"{base_url}\n{source_id}\n{push_key}".encode("utf-8")).hexdigest(), base_url

    def settings(self) -> dict[str, object]:
        return {
            "enabled": self.policy_enabled(),
            "environment_class": self.environment_gate.environment_class() or "unknown",
            "execute_available": self.environment_gate.can_execute(),
            "default": False,
        }

    @staticmethod
    def _record_key(scope: str, source_id: str, path: str, digest: str) -> str:
        return "\n".join((scope, source_id, path, digest))

    def record_receipt(
        self,
        *,
        destination_scope: str,
        source_id: str,
        remote_path: str,
        source_sha256: str,
        receipt_status: str,
        safe_to_delete_source: bool,
        size_bytes: int = 0,
    ) -> dict[str, object]:
        path = _strict_relative_path(remote_path)
        digest = _valid_digest(source_sha256)
        scope = _clean(destination_scope)
        sid = _clean(source_id)
        status = _clean(receipt_status)
        if not scope or not sid or status not in VALID_RECEIPT_STATUSES:
            raise ValueError("cleanup receipt identity is invalid")
        identity_result = self.image_storage.verify_local_identity(
            path,
            digest,
            expected_size=max(0, int(size_bytes or 0)) or None,
        )
        source_identity = identity_result.get("source_identity") if identity_result.get("ok") else None
        source_identity_reason = "" if source_identity else str(
            identity_result.get("reason") or "source-identity-unverified"
        )
        key = self._record_key(scope, sid, path, digest)
        now = self.now()
        runtime_identity_digest = self.environment_gate.runtime_identity_digest()
        cleanup_authorized = (
            safe_to_delete_source is True
            and isinstance(source_identity, dict)
            and bool(runtime_identity_digest)
        )
        record = {
            "record_version": CLEANUP_RECORD_VERSION,
            "destination_scope": scope,
            "source_id": sid,
            "remote_path": path,
            "source_sha256": digest,
            "receipt": {
                "contract_version": "v1",
                "source_id": sid,
                "sha256": digest,
                "status": status,
                "safe_to_delete_source": safe_to_delete_source is True,
            },
            "receipt_validated_at": now,
            "transfer_status": "confirmed",
            "cleanup_policy_snapshot": self.policy_enabled(),
            "cleanup_status": "eligible" if cleanup_authorized else "ineligible",
            "decision_reason": (
                "receipt-no-cleanup-permission"
                if safe_to_delete_source is not True
                else "receipt-confirmed"
                if cleanup_authorized
                else source_identity_reason
            ),
            "decided_at": now,
            "size_bytes": max(0, int(size_bytes or 0)),
            "source_identity": source_identity,
            "source_identity_reason": source_identity_reason,
            "runtime_identity_digest": runtime_identity_digest,
        }
        with self._lock:
            records = self._load_state_locked()
            existing = records.get(key)
            if isinstance(existing, dict) and existing.get("cleanup_status") in {"deleted", "deleting", "delete_failed", "delete_unknown"}:
                return dict(existing)
            records[key] = record
            self._save_state_locked(records)
        return dict(record)

    def _public_record(self, record: dict[str, Any]) -> dict[str, object]:
        return {
            "source_id": str(record.get("source_id") or ""),
            "remote_path": str(record.get("remote_path") or ""),
            "source_sha256": str(record.get("source_sha256") or ""),
            "receipt_status": str((record.get("receipt") or {}).get("status") or ""),
            "safe_to_delete_source": (record.get("receipt") or {}).get("safe_to_delete_source") is True,
            "cleanup_status": str(record.get("cleanup_status") or ""),
            "decision_reason": str(record.get("decision_reason") or ""),
            "size_bytes": max(0, int(record.get("size_bytes") or 0)),
            "source_retained": str(record.get("cleanup_status") or "") != "deleted",
        }

    def _inspect(self, record: dict[str, Any]) -> tuple[str, str, int]:
        receipt = record.get("receipt") if isinstance(record.get("receipt"), dict) else {}
        if record.get("transfer_status") != "confirmed":
            return "retained", "transfer-unconfirmed", 0
        if (
            not isinstance(receipt.get("contract_version"), str)
            or receipt.get("contract_version") != "v1"
            or not isinstance(receipt.get("source_id"), str)
            or not isinstance(record.get("source_id"), str)
            or receipt.get("source_id") != record.get("source_id")
        ):
            return "retained", "receipt-invalid", 0
        if (
            not isinstance(receipt.get("sha256"), str)
            or not isinstance(record.get("source_sha256"), str)
            or receipt.get("sha256") != record.get("source_sha256")
            or not isinstance(receipt.get("status"), str)
            or receipt.get("status") not in VALID_RECEIPT_STATUSES
        ):
            return "retained", "receipt-invalid", 0
        if receipt.get("safe_to_delete_source") is not True:
            return "retained", "receipt-no-cleanup-permission", 0
        current_runtime_identity = self.environment_gate.runtime_identity_digest()
        if not current_runtime_identity or record.get("runtime_identity_digest") != current_runtime_identity:
            return "retained", "runtime-identity-changed", 0
        source_identity = record.get("source_identity")
        if not isinstance(source_identity, dict):
            return "retained", str(record.get("source_identity_reason") or "source-identity-missing"), 0
        current_scope, current_url = self._current_destination()
        if not current_scope or current_scope != str(record.get("destination_scope") or ""):
            return "retained", "destination-scope-changed", 0
        if not self.environment_gate.destination_trusted(current_scope, current_url):
            return "retained", "destination-unverified", 0
        result = self.image_storage.verify_local_identity(
            str(record.get("remote_path") or ""),
            str(record.get("source_sha256") or ""),
            expected_size=int(record.get("size_bytes") or 0) or None,
            expected_identity=source_identity,
        )
        if not result.get("ok"):
            reason = str(result.get("reason") or "source-unverified")
            return "retained", reason, int(result.get("size_bytes") or 0)
        return "eligible", "eligible", int(result.get("size_bytes") or 0)

    def _operation_summary(self, operation_id: str, mode: str) -> dict[str, object]:
        return {
            "operation_id": operation_id,
            "mode": mode,
            "candidates": 0,
            "eligible": 0,
            "deleted": 0,
            "retained": 0,
            "failed": 0,
            "already_deleted": 0,
            "unknown": 0,
            "potential_bytes": 0,
            "reclaimed_bytes": 0,
            "items": [],
            "environment_class": self.environment_gate.environment_class() or "unknown",
        }

    def _audit_event(
        self,
        *,
        operation_id: str,
        mode: str,
        record: dict[str, Any],
        prior_status: str,
        decision: str,
        reason: str,
        size_bytes: int,
        reclaimed_bytes: int = 0,
        detail: str = "",
    ) -> dict[str, object]:
        return {
            "audit_id": uuid.uuid4().hex,
            "operation_id": operation_id,
            "mode": mode,
            "timestamp": self.now(),
            "source_id": str(record.get("source_id") or ""),
            "item_identifier": str(record.get("remote_path") or ""),
            "source_sha256": str(record.get("source_sha256") or ""),
            "prior_cleanup_status": prior_status,
            "decision": decision,
            "decision_reason": reason,
            "decision_detail": str(detail or ""),
            "size_bytes": max(0, int(size_bytes or 0)),
            "reclaimed_bytes": max(0, int(reclaimed_bytes or 0)),
            "receipt_status": str((record.get("receipt") or {}).get("status") or ""),
            "runtime_identity_digest": str(record.get("runtime_identity_digest") or ""),
        }

    def _update_record_locked(self, records: dict[str, dict[str, Any]], key: str, **updates: object) -> dict[str, Any]:
        record = dict(records[key])
        record.update(updates)
        records[key] = record
        self._save_state_locked(records)
        return record

    def run(self, *, dry_run: bool = True) -> dict[str, object]:
        operation_id = uuid.uuid4().hex
        mode = "dry-run" if dry_run else "execute"
        summary = self._operation_summary(operation_id, mode)
        with self._lock:
            records = self._load_state_locked()
        if not dry_run and not self.policy_enabled():
            summary["blocked_reason"] = "cleanup-policy-disabled"
        elif not dry_run and not self.environment_gate.can_execute():
            summary["blocked_reason"] = self.environment_gate.reason()

        for key, original in records.items():
            record = dict(original)
            summary["candidates"] = int(summary["candidates"]) + 1
            current_status = str(record.get("cleanup_status") or "")
            if current_status == "deleted":
                summary["already_deleted"] = int(summary.get("already_deleted", 0)) + 1
                summary["items"].append({
                    **self._public_record(record),
                    "decision": "already-deleted",
                    "decision_reason": "already-deleted",
                    "size_bytes": 0,
                    "reclaimed_bytes": 0,
                })
                continue
            elif current_status == "deleting":
                # A durable intent means another cleanup attempt may have
                # reached the storage boundary. Only recovery may resolve it;
                # normal preview/execute runs must never delete again.
                summary["unknown"] = int(summary.get("unknown", 0)) + 1
                summary["items"].append({
                    **self._public_record(record),
                    "decision": "delete-inflight",
                    "decision_reason": "recovery-required",
                    "size_bytes": 0,
                    "reclaimed_bytes": 0,
                    "source_retained": True,
                    "source_state": "unknown",
                })
                continue
            elif current_status == "delete_failed":
                # Storage failures are terminal until an explicit future
                # recovery policy changes the record; do not retry silently.
                summary["failed"] = int(summary.get("failed", 0)) + 1
                summary["items"].append({
                    **self._public_record(record),
                    "decision": "delete-failed-terminal",
                    "decision_reason": "delete-failed-terminal",
                    "size_bytes": 0,
                    "reclaimed_bytes": 0,
                    "source_retained": True,
                })
                continue
            elif current_status == "delete_unknown":
                summary["unknown"] = int(summary.get("unknown", 0)) + 1
                summary["items"].append({
                    **self._public_record(record),
                    "decision": "delete-unknown-terminal",
                    "decision_reason": "delete-unknown-terminal",
                    "size_bytes": 0,
                    "reclaimed_bytes": 0,
                    "source_retained": True,
                    "source_state": "unknown",
                })
                continue
            elif not self.policy_enabled():
                decision, reason, size = "retained", "cleanup-policy-disabled", 0
            else:
                decision, reason, size = self._inspect(record)
                if not dry_run and summary.get("blocked_reason"):
                    decision, reason = "retained", str(summary["blocked_reason"])
            if decision == "eligible":
                summary["eligible"] = int(summary["eligible"]) + 1
                summary["potential_bytes"] = int(summary["potential_bytes"]) + size
            else:
                summary["retained"] = int(summary["retained"]) + 1
            initially_eligible = decision == "eligible"
            initially_eligible_bytes = size if initially_eligible else 0
            item_result = {
                **self._public_record(record),
                "decision": decision,
                "decision_reason": reason,
                "size_bytes": size,
                "reclaimed_bytes": 0,
            }
            if dry_run or decision != "eligible" or summary.get("blocked_reason"):
                with self._lock:
                    live = self._load_state_locked()
                    if key in live:
                        live[key].update({"cleanup_status": decision if decision != "eligible" else "eligible", "decision_reason": reason, "decided_at": self.now()})
                        self._save_state_locked(live)
                        self._append_audit_locked(self._audit_event(
                            operation_id=operation_id,
                            mode=mode,
                            record=live[key],
                            prior_status=current_status,
                            decision=decision,
                            reason=reason,
                            size_bytes=size,
                        ))
                summary["items"].append(item_result)
                continue

            claim = source_claim(
                str(record.get("remote_path") or ""),
                str(record.get("source_sha256") or ""),
                str(record.get("runtime_identity_digest") or ""),
            )
            if not claim.acquire(timeout_secs=0):
                reason = "source-busy"
                summary["eligible"] = max(0, int(summary["eligible"]) - 1)
                summary["potential_bytes"] = max(0, int(summary["potential_bytes"]) - size)
                summary["retained"] = int(summary["retained"]) + 1
                item_result.update({"decision": "retained", "decision_reason": reason, "size_bytes": 0})
                summary["items"].append(item_result)
                continue
            try:
                # Settings rotation and policy changes are coordinated with
                # the cleanup operation through the terminal audit write.
                with self._settings_lock:
                    with self._lock:
                        live = self._load_state_locked()
                        current = live.get(key)
                        if current is None:
                            if initially_eligible:
                                summary["eligible"] = max(0, int(summary["eligible"]) - 1)
                                summary["potential_bytes"] = max(0, int(summary["potential_bytes"]) - initially_eligible_bytes)
                            summary["retained"] = int(summary["retained"]) + 1
                            item_result.update({
                                "decision": "retained",
                                "decision_reason": "state-changed",
                                "size_bytes": 0,
                            })
                            summary["items"].append(item_result)
                            continue
                        decision, reason, size = self._inspect(current)
                        if decision == "eligible":
                            # `_inspect` reads settings, so take one final
                            # scope snapshot after it returns. This catches a
                            # direct settings-file replacement as well as a
                            # managed writer that raced before the lock was
                            # acquired.
                            final_scope, _ = self._current_destination()
                            if final_scope != str(current.get("destination_scope") or ""):
                                decision, reason, size = "retained", "destination-scope-changed", 0
                        if decision != "eligible":
                            if initially_eligible:
                                summary["eligible"] = max(0, int(summary["eligible"]) - 1)
                                summary["potential_bytes"] = max(0, int(summary["potential_bytes"]) - initially_eligible_bytes)
                            current.update({"cleanup_status": "retained", "decision_reason": reason, "decided_at": self.now()})
                            self._append_audit_locked(self._audit_event(operation_id=operation_id, mode=mode, record=current, prior_status=current_status, decision="retained", reason=reason, size_bytes=size))
                            self._save_state_locked(live)
                            summary["retained"] = int(summary["retained"]) + 1
                            item_result.update({"decision": "retained", "decision_reason": reason, "size_bytes": size})
                            summary["items"].append(item_result)
                            continue
                        if not dry_run and not self.policy_enabled():
                            current.update({"cleanup_status": "retained", "decision_reason": "cleanup-policy-disabled", "decided_at": self.now()})
                            self._append_audit_locked(self._audit_event(operation_id=operation_id, mode=mode, record=current, prior_status=current_status, decision="retained", reason="cleanup-policy-disabled", size_bytes=0))
                            self._save_state_locked(live)
                            summary["eligible"] = max(0, int(summary["eligible"]) - 1)
                            summary["potential_bytes"] = max(0, int(summary["potential_bytes"]) - size)
                            summary["retained"] = int(summary["retained"]) + 1
                            item_result.update({"decision": "retained", "decision_reason": "cleanup-policy-disabled", "size_bytes": 0})
                            summary["items"].append(item_result)
                            continue
                        cleanup_token = hashlib.sha256(f"{operation_id}\n{key}".encode("utf-8")).hexdigest()
                        intent = dict(current)
                        intent.update({
                            "cleanup_status": "deleting",
                            "decision_reason": "deletion-intent",
                            "decided_at": self.now(),
                            "operation_id": operation_id,
                            "cleanup_token": cleanup_token,
                        })
                        live[key] = intent
                        try:
                            self._save_state_locked(live)
                        except Exception:
                            # The storage operation is forbidden until the
                            # deletion intent is durable. Make one direct
                            # atomic attempt to persist a terminal unknown
                            # record, bypassing a failing state-save wrapper.
                            fallback = dict(current)
                            fallback.update({
                                "cleanup_status": "delete_unknown",
                                "decision_reason": "deletion-intent-state-write-failed",
                                "decided_at": self.now(),
                                "operation_id": operation_id,
                                "cleanup_token": cleanup_token,
                            })
                            fallback_records = dict(live)
                            fallback_records[key] = fallback
                            try:
                                _atomic_json_write(
                                    self.state_file,
                                    {"record_version": CLEANUP_RECORD_VERSION, "records": fallback_records},
                                )
                            except Exception:
                                # If neither the intent nor the terminal
                                # fallback is durable, retain the source and
                                # leave the original eligible record intact.
                                summary["eligible"] = max(0, int(summary["eligible"]) - 1)
                                summary["potential_bytes"] = max(0, int(summary["potential_bytes"]) - size)
                                summary["retained"] = int(summary["retained"]) + 1
                                item_result.update({
                                    "decision": "retained",
                                    "decision_reason": "deletion-intent-state-write-failed",
                                    "size_bytes": 0,
                                    "source_retained": True,
                                })
                                summary["items"].append(item_result)
                                continue
                            live = fallback_records
                            current = fallback
                            try:
                                self._append_audit_locked(self._audit_event(
                                    operation_id=operation_id,
                                    mode=mode,
                                    record=current,
                                    prior_status=current_status,
                                    decision="delete_unknown",
                                    reason="deletion-intent-state-write-failed",
                                    size_bytes=size,
                                ))
                            except Exception:
                                # The terminal state is already durable; an
                                # audit write must not reopen deletion.
                                pass
                            summary["eligible"] = max(0, int(summary["eligible"]) - 1)
                            summary["potential_bytes"] = max(0, int(summary["potential_bytes"]) - size)
                            summary["failed"] = int(summary["failed"]) + 1
                            item_result.update({
                                "decision": "delete_unknown",
                                "decision_reason": "deletion-intent-state-write-failed",
                                "size_bytes": size,
                                "source_retained": True,
                                "source_state": "unknown",
                            })
                            summary["items"].append(item_result)
                            continue
                        self._append_audit_locked(self._audit_event(operation_id=operation_id, mode=mode, record=intent, prior_status=current_status, decision="deleting", reason="deletion-intent", size_bytes=size))
                    deletion = self.image_storage.delete_verified_local(
                        str(record.get("remote_path") or ""),
                        str(record.get("source_sha256") or ""),
                        expected_size=size,
                        expected_identity=current.get("source_identity"),
                        claim_held=True,
                        cleanup_token=cleanup_token,
                    )
                    with self._lock:
                        live = self._load_state_locked()
                        current = live.get(key, record)
                        if deletion.status == "deleted":
                            current.update({"cleanup_status": "deleted", "decision_reason": "deleted", "decided_at": self.now(), "size_bytes": deletion.size_bytes})
                            live[key] = current
                            self._append_audit_locked(self._audit_event(operation_id=operation_id, mode=mode, record=current, prior_status="deleting", decision="deleted", reason="deleted", size_bytes=deletion.size_bytes, reclaimed_bytes=deletion.size_bytes, detail=deletion.detail))
                            self._save_state_locked(live)
                            summary["deleted"] = int(summary["deleted"]) + 1
                            summary["reclaimed_bytes"] = int(summary["reclaimed_bytes"]) + deletion.size_bytes
                            item_result.update({"decision": "deleted", "decision_reason": "deleted", "size_bytes": deletion.size_bytes, "reclaimed_bytes": deletion.size_bytes, "source_retained": False})
                        elif deletion.status == "delete_failed":
                            current.update({"cleanup_status": "delete_failed", "decision_reason": deletion.reason, "decided_at": self.now()})
                            live[key] = current
                            self._append_audit_locked(self._audit_event(operation_id=operation_id, mode=mode, record=current, prior_status="deleting", decision="delete_failed", reason=deletion.reason, size_bytes=deletion.size_bytes, detail=deletion.detail))
                            self._save_state_locked(live)
                            summary["failed"] = int(summary["failed"]) + 1
                            item_result.update({"decision": "delete_failed", "decision_reason": deletion.reason, "size_bytes": deletion.size_bytes})
                        elif deletion.status == "delete_unknown":
                            current.update({"cleanup_status": "delete_unknown", "decision_reason": deletion.reason, "decided_at": self.now()})
                            live[key] = current
                            self._append_audit_locked(self._audit_event(operation_id=operation_id, mode=mode, record=current, prior_status="deleting", decision="delete_unknown", reason=deletion.reason, size_bytes=deletion.size_bytes, detail=deletion.detail))
                            self._save_state_locked(live)
                            summary["failed"] = int(summary["failed"]) + 1
                            item_result.update({"decision": "delete_unknown", "decision_reason": deletion.reason, "size_bytes": deletion.size_bytes, "source_retained": True, "source_state": "unknown"})
                        else:
                            current.update({"cleanup_status": "retained", "decision_reason": deletion.reason, "decided_at": self.now()})
                            live[key] = current
                            self._append_audit_locked(self._audit_event(operation_id=operation_id, mode=mode, record=current, prior_status="deleting", decision="retained", reason=deletion.reason, size_bytes=deletion.size_bytes, detail=deletion.detail))
                            self._save_state_locked(live)
                            summary["retained"] = int(summary["retained"]) + 1
                            item_result.update({"decision": "retained", "decision_reason": deletion.reason, "size_bytes": deletion.size_bytes})
                        summary["items"].append(item_result)
            except Exception:
                # A failed write after the intent is durable leaves the record
                # in ``deleting``. Recovery reports it as unknown; it never
                # guesses success or selects another path.
                summary["failed"] = int(summary["failed"]) + 1
                if initially_eligible:
                    summary["eligible"] = max(0, int(summary["eligible"]) - 1)
                    summary["potential_bytes"] = max(0, int(summary["potential_bytes"]) - initially_eligible_bytes)
                item_result.update({"decision": "delete_unknown", "decision_reason": "state-write-failed"})
                summary["items"].append(item_result)
            finally:
                claim.release()
        return summary

    def preview(self) -> dict[str, object]:
        return self.run(dry_run=True)

    def execute(self) -> dict[str, object]:
        return self.run(dry_run=False)

    def recover_inflight(self) -> dict[str, int]:
        """Resolve only durable deleting intents; never deletes on startup."""
        recovered = {"retained": 0, "unknown": 0}
        with self._lock:
            records = self._load_state_locked()
            changed = False
            for key, record in records.items():
                if record.get("cleanup_status") != "deleting":
                    continue
                path = str(record.get("remote_path") or "")
                digest = str(record.get("source_sha256") or "")
                cleanup_token = str(record.get("cleanup_token") or "")
                artifact_evidence = self.image_storage.inspect_verified_delete_artifacts(
                    path,
                    digest,
                    cleanup_token,
                    expected_size=int(record.get("size_bytes") or 0) or None,
                ) if cleanup_token else {"artifacts": [], "matching_artifacts": []}
                artifact_names = [str(name) for name in artifact_evidence.get("artifacts") or []]
                matching_artifacts = [str(name) for name in artifact_evidence.get("matching_artifacts") or []]
                detail = f"artifacts:{';'.join(artifact_names)}" if artifact_names else ""
                identity = self.image_storage.verify_local_identity(
                    path,
                    digest,
                    expected_size=int(record.get("size_bytes") or 0) or None,
                    expected_identity=record.get("source_identity"),
                )
                # Recovery may only claim that the source was retained when
                # the durable device/inode identity still matches. Matching
                # bytes or size alone is insufficient: a replacement inode
                # can contain identical content after a crash.
                if identity.get("ok"):
                    record.update({
                        "cleanup_status": "retained",
                        "decision_reason": "interrupted-before-delete",
                        "decided_at": self.now(),
                        "recovery_detail": detail,
                    })
                    recovered["retained"] += 1
                    decision = "retained"
                    reason = "interrupted-before-delete"
                else:
                    reason = "interrupted-artifact-retained" if matching_artifacts else "interrupted-ambiguous"
                    record.update({
                        "cleanup_status": "delete_unknown",
                        "decision_reason": reason,
                        "decided_at": self.now(),
                        "recovery_detail": detail,
                    })
                    recovered["unknown"] += 1
                    decision = "delete_unknown"
                records[key] = record
                try:
                    self._append_audit_locked(self._audit_event(
                        operation_id=f"recovery-{uuid.uuid4().hex}",
                        mode="recovery",
                        record=record,
                        prior_status="deleting",
                        decision=decision,
                        reason=reason,
                        size_bytes=int(record.get("size_bytes") or 0),
                        detail=detail,
                    ))
                except Exception:
                    # A missing recovery audit must never leave an intent that
                    # a later operation could mistake for active deletion.
                    if decision == "retained":
                        recovered["retained"] = max(0, recovered["retained"] - 1)
                    record.update({
                        "cleanup_status": "delete_unknown",
                        "decision_reason": "recovery-audit-write-failed",
                        "decided_at": self.now(),
                        "recovery_detail": detail,
                    })
                    records[key] = record
                    recovered["unknown"] += 1
                changed = True
            if changed:
                self._save_recovery_state_locked(records)
        return recovered


genbox_push_cleanup_service = GenBoxPushCleanupService()
