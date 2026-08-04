from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "local-test-admin-key")

from services.config import config
from services.genbox_push_cleanup import CleanupEnvironmentGate, GenBoxPushCleanupService, issue_runtime_attestation
from services.genbox_push_service import GenBoxPushService
from services.image_storage_service import ImageStorageService, VerifiedDeleteResult
from services.json_file import write_json_file
from services.source_claim import source_claim


def _request(*headers: tuple[str, str]) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/api/genbox-push/cleanup/run",
        "query_string": b"",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers],
    })


class _ImageConfig:
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def images_dir(self) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root

    def get_image_storage_settings(self) -> dict[str, object]:
        return {"mode": "local"}

    @property
    def base_url(self) -> str:
        return ""


class _NoopThread:
    def join(self, timeout: float | None = None) -> None:
        del timeout


def _cleanup_process_worker(
    images: str,
    settings: str,
    state: str,
    audit: str,
    index: str,
    environment: dict[str, str],
    result_queue,
) -> None:
    import services.image_storage_service as storage_module
    import services.source_claim as claim_module

    storage_module.config = _ImageConfig(Path(images))
    claim_module.DATA_DIR = Path(state).parent / "claims"
    storage = ImageStorageService(index_file=Path(index))
    service = GenBoxPushCleanupService(
        state_file=Path(state),
        audit_file=Path(audit),
        settings_file=Path(settings),
        image_storage=storage,
        environment_gate=CleanupEnvironmentGate(
            dict(environment),
            capability=environment.get("CHATGPT2API_CLEANUP_CAPABILITY"),
            attestation_file=Path(environment["CHATGPT2API_CLEANUP_ATTESTATION_FILE"]),
        ),
    )
    result_queue.put(service.execute())


def _cleanup_crash_worker(
    images: str,
    settings: str,
    state: str,
    audit: str,
    index: str,
    environment: dict[str, str],
) -> None:
    import services.image_storage_service as storage_module
    import services.source_claim as claim_module

    storage_module.config = _ImageConfig(Path(images))
    claim_module.DATA_DIR = Path(state).parent / "claims"
    storage = ImageStorageService(index_file=Path(index))
    storage._before_verified_unlink = lambda _target: os._exit(17)
    service = GenBoxPushCleanupService(
        state_file=Path(state),
        audit_file=Path(audit),
        settings_file=Path(settings),
        image_storage=storage,
        environment_gate=CleanupEnvironmentGate(
            dict(environment),
            capability=environment.get("CHATGPT2API_CLEANUP_CAPABILITY"),
            attestation_file=Path(environment["CHATGPT2API_CLEANUP_ATTESTATION_FILE"]),
        ),
    )
    service.execute()


def _cleanup_crash_after_unlink_worker(
    images: str,
    settings: str,
    state: str,
    audit: str,
    index: str,
    environment: dict[str, str],
    relative_path: str,
) -> None:
    import services.image_storage_service as storage_module
    import services.source_claim as claim_module

    storage_module.config = _ImageConfig(Path(images))
    claim_module.DATA_DIR = Path(state).parent / "claims"
    storage = ImageStorageService(index_file=Path(index))

    def unlink_then_exit(rel: str, *_args, **_kwargs):
        (Path(images) / rel).unlink()
        os._exit(19)

    storage.delete_verified_local = unlink_then_exit
    service = GenBoxPushCleanupService(
        state_file=Path(state),
        audit_file=Path(audit),
        settings_file=Path(settings),
        image_storage=storage,
        environment_gate=CleanupEnvironmentGate(
            dict(environment),
            capability=environment.get("CHATGPT2API_CLEANUP_CAPABILITY"),
            attestation_file=Path(environment["CHATGPT2API_CLEANUP_ATTESTATION_FILE"]),
        ),
    )
    service.execute()


def _cleanup_atomic_crash_worker(
    images: str,
    settings: str,
    state: str,
    audit: str,
    index: str,
    environment: dict[str, str],
    stage: str,
    exit_code: int,
) -> None:
    import services.image_storage_service as storage_module
    import services.source_claim as claim_module

    storage_module.config = _ImageConfig(Path(images))
    claim_module.DATA_DIR = Path(state).parent / "claims"
    storage = ImageStorageService(index_file=Path(index))
    crash = lambda _target: os._exit(exit_code)
    if stage == "after-exchange":
        storage._after_posix_exchange = crash
    elif stage == "after-tombstone":
        storage._after_posix_tombstone = crash
    else:
        raise RuntimeError("unsupported crash stage")
    service = GenBoxPushCleanupService(
        state_file=Path(state),
        audit_file=Path(audit),
        settings_file=Path(settings),
        image_storage=storage,
        environment_gate=CleanupEnvironmentGate(
            dict(environment),
            capability=environment.get("CHATGPT2API_CLEANUP_CAPABILITY"),
            attestation_file=Path(environment["CHATGPT2API_CLEANUP_ATTESTATION_FILE"]),
        ),
    )
    service.execute()


def _cleanup_crash_after_terminal_audit_worker(
    images: str,
    settings: str,
    state: str,
    audit: str,
    index: str,
    environment: dict[str, str],
) -> None:
    import services.image_storage_service as storage_module
    import services.source_claim as claim_module

    storage_module.config = _ImageConfig(Path(images))
    claim_module.DATA_DIR = Path(state).parent / "claims"
    storage = ImageStorageService(index_file=Path(index))
    service = GenBoxPushCleanupService(
        state_file=Path(state),
        audit_file=Path(audit),
        settings_file=Path(settings),
        image_storage=storage,
        environment_gate=CleanupEnvironmentGate(
            dict(environment),
            capability=environment.get("CHATGPT2API_CLEANUP_CAPABILITY"),
            attestation_file=Path(environment["CHATGPT2API_CLEANUP_ATTESTATION_FILE"]),
        ),
    )
    original_save = service._save_state_locked

    def crash_on_terminal_state(records: dict[str, dict[str, object]]) -> None:
        if any(record.get("cleanup_status") == "deleted" for record in records.values()):
            os._exit(23)
        original_save(records)

    service._save_state_locked = crash_on_terminal_state
    service.execute()


class GenBoxPushCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config_patch = None
        self.claim_path_patch = None
        self.staging_env_patch = None
        self.tmp = Path(tempfile.mkdtemp(prefix="genbox-cleanup-"))
        self.images = self.tmp / "images"
        self.protected_staging = self.tmp / "protected-staging"
        self.protected_staging.mkdir()
        if os.name != "nt":
            try:
                os.chown(self.protected_staging, 65534, 65534)
            except (AttributeError, PermissionError, OSError):
                self.skipTest("protected POSIX staging requires a provisioned ownership boundary")
            self.staging_env_patch = patch.dict(
                os.environ,
                {"GENBOX_CLEANUP_PROTECTED_STAGING_ROOT": str(self.protected_staging)},
            )
            self.staging_env_patch.start()
        self.settings = self.tmp / "settings.json"
        self.state = self.tmp / "cleanup.json"
        self.audit = self.tmp / "audit.json"
        self.instance_id = "synthetic-isolated-sender"
        self.compose_project = "genbox-phase6-synthetic"
        self.container_name = "genbox-phase6-sender"
        self.service_port = 33010
        self.image_digest = "sha256:" + "a" * 64
        self.instance_marker = self.tmp / ".genbox-isolated-cleanup"
        self.instance_marker.write_text(json.dumps({
            "version": 1,
            "instance_id": self.instance_id,
            "role": "isolated-development",
            "storage_root": str(self.images.resolve()),
            "compose_project": self.compose_project,
            "container_name": self.container_name,
            "service_port": self.service_port,
            "image_digest": self.image_digest,
        }, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        marker_hash = hashlib.sha256(self.instance_marker.read_bytes()).hexdigest()
        self.capability = "synthetic-capability-32-bytes-000000000000"
        self.attestation = self.tmp / "runtime-attestation.json"
        destination_scope = hashlib.sha256(
            b"https://genbox.test\nchatgpt2api-dev\nsynthetic-push-key"
        ).hexdigest()
        issue_runtime_attestation(
            self.attestation,
            identity={
                "instance_id": self.instance_id,
                "role": "isolated-development",
                "storage_root": str(self.images.resolve()),
                "compose_project": self.compose_project,
                "container_name": self.container_name,
                "service_port": self.service_port,
                "image_digest": self.image_digest,
                "marker_sha256": marker_hash,
                "trusted_destination_scope": destination_scope,
            },
            capability=self.capability,
        )
        write_json_file(self.settings, {
            "enabled": True,
            "cleanup_enabled": False,
            "base_url": "https://genbox.test",
            "source_id": "chatgpt2api-dev",
            "push_key": "synthetic-push-key",
        })
        self.storage = ImageStorageService(index_file=self.tmp / "index.json")
        self.gate = CleanupEnvironmentGate({
            "CHATGPT2API_CLEANUP_ENVIRONMENT": "isolated-vps",
            "CHATGPT2API_CLEANUP_EXECUTE": "1",
            "CHATGPT2API_CLEANUP_INSTANCE_ROLE": "isolated-development",
            "CHATGPT2API_CLEANUP_INSTANCE_ID": self.instance_id,
            "CHATGPT2API_CLEANUP_STORAGE_ROOT": str(self.images.resolve()),
            "CHATGPT2API_CLEANUP_COMPOSE_PROJECT": self.compose_project,
            "CHATGPT2API_CLEANUP_CONTAINER_NAME": self.container_name,
            "CHATGPT2API_CLEANUP_SERVICE_PORT": str(self.service_port),
            "CHATGPT2API_CLEANUP_IMAGE_DIGEST": self.image_digest,
            "CHATGPT2API_CLEANUP_CAPABILITY": self.capability,
            "CHATGPT2API_CLEANUP_ATTESTATION_FILE": str(self.attestation),
            "CHATGPT2API_CLEANUP_MARKER_SHA256": marker_hash,
            "CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_KIND": "https",
            "CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_URL": "https://genbox.test",
            "CHATGPT2API_CLEANUP_TRUSTED_PRIVATE_HOST": "genbox.test",
            "CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_SCOPE": hashlib.sha256(
                b"https://genbox.test\nchatgpt2api-dev\nsynthetic-push-key"
            ).hexdigest(),
        }, capability=self.capability, attestation_file=self.attestation)
        self.service = GenBoxPushCleanupService(
            state_file=self.state,
            audit_file=self.audit,
            settings_file=self.settings,
            image_storage=self.storage,
            environment_gate=self.gate,
        )
        self.config_patch = patch("services.image_storage_service.config", _ImageConfig(self.images))
        self.config_patch.start()
        # Keep all process-shared source claims inside this test's temporary
        # root. A live development server must not make a synthetic item look
        # busy or alter the recovery outcome.
        self.claim_path_patch = patch("services.source_claim.DATA_DIR", self.tmp / "claims")
        self.claim_path_patch.start()

    def tearDown(self) -> None:
        if self.staging_env_patch is not None:
            self.staging_env_patch.stop()
        if self.config_patch is not None:
            self.config_patch.stop()
        if self.claim_path_patch is not None:
            self.claim_path_patch.stop()
        for path in sorted(self.tmp.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.tmp.rmdir()

    def _scope(self) -> str:
        import hashlib

        return hashlib.sha256(b"https://genbox.test\nchatgpt2api-dev\nsynthetic-push-key").hexdigest()

    def _record(self, path: str = "2026/08/01/image.png", payload: bytes = b"synthetic-image") -> Path:
        target = self.images / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        self.service.record_receipt(
            destination_scope=self._scope(),
            source_id="chatgpt2api-dev",
            remote_path=path,
            source_sha256=digest,
            receipt_status="imported",
            safe_to_delete_source=True,
            size_bytes=len(payload),
        )
        return target

    def _enable_policy(self) -> None:
        write_json_file(self.settings, {
            "enabled": True,
            "cleanup_enabled": True,
            "base_url": "https://genbox.test",
            "source_id": "chatgpt2api-dev",
            "push_key": "synthetic-push-key",
        })

    def test_cleanup_is_off_by_default_and_execute_is_blocked(self) -> None:
        target = self._record()

        result = self.service.execute()

        self.assertEqual(result.get("blocked_reason"), "cleanup-policy-disabled")
        self.assertTrue(target.exists())
        self.assertEqual(result["deleted"], 0)

    def test_dry_run_reports_bytes_without_unlink_and_execute_reclaims_after_gate(self) -> None:
        target = self._record()
        self._enable_policy()

        preview = self.service.preview()
        self.assertEqual(preview["eligible"], 1)
        self.assertEqual(preview["potential_bytes"], len(b"synthetic-image"))
        self.assertTrue(target.exists())

        result = self.service.execute()
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["reclaimed_bytes"], len(b"synthetic-image"))
        self.assertFalse(target.exists())
        audit = json.loads(self.audit.read_text(encoding="utf-8"))
        self.assertTrue(any(event.get("decision") == "deleted" for event in audit["events"]))

    def test_quarantine_names_are_disclosed_in_audit_detail(self) -> None:
        target = self._record()
        self._enable_policy()

        def quarantined_retain(*_args: object, **_kwargs: object) -> VerifiedDeleteResult:
            return VerifiedDeleteResult(
                "retained",
                "source-changed",
                int(target.stat().st_size),
                detail="quarantined:.genbox-retained-synthetic",
            )

        with patch.object(self.storage, "delete_verified_local", side_effect=quarantined_retain):
            result = self.service.execute()

        self.assertEqual(result["retained"], 1)
        self.assertTrue(target.exists())
        audit = json.loads(self.audit.read_text(encoding="utf-8"))
        retained = [event for event in audit["events"] if event.get("decision") == "retained"]
        self.assertTrue(retained)
        self.assertEqual(
            retained[-1].get("decision_detail"),
            "quarantined:.genbox-retained-synthetic",
        )

    def test_changed_source_is_retained(self) -> None:
        target = self._record()
        self._enable_policy()
        target.write_bytes(b"changed-source")

        result = self.service.execute()

        self.assertEqual(result["deleted"], 0)
        self.assertTrue(target.exists())
        self.assertEqual(result["items"][0]["decision_reason"], "source-changed")

    def test_same_content_new_file_identity_after_receipt_is_retained(self) -> None:
        target = self._record()
        self._enable_policy()
        replacement = target.with_name("same-content-replacement.png")
        replacement.write_bytes(target.read_bytes())
        os.replace(replacement, target)

        result = self.service.execute()

        self.assertEqual(result["deleted"], 0)
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"synthetic-image")
        self.assertEqual(result["items"][0]["decision_reason"], "source-changed")

    def test_receipt_without_durable_source_identity_is_retained(self) -> None:
        target = self._record()
        self._enable_policy()
        payload = json.loads(self.state.read_text(encoding="utf-8"))
        record = next(iter(payload["records"].values()))
        record.pop("source_identity", None)
        record.pop("source_identity_reason", None)
        write_json_file(self.state, payload)

        result = self.service.execute()

        self.assertEqual(result["deleted"], 0)
        self.assertTrue(target.exists())
        self.assertEqual(result["items"][0]["decision_reason"], "source-identity-missing")

    def test_destination_rotation_invalidates_old_receipt(self) -> None:
        target = self._record()
        self._enable_policy()
        write_json_file(self.settings, {
            "enabled": True,
            "cleanup_enabled": True,
            "base_url": "https://genbox.test",
            "source_id": "chatgpt2api-dev",
            "push_key": "rotated-key",
        })

        result = self.service.execute()

        self.assertTrue(target.exists())
        self.assertEqual(result["items"][0]["decision_reason"], "destination-scope-changed")
        self.assertEqual(result["eligible"], 0)
        self.assertEqual(result["potential_bytes"], 0)
        self.assertEqual(result["retained"], 1)
        self.assertEqual(result["candidates"], 1)

    def test_symlink_and_hardlink_aliases_are_retained(self) -> None:
        self._enable_policy()
        source = self.images / "2026/08/01/source.png"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source")
        symlink = self.images / "2026/08/01/link.png"
        try:
            symlink.symlink_to(source)
        except (OSError, NotImplementedError):
            symlink = None
        if symlink is not None:
            digest = hashlib.sha256(b"source").hexdigest()
            self.service.record_receipt(
                destination_scope=self._scope(), source_id="chatgpt2api-dev", remote_path="2026/08/01/link.png",
                source_sha256=digest, receipt_status="imported", safe_to_delete_source=True, size_bytes=6,
            )
            result = self.service.execute()
            self.assertTrue(source.exists())
            self.assertEqual(result["items"][0]["decision_reason"], "path-alias")

        outside = self.tmp / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "parent-source.png").write_bytes(b"parent")
        parent_alias = self.images / "2026/08/01/alias-parent"
        try:
            parent_alias.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            parent_alias = None
        if parent_alias is not None:
            digest = hashlib.sha256(b"parent").hexdigest()
            self.service.record_receipt(
                destination_scope=self._scope(), source_id="chatgpt2api-dev", remote_path="2026/08/01/alias-parent/parent-source.png",
                source_sha256=digest, receipt_status="imported", safe_to_delete_source=True, size_bytes=6,
            )
            result = self.service.execute()
            self.assertTrue((outside / "parent-source.png").exists())
            self.assertTrue(any(item["decision_reason"] == "path-alias" for item in result["items"]))

        hard = self.images / "2026/08/01/hard.png"
        hard.write_bytes(b"hard")
        alias = self.images / "2026/08/01/hard-alias.png"
        try:
            os.link(hard, alias)
        except OSError:
            self.skipTest("hard links are unavailable on this filesystem")
        digest = hashlib.sha256(b"hard").hexdigest()
        self.service.record_receipt(
            destination_scope=self._scope(), source_id="chatgpt2api-dev", remote_path="2026/08/01/hard.png",
            source_sha256=digest, receipt_status="imported", safe_to_delete_source=True, size_bytes=4,
        )
        result = self.service.execute()
        self.assertTrue(hard.exists())
        self.assertTrue(alias.exists())
        self.assertTrue(any(item["decision_reason"] == "path-alias" for item in result["items"]))

    def test_missing_gate_is_fail_closed(self) -> None:
        self._enable_policy()
        target = self._record()
        gate = CleanupEnvironmentGate({})
        service = GenBoxPushCleanupService(
            state_file=self.state,
            audit_file=self.audit,
            settings_file=self.settings,
            image_storage=self.storage,
            environment_gate=gate,
        )

        result = service.execute()

        self.assertEqual(result.get("blocked_reason"), "development-disabled")
        self.assertTrue(target.exists())

    def test_destination_scope_without_server_trust_is_retained(self) -> None:
        target = self._record()
        self._enable_policy()
        self.gate.environ["CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_SCOPE"] = "wrong-scope"

        result = self.service.execute()

        self.assertTrue(target.exists())
        self.assertEqual(result["blocked_reason"], "runtime-identity-unverified")

    def test_http_destination_is_untrusted_even_with_matching_scope(self) -> None:
        target = self._record()
        self._enable_policy()
        self.gate.environ["CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_URL"] = "http://genbox.test"

        result = self.service.execute()

        self.assertTrue(target.exists())
        self.assertEqual(result["items"][0]["decision_reason"], "destination-unverified")

    def test_private_verified_hostname_requires_server_attestation(self) -> None:
        target = self._record()
        self._enable_policy()
        self.gate.environ["CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_KIND"] = "private-verified"
        self.gate.environ["CHATGPT2API_CLEANUP_TRUSTED_PRIVATE_HOST"] = "other.internal"

        result = self.service.execute()

        self.assertTrue(target.exists())
        self.assertEqual(result["items"][0]["decision_reason"], "destination-unverified")

    def test_policy_flip_after_inspection_retains_source(self) -> None:
        target = self._record()
        self._enable_policy()
        original_inspect = self.service._inspect

        def flip_policy(record):
            result = original_inspect(record)
            write_json_file(self.settings, {
                "enabled": True,
                "cleanup_enabled": False,
                "base_url": "https://genbox.test",
                "source_id": "chatgpt2api-dev",
                "push_key": "synthetic-push-key",
            })
            return result

        with patch.object(self.service, "_inspect", side_effect=flip_policy):
            result = self.service.execute()

        self.assertTrue(target.exists())
        self.assertEqual(result["items"][0]["decision_reason"], "cleanup-policy-disabled")

    def test_destination_rotation_after_final_inspection_retains_source(self) -> None:
        target = self._record()
        self._enable_policy()
        original_inspect = self.service._inspect
        calls = 0

        def rotate_after_final_inspect(record):
            nonlocal calls
            calls += 1
            result = original_inspect(record)
            if calls == 2:
                write_json_file(self.settings, {
                    "enabled": True,
                    "cleanup_enabled": True,
                    "base_url": "https://genbox.test",
                    "source_id": "chatgpt2api-dev",
                    "push_key": "rotated-after-inspection",
                })
            return result

        with patch.object(self.service, "_inspect", side_effect=rotate_after_final_inspect):
            result = self.service.execute()

        self.assertTrue(target.exists())
        self.assertEqual(result["items"][0]["decision_reason"], "destination-scope-changed")

    def test_settings_writer_waits_until_cleanup_terminal_state(self) -> None:
        target = self._record()
        self._enable_policy()
        writer = GenBoxPushService(settings_file=self.settings, state_file=self.tmp / "push-state.json")
        finished = threading.Event()
        rotation_thread: list[threading.Thread] = []
        original_delete = self.storage.delete_verified_local

        def rotate_after_delete_starts(*args, **kwargs):
            def rotate() -> None:
                writer.update_settings({"push_key": "rotated-after-delete"})
                finished.set()

            thread = threading.Thread(target=rotate)
            thread.start()
            rotation_thread.append(thread)
            time.sleep(0.05)
            self.assertFalse(finished.is_set())
            return original_delete(*args, **kwargs)

        with patch.object(self.storage, "delete_verified_local", side_effect=rotate_after_delete_starts):
            result = self.service.execute()

        self.assertEqual(result["deleted"], 1)
        self.assertFalse(target.exists())
        rotation_thread[0].join(timeout=5)
        self.assertTrue(finished.is_set())

    def test_terminal_audit_failure_leaves_durable_deleting_intent(self) -> None:
        target = self._record()
        self._enable_policy()
        original_append = self.service._append_audit_locked

        def fail_terminal(event):
            if event.get("decision") == "deleting":
                return original_append(event)
            raise OSError("synthetic audit failure")

        with patch.object(self.service, "_append_audit_locked", side_effect=fail_terminal):
            result = self.service.execute()

        self.assertFalse(target.exists())
        self.assertEqual(result["items"][0]["decision"], "delete_unknown")
        records = json.loads(self.state.read_text(encoding="utf-8"))["records"]
        self.assertEqual(next(iter(records.values()))["cleanup_status"], "deleting")

    def test_recovery_audit_failure_persists_terminal_unknown(self) -> None:
        target = self._record()
        records = json.loads(self.state.read_text(encoding="utf-8"))["records"]
        key, record = next(iter(records.items()))
        record.update({"cleanup_status": "deleting", "decision_reason": "deletion-intent"})
        records[key] = record
        write_json_file(self.state, {"record_version": 1, "records": records})

        with patch.object(self.service, "_append_audit_locked", side_effect=OSError("synthetic recovery audit failure")):
            result = self.service.recover_inflight()

        self.assertEqual(result, {"retained": 0, "unknown": 1})
        self.assertTrue(target.exists())
        recovered = json.loads(self.state.read_text(encoding="utf-8"))["records"][key]
        self.assertEqual(recovered["cleanup_status"], "delete_unknown")
        self.assertEqual(recovered["decision_reason"], "recovery-audit-write-failed")

    def test_missing_isolated_marker_blocks_execute(self) -> None:
        target = self._record()
        self._enable_policy()
        self.instance_marker.unlink()

        result = self.service.execute()

        self.assertTrue(target.exists())
        self.assertEqual(result.get("blocked_reason"), "runtime-identity-unverified")

    def test_each_isolated_runtime_identity_mismatch_blocks_execute(self) -> None:
        target = self._record()
        self._enable_policy()
        mismatches = {
            "CHATGPT2API_CLEANUP_INSTANCE_ID": "wrong-instance",
            "CHATGPT2API_CLEANUP_INSTANCE_ROLE": "production",
            "CHATGPT2API_CLEANUP_STORAGE_ROOT": str(self.tmp / "wrong-images"),
            "CHATGPT2API_CLEANUP_COMPOSE_PROJECT": "wrong-project",
            "CHATGPT2API_CLEANUP_CONTAINER_NAME": "wrong-container",
            "CHATGPT2API_CLEANUP_SERVICE_PORT": "33018",
            "CHATGPT2API_CLEANUP_IMAGE_DIGEST": "sha256:" + "b" * 64,
        }

        for field, value in mismatches.items():
            with self.subTest(field=field):
                original = self.gate.environ.get(field)
                self.gate.environ[field] = value
                try:
                    result = self.service.execute()
                finally:
                    if original is None:
                        self.gate.environ.pop(field, None)
                    else:
                        self.gate.environ[field] = original
                self.assertEqual(result.get("blocked_reason"), "runtime-identity-unverified")
                self.assertTrue(target.exists())

    def test_environment_capability_spoof_is_not_authority(self) -> None:
        target = self._record()
        self._enable_policy()
        spoofed = CleanupEnvironmentGate(
            dict(self.gate.environ),
            attestation_file=self.attestation,
        )
        service = GenBoxPushCleanupService(
            state_file=self.state,
            audit_file=self.audit,
            settings_file=self.settings,
            image_storage=self.storage,
            environment_gate=spoofed,
        )
        result = service.execute()
        self.assertEqual(result.get("blocked_reason"), "runtime-identity-unverified")
        self.assertTrue(target.exists())

    def test_startup_attestation_uses_explicit_path_without_environment_override(self) -> None:
        self.images.mkdir()
        environment = dict(self.gate.environ)
        environment.pop("CHATGPT2API_CLEANUP_ATTESTATION_FILE")
        gate = CleanupEnvironmentGate(environment, attestation_file=self.attestation)
        service = GenBoxPushCleanupService(
            state_file=self.state,
            audit_file=self.audit,
            settings_file=self.settings,
            image_storage=self.storage,
            environment_gate=gate,
        )

        self.assertTrue(service.initialize_runtime_capability())
        self.assertTrue(service.environment_gate.can_execute())

    def test_full_cloned_state_and_attestation_cannot_inherit_cleanup_authority(self) -> None:
        target = self._record()
        self._enable_policy()
        clone = self.tmp / "clone"
        clone.mkdir()
        clone_state = clone / "cleanup.json"
        clone_audit = clone / "audit.json"
        clone_settings = clone / "settings.json"
        clone_attestation = clone / "runtime-attestation.json"
        clone_state.write_bytes(self.state.read_bytes())
        clone_audit.write_bytes(self.audit.read_bytes()) if self.audit.exists() else None
        clone_settings.write_bytes(self.settings.read_bytes())
        clone_attestation.write_bytes(self.attestation.read_bytes())
        cloned_gate = CleanupEnvironmentGate(
            dict(self.gate.environ),
            capability="",
            attestation_file=clone_attestation,
        )
        cloned_service = GenBoxPushCleanupService(
            state_file=clone_state,
            audit_file=clone_audit,
            settings_file=clone_settings,
            image_storage=self.storage,
            environment_gate=cloned_gate,
        )
        self.assertTrue(cloned_service.initialize_runtime_capability())
        result = cloned_service.execute()
        self.assertNotIn("blocked_reason", result)
        self.assertEqual(result["items"][0]["decision_reason"], "runtime-identity-changed")
        self.assertTrue(target.exists())

    def test_capability_replay_against_new_runtime_is_rejected(self) -> None:
        target = self._record()
        self._enable_policy()
        replayed = CleanupEnvironmentGate(
            dict(self.gate.environ),
            capability="different-capability-32-bytes-000000000000",
            attestation_file=self.attestation,
        )
        service = GenBoxPushCleanupService(
            state_file=self.state,
            audit_file=self.audit,
            settings_file=self.settings,
            image_storage=self.storage,
            environment_gate=replayed,
        )
        result = service.execute()
        self.assertEqual(result.get("blocked_reason"), "runtime-identity-unverified")
        self.assertTrue(target.exists())

    def test_runtime_identity_digest_is_bound_to_receipt(self) -> None:
        self._record()
        record = next(iter(json.loads(self.state.read_text(encoding="utf-8"))["records"].values()))
        self.assertEqual(record["runtime_identity_digest"], self.gate.runtime_identity_digest())
        self.gate.environ["CHATGPT2API_CLEANUP_IMAGE_DIGEST"] = "sha256:" + "b" * 64
        self.assertEqual(self.gate.runtime_identity_digest(), "")

    def test_marker_with_extra_or_missing_identity_field_blocks_execute(self) -> None:
        target = self._record()
        self._enable_policy()
        original_marker = json.loads(self.instance_marker.read_text(encoding="utf-8"))
        for mutation in ("extra", "missing"):
            with self.subTest(mutation=mutation):
                marker = dict(original_marker)
                if mutation == "extra":
                    marker["unexpected"] = "value"
                else:
                    marker.pop("compose_project")
                self.instance_marker.write_text(
                    json.dumps(marker, separators=(",", ":"), sort_keys=True),
                    encoding="utf-8",
                )
                self.gate.environ["CHATGPT2API_CLEANUP_MARKER_SHA256"] = hashlib.sha256(
                    self.instance_marker.read_bytes()
                ).hexdigest()
                result = self.service.execute()
                self.assertEqual(result.get("blocked_reason"), "runtime-identity-unverified")
                self.assertTrue(target.exists())

    def test_cleanup_admin_rejects_cross_origin_browser_request(self) -> None:
        from api.support import require_cleanup_admin

        with patch("api.support.require_admin", return_value={"role": "admin"}):
            with self.assertRaises(HTTPException) as caught:
                require_cleanup_admin(
                    _request(("origin", "https://attacker.invalid"), ("sec-fetch-site", "cross-site")),
                    "Bearer synthetic-admin",
                )
        self.assertEqual(caught.exception.status_code, 403)

    def test_cleanup_admin_accepts_same_origin_request(self) -> None:
        from api.support import require_cleanup_admin

        with patch("api.support.require_admin", return_value={"role": "admin"}):
            identity = require_cleanup_admin(
                _request(("origin", "http://testserver"), ("sec-fetch-site", "same-origin")),
                "Bearer synthetic-admin",
            )
        self.assertEqual(identity["role"], "admin")

    def test_cleanup_settings_rejects_browser_supplied_target_fields(self) -> None:
        from pydantic import ValidationError

        from api.genbox_push import GenBoxPushCleanupSettingsRequest

        with self.assertRaises(ValidationError):
            GenBoxPushCleanupSettingsRequest.model_validate({
                "enabled": True,
                "path": "outside/root.png",
                "receipt": {"safe_to_delete_source": True},
                "environment": "isolated-vps",
            })

    def test_cleanup_operation_rejects_browser_supplied_authority_fields(self) -> None:
        from pydantic import ValidationError

        from api.genbox_push import GenBoxPushCleanupOperationRequest

        with self.assertRaises(ValidationError):
            GenBoxPushCleanupOperationRequest.model_validate({
                "environment": "isolated-vps",
                "path": "2026/08/01/image.png",
                "receipt": {"safe_to_delete_source": True},
                "marker": "forged",
                "capability": "forged",
            })

    def test_automatic_retention_protects_unresolved_push_source(self) -> None:
        from services.config import config

        with patch("services.config.read_json_object", return_value={
            "records": {
                "source": {
                    "remote_path": "2026/08/01/protected.png",
                    "cleanup_status": "eligible",
                },
                "deleted": {
                    "remote_path": "2026/08/01/already-deleted.png",
                    "cleanup_status": "deleted",
                },
            }
        }):
            protected = config.receipt_protected_image_paths()

        self.assertIn("2026/08/01/protected.png", protected)
        self.assertNotIn("2026/08/01/already-deleted.png", protected)

    def test_restart_recovery_retains_existing_source_after_deleting_intent(self) -> None:
        target = self._record()
        records = json.loads(self.state.read_text(encoding="utf-8"))["records"]
        key, record = next(iter(records.items()))
        record["cleanup_status"] = "deleting"
        record["decision_reason"] = "deletion-intent"
        records[key] = record
        write_json_file(self.state, {"record_version": 1, "records": records})

        result = self.service.recover_inflight()

        self.assertEqual(result, {"retained": 1, "unknown": 0})
        self.assertTrue(target.exists())
        recovered = json.loads(self.state.read_text(encoding="utf-8"))["records"][key]
        self.assertEqual(recovered["cleanup_status"], "retained")
        self.assertEqual(recovered["decision_reason"], "interrupted-before-delete")

    def test_restart_recovery_marks_missing_target_unknown_without_deleting_another_file(self) -> None:
        target = self._record()
        records = json.loads(self.state.read_text(encoding="utf-8"))["records"]
        key, record = next(iter(records.items()))
        record["cleanup_status"] = "deleting"
        records[key] = record
        write_json_file(self.state, {"record_version": 1, "records": records})
        target.unlink()
        substitute = self.images / "2026/08/01/substitute.png"
        substitute.write_bytes(b"substitute")

        result = self.service.recover_inflight()

        self.assertEqual(result, {"retained": 0, "unknown": 1})
        self.assertTrue(substitute.exists())
        recovered = json.loads(self.state.read_text(encoding="utf-8"))["records"][key]
        self.assertEqual(recovered["cleanup_status"], "delete_unknown")

    def test_restart_recovery_marks_same_content_replacement_unknown(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX inode replacement requires Linux")
        target = self._record()
        records = json.loads(self.state.read_text(encoding="utf-8"))["records"]
        key, record = next(iter(records.items()))
        record["cleanup_status"] = "deleting"
        record["decision_reason"] = "deletion-intent"
        records[key] = record
        write_json_file(self.state, {"record_version": 1, "records": records})

        replacement = target.with_name("same-content-replacement.tmp")
        replacement.write_bytes(b"synthetic-image")
        original_identity = record["source_identity"]
        os.replace(replacement, target)
        current_identity = self.storage.verify_local_identity(
            "2026/08/01/image.png",
            hashlib.sha256(b"synthetic-image").hexdigest(),
            expected_size=len(b"synthetic-image"),
        )["source_identity"]
        self.assertNotEqual(current_identity, original_identity)

        result = self.service.recover_inflight()

        self.assertEqual(result, {"retained": 0, "unknown": 1})
        self.assertTrue(target.exists())
        recovered = json.loads(self.state.read_text(encoding="utf-8"))["records"][key]
        self.assertEqual(recovered["cleanup_status"], "delete_unknown")
        self.assertEqual(recovered["decision_reason"], "interrupted-ambiguous")

    def test_execute_never_reprocesses_durable_deleting_intent(self) -> None:
        target = self._record()
        self._enable_policy()
        records = json.loads(self.state.read_text(encoding="utf-8"))["records"]
        key, record = next(iter(records.items()))
        record["cleanup_status"] = "deleting"
        record["decision_reason"] = "deletion-intent"
        records[key] = record
        write_json_file(self.state, {"record_version": 1, "records": records})

        with patch.object(self.storage, "delete_verified_local") as delete:
            result = self.service.execute()

        self.assertFalse(delete.called)
        self.assertTrue(target.exists())
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(result["items"][0]["decision"], "delete-inflight")
        self.assertEqual(result["items"][0]["decision_reason"], "recovery-required")

    def test_intent_state_write_failure_is_terminal_unknown_and_not_retried(self) -> None:
        target = self._record()
        self._enable_policy()

        with patch.object(self.service, "_save_state_locked", side_effect=OSError("synthetic intent write failure")):
            first = self.service.execute()

        self.assertTrue(target.exists())
        self.assertEqual(first["items"][0]["decision"], "delete_unknown")
        self.assertEqual(first["items"][0]["decision_reason"], "deletion-intent-state-write-failed")
        records = json.loads(self.state.read_text(encoding="utf-8"))["records"]
        self.assertEqual(next(iter(records.values()))["cleanup_status"], "delete_unknown")

        second = self.service.execute()

        self.assertTrue(target.exists())
        self.assertEqual(second["unknown"], 1)
        self.assertEqual(second["items"][0]["decision"], "delete-unknown-terminal")

    def test_delete_failed_is_terminal_on_repeat_execute_and_receipt(self) -> None:
        target = self._record()
        self._enable_policy()
        failed = VerifiedDeleteResult("delete_failed", "synthetic-delete-failure", len(b"synthetic-image"))
        with patch.object(self.storage, "delete_verified_local", return_value=failed) as delete:
            first = self.service.execute()
            second = self.service.execute()

        self.assertEqual(first["items"][0]["decision"], "delete_failed")
        self.assertEqual(second["items"][0]["decision"], "delete-failed-terminal")
        self.assertEqual(delete.call_count, 1)
        self.assertTrue(target.exists())

        digest = hashlib.sha256(b"synthetic-image").hexdigest()
        receipt = self.service.record_receipt(
            destination_scope=self._scope(),
            source_id="chatgpt2api-dev",
            remote_path="2026/08/01/image.png",
            source_sha256=digest,
            receipt_status="imported",
            safe_to_delete_source=True,
            size_bytes=len(b"synthetic-image"),
        )
        self.assertEqual(receipt["cleanup_status"], "delete_failed")

    def test_concurrent_execute_has_one_terminal_delete(self) -> None:
        target = self._record()
        self._enable_policy()
        results: list[dict[str, object]] = []
        barrier = threading.Barrier(2)

        def run() -> None:
            barrier.wait()
            results.append(self.service.execute())

        workers = [threading.Thread(target=run) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)

        self.assertEqual(len(results), 2)
        self.assertFalse(target.exists())
        self.assertEqual(sum(int(result["deleted"]) for result in results), 1)
        self.assertEqual(sum(int(result["reclaimed_bytes"]) for result in results), len(b"synthetic-image"))

    def test_cleanup_claim_is_shared_across_processes(self) -> None:
        target = self._record()
        self._enable_policy()
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        args = (
            str(self.images),
            str(self.settings),
            str(self.state),
            str(self.audit),
            str(self.tmp / "index.json"),
            dict(self.gate.environ),
            result_queue,
        )
        workers = [context.Process(target=_cleanup_process_worker, args=args) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
            self.assertEqual(worker.exitcode, 0)
        results = [result_queue.get(timeout=5), result_queue.get(timeout=5)]

        self.assertFalse(target.exists())
        self.assertEqual(sum(int(result["deleted"]) for result in results), 1)

    def test_application_lifespan_recovery_after_crash_before_unlink(self) -> None:
        target = self._record()
        self._enable_policy()
        context = multiprocessing.get_context("spawn")
        worker = context.Process(
            target=_cleanup_crash_worker,
            args=(
                str(self.images),
                str(self.settings),
                str(self.state),
                str(self.audit),
                str(self.tmp / "index.json"),
                dict(self.gate.environ),
            ),
        )
        worker.start()
        worker.join(timeout=15)

        self.assertEqual(worker.exitcode, 17)
        self.assertTrue(target.exists())

        # Exercise the actual FastAPI lifespan hook rather than calling the
        # recovery method directly. Unrelated schedulers are replaced by
        # no-op collaborators so the test cannot touch live data or workers.
        with ExitStack() as stack:
            for target_name in (
                "genbox_push_outbox.resume",
                "genbox_push_batch_service.resume",
                "genbox_push_schedule_service.resume",
                "account_service.cleanup_auto_remove_accounts",
                "backup_service.start",
                "backup_service.stop",
                "config.cleanup_old_images",
                "cleanup_old_logs",
                "dashboard_metrics_service.flush",
                "genbox_push_schedule_service.stop",
            ):
                stack.enter_context(patch(f"api.app.{target_name}"))
            for target_name in (
                "start_limited_account_watcher",
                "start_image_cleanup_scheduler",
                "start_log_cleanup_scheduler",
            ):
                stack.enter_context(patch(f"api.app.{target_name}", return_value=_NoopThread()))
            stack.enter_context(patch("api.app.genbox_push_cleanup_service", self.service))
            from api.app import create_app

            with TestClient(create_app()):
                pass

        raw = json.loads(self.state.read_text(encoding="utf-8"))
        records = raw.get("records") or {}
        recovered = next(iter(records.values()))
        self.assertEqual(recovered["cleanup_status"], "retained")
        self.assertEqual(recovered["decision_reason"], "interrupted-before-delete")
        self.assertTrue(target.exists())

    def test_application_lifespan_recovery_audit_failure_persists_terminal_unknown(self) -> None:
        target = self._record()
        records = json.loads(self.state.read_text(encoding="utf-8"))["records"]
        key, record = next(iter(records.items()))
        record.update({"cleanup_status": "deleting", "decision_reason": "deletion-intent"})
        records[key] = record
        write_json_file(self.state, {"record_version": 1, "records": records})

        with ExitStack() as stack:
            for target_name in (
                "genbox_push_outbox.resume",
                "genbox_push_batch_service.resume",
                "genbox_push_schedule_service.resume",
                "account_service.cleanup_auto_remove_accounts",
                "backup_service.start",
                "backup_service.stop",
                "config.cleanup_old_images",
                "cleanup_old_logs",
                "dashboard_metrics_service.flush",
                "genbox_push_schedule_service.stop",
            ):
                stack.enter_context(patch(f"api.app.{target_name}"))
            for target_name in (
                "start_limited_account_watcher",
                "start_image_cleanup_scheduler",
                "start_log_cleanup_scheduler",
            ):
                stack.enter_context(patch(f"api.app.{target_name}", return_value=_NoopThread()))
            stack.enter_context(patch("api.app.genbox_push_cleanup_service", self.service))
            stack.enter_context(patch.object(self.service, "_append_audit_locked", side_effect=OSError("synthetic recovery audit failure")))
            from api.app import create_app

            with TestClient(create_app()):
                pass

        self.assertTrue(target.exists())
        recovered = json.loads(self.state.read_text(encoding="utf-8"))["records"][key]
        self.assertEqual(recovered["cleanup_status"], "delete_unknown")
        self.assertEqual(recovered["decision_reason"], "recovery-audit-write-failed")

    def test_restart_after_unlink_before_terminal_audit_is_unknown(self) -> None:
        relative_path = "2026/08/01/crash-after-unlink.png"
        target = self._record(relative_path)
        self._enable_policy()
        context = multiprocessing.get_context("spawn")
        worker = context.Process(
            target=_cleanup_crash_after_unlink_worker,
            args=(
                str(self.images),
                str(self.settings),
                str(self.state),
                str(self.audit),
                str(self.tmp / "index.json"),
                dict(self.gate.environ),
                relative_path,
            ),
        )
        worker.start()
        worker.join(timeout=15)

        self.assertEqual(worker.exitcode, 19)
        self.assertFalse(target.exists())
        substitute = self.images / "2026/08/01/crash-after-unlink-substitute.png"
        substitute.write_bytes(b"substitute")

        recovered = self.service.recover_inflight()

        self.assertEqual(recovered, {"retained": 0, "unknown": 1})
        self.assertTrue(substitute.exists())
        records = json.loads(self.state.read_text(encoding="utf-8"))["records"]
        record = next(record for record in records.values() if record["remote_path"] == relative_path)
        self.assertEqual(record["cleanup_status"], "delete_unknown")

    def test_restart_after_atomic_exchange_marks_state_unknown_and_lists_artifacts(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX atomic exchange crash requires Linux")
        target = self._record("2026/08/01/crash-after-exchange.png")
        self._enable_policy()
        context = multiprocessing.get_context("spawn")
        worker = context.Process(
            target=_cleanup_atomic_crash_worker,
            args=(
                str(self.images),
                str(self.settings),
                str(self.state),
                str(self.audit),
                str(self.tmp / "index.json"),
                dict(self.gate.environ),
                "after-exchange",
                21,
            ),
        )
        worker.start()
        worker.join(timeout=15)

        self.assertEqual(worker.exitcode, 21)
        self.assertTrue(target.exists())
        records = json.loads(self.state.read_text(encoding="utf-8"))["records"]
        intent = next(iter(records.values()))
        token = intent.get("cleanup_token")
        self.assertIsInstance(token, str)
        self.assertEqual(len(token), 64)

        recovered = self.service.recover_inflight()

        self.assertEqual(recovered, {"retained": 0, "unknown": 1})
        record = next(iter(json.loads(self.state.read_text(encoding="utf-8"))["records"].values()))
        self.assertEqual(record["cleanup_status"], "delete_unknown")
        self.assertIn(f".genbox-cleanup-{token}.", record.get("recovery_detail") or "")
        self.assertTrue(target.exists())

    def test_restart_after_atomic_tombstone_preserves_locatable_artifacts(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX atomic tombstone crash requires Linux")
        relative_path = "2026/08/01/crash-after-tombstone.png"
        target = self._record(relative_path)
        self._enable_policy()
        context = multiprocessing.get_context("spawn")
        worker = context.Process(
            target=_cleanup_atomic_crash_worker,
            args=(
                str(self.images),
                str(self.settings),
                str(self.state),
                str(self.audit),
                str(self.tmp / "index.json"),
                dict(self.gate.environ),
                "after-tombstone",
                22,
            ),
        )
        worker.start()
        worker.join(timeout=15)

        self.assertEqual(worker.exitcode, 22)
        self.assertFalse(target.exists())
        intent = next(iter(json.loads(self.state.read_text(encoding="utf-8"))["records"].values()))
        token = str(intent.get("cleanup_token") or "")
        evidence = self.storage.inspect_verified_delete_artifacts(
            relative_path,
            hashlib.sha256(b"synthetic-image").hexdigest(),
            token,
            expected_size=len(b"synthetic-image"),
        )
        self.assertGreaterEqual(len(evidence["matching_artifacts"]), 1)
        for name in evidence["matching_artifacts"]:
            artifact = (
                self.protected_staging / name.split("/", 1)[1]
                if name.startswith("protected-staging/")
                else target.parent / name
            )
            self.assertEqual(artifact.read_bytes(), b"synthetic-image")

        recovered = self.service.recover_inflight()

        self.assertEqual(recovered, {"retained": 0, "unknown": 1})
        record = next(iter(json.loads(self.state.read_text(encoding="utf-8"))["records"].values()))
        self.assertEqual(record["cleanup_status"], "delete_unknown")
        self.assertEqual(record["decision_reason"], "interrupted-artifact-retained")
        self.assertIn(f".genbox-cleanup-{token}.", record.get("recovery_detail") or "")

    def test_restart_after_terminal_audit_before_state_commit_is_unknown(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX exact-delete terminal audit crash requires Linux")
        target = self._record("2026/08/01/crash-after-terminal-audit.png")
        self._enable_policy()
        context = multiprocessing.get_context("spawn")
        worker = context.Process(
            target=_cleanup_crash_after_terminal_audit_worker,
            args=(
                str(self.images),
                str(self.settings),
                str(self.state),
                str(self.audit),
                str(self.tmp / "index.json"),
                dict(self.gate.environ),
            ),
        )
        worker.start()
        worker.join(timeout=15)

        self.assertEqual(worker.exitcode, 23)
        self.assertFalse(target.exists())
        intent = next(iter(json.loads(self.state.read_text(encoding="utf-8"))["records"].values()))
        self.assertEqual(intent["cleanup_status"], "deleting")
        decisions = [event.get("decision") for event in json.loads(self.audit.read_text(encoding="utf-8"))["events"]]
        self.assertIn("deleted", decisions)

        recovered = self.service.recover_inflight()

        self.assertEqual(recovered, {"retained": 0, "unknown": 1})
        record = next(iter(json.loads(self.state.read_text(encoding="utf-8"))["records"].values()))
        self.assertEqual(record["cleanup_status"], "delete_unknown")
        decisions = [event.get("decision") for event in json.loads(self.audit.read_text(encoding="utf-8"))["events"]]
        self.assertEqual(decisions[-1], "delete_unknown")

    def test_mixed_execute_results_reconcile_totals(self) -> None:
        valid = self._record("2026/08/01/valid.png", b"valid")
        changed = self._record("2026/08/01/changed.png", b"original")
        changed.write_bytes(b"changed")
        false_path = self.images / "2026/08/01/permission.png"
        false_path.parent.mkdir(parents=True, exist_ok=True)
        false_path.write_bytes(b"permission")
        self.service.record_receipt(
            destination_scope=self._scope(),
            source_id="chatgpt2api-dev",
            remote_path="2026/08/01/permission.png",
            source_sha256=hashlib.sha256(b"permission").hexdigest(),
            receipt_status="imported",
            safe_to_delete_source=False,
            size_bytes=len(b"permission"),
        )
        self._enable_policy()

        result = self.service.execute()

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["reclaimed_bytes"], len(b"valid"))
        self.assertFalse(valid.exists())
        self.assertTrue(changed.exists())
        self.assertTrue(false_path.exists())
        self.assertEqual(result["candidates"], 3)
        self.assertEqual(result["deleted"] + result["retained"] + result["failed"], 3)

    def test_source_busy_does_not_leave_eligible_bytes_in_summary(self) -> None:
        target = self._record()
        self._enable_policy()
        digest = hashlib.sha256(b"synthetic-image").hexdigest()
        claim = source_claim("2026/08/01/image.png", digest, self.gate.runtime_identity_digest())
        self.assertTrue(claim.acquire(timeout_secs=0))
        try:
            result = self.service.execute()
        finally:
            claim.release()

        self.assertEqual(result["eligible"], 0)
        self.assertEqual(result["potential_bytes"], 0)
        self.assertEqual(result["retained"], 1)
        self.assertEqual(result["items"][0]["decision_reason"], "source-busy")
        self.assertTrue(target.exists())

    def test_deleted_record_remains_terminal_on_repeat_preview(self) -> None:
        target = self._record()
        self._enable_policy()
        first = self.service.execute()
        self.assertEqual(first["deleted"], 1)
        self.assertFalse(target.exists())

        second = self.service.preview()

        self.assertEqual(second["already_deleted"], 1)
        self.assertEqual(second["items"][0]["decision"], "already-deleted")
        records = json.loads(self.state.read_text(encoding="utf-8"))["records"]
        self.assertEqual(next(iter(records.values()))["cleanup_status"], "deleted")

    def test_index_write_failure_is_delete_unknown_not_delete_failed(self) -> None:
        target = self._record()
        self._enable_policy()
        self.storage._save_index({"2026/08/01/image.png": {"rel": "2026/08/01/image.png", "local": True, "webdav": False}})
        with patch.object(self.storage, "_save_index", side_effect=OSError("synthetic index failure")):
            result = self.service.execute()

        self.assertFalse(target.exists())
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["items"][0]["decision"], "delete_unknown")
        records = json.loads(self.state.read_text(encoding="utf-8"))["records"]
        self.assertEqual(next(iter(records.values()))["cleanup_status"], "delete_unknown")

    def test_delete_unknown_is_terminal_for_same_path_and_hash(self) -> None:
        target = self._record()
        self._enable_policy()
        self.storage._save_index({"2026/08/01/image.png": {"rel": "2026/08/01/image.png", "local": True, "webdav": False}})
        with patch.object(self.storage, "_save_index", side_effect=OSError("synthetic index failure")):
            first = self.service.execute()
        self.assertEqual(first["items"][0]["decision"], "delete_unknown")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic-image")

        second = self.service.preview()

        self.assertEqual(second["unknown"], 1)
        self.assertEqual(second["items"][0]["decision"], "delete-unknown-terminal")
        self.assertTrue(second["items"][0]["source_retained"])
        self.assertTrue(target.exists())

    def test_delete_unknown_cannot_be_overwritten_by_same_identity_receipt(self) -> None:
        target = self._record()
        self._enable_policy()
        self.storage._save_index({"2026/08/01/image.png": {"rel": "2026/08/01/image.png", "local": True, "webdav": False}})
        with patch.object(self.storage, "_save_index", side_effect=OSError("synthetic index failure")):
            self.service.execute()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"synthetic-image")
        digest = hashlib.sha256(b"synthetic-image").hexdigest()
        existing = self.service.record_receipt(
            destination_scope=self._scope(),
            source_id="chatgpt2api-dev",
            remote_path="2026/08/01/image.png",
            source_sha256=digest,
            receipt_status="imported",
            safe_to_delete_source=True,
            size_bytes=len(b"synthetic-image"),
        )

        self.assertEqual(existing["cleanup_status"], "delete_unknown")
        self.assertTrue(target.exists())

    def test_persisted_malformed_receipt_is_retained(self) -> None:
        target = self._record()
        self._enable_policy()
        records = json.loads(self.state.read_text(encoding="utf-8"))["records"]
        record = next(iter(records.values()))
        record["receipt"]["status"] = ["imported"]
        write_json_file(self.state, {"record_version": 1, "records": records})

        result = self.service.execute()

        self.assertTrue(target.exists())
        self.assertEqual(result["items"][0]["decision_reason"], "receipt-invalid")

    def test_marker_symlink_blocks_execute(self) -> None:
        target = self._record()
        self._enable_policy()
        replacement = self.tmp / "replacement-marker"
        replacement.write_text(self.instance_marker.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            self.instance_marker.unlink()
            self.instance_marker.symlink_to(replacement)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable on this filesystem")

        result = self.service.execute()

        self.assertTrue(target.exists())
        self.assertEqual(result.get("blocked_reason"), "runtime-identity-unverified")


if __name__ == "__main__":
    unittest.main()
