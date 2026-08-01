from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
import unittest

from curl_cffi import CurlMime


os.environ.setdefault("CHATGPT2API_AUTH_KEY", "local-test-admin-key")

from services.genbox_push_service import GenBoxPushError, GenBoxPushService


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, object],
        *,
        stream_chunks: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.payload = payload
        self.ok = 200 <= status_code < 300
        self.headers = headers or {}
        self.stream_chunks = stream_chunks

    def json(self) -> dict[str, object]:
        return self.payload

    def iter_content(self, chunk_size: int = 65536):
        del chunk_size
        if self.stream_chunks is not None:
            yield from self.stream_chunks
            return
        yield json.dumps(self.payload).encode("utf-8")


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
        self.assertTrue(call["verify"])

    def test_full_push_endpoint_is_normalized_before_probe(self) -> None:
        settings = self.service.update_settings({
            "enabled": True,
            "base_url": "https://genbox.test/api/sync/push",
            "source_id": "chatgpt2api-dev",
            "push_key": "secret-not-for-responses",
        })
        self.factory.responses.append(FakeResponse(200, {
            "ok": True,
            "contract_version": "v1",
            "source_id": "chatgpt2api-dev",
            "max_image_bytes": 4096,
        }))

        self.service.probe()

        self.assertEqual(settings["base_url"], "https://genbox.test")
        self.assertEqual(
            self.factory.sessions[0].calls[0]["url"],
            "https://genbox.test/api/sync/push/status",
        )

    def test_push_endpoint_with_extra_suffix_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service.update_settings({
                "base_url": "https://genbox.test/api/sync/push/unexpected",
            })

    def test_incompatible_probe_contract_refuses_push_without_state(self) -> None:
        self.configure()
        self.factory.responses.append(FakeResponse(200, {
            "ok": True,
            "contract_version": "v2",
            "source_id": "chatgpt2api-dev",
            "max_image_bytes": 4096,
        }))

        with self.assertRaisesRegex(GenBoxPushError, "不支持"):
            self.service.push_image("2026/07/28/image.png")

        self.assertFalse((self.tmp / "state.json").exists())
        self.assertEqual(len(self.factory.sessions), 1)
        self.assertEqual([call["method"] for call in self.factory.sessions[0].calls], ["GET"])

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
        self.assertNotIn("files", request)
        self.assertIsInstance(request["multipart"], CurlMime)
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

    def test_malformed_receipt_status_type_is_rejected_without_server_error(self) -> None:
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
                "status": ["imported"],
            }),
        ])

        with self.assertRaises(GenBoxPushError):
            self.service.push_image("2026/07/28/image.png")

    def test_streaming_receipt_is_bounded_before_json_parse(self) -> None:
        self.configure()
        oversized = b"{" + (b"x" * (1024 * 1024 + 1))
        self.factory.responses.append(FakeResponse(200, {}, stream_chunks=[oversized]))

        with self.assertRaisesRegex(GenBoxPushError, "oversized"):
            self.service.probe()

        call = self.factory.sessions[0].calls[0]
        self.assertTrue(call["stream"])
        self.assertTrue(call["verify"])

    def test_streaming_malformed_receipt_is_retained(self) -> None:
        self.configure()
        self.factory.responses.append(FakeResponse(200, {}, stream_chunks=[b"not-json"]))

        with self.assertRaisesRegex(GenBoxPushError, "unreadable"):
            self.service.probe()

    def test_expected_source_hash_refuses_changed_content_before_any_request(self) -> None:
        self.configure()

        with self.assertRaisesRegex(GenBoxPushError, "changed"):
            self.service.push_image("2026/07/28/image.png", expected_sha256="0" * 64)

        self.assertEqual(self.factory.sessions, [])
        self.assertFalse((self.tmp / "state.json").exists())

    def test_transfer_scope_changes_when_push_key_changes_without_exposing_it(self) -> None:
        self.configure()
        before = self.service.transfer_scope()
        self.service.update_settings({"push_key": "rotated-local-test-key"})
        after = self.service.transfer_scope()

        self.assertNotEqual(before, after)
        self.assertNotIn("rotated-local-test-key", after)

    def test_captured_transfer_context_refuses_a_rotated_destination_before_request(self) -> None:
        self.configure()
        context = self.service.capture_transfer_context()
        self.service.update_settings({"push_key": "rotated-local-test-key"})

        with self.assertRaisesRegex(GenBoxPushError, "configuration changed"):
            self.service.push_image("2026/07/28/image.png", _transfer_context=context)

        self.assertEqual(self.factory.sessions, [])


if __name__ == "__main__":
    unittest.main()
