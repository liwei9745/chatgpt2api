from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from datetime import datetime

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "local-test-admin-key")

from services.genbox_push_batch import GenBoxPushBatchService
from services.genbox_push_outbox import GenBoxPushOutbox
from services.genbox_push_schedule import GenBoxPushScheduleService
from services.genbox_push_transfer import GenBoxPushTransferCoordinator
from utils.timezone import BEIJING_TZ


class BlockingPushService:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def push_image(self, path: str, **kwargs: str) -> dict[str, object]:
        self.calls.append({"path": path, **kwargs})
        self.started.set()
        self.release.wait(timeout=2)
        return {
            "status": "imported",
            "sha256": kwargs["expected_sha256"],
            "safe_to_delete_source": False,
            "source_retained": True,
        }


class FailingPushService:
    def __init__(self) -> None:
        self.calls = 0

    def push_image(self, _path: str, **kwargs: str) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("untrusted transport detail")
        return {
            "status": "imported",
            "sha256": kwargs["expected_sha256"],
            "safe_to_delete_source": False,
            "source_retained": True,
        }


class GenBoxPushTransferCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        for path in self.tmp.iterdir():
            path.unlink()
        self.tmp.rmdir()

    def _wait_for_follower(self, coordinator: GenBoxPushTransferCoordinator) -> None:
        for _ in range(100):
            if any(transfer.waiters for transfer in coordinator._inflight.values()):
                return
            threading.Event().wait(0.01)
        self.fail("the duplicate request did not join the in-flight transfer")

    def test_outbox_and_batch_share_one_inflight_physical_send(self) -> None:
        path = "2026/07/28/shared.png"
        image = b"synthetic-shared-image"
        digest = hashlib.sha256(image).hexdigest()
        sender = BlockingPushService()
        coordinator = GenBoxPushTransferCoordinator()
        outbox = GenBoxPushOutbox(
            state_file=self.tmp / "outbox.json",
            push_service=sender,
            transfer_coordinator=coordinator,
        )
        batches = GenBoxPushBatchService(
            state_file=self.tmp / "batches.json",
            push_service=sender,
            transfer_coordinator=coordinator,
            image_reader=lambda current_path: image if current_path == path else b"",
            image_exists=lambda current_path: current_path == path,
        )

        outbox.enqueue(path, digest, prompt="prompt must stay memory-only", start_worker=False)
        batch = batches.create([path], start_worker=False)
        outbox_thread = threading.Thread(target=outbox._drain)
        batch_thread = threading.Thread(target=batches._drain)
        outbox_thread.start()
        self.assertTrue(sender.started.wait(timeout=1))
        batch_thread.start()
        self._wait_for_follower(coordinator)
        self.assertEqual(len(sender.calls), 1)
        sender.release.set()
        outbox_thread.join(timeout=2)
        batch_thread.join(timeout=2)

        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(outbox.status_for_path(path)["status"], "succeeded")
        self.assertEqual(batches.get(str(batch["id"]))["status"], "succeeded")
        self.assertNotIn("prompt must stay memory-only", str(coordinator._inflight))

    def test_rich_metadata_cannot_be_silently_dropped_by_a_blank_owner(self) -> None:
        path = "2026/07/28/shared.png"
        digest = hashlib.sha256(b"synthetic-shared-image").hexdigest()
        sender = BlockingPushService()
        coordinator = GenBoxPushTransferCoordinator()
        owner = threading.Thread(target=lambda: coordinator.push_image(sender, path, digest))
        owner.start()
        self.assertTrue(sender.started.wait(timeout=1))

        with self.assertRaisesRegex(Exception, "different metadata"):
            coordinator.push_image(sender, path, digest, prompt="private prompt")
        self.assertEqual(len(sender.calls), 1)
        sender.release.set()
        owner.join(timeout=2)

        coordinator.push_image(sender, path, digest, prompt="private prompt")
        self.assertEqual(len(sender.calls), 2)

    def test_path_aliases_share_one_inflight_send(self) -> None:
        path = "2026/07/28/shared.png"
        digest = hashlib.sha256(b"synthetic-shared-image").hexdigest()
        sender = BlockingPushService()
        coordinator = GenBoxPushTransferCoordinator()
        owner = threading.Thread(target=lambda: coordinator.push_image(sender, path, digest))
        follower_result: list[dict[str, object]] = []
        follower = threading.Thread(
            target=lambda: follower_result.append(coordinator.push_image(sender, "/2026\\07\\28\\shared.png", digest))
        )
        owner.start()
        self.assertTrue(sender.started.wait(timeout=1))
        follower.start()
        self._wait_for_follower(coordinator)
        sender.release.set()
        owner.join(timeout=2)
        follower.join(timeout=2)

        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(sender.calls[0]["path"], path)
        self.assertEqual(follower_result[0]["sha256"], digest)

    def test_schedule_and_manual_batch_share_one_physical_send(self) -> None:
        path = "2026/07/27/scheduled.png"
        image = b"synthetic-scheduled-image"
        sender = BlockingPushService()
        coordinator = GenBoxPushTransferCoordinator()
        batches = GenBoxPushBatchService(
            state_file=self.tmp / "batches.json",
            push_service=sender,
            transfer_coordinator=coordinator,
            image_reader=lambda current_path: image if current_path == path else b"",
            image_exists=lambda current_path: current_path == path,
        )
        schedule = GenBoxPushScheduleService(
            state_file=self.tmp / "schedule.json",
            batch_service=batches,
            push_service=object(),
            image_reader=lambda current_path: image if current_path == path else b"",
            image_exists=lambda current_path: current_path == path,
            image_lister=lambda _base_url, **_filters: [{"path": path}],
            now=lambda: datetime(2026, 7, 27, 10, 0, tzinfo=BEIJING_TZ),
        )

        manual = batches.create([path], start_worker=False)
        manual_thread = threading.Thread(target=batches._drain)
        manual_thread.start()
        self.assertTrue(sender.started.wait(timeout=1))

        schedule.run_now()
        self._wait_for_follower(coordinator)
        self.assertEqual(len(sender.calls), 1)
        sender.release.set()
        manual_thread.join(timeout=2)
        if batches._worker is not None:
            batches._worker.join(timeout=2)

        synced = schedule.run_now()
        self.assertEqual(len(sender.calls), 1)
        self.assertEqual(batches.get(str(manual["id"]))["status"], "succeeded")
        self.assertEqual(synced["succeeded"], 1)

    def test_failed_transfer_releases_claim_for_a_clean_retry(self) -> None:
        path = "2026/07/28/shared.png"
        digest = hashlib.sha256(b"synthetic-shared-image").hexdigest()
        sender = FailingPushService()
        coordinator = GenBoxPushTransferCoordinator()

        with self.assertRaisesRegex(Exception, "source image was retained"):
            coordinator.push_image(sender, path, digest)
        result = coordinator.push_image(sender, path, digest)

        self.assertEqual(sender.calls, 2)
        self.assertEqual(result["sha256"], digest)
        self.assertNotIn("untrusted transport detail", str(coordinator._inflight))


if __name__ == "__main__":
    unittest.main()
