from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "local-test-admin-key")

from services.config import config
from services.genbox_push_cleanup import CleanupEnvironmentGate, GenBoxPushCleanupService
from services.image_storage_service import ImageStorageService
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


class GenBoxPushCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="genbox-cleanup-"))
        self.images = self.tmp / "images"
        self.settings = self.tmp / "settings.json"
        self.state = self.tmp / "cleanup.json"
        self.audit = self.tmp / "audit.json"
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
            "CHATGPT2API_CLEANUP_INSTANCE_ID": "synthetic-isolated-sender",
            "CHATGPT2API_CLEANUP_STORAGE_ROOT": str(self.images.resolve()),
            "CHATGPT2API_CLEANUP_CAPABILITY": "synthetic-capability-32-bytes-000000000000",
            "CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_KIND": "private-verified",
            "CHATGPT2API_CLEANUP_TRUSTED_DESTINATION_SCOPE": hashlib.sha256(
                b"https://genbox.test\nchatgpt2api-dev\nsynthetic-push-key"
            ).hexdigest(),
        })
        self.service = GenBoxPushCleanupService(
            state_file=self.state,
            audit_file=self.audit,
            settings_file=self.settings,
            image_storage=self.storage,
            environment_gate=self.gate,
        )
        self.config_patch = patch("services.image_storage_service.config", _ImageConfig(self.images))
        self.config_patch.start()

    def tearDown(self) -> None:
        self.config_patch.stop()
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

    def test_changed_source_is_retained(self) -> None:
        target = self._record()
        self._enable_policy()
        target.write_bytes(b"changed-source")

        result = self.service.execute()

        self.assertEqual(result["deleted"], 0)
        self.assertTrue(target.exists())
        self.assertEqual(result["items"][0]["decision_reason"], "source-changed")

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

    def test_source_busy_does_not_leave_eligible_bytes_in_summary(self) -> None:
        target = self._record()
        self._enable_policy()
        digest = hashlib.sha256(b"synthetic-image").hexdigest()
        claim = source_claim("2026/08/01/image.png", digest)
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


if __name__ == "__main__":
    unittest.main()
