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
        self.pushed: tuple[str, dict[str, object]] | None = None
        self.source_path: str | None = None

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

    def source_sha256(self, path: str) -> str:
        self.source_path = path
        return "a" * 64

    def push_image(self, path: str, **kwargs: object) -> dict[str, object]:
        self.pushed = (path, kwargs)
        return {
            "status": "imported",
            "sha256": "a" * 64,
            "safe_to_delete_source": True,
            "source_retained": True,
        }


class StubCleanupService:
    def __init__(self) -> None:
        self.preview_calls = 0
        self.execute_calls = 0

    def settings(self) -> dict[str, object]:
        return {
            "enabled": False,
            "environment_class": "isolated-vps",
            "execute_available": False,
            "execute_reason": "test-only",
            "default": False,
        }

    def preview(self) -> dict[str, object]:
        self.preview_calls += 1
        return {"mode": "preview"}

    def execute(self) -> dict[str, object]:
        self.execute_calls += 1
        return {"mode": "execute"}


class GenBoxPushApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stub = StubPushService()
        self.cleanup_stub = StubCleanupService()
        self.previous = genbox_push.genbox_push_service
        self.previous_cleanup = genbox_push.genbox_push_cleanup_service
        genbox_push.genbox_push_service = self.stub
        genbox_push.genbox_push_cleanup_service = self.cleanup_stub
        self.client = TestClient(create_app())
        self.headers = {"Authorization": "Bearer local-test-admin-key"}

    def tearDown(self) -> None:
        genbox_push.genbox_push_service = self.previous
        genbox_push.genbox_push_cleanup_service = self.previous_cleanup
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

    def test_image_push_forwards_server_owned_metadata(self) -> None:
        response = self.client.post("/api/genbox-push/images", headers=self.headers, json={
            "path": "/2026\\07\\28/image.png",
            "created_at": "2026-07-28 12:00:00",
            "prompt": "local prompt",
            "model": "local-model",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stub.pushed, (
            "2026/07/28/image.png",
            {
                "created_at": "2026-07-28 12:00:00",
                "prompt": "local prompt",
                "model": "local-model",
                "expected_sha256": "a" * 64,
            },
        ))
        self.assertEqual(self.stub.source_path, "2026/07/28/image.png")

    def test_cleanup_operations_reject_each_browser_supplied_authority_field_before_service(self) -> None:
        """The cleanup API is intent-only; authority stays in server state."""
        forbidden_fields = {
            "path": "2026/08/05/image.png",
            "file_path": "2026/08/05/image.png",
            "sha256": "a" * 64,
            "source_sha256": "a" * 64,
            "receipt": {"safe_to_delete_source": True},
            "destination": "https://attacker.invalid",
            "destination_scope": "attacker-scope",
            "source_id": "attacker-source",
            "environment_class": "isolated-vps",
            "cleanup_capability": "forged-capability",
            "capability": "forged-capability",
            "execute_marker": "forged-marker",
            "marker": "forged-marker",
            "attestation": {"runtime_identity": "forged"},
            "runtime_identity": "forged",
        }

        for endpoint in ("/api/genbox-push/cleanup/preview", "/api/genbox-push/cleanup/run"):
            for field, value in forbidden_fields.items():
                with self.subTest(endpoint=endpoint, field=field):
                    response = self.client.post(endpoint, headers=self.headers, json={field: value})
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(self.cleanup_stub.preview_calls, 0)
                    self.assertEqual(self.cleanup_stub.execute_calls, 0)

    def test_cleanup_settings_rejects_browser_supplied_cleanup_authority_fields(self) -> None:
        forbidden_fields = {
            "path": "2026/08/05/image.png",
            "receipt": {"safe_to_delete_source": True},
            "destination": "https://attacker.invalid",
            "source_id": "attacker-source",
            "environment_class": "isolated-vps",
            "cleanup_capability": "forged-capability",
            "execute_marker": "forged-marker",
            "attestation": {"runtime_identity": "forged"},
        }

        for field, value in forbidden_fields.items():
            with self.subTest(field=field):
                response = self.client.post(
                    "/api/genbox-push/cleanup/settings",
                    headers=self.headers,
                    json={"enabled": True, field: value},
                )
                self.assertEqual(response.status_code, 422)
                self.assertIsNone(self.stub.saved)

    def test_cleanup_operations_reject_cross_site_browser_request_before_service(self) -> None:
        headers = {
            **self.headers,
            "Origin": "https://attacker.invalid",
            "Sec-Fetch-Site": "cross-site",
        }

        for endpoint in ("/api/genbox-push/cleanup/preview", "/api/genbox-push/cleanup/run"):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(endpoint, headers=headers, json={})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.cleanup_stub.preview_calls, 0)
                self.assertEqual(self.cleanup_stub.execute_calls, 0)

    def test_cleanup_routes_reject_browser_authority_in_query_or_custom_headers(self) -> None:
        forged_query = {
            "path": "2026/08/05/image.png",
            "sha256": "a" * 64,
            "receipt": "forged",
            "destination": "https://attacker.invalid",
            "source_id": "attacker-source",
            "environment_class": "isolated-vps",
            "capability": "forged-capability",
            "execute_marker": "forged-marker",
            "attestation": "forged",
        }
        forged_headers = {
            **self.headers,
            "X-GenBox-Cleanup-Capability": "forged-capability",
            "X-GenBox-Cleanup-Attestation": "forged-attestation",
        }

        for endpoint in (
            "/api/genbox-push/cleanup/settings",
            "/api/genbox-push/cleanup/preview",
            "/api/genbox-push/cleanup/run",
        ):
            body = {"enabled": True} if endpoint.endswith("/settings") else {}
            with self.subTest(endpoint=endpoint, channel="query"):
                response = self.client.post(endpoint, headers=self.headers, params=forged_query, json=body)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.cleanup_stub.preview_calls, 0)
                self.assertEqual(self.cleanup_stub.execute_calls, 0)
            with self.subTest(endpoint=endpoint, channel="header"):
                response = self.client.post(endpoint, headers=forged_headers, json=body)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.cleanup_stub.preview_calls, 0)
                self.assertEqual(self.cleanup_stub.execute_calls, 0)

    def test_cleanup_authority_fields_are_rejected_on_every_route_and_browser_input_channel(self) -> None:
        """All cleanup authority is server-owned, independent of request channel."""
        forbidden_fields = {
            "path": "2026/08/05/image.png",
            "file_path": "2026/08/05/image.png",
            "sha256": "a" * 64,
            "source_sha256": "a" * 64,
            "receipt": {"safe_to_delete_source": True},
            "destination": "https://attacker.invalid",
            "destination_scope": "attacker-scope",
            "source_id": "attacker-source",
            "environment_class": "isolated-vps",
            "capability": "forged-capability",
            "cleanup_capability": "forged-capability",
            "marker": "forged-marker",
            "execute_marker": "forged-marker",
            "attestation": {"runtime_identity": "forged"},
            "runtime_identity": "forged",
        }
        endpoints = (
            "/api/genbox-push/cleanup/settings",
            "/api/genbox-push/cleanup/preview",
            "/api/genbox-push/cleanup/run",
        )

        for endpoint in endpoints:
            base_body = {"enabled": True} if endpoint.endswith("/settings") else {}
            for field, value in forbidden_fields.items():
                with self.subTest(endpoint=endpoint, field=field, channel="json"):
                    response = self.client.post(endpoint, headers=self.headers, json={**base_body, field: value})
                    self.assertEqual(response.status_code, 422)
                    self.assertIsNone(self.stub.saved)
                    self.assertEqual(self.cleanup_stub.preview_calls, 0)
                    self.assertEqual(self.cleanup_stub.execute_calls, 0)

                with self.subTest(endpoint=endpoint, field=field, channel="query"):
                    response = self.client.post(endpoint, headers=self.headers, params={field: str(value)}, json=base_body)
                    self.assertEqual(response.status_code, 422)
                    self.assertIsNone(self.stub.saved)
                    self.assertEqual(self.cleanup_stub.preview_calls, 0)
                    self.assertEqual(self.cleanup_stub.execute_calls, 0)

                with self.subTest(endpoint=endpoint, field=field, channel="header"):
                    header_name = "X-GenBox-" + field.replace("_", "-").title()
                    response = self.client.post(
                        endpoint,
                        headers={**self.headers, header_name: "forged"},
                        json=base_body,
                    )
                    self.assertEqual(response.status_code, 422)
                    self.assertIsNone(self.stub.saved)
                    self.assertEqual(self.cleanup_stub.preview_calls, 0)
                    self.assertEqual(self.cleanup_stub.execute_calls, 0)

                with self.subTest(endpoint=endpoint, field=field, channel="cross-site-origin"):
                    response = self.client.post(
                        endpoint,
                        headers={
                            **self.headers,
                            "Origin": "https://attacker.invalid",
                            "Sec-Fetch-Site": "cross-site",
                        },
                        json={**base_body, field: value},
                    )
                    # Invalid JSON is rejected by Pydantic before the route's
                    # origin guard; a valid body is checked below for 403.
                    self.assertIn(response.status_code, {403, 422})
                    self.assertIsNone(self.stub.saved)
                    self.assertEqual(self.cleanup_stub.preview_calls, 0)
                    self.assertEqual(self.cleanup_stub.execute_calls, 0)

            with self.subTest(endpoint=endpoint, channel="cross-site-origin-valid-body"):
                response = self.client.post(
                    endpoint,
                    headers={
                        **self.headers,
                        "Origin": "https://attacker.invalid",
                        "Sec-Fetch-Site": "cross-site",
                    },
                    json=base_body,
                )
                self.assertEqual(response.status_code, 403)
                self.assertIsNone(self.stub.saved)
                self.assertEqual(self.cleanup_stub.preview_calls, 0)
                self.assertEqual(self.cleanup_stub.execute_calls, 0)


if __name__ == "__main__":
    unittest.main()
