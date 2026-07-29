from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "local-test-admin-key")

from services.genbox_push_outbox import GenBoxPushOutbox
from services.protocol import conversation


class FakePushService:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, str]] = []

    def push_image(self, path: str, *, created_at: str = "", prompt: str = "", model: str = "", expected_sha256: str | None = None) -> dict[str, object]:
        self.calls.append({"path": path, "created_at": created_at, "prompt": prompt, "model": model})
        if self.error:
            raise self.error
        return {
            "status": "imported",
            "sha256": expected_sha256 or "a" * 64,
            "safe_to_delete_source": True,
            "source_retained": True,
        }


class GenBoxPushOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def test_enqueue_and_delivery_persist_safe_transfer_state(self) -> None:
        sender = FakePushService()
        outbox = GenBoxPushOutbox(state_file=self.tmp / "outbox.json", push_service=sender)
        digest = hashlib.sha256(b"synthetic").hexdigest()

        queued = outbox.enqueue(
            "2026/07/28/synthetic.png",
            digest,
            created_at="2026-07-28 12:00:00",
            prompt="private prompt must not persist",
            model="local-model",
            start_worker=False,
        )
        outbox._drain()

        state = outbox.status_for_path("2026/07/28/synthetic.png")
        persisted = (self.tmp / "outbox.json").read_text(encoding="utf-8")
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(state["status"], "succeeded")
        self.assertTrue(state["source_retained"])
        self.assertEqual(sender.calls[0]["prompt"], "private prompt must not persist")
        self.assertNotIn("private prompt must not persist", persisted)
        self.assertNotIn("push_key", persisted)
        self.assertFalse(outbox._metadata)

    def test_failed_delivery_does_not_remove_or_mark_source_deletable(self) -> None:
        outbox = GenBoxPushOutbox(
            state_file=self.tmp / "outbox.json",
            push_service=FakePushService(RuntimeError("destination unavailable")),
        )
        digest = hashlib.sha256(b"synthetic").hexdigest()

        outbox.enqueue("2026/07/28/synthetic.png", digest, start_worker=False)
        outbox._drain()

        state = outbox.status_for_path("2026/07/28/synthetic.png")
        persisted = (self.tmp / "outbox.json").read_text(encoding="utf-8")
        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["source_retained"])
        self.assertEqual(state["attempts"], 1)
        self.assertNotIn("destination unavailable", persisted)
        self.assertFalse(outbox._metadata)

    def test_restart_recovers_sending_item_and_retry_only_requeues_failed_item(self) -> None:
        digest = hashlib.sha256(b"synthetic").hexdigest()
        state_file = self.tmp / "outbox.json"
        state_file.write_text(
            '{"items":{"2026/07/28/synthetic.png:' + digest + '":'
            '{"path":"2026/07/28/synthetic.png","source_sha256":"' + digest + '",'
            '"status":"sending","attempts":1,"updated_at":"2026-07-28 12:00:00"}}}',
            encoding="utf-8",
        )

        class IdleWorker:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def is_alive(self) -> bool:
                return False

            def start(self) -> None:
                pass

        outbox = GenBoxPushOutbox(
            state_file=state_file,
            push_service=FakePushService(),
            worker_factory=IdleWorker,
        )

        outbox.resume(start_worker=False)
        recovered = outbox.status_for_path("2026/07/28/synthetic.png")
        self.assertEqual(recovered["status"], "queued")
        self.assertIsNone(outbox.retry_path("2026/07/28/synthetic.png"))

        outbox._drain()
        self.assertEqual(outbox.status_for_path("2026/07/28/synthetic.png")["status"], "succeeded")

        failed_path = "2026/07/28/failed.png"
        with outbox._lock:
            items = outbox._load_locked()
            items[outbox._entry_id(failed_path, digest)] = {
                "path": failed_path,
                "source_sha256": digest,
                "status": "failed",
                "attempts": 1,
                "updated_at": "2026-07-28 12:00:01",
            }
            outbox._save_locked(items)

        retried = outbox.retry_path(failed_path)
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(outbox.status_for_path(failed_path)["status"], "queued")


class ConversationPushRegistrationTests(unittest.TestCase):
    def test_generation_result_exposes_safe_local_path_and_queues_only_when_selected(self) -> None:
        payload = b"synthetic-image"
        original_storage = conversation.image_storage_service
        original_outbox = conversation.genbox_push_outbox

        class Stored:
            rel = "2026/07/28/synthetic.png"
            url = "http://local.test/images/2026/07/28/synthetic.png"

        class Storage:
            def save(self, _payload: bytes, _base_url: str | None):
                return Stored()

        class Outbox:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def enqueue(self, path: str, digest: str, **metadata: object) -> dict[str, object]:
                self.calls.append({"path": path, "digest": digest, **metadata})
                return {"status": "queued", "source_retained": True}

        outbox = Outbox()
        conversation.image_storage_service = Storage()
        conversation.genbox_push_outbox = outbox
        try:
            result = conversation.format_image_result(
                [{"b64_json": base64.b64encode(payload).decode("ascii")}],
                "private prompt",
                "url",
                model="local-model",
                push_to_genbox=True,
            )
        finally:
            conversation.image_storage_service = original_storage
            conversation.genbox_push_outbox = original_outbox

        asset = result["data"][0]
        self.assertEqual(asset["path"], "2026/07/28/synthetic.png")
        self.assertEqual(asset["genbox_push"]["status"], "queued")
        self.assertEqual(outbox.calls[0]["digest"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(outbox.calls[0]["model"], "local-model")
