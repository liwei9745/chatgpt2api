from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any
import threading
import time
import unittest
from unittest.mock import patch

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from curl_cffi import CurlMime, requests


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
        self.closed = False

    def json(self) -> dict[str, object]:
        return self.payload

    def iter_content(self, chunk_size: int = 65536):
        del chunk_size
        if self.stream_chunks is not None:
            yield from self.stream_chunks
            return
        yield json.dumps(self.payload).encode("utf-8")

    def close(self) -> None:
        self.closed = True


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
        self.claim_path_patch = patch("services.source_claim.DATA_DIR", self.tmp / "claims")
        self.claim_path_patch.start()

    def tearDown(self) -> None:
        self.claim_path_patch.stop()
        for path in sorted(self.tmp.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
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

    def test_cleanup_enabled_rejects_http_destination_before_request(self) -> None:
        self.service.update_settings({
            "enabled": True,
            "cleanup_enabled": True,
            "base_url": "http://genbox.test",
            "source_id": "chatgpt2api-dev",
            "push_key": "secret-not-for-responses",
        })

        with self.assertRaisesRegex(GenBoxPushError, "HTTPS"):
            self.service.probe()

        self.assertEqual(self.factory.sessions, [])

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

    def test_forged_receipt_source_identity_is_not_recorded(self) -> None:
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
                "source_id": "forged-source",
                "sha256": digest,
                "status": "imported",
                "safe_to_delete_source": True,
            }),
        ])

        with self.assertRaisesRegex(GenBoxPushError, "回执校验失败"):
            self.service.push_image("2026/07/28/image.png")

        self.assertFalse((self.tmp / "state.json").exists())
        self.assertFalse((self.tmp / "genbox_push_cleanup.json").exists())

    def test_redirects_are_not_followed(self) -> None:
        self.configure()
        self.factory.responses.append(FakeResponse(302, {}))

        with self.assertRaisesRegex(GenBoxPushError, "重定向"):
            self.service.probe()

        call = self.factory.sessions[0].calls[0]
        self.assertFalse(call["allow_redirects"])

    def test_push_redirect_is_not_followed_or_recorded(self) -> None:
        self.configure()
        self.factory.responses.extend([
            FakeResponse(200, {
                "ok": True,
                "contract_version": "v1",
                "source_id": "chatgpt2api-dev",
                "max_image_bytes": 4096,
            }),
            FakeResponse(307, {}),
        ])

        with self.assertRaisesRegex(GenBoxPushError, "重定向"):
            self.service.push_image("2026/07/28/image.png")

        calls = [call for session in self.factory.sessions for call in session.calls]
        self.assertEqual([call["method"] for call in calls], ["GET", "POST"])
        self.assertFalse(calls[1]["allow_redirects"])
        self.assertFalse((self.tmp / "state.json").exists())
        self.assertFalse((self.tmp / "genbox_push_cleanup.json").exists())

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

    def test_slow_drip_receipt_hits_total_deadline(self) -> None:
        self.configure()
        self.factory.responses.append(FakeResponse(200, {}, stream_chunks=[b"{}", b"{}", b"{}"]))
        ticks = iter([0.0, 31.0])
        with patch("services.genbox_push_service.time.monotonic", side_effect=lambda: next(ticks)):
            with self.assertRaisesRegex(GenBoxPushError, "timed out"):
                self.service.probe()

    def test_receipt_that_stalls_before_first_chunk_hits_total_deadline(self) -> None:
        self.configure()
        release = threading.Event()

        class BlockingResponse(FakeResponse):
            def iter_content(self, chunk_size: int = 65536):
                del chunk_size
                release.wait(timeout=5)
                yield b"{}"

        self.factory.responses.append(BlockingResponse(200, {}))
        try:
            with patch("services.genbox_push_service.MAX_RECEIPT_SECONDS", 0.05):
                with self.assertRaisesRegex(GenBoxPushError, "timed out"):
                    self.service.probe()
        finally:
            release.set()

    def test_non_json_receipt_content_type_is_rejected(self) -> None:
        self.configure()
        self.factory.responses.append(FakeResponse(
            200,
            {},
            headers={"Content-Type": "text/html"},
        ))

        with self.assertRaisesRegex(GenBoxPushError, "non-JSON"):
            self.service.probe()

    def test_real_slow_drip_push_retains_source_and_state(self) -> None:
        source_rel = "2026/07/28/image.png"
        source = self.tmp / source_rel
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(self.image)
        self.service.image_reader = lambda path: source.read_bytes() if path == source_rel else b""

        digest = hashlib.sha256(self.image).hexdigest()
        received = {"post": 0}

        class SlowReceiptHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                return None

            def _send_json(self, payload: dict[str, object]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def do_GET(self) -> None:
                if self.path.endswith("/api/sync/push/status"):
                    self._send_json({
                        "ok": True,
                        "contract_version": "v1",
                        "source_id": "chatgpt2api-dev",
                        "max_image_bytes": 4096,
                    })
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                received["post"] += 1
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length:
                        self.rfile.read(length)
                    body = json.dumps({
                        "ok": True,
                        "contract_version": "v1",
                        "source_id": "chatgpt2api-dev",
                        "sha256": digest,
                        "status": "imported",
                        "safe_to_delete_source": False,
                    }).encode("utf-8")
                    first, rest = body[:12], body[12:]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()

                    def write_chunk(chunk: bytes) -> None:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk + b"\r\n")
                        self.wfile.flush()

                    write_chunk(first)
                    time.sleep(0.25)
                    write_chunk(rest)
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowReceiptHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.service.update_settings({
                "enabled": True,
                "base_url": f"http://127.0.0.1:{server.server_port}",
                "source_id": "chatgpt2api-dev",
                "push_key": "secret-not-for-responses",
                "timeout_secs": 5,
            })
            self.service.session_factory = requests.Session
            with patch("services.genbox_push_service.MAX_RECEIPT_SECONDS", 0.1):
                with self.assertRaisesRegex(GenBoxPushError, "timed out"):
                    self.service.push_image(source_rel)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(received["post"], 1)
        self.assertTrue(source.exists())
        self.assertFalse((self.tmp / "state.json").exists())
        self.assertFalse((self.tmp / "genbox_push_cleanup.json").exists())

    def test_streaming_malformed_receipt_is_retained(self) -> None:
        self.configure()
        self.factory.responses.append(FakeResponse(200, {}, stream_chunks=[b"not-json"]))

        with self.assertRaisesRegex(GenBoxPushError, "unreadable"):
            self.service.probe()

    def test_streaming_responses_are_closed_after_successful_push(self) -> None:
        self.configure()
        digest = hashlib.sha256(self.image).hexdigest()
        probe_response = FakeResponse(200, {
            "ok": True,
            "contract_version": "v1",
            "source_id": "chatgpt2api-dev",
            "max_image_bytes": 4096,
        })
        push_response = FakeResponse(200, {
            "ok": True,
            "contract_version": "v1",
            "source_id": "chatgpt2api-dev",
            "sha256": digest,
            "status": "imported",
            "safe_to_delete_source": False,
        })
        self.factory.responses.extend([probe_response, push_response])

        result = self.service.push_image("2026/07/28/image.png")

        self.assertEqual(result["status"], "imported")
        self.assertTrue(probe_response.closed)
        self.assertTrue(push_response.closed)

    def test_streaming_responses_are_closed_when_receipt_is_malformed(self) -> None:
        self.configure()
        probe_response = FakeResponse(200, {
            "ok": True,
            "contract_version": "v1",
            "source_id": "chatgpt2api-dev",
            "max_image_bytes": 4096,
        })
        push_response = FakeResponse(200, {}, stream_chunks=[b"not-json"])
        self.factory.responses.extend([probe_response, push_response])

        with self.assertRaisesRegex(GenBoxPushError, "unreadable"):
            self.service.push_image("2026/07/28/image.png")

        self.assertTrue(probe_response.closed)
        self.assertTrue(push_response.closed)

    def test_duplicate_receipt_fields_are_rejected(self) -> None:
        self.configure()
        duplicate = b'{"ok":true,"contract_version":"v1","source_id":"chatgpt2api-dev","source_id":"other"}'
        self.factory.responses.append(FakeResponse(200, {}, stream_chunks=[duplicate]))

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
