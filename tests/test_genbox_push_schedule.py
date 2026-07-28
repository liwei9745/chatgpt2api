from __future__ import annotations

from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "local-test-admin-key")

from services.genbox_push_schedule import GenBoxPushScheduleService
from utils.timezone import BEIJING_TZ


class FakePushService:
    def get_settings(self) -> dict[str, object]:
        return {"enabled": True}


class FakeBatchService:
    def __init__(self, *, failed: bool = False) -> None:
        self.failed = failed
        self.calls: list[str] = []
        self.batches: dict[str, dict[str, object]] = {}

    def create(self, paths: list[str]) -> dict[str, object]:
        path = paths[0]
        batch_id = f"batch-{len(self.calls) + 1}"
        self.calls.append(path)
        status = "failed" if self.failed else "succeeded"
        self.batches[batch_id] = {"id": batch_id, "status": status}
        return self.batches[batch_id]

    def get(self, batch_id: str) -> dict[str, object] | None:
        return self.batches.get(batch_id)

    def retry_failed(self, batch_id: str) -> dict[str, object] | None:
        batch = self.batches.get(batch_id)
        if batch is None or batch["status"] != "failed":
            return None
        batch["status"] = "succeeded"
        return batch


class GenBoxPushScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.current = datetime(2026, 7, 27, 10, 0, tzinfo=BEIJING_TZ)
        self.images = {"2026/07/27/one.png": b"one"}
        self.batch = FakeBatchService()
        self.service = self._service()

    def _service(self, **kwargs: object) -> GenBoxPushScheduleService:
        def image_lister(_base_url: str, **filters: object) -> list[dict[str, object]]:
            start = str(filters.get("start_date") or "")
            end = str(filters.get("end_date") or "")
            return [
                {"path": path}
                for path in self.images
                if (not start or path[:10].replace("/", "-") >= start)
                and (not end or path[:10].replace("/", "-") <= end)
            ]

        return GenBoxPushScheduleService(
            state_file=self.tmp / "schedule.json",
            batch_service=kwargs.pop("batch_service", self.batch),
            push_service=FakePushService(),
            image_reader=self.images.__getitem__,
            image_exists=lambda path: path in self.images,
            image_lister=image_lister,
            now=lambda: self.current,
            **kwargs,
        )

    def test_overlap_scan_discovers_late_image_without_requeueing_confirmed_item(self) -> None:
        first = self.service.run_now()
        self.assertEqual(first["succeeded"], 1)
        self.assertEqual(self.batch.calls, ["2026/07/27/one.png"])

        self.current += timedelta(days=1)
        self.images["2026/07/27/late.png"] = b"late"
        second = self.service.run_now()

        self.assertEqual(second["succeeded"], 2)
        self.assertEqual(self.batch.calls, ["2026/07/27/one.png", "2026/07/27/late.png"])
        self.assertTrue(second["source_retained"])

    def test_lease_blocks_another_worker_and_expired_lease_recovers(self) -> None:
        with self.service._lock:
            state = self.service._load_locked()
            token = self.service._acquire_lease(state, self.current)
        other = self._service()
        with self.assertRaisesRegex(ValueError, "already running"):
            other.run_now()
        self.service._release_lease(str(token))
        self.assertEqual(other.run_now()["succeeded"], 1)

    def test_failed_batch_retries_only_with_a_bounded_attempt_count(self) -> None:
        self.batch.failed = True
        first = self.service.run_now()
        self.assertEqual(first["failed"], 1)
        self.assertEqual(self.batch.calls, ["2026/07/27/one.png"])

        self.current += timedelta(minutes=3)
        self.batch.failed = False
        state = self.service.run_now()
        self.assertEqual(state["succeeded"], 1)
        raw = (self.tmp / "schedule.json").read_text(encoding="utf-8")
        self.assertNotIn("push_key", raw)
        self.assertNotIn("prompt", raw)

    def test_disabled_schedule_keeps_history_and_does_not_run_due_twice(self) -> None:
        configured = self.service.update_settings({"enabled": True, "weekday": 0, "time": "09:00"})
        self.assertTrue(configured["enabled"])
        self.assertIsNotNone(self.service.run_due_once())
        self.assertIsNone(self.service.run_due_once())
        disabled = self.service.update_settings({"enabled": False})
        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["succeeded"], 1)
        self.assertTrue(disabled["source_retained"])


if __name__ == "__main__":
    unittest.main()
