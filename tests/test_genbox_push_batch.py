from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "local-test-admin-key")

from fastapi.testclient import TestClient

from api import genbox_push
from api.app import create_app
from services.genbox_push_batch import GenBoxPushBatchService


class FakePushService:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.calls: list[str] = []

    def push_image(self, path: str) -> dict[str, object]:
        self.calls.append(path)
        if path in self.failures:
            raise RuntimeError("untrusted remote detail")
        return {"status": "imported", "source_retained": True}


class GenBoxPushBatchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.images = {
            "2026/07/28/one.png": b"one",
            "2026/07/28/two.png": b"two",
        }
        self.sender = FakePushService()
        self.batches = GenBoxPushBatchService(
            state_file=self.tmp / "batches.json",
            push_service=self.sender,
            image_reader=self.images.__getitem__,
            image_exists=lambda path: path in self.images,
            image_lister=lambda _base_url, **_kwargs: [
                {"path": "2026/07/28/one.png"},
                {"path": "2026/07/28/two.png"},
                {"path": "2026/07/28/missing.png"},
            ],
        )

    def test_batch_persists_safe_source_identity_and_pushes_each_image(self) -> None:
        batch = self.batches.create(["2026/07/28/one.png", "2026/07/28/two.png"], start_worker=False)
        persisted = (self.tmp / "batches.json").read_text(encoding="utf-8")

        self.assertEqual(batch["status"], "queued")
        self.assertEqual(batch["total"], 2)
        self.assertNotIn("prompt", persisted)
        self.assertNotIn("push_key", persisted)
        self.assertIn(hashlib.sha256(b"one").hexdigest(), persisted)

        self.batches._drain()
        completed = self.batches.get(str(batch["id"]))
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["succeeded"], 2)
        self.assertTrue(completed["source_retained"])
        self.assertEqual(self.sender.calls, ["2026/07/28/one.png", "2026/07/28/two.png"])

    def test_cancel_only_stops_queued_images(self) -> None:
        batch = self.batches.create(["2026/07/28/one.png", "2026/07/28/two.png"], start_worker=False)
        cancelled = self.batches.cancel(str(batch["id"]))
        self.batches._drain()

        self.assertEqual(cancelled["cancelled"], 2)
        self.assertEqual(self.batches.get(str(batch["id"]))["status"], "cancelled")
        self.assertEqual(self.sender.calls, [])

    def test_retry_requeues_only_failed_items_and_error_is_generic(self) -> None:
        self.sender.failures = {"2026/07/28/two.png"}
        batch = self.batches.create(["2026/07/28/one.png", "2026/07/28/two.png"], start_worker=False)
        self.batches._drain()
        failed = self.batches.get(str(batch["id"]))

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["failed"], 1)
        self.assertNotIn("untrusted remote detail", json.dumps(failed))
        self.assertTrue(all(item["source_retained"] for item in failed["items"]))

        self.sender.failures.clear()
        retried = self.batches.retry_failed(str(batch["id"]), start_worker=False)
        self.assertEqual(retried["queued"], 1)
        self.assertEqual(retried["succeeded"], 1)
        self.batches._drain()
        completed = self.batches.get(str(batch["id"]))
        self.assertEqual(completed["succeeded"], 2)
        self.assertEqual(completed["items"][0]["attempts"], 1)
        self.assertEqual(completed["items"][1]["attempts"], 2)

    def test_restart_recovers_sending_and_refuses_changed_source(self) -> None:
        digest = hashlib.sha256(b"one").hexdigest()
        state_file = self.tmp / "batches.json"
        state_file.write_text(json.dumps({"batches": {"batch": {
            "created_at": "2026-07-28 12:00:00",
            "updated_at": "2026-07-28 12:00:00",
            "items": {"item": {
                "path": "2026/07/28/one.png", "source_sha256": digest,
                "status": "sending", "attempts": 1, "updated_at": "2026-07-28 12:00:00",
            }},
        }}}), encoding="utf-8")
        recovered = GenBoxPushBatchService(
            state_file=state_file,
            push_service=self.sender,
            image_reader=self.images.__getitem__,
            image_exists=lambda path: path in self.images,
        )

        recovered.resume(start_worker=False)
        self.assertEqual(recovered.get("batch")["status"], "queued")
        self.images["2026/07/28/one.png"] = b"changed"
        recovered._drain()
        state = recovered.get("batch")
        self.assertEqual(state["failed"], 1)
        self.assertNotIn("source changed", json.dumps(state))

    def test_date_range_preview_uses_server_index_and_rejects_invalid_bounds(self) -> None:
        preview = self.batches.preview_date_range("2026-07-01", "2026-07-31")

        self.assertEqual(preview["eligible_count"], 2)
        self.assertEqual(preview["skipped_count"], 1)
        self.assertEqual(preview["paths"], ["2026/07/28/one.png", "2026/07/28/two.png"])
        with self.assertRaisesRegex(ValueError, "end date"):
            self.batches.preview_date_range("2026-07-31", "2026-07-01")


class StubBatchService:
    def __init__(self) -> None:
        self.paths: list[str] | None = None

    @staticmethod
    def _batch() -> dict[str, object]:
        return {"id": "batch", "status": "queued", "total": 1, "source_retained": True, "items": []}

    def create(self, paths: list[str]) -> dict[str, object]:
        self.paths = paths
        return self._batch()

    def get(self, batch_id: str) -> dict[str, object] | None:
        return self._batch() if batch_id == "batch" else None

    def cancel(self, batch_id: str) -> dict[str, object] | None:
        return self._batch() if batch_id == "batch" else None

    def retry_failed(self, batch_id: str) -> dict[str, object] | None:
        return self._batch() if batch_id == "batch" else None

    @staticmethod
    def preview_date_range(start_date: str, end_date: str) -> dict[str, object]:
        return {
            "start_date": start_date,
            "end_date": end_date,
            "eligible_count": 1,
            "skipped_count": 0,
            "samples": ["2026/07/28/one.png"],
            "paths": ["2026/07/28/one.png"],
        }


class GenBoxPushBatchApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stub = StubBatchService()
        self.previous = genbox_push.genbox_push_batch_service
        genbox_push.genbox_push_batch_service = self.stub
        self.client = TestClient(create_app())
        self.headers = {"Authorization": "Bearer local-test-admin-key"}

    def tearDown(self) -> None:
        genbox_push.genbox_push_batch_service = self.previous
        self.client.close()

    def test_batch_routes_are_admin_only_and_accept_only_source_paths(self) -> None:
        self.assertEqual(self.client.post("/api/genbox-push/batches", json={"paths": ["a.png"]}).status_code, 401)
        created = self.client.post("/api/genbox-push/batches", headers=self.headers, json={
            "paths": ["2026/07/28/one.png"],
            "prompt": "must not be accepted by the batch service",
            "push_key": "must not be accepted by the batch service",
        })
        self.assertEqual(created.status_code, 200)
        self.assertEqual(self.stub.paths, ["2026/07/28/one.png"])
        self.assertNotIn("prompt", created.text)
        self.assertNotIn("push_key", created.text)
        self.assertEqual(self.client.get("/api/genbox-push/batches/batch", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post("/api/genbox-push/batches/batch/cancel", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post("/api/genbox-push/batches/batch/retry-failed", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/genbox-push/batches/missing", headers=self.headers).status_code, 404)
        self.assertEqual(self.client.post("/api/genbox-push/batches/preview-date-range", json={
            "start_date": "2026-07-01", "end_date": "2026-07-31",
        }).status_code, 401)
        self.assertEqual(self.client.post("/api/genbox-push/batches/preview-date-range", headers=self.headers, json={
            "start_date": "2026-07-01", "end_date": "2026-07-31",
        }).status_code, 200)


if __name__ == "__main__":
    unittest.main()
