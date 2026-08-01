from __future__ import annotations

import json
import hashlib
import hmac
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from services.config import DATA_DIR
from services.image_storage_service import ImageStorageService, image_storage_service
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


class CleanupEnvironmentGate:
    """Server-side capability gate for destructive cleanup.

    Only an explicitly classified isolated-vps runtime with an explicit
    execute switch can unlink. Missing or unknown classification is disabled.
    Browser payloads never reach this class.
    """

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self.environ = environ if environ is not None else os.environ
        self._storage_root_provider: Callable[[], Path] | None = None

    def bind_storage_root(self, provider: Callable[[], Path]) -> None:
        self._storage_root_provider = provider

    def environment_class(self) -> str:
        return _clean(
            self.environ.get("CHATGPT2API_CLEANUP_ENVIRONMENT")
            or self.environ.get("GENBOX_CLEANUP_ENVIRONMENT")
            or self.environ.get("CHATGPT2API_ENVIRONMENT")
        ).lower()

    def _runtime_identity_matches(self) -> bool:
        configured_root = _clean(self.environ.get("CHATGPT2API_CLEANUP_STORAGE_ROOT"))
        if not configured_root or self._storage_root_provider is None:
            return False
        try:
            roots_match = Path(configured_root).resolve() == Path(self._storage_root_provider()).resolve()
        except (OSError, RuntimeError, TypeError):
            roots_match = False
        return bool(
            _clean(self.environ.get("CHATGPT2API_CLEANUP_INSTANCE_ROLE")) == "isolated-development"
            and _clean(self.environ.get("CHATGPT2API_CLEANUP_INSTANCE_ID"))
            and _clean(self.environ.get("CHATGPT2API_CLEANUP_CAPABILITY"))
            and roots_match
        )

    def destination_trusted(self, destination_scope: str) -> bool:
        trusted = _clean(self.environ.get("CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_SCOPE"))
        trust_kind = _clean(self.environ.get("CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_KIND"))
        return (
            trust_kind in {"https", "private-verified"}
            and bool(trusted)
            and hmac.compare_digest(trusted, _clean(destination_scope))
        )

    def can_execute(self) -> bool:
        return (
            self.environment_class() == "isolated-vps"
            and _bool_env(
                self.environ.get("CHATGPT2API_CLEANUP_EXECUTE")
                or self.environ.get("GENBOX_CLEANUP_EXECUTE")
            )
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
        self.environment_gate.bind_storage_root(lambda: self.image_storage_root())

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

    def _current_destination_scope(self) -> str:
        raw = read_json_object(self.settings_file, name=self.settings_file.name)
        base_url = _clean(raw.get("base_url")).rstrip("/")
        source_id = _clean(raw.get("source_id"))
        push_key = _clean(raw.get("push_key"))
        if not base_url or not source_id or not push_key:
            return ""
        return hashlib.sha256(f"{base_url}\n{source_id}\n{push_key}".encode("utf-8")).hexdigest()

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
        key = self._record_key(scope, sid, path, digest)
        now = self.now()
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
            "cleanup_status": "eligible" if safe_to_delete_source is True else "ineligible",
            "decision_reason": "receipt-confirmed" if safe_to_delete_source is True else "receipt-no-cleanup-permission",
            "decided_at": now,
            "size_bytes": max(0, int(size_bytes or 0)),
        }
        with self._lock:
            records = self._load_state_locked()
            existing = records.get(key)
            if isinstance(existing, dict) and existing.get("cleanup_status") in {"deleted", "deleting"}:
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
        if receipt.get("contract_version") != "v1" or receipt.get("source_id") != record.get("source_id"):
            return "retained", "receipt-invalid", 0
        if receipt.get("sha256") != record.get("source_sha256") or receipt.get("status") not in VALID_RECEIPT_STATUSES:
            return "retained", "receipt-invalid", 0
        if receipt.get("safe_to_delete_source") is not True:
            return "retained", "receipt-no-cleanup-permission", 0
        current_scope = self._current_destination_scope()
        if not current_scope or current_scope != str(record.get("destination_scope") or ""):
            return "retained", "destination-scope-changed", 0
        if not self.environment_gate.destination_trusted(current_scope):
            return "retained", "destination-unverified", 0
        result = self.image_storage.verify_local_identity(
            str(record.get("remote_path") or ""),
            str(record.get("source_sha256") or ""),
            expected_size=int(record.get("size_bytes") or 0) or None,
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
            "size_bytes": max(0, int(size_bytes or 0)),
            "reclaimed_bytes": max(0, int(reclaimed_bytes or 0)),
            "receipt_status": str((record.get("receipt") or {}).get("status") or ""),
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

            claim = source_claim(str(record.get("remote_path") or ""), str(record.get("source_sha256") or ""))
            if not claim.acquire(timeout_secs=0):
                reason = "source-busy"
                summary["eligible"] = max(0, int(summary["eligible"]) - 1)
                summary["potential_bytes"] = max(0, int(summary["potential_bytes"]) - size)
                summary["retained"] = int(summary["retained"]) + 1
                item_result.update({"decision": "retained", "decision_reason": reason, "size_bytes": 0})
                summary["items"].append(item_result)
                continue
            try:
                with self._lock:
                    live = self._load_state_locked()
                    current = live.get(key)
                    if current is None:
                        continue
                    decision, reason, size = self._inspect(current)
                    if decision != "eligible":
                        current.update({"cleanup_status": "retained", "decision_reason": reason, "decided_at": self.now()})
                        self._save_state_locked(live)
                        self._append_audit_locked(self._audit_event(operation_id=operation_id, mode=mode, record=current, prior_status=current_status, decision="retained", reason=reason, size_bytes=size))
                        summary["retained"] = int(summary["retained"]) + 1
                        item_result.update({"decision": "retained", "decision_reason": reason, "size_bytes": size})
                        summary["items"].append(item_result)
                        continue
                    intent = dict(current)
                    intent.update({"cleanup_status": "deleting", "decision_reason": "deletion-intent", "decided_at": self.now(), "operation_id": operation_id})
                    live[key] = intent
                    self._save_state_locked(live)
                    self._append_audit_locked(self._audit_event(operation_id=operation_id, mode=mode, record=intent, prior_status=current_status, decision="deleting", reason="deletion-intent", size_bytes=size))
                deletion = self.image_storage.delete_verified_local(
                    str(record.get("remote_path") or ""),
                    str(record.get("source_sha256") or ""),
                    expected_size=size,
                    claim_held=True,
                )
                with self._lock:
                    live = self._load_state_locked()
                    current = live.get(key, record)
                    if deletion.status == "deleted":
                        current.update({"cleanup_status": "deleted", "decision_reason": "deleted", "decided_at": self.now(), "size_bytes": deletion.size_bytes})
                        live[key] = current
                        self._save_state_locked(live)
                        self._append_audit_locked(self._audit_event(operation_id=operation_id, mode=mode, record=current, prior_status="deleting", decision="deleted", reason="deleted", size_bytes=deletion.size_bytes, reclaimed_bytes=deletion.size_bytes))
                        summary["deleted"] = int(summary["deleted"]) + 1
                        summary["reclaimed_bytes"] = int(summary["reclaimed_bytes"]) + deletion.size_bytes
                        item_result.update({"decision": "deleted", "decision_reason": "deleted", "size_bytes": deletion.size_bytes, "reclaimed_bytes": deletion.size_bytes, "source_retained": False})
                    elif deletion.status == "delete_failed":
                        current.update({"cleanup_status": "delete_failed", "decision_reason": deletion.reason, "decided_at": self.now()})
                        live[key] = current
                        self._save_state_locked(live)
                        self._append_audit_locked(self._audit_event(operation_id=operation_id, mode=mode, record=current, prior_status="deleting", decision="delete_failed", reason=deletion.reason, size_bytes=deletion.size_bytes))
                        summary["failed"] = int(summary["failed"]) + 1
                        item_result.update({"decision": "delete_failed", "decision_reason": deletion.reason, "size_bytes": deletion.size_bytes})
                    elif deletion.status == "delete_unknown":
                        current.update({"cleanup_status": "delete_unknown", "decision_reason": deletion.reason, "decided_at": self.now()})
                        live[key] = current
                        self._save_state_locked(live)
                        self._append_audit_locked(self._audit_event(operation_id=operation_id, mode=mode, record=current, prior_status="deleting", decision="delete_unknown", reason=deletion.reason, size_bytes=deletion.size_bytes))
                        summary["failed"] = int(summary["failed"]) + 1
                        item_result.update({"decision": "delete_unknown", "decision_reason": deletion.reason, "size_bytes": deletion.size_bytes, "source_retained": False})
                    else:
                        current.update({"cleanup_status": "retained", "decision_reason": deletion.reason, "decided_at": self.now()})
                        live[key] = current
                        self._save_state_locked(live)
                        self._append_audit_locked(self._audit_event(operation_id=operation_id, mode=mode, record=current, prior_status="deleting", decision="retained", reason=deletion.reason, size_bytes=deletion.size_bytes))
                        summary["retained"] = int(summary["retained"]) + 1
                        item_result.update({"decision": "retained", "decision_reason": deletion.reason, "size_bytes": deletion.size_bytes})
                    summary["items"].append(item_result)
            except Exception:
                # A failed write after the intent is durable leaves the record
                # in ``deleting``. Recovery reports it as unknown; it never
                # guesses success or selects another path.
                summary["failed"] = int(summary["failed"]) + 1
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
                identity = self.image_storage.verify_local_identity(path, digest, expected_size=int(record.get("size_bytes") or 0) or None)
                if identity.get("ok"):
                    record.update({"cleanup_status": "retained", "decision_reason": "interrupted-before-delete", "decided_at": self.now()})
                    recovered["retained"] += 1
                    decision = "retained"
                    reason = "interrupted-before-delete"
                else:
                    record.update({"cleanup_status": "delete_unknown", "decision_reason": "interrupted-ambiguous", "decided_at": self.now()})
                    recovered["unknown"] += 1
                    decision = "delete_unknown"
                    reason = "interrupted-ambiguous"
                records[key] = record
                self._append_audit_locked(self._audit_event(
                    operation_id=f"recovery-{uuid.uuid4().hex}",
                    mode="recovery",
                    record=record,
                    prior_status="deleting",
                    decision=decision,
                    reason=reason,
                    size_bytes=int(record.get("size_bytes") or 0),
                ))
                changed = True
            if changed:
                self._save_state_locked(records)
        return recovered


genbox_push_cleanup_service = GenBoxPushCleanupService()
