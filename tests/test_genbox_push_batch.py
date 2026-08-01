from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import unittest

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "local-test-admin-key")

from fastapi.testclient import TestClient

from api import genbox_push
from api.app import create_app
from services.genbox_push_batch import GenBoxPushBatchService
from services.genbox_push_service import GenBoxPushError


def _claim_from_separate_process(state_file: str, start: object, results: object) -> None:
    service = GenBoxPushBatchService(state_file=Path(state_file), push_service=object())
    start.wait(timeout=5)
    results.put(service._claim_next() is not None)


class FakePushService:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.calls: list[str] = []

    def push_image(self, path: str, **kwargs: object) -> dict[str, object]:
        self.calls.append(path)
        if path in self.failures:
            raise RuntimeError("untrusted remote detail")
        return {"status": "imported", "sha256": str(kwargs.get("expected_sha256") or ""), "source_retained": True}


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

    def test_public_projection_only_becomes_terminal_after_every_item_is_processed(self) -> None:
        batch = self.batches.create(["2026/07/28/one.png", "2026/07/28/two.png"], start_worker=False)
        claimed = self.batches._claim_next()
        self.assertIsNotNone(claimed)
        assert claimed is not None
        batch_id, item_id, _item, item_lock = claimed
        try:
            in_progress = self.batches.get(batch_id)
            self.assertEqual(in_progress["total"], 2)
            self.assertEqual(in_progress["processed"], 0)
            self.assertFalse(in_progress["is_terminal"])

            self.batches._finish(batch_id, item_id, receipt_status="imported")
            partial = self.batches.get(batch_id)
            self.assertEqual(partial["processed"], 1)
            self.assertFalse(partial["is_terminal"])
        finally:
            item_lock.release()

        self.batches._drain()
        completed = self.batches.get(str(batch["id"]))
        self.assertEqual(completed["processed"], 2)
        self.assertTrue(completed["is_terminal"])

    def test_claim_skips_an_item_owned_by_another_worker(self) -> None:
        batch = self.batches.create(["2026/07/28/one.png", "2026/07/28/two.png"], start_worker=False)
        state = self.batches.get(str(batch["id"]))
        first_item = state["items"][0]
        held_lock = self.batches._item_lock(str(batch["id"]), str(first_item["id"]))
        self.assertTrue(held_lock.acquire(timeout_secs=0))
        try:
            claimed = self.batches._claim_next()
            self.assertIsNotNone(claimed)
            assert claimed is not None
            _batch_id, _item_id, item, item_lock = claimed
            try:
                self.assertEqual(item["path"], "2026/07/28/two.png")
            finally:
                item_lock.release()
        finally:
            held_lock.release()

    def test_batch_preserves_already_imported_receipt_outcome(self) -> None:
        class DuplicatePushService(FakePushService):
            def push_image(self, path: str, **kwargs: object) -> dict[str, object]:
                self.calls.append(path)
                return {
                    "status": "already-imported",
                    "sha256": str(kwargs.get("expected_sha256") or ""),
                    "source_retained": True,
                }

        duplicate_sender = DuplicatePushService()
        batches = GenBoxPushBatchService(
            state_file=self.tmp / "duplicates.json",
            push_service=duplicate_sender,
            image_reader=self.images.__getitem__,
            image_exists=lambda path: path in self.images,
        )
        batch = batches.create(["2026/07/28/one.png"], start_worker=False)
        batches._drain()

        completed = batches.get(str(batch["id"]))
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["succeeded"], 0)
        self.assertEqual(completed["already_imported"], 1)
        self.assertEqual(completed["items"][0]["status"], "already-imported")
        self.assertEqual(completed["items"][0]["receipt_status"], "already-imported")

    def test_batch_treats_duplicate_local_receipt_as_already_imported(self) -> None:
        class DuplicateLocalPushService(FakePushService):
            def push_image(self, path: str, **kwargs: object) -> dict[str, object]:
                self.calls.append(path)
                return {
                    "status": "duplicate-local",
                    "sha256": str(kwargs.get("expected_sha256") or ""),
                    "source_retained": True,
                }

        duplicate_sender = DuplicateLocalPushService()
        batches = GenBoxPushBatchService(
            state_file=self.tmp / "duplicate-local.json",
            push_service=duplicate_sender,
            image_reader=self.images.__getitem__,
            image_exists=lambda path: path in self.images,
        )
        batch = batches.create(["2026/07/28/one.png"], start_worker=False)
        batches._drain()

        completed = batches.get(str(batch["id"]))
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["succeeded"], 0)
        self.assertEqual(completed["already_imported"], 1)
        self.assertEqual(completed["items"][0]["status"], "already-imported")
        self.assertEqual(completed["items"][0]["receipt_status"], "duplicate-local")

    def test_retry_due_accepts_legacy_naive_timestamp(self) -> None:
        item = {"next_retry_at": "2026-07-28T12:00:00"}
        self.assertTrue(GenBoxPushBatchService._retry_due(item))

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

    def test_immediate_restart_recovers_a_free_sending_item_and_continues(self) -> None:
        digest = hashlib.sha256(b"one").hexdigest()
        state_file = self.tmp / "immediate-restart.json"
        state_file.write_text(json.dumps({"batches": {"batch": {
            "created_at": datetime.now().astimezone().isoformat(),
            "updated_at": datetime.now().astimezone().isoformat(),
            "items": {"item": {
                "path": "2026/07/28/one.png", "source_sha256": digest,
                "status": "sending", "attempts": 1,
                "updated_at": datetime.now().astimezone().isoformat(),
            }},
        }}}), encoding="utf-8")
        recovered = GenBoxPushBatchService(
            state_file=state_file,
            push_service=self.sender,
            image_reader=self.images.__getitem__,
            image_exists=lambda path: path in self.images,
            sending_recovery_grace_seconds=0,
        )

        recovered.resume()
        worker = recovered._worker
        self.assertIsNotNone(worker)
        assert worker is not None
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        completed = recovered.get("batch")
        self.assertEqual(completed["succeeded"], 1)
        self.assertEqual(completed["items"][0]["attempts"], 2)
        self.assertEqual(self.sender.calls, ["2026/07/28/one.png"])

    def test_date_range_preview_uses_server_index_and_rejects_invalid_bounds(self) -> None:
        preview = self.batches.preview_date_range("2026-07-01", "2026-07-31")

        self.assertEqual(preview["eligible_count"], 2)
        self.assertEqual(preview["skipped_count"], 1)
        self.assertEqual(preview["paths"], ["2026/07/28/one.png", "2026/07/28/two.png"])
        with self.assertRaisesRegex(ValueError, "end date"):
            self.batches.preview_date_range("2026-07-31", "2026-07-01")

    def test_latest_recoverable_batch_ignores_finished_batches(self) -> None:
        finished = self.batches.create(["2026/07/28/one.png"], start_worker=False)
        self.batches._drain()
        active = self.batches.create(["2026/07/28/two.png"], start_worker=False)

        latest = self.batches.get_latest_recoverable()
        self.assertNotEqual(latest["id"], finished["id"])
        self.assertEqual(latest["id"], active["id"])

    def test_shared_state_allows_only_one_worker_to_claim_an_item(self) -> None:
        batch = self.batches.create(["2026/07/28/one.png"], start_worker=False)
        second_worker = GenBoxPushBatchService(
            state_file=self.tmp / "batches.json",
            push_service=self.sender,
            image_reader=self.images.__getitem__,
            image_exists=lambda path: path in self.images,
        )
        barrier = threading.Barrier(2)
        claimed: list[object] = []

        def claim(service: GenBoxPushBatchService) -> None:
            barrier.wait()
            claimed.append(service._claim_next())

        first = threading.Thread(target=claim, args=(self.batches,))
        second = threading.Thread(target=claim, args=(second_worker,))
        first.start()
        second.start()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(sum(item is not None for item in claimed), 1)
        state = self.batches.get(str(batch["id"]))
        self.assertEqual(state["sending"], 1)

    def test_separate_processes_cannot_claim_the_same_item(self) -> None:
        batch = self.batches.create(["2026/07/28/one.png"], start_worker=False)
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        workers = [
            context.Process(target=_claim_from_separate_process, args=(str(self.tmp / "batches.json"), start, results))
            for _ in range(2)
        ]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(timeout=10)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual([worker.exitcode for worker in workers], [0, 0])
        self.assertEqual(sum(results.get(timeout=2) for _ in workers), 1)
        self.assertEqual(self.batches.get(str(batch["id"]))["sending"], 1)

    def test_retryable_failure_has_a_bounded_automatic_retry(self) -> None:
        class TemporaryFailurePushService(FakePushService):
            def push_image(self, path: str, **kwargs: object) -> dict[str, object]:
                self.calls.append(path)
                if len(self.calls) < 3:
                    raise GenBoxPushError("temporary", retryable=True)
                return {
                    "status": "imported",
                    "sha256": str(kwargs.get("expected_sha256") or ""),
                    "source_retained": True,
                }

        sender = TemporaryFailurePushService()
        batches = GenBoxPushBatchService(
            state_file=self.tmp / "retry.json",
            push_service=sender,
            image_reader=self.images.__getitem__,
            image_exists=lambda path: path in self.images,
            retry_delay_seconds=lambda _attempt: 0,
        )
        batch = batches.create(["2026/07/28/one.png"], start_worker=False)
        batches._drain()

        completed = batches.get(str(batch["id"]))
        self.assertEqual(completed["succeeded"], 1)
        self.assertEqual(completed["failed"], 0)
        self.assertEqual(completed["items"][0]["attempts"], 3)

    def test_retryable_failure_stops_after_the_attempt_limit(self) -> None:
        class AlwaysTemporaryFailurePushService(FakePushService):
            def push_image(self, path: str, **kwargs: object) -> dict[str, object]:
                self.calls.append(path)
                raise GenBoxPushError("temporary", retryable=True)

        sender = AlwaysTemporaryFailurePushService()
        batches = GenBoxPushBatchService(
            state_file=self.tmp / "retry-limit.json",
            push_service=sender,
            image_reader=self.images.__getitem__,
            image_exists=lambda path: path in self.images,
            retry_delay_seconds=lambda _attempt: 0,
        )
        batch = batches.create(["2026/07/28/one.png"], start_worker=False)
        batches._drain()

        failed = batches.get(str(batch["id"]))
        self.assertEqual(failed["failed"], 1)
        self.assertEqual(failed["items"][0]["attempts"], 3)
        self.assertFalse(failed["items"][0]["retryable"])


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

    def get_latest_recoverable(self) -> dict[str, object] | None:
        return self._batch()

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
        latest = self.client.get("/api/genbox-push/batches/latest-recoverable", headers=self.headers)
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(latest.json()["batch"]["id"], "batch")
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
