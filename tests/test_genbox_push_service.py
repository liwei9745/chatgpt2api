from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
import unittest


os.environ.setdefault("CHATGPT2API_AUTH_KEY", "local-test-admin-key")

from services.genbox_push_service import GenBoxPushError, GenBoxPushService


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self.payload = payload
        self.ok = 200 <= status_code < 300

    def json(self) -> dict[str, object]:
        return self.payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self.responses.pop(0)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return self.responses.pop(0)

    def close(self) -> None:
        return None


class FakeSessionFactory:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.sessions: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        session = FakeSession(self.responses)
        self.sessions.append(session)
        return session


class GenBoxPushServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self._get_tmp_dir())
        self.image = b"test-image-bytes"
        self.factory = FakeSessionFactory([])
        self.service = GenBoxPushService(
            settings_file=self.tmp / "settings.json",
            state_file=self.tmp / "state.json",
            image_reader=lambda path: self.image if path == "2026/07/28/image.png" else b"",
            session_factory=self.factory,
        )

    def tearDown(self) -> None:
        for path in self.tmp.iterdir():
            path.unlink()
        self.tmp.rmdir()

    def _get_tmp_dir(self) -> str:
        import tempfile

        return tempfile.mkdtemp(prefix="genbox-push-")

    def configure(self) -> dict[str, object]:
        return self.service.update_settings({
            "enabled": True,
            "base_url": "https://genbox.test",
            "source_id": "chatgpt2api-dev",
            "push_key": "secret-not-for-responses",
            "timeout_secs": 20,
        })

    def test_public_settings_mask_the_push_key(self) -> None:
        settings = self.configure()

        self.assertTrue(settings["has_push_key"])
        self.assertNotIn("push_key", settings)
        self.assertNotIn("secret-not-for-responses", str(settings))

    def test_probe_validates_identity_and_contract(self) -> None:
        self.configure()
        self.factory.responses.append(FakeResponse(200, {
            "ok": True,
            "contract_version": "v1",
            "source_id": "chatgpt2api-dev",
            "max_image_bytes": 4096,
        }))

        result = self.service.probe()

        self.assertEqual(result, {"ok": True, "contract_version": "v1", "max_image_bytes": 4096})
        call = self.factory.sessions[0].calls[0]
        self.assertEqual(call["headers"]["X-GenBox-Source"], "chatgpt2api-dev")
        self.assertEqual(call["headers"]["X-GenBox-Key"], "secret-not-for-responses")

    def test_successful_push_requires_matching_receipt_and_retains_source(self) -> None:
        self.configure()
        digest = hashlib.sha256(self.image).hexdigest()
        self.factory.responses.extend([
            FakeResponse(200, {
                "ok": True,
                "contract_version": "v1",
                "source_id": "chatgpt2api-dev",
                "max_image_bytes": 4096,
            }),
            FakeResponse(200, {
                "ok": True,
                "contract_version": "v1",
                "source_id": "chatgpt2api-dev",
                "sha256": digest,
                "status": "imported",
                "safe_to_delete_source": True,
            }),
        ])

        result = self.service.push_image("2026/07/28/image.png", prompt="local test", model="test-model")

        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["sha256"], digest)
        self.assertTrue(result["safe_to_delete_source"])
        self.assertTrue(result["source_retained"])
        request = self.factory.sessions[1].calls[0]
        self.assertEqual(request["data"]["source_sha256"], digest)
        self.assertEqual(request["data"]["remote_path"], "2026/07/28/image.png")
        self.assertEqual(request["files"]["image"][1], self.image)
        saved = (self.tmp / "state.json").read_text(encoding="utf-8")
        self.assertNotIn("secret-not-for-responses", saved)

    def test_mismatched_receipt_fails_without_source_deletion(self) -> None:
        self.configure()
        self.factory.responses.extend([
            FakeResponse(200, {
                "ok": True,
                "contract_version": "v1",
                "source_id": "chatgpt2api-dev",
                "max_image_bytes": 4096,
            }),
            FakeResponse(200, {
                "ok": True,
                "contract_version": "v1",
                "source_id": "chatgpt2api-dev",
                "sha256": "0" * 64,
                "status": "imported",
            }),
        ])

        with self.assertRaisesRegex(GenBoxPushError, "回执校验失败"):
            self.service.push_image("2026/07/28/image.png")

        self.assertFalse((self.tmp / "state.json").exists())

    def test_redirects_are_not_followed(self) -> None:
        self.configure()
        self.factory.responses.append(FakeResponse(302, {}))

        with self.assertRaisesRegex(GenBoxPushError, "重定向"):
            self.service.probe()

        call = self.factory.sessions[0].calls[0]
        self.assertFalse(call["allow_redirects"])

    def test_safe_to_delete_source_requires_json_boolean_true(self) -> None:
        self.configure()
        digest = hashlib.sha256(self.image).hexdigest()
        self.factory.responses.extend([
            FakeResponse(200, {
                "ok": True,
                "contract_version": "v1",
                "source_id": "chatgpt2api-dev",
                "max_image_bytes": 4096,
            }),
            FakeResponse(200, {
                "ok": True,
                "contract_version": "v1",
                "source_id": "chatgpt2api-dev",
                "sha256": digest,
                "status": "imported",
                "safe_to_delete_source": "true",
            }),
        ])

        result = self.service.push_image("2026/07/28/image.png")

        self.assertIs(result["safe_to_delete_source"], False)


if __name__ == "__main__":
    unittest.main()
