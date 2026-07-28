from __future__ import annotations

import os
import unittest


os.environ.setdefault("CHATGPT2API_AUTH_KEY", "local-test-admin-key")

from fastapi.testclient import TestClient

from api import genbox_push
from api.app import create_app


class StubPushService:
    def __init__(self) -> None:
        self.saved: dict[str, object] | None = None

    def get_settings(self) -> dict[str, object]:
        return {
            "enabled": True,
            "base_url": "https://genbox.test",
            "source_id": "chatgpt2api-dev",
            "has_push_key": True,
            "timeout_secs": 20,
        }

    def update_settings(self, payload: dict[str, object]) -> dict[str, object]:
        self.saved = payload
        return self.get_settings()

    def probe(self) -> dict[str, object]:
        return {"ok": True, "contract_version": "v1", "max_image_bytes": 1024}

    def push_image(self, path: str, **kwargs: object) -> dict[str, object]:
        return {
            "status": "imported",
            "sha256": "a" * 64,
            "safe_to_delete_source": True,
            "source_retained": True,
        }


class GenBoxPushApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stub = StubPushService()
        self.previous = genbox_push.genbox_push_service
        genbox_push.genbox_push_service = self.stub
        self.client = TestClient(create_app())
        self.headers = {"Authorization": "Bearer local-test-admin-key"}

    def tearDown(self) -> None:
        genbox_push.genbox_push_service = self.previous
        self.client.close()

    def test_settings_response_never_returns_push_key(self) -> None:
        response = self.client.post("/api/genbox-push/settings", headers=self.headers, json={
            "enabled": True,
            "base_url": "https://genbox.test",
            "source_id": "chatgpt2api-dev",
            "push_key": "must-not-appear-in-response",
            "timeout_secs": 20,
        })

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("must-not-appear-in-response", response.text)
        self.assertNotIn("push_key", response.json()["settings"])
        self.assertEqual(self.stub.saved["push_key"], "must-not-appear-in-response")

    def test_probe_and_image_push_are_admin_only(self) -> None:
        self.assertEqual(self.client.post("/api/genbox-push/probe").status_code, 401)
        self.assertEqual(self.client.post("/api/genbox-push/images", json={"path": "2026/07/28/image.png"}).status_code, 401)

        probe = self.client.post("/api/genbox-push/probe", headers=self.headers)
        pushed = self.client.post("/api/genbox-push/images", headers=self.headers, json={"path": "2026/07/28/image.png"})

        self.assertEqual(probe.status_code, 200)
        self.assertEqual(pushed.status_code, 200)
        self.assertTrue(pushed.json()["result"]["source_retained"])


if __name__ == "__main__":
    unittest.main()
