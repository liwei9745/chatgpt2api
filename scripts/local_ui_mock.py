"""Synthetic local API for manual Phase 5 browser checks.

This server intentionally has no imports from the sender application.  It keeps
all state in memory, accepts only the fixed test bearer value, and never reads
or writes the project's data, configuration, or image directories.
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


TEST_BEARER = "local-ui-test-only"
JSON_HEADERS = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
NOW = "2026-07-28T09:00:00Z"

SYNTHETIC_IMAGES = [
    {
        "name": "sample-sunrise.png",
        "path": "synthetic/sample-sunrise.png",
        "url": "/images/synthetic/sample-sunrise.png",
        "thumbnail_url": "/images/synthetic/sample-sunrise.png",
        "size": 1024,
        "created_at": "2026-07-21T09:00:00Z",
        "tags": ["synthetic", "sunrise"],
        "type": "image",
        "width": 64,
        "height": 64,
    },
    {
        "name": "sample-mountain.png",
        "path": "synthetic/sample-mountain.png",
        "url": "/images/synthetic/sample-mountain.png",
        "thumbnail_url": "/images/synthetic/sample-mountain.png",
        "size": 2048,
        "created_at": "2026-07-22T09:00:00Z",
        "tags": ["synthetic", "mountain"],
        "type": "image",
        "width": 64,
        "height": 64,
    },
    {
        "name": "sample-river.png",
        "path": "synthetic/sample-river.png",
        "url": "/images/synthetic/sample-river.png",
        "thumbnail_url": "/images/synthetic/sample-river.png",
        "size": 3072,
        "created_at": "2026-07-23T09:00:00Z",
        "tags": ["synthetic", "river"],
        "type": "image",
        "width": 64,
        "height": 64,
    },
]


def default_settings() -> dict[str, Any]:
    return {
        "enabled": True,
        "base_url": "http://genbox.local.invalid",
        "source_id": "local-ui-mock",
        "has_push_key": True,
        "timeout_secs": 20,
    }


def default_schedule() -> dict[str, Any]:
    return {
        "enabled": False,
        "weekday": 0,
        "time": "09:00",
        "start_date": "",
        "end_date": "",
        "cursor": "",
        "last_run_at": "",
        "last_error": "",
        "queued": 0,
        "succeeded": 0,
        "failed": 0,
        "source_retained": True,
    }


class MockState:
    def __init__(self) -> None:
        self.settings = default_settings()
        self.schedule = default_schedule()
        self.batches: dict[str, dict[str, Any]] = {}
        self.next_batch = 1

    def create_batch(self, paths: list[str]) -> dict[str, Any]:
        batch_id = f"local-ui-batch-{self.next_batch}"
        self.next_batch += 1
        items = []
        for index, path in enumerate(paths):
            items.append(
                {
                    "id": f"{batch_id}-{index + 1}",
                    "path": path,
                    "status": "failed" if index == 1 else "queued",
                    "attempts": 1 if index == 1 else 0,
                    "updated_at": NOW,
                    "error": "Synthetic retryable failure." if index == 1 else "",
                    "source_retained": True,
                }
            )
        batch = {
            "id": batch_id,
            "status": "sending",
            "created_at": NOW,
            "updated_at": NOW,
            "items": items,
            "source_retained": True,
        }
        self.batches[batch_id] = batch
        return self.batch_view(batch)

    @staticmethod
    def batch_view(batch: dict[str, Any]) -> dict[str, Any]:
        result = deepcopy(batch)
        items = result["items"]
        result.update(
            total=len(items),
            queued=sum(item["status"] == "queued" for item in items),
            sending=sum(item["status"] == "sending" for item in items),
            succeeded=sum(item["status"] == "succeeded" for item in items),
            failed=sum(item["status"] == "failed" for item in items),
            cancelled=sum(item["status"] == "cancelled" for item in items),
        )
        return result

    def cancel_batch(self, batch_id: str) -> dict[str, Any] | None:
        batch = self.batches.get(batch_id)
        if batch is None:
            return None
        for item in batch["items"]:
            if item["status"] == "queued":
                item["status"] = "cancelled"
                item["updated_at"] = NOW
        batch["status"] = "cancelled"
        return self.batch_view(batch)

    def retry_failed(self, batch_id: str) -> dict[str, Any] | None:
        batch = self.batches.get(batch_id)
        if batch is None:
            return None
        for item in batch["items"]:
            if item["status"] == "failed":
                item["status"] = "succeeded"
                item["attempts"] += 1
                item["error"] = ""
                item["updated_at"] = NOW
        batch["status"] = "succeeded"
        return self.batch_view(batch)


STATE = MockState()


def compact_settings() -> dict[str, Any]:
    return {
        "basic": {"base_url": "http://localhost:5173", "image_expire_hours": 15, "api_key": ""},
        "base_url": "http://localhost:5173",
        "image_retention_days": 15,
        "log_retention_days": 30,
        "proxy_runtime": {"enabled": False, "egress_mode": "direct", "clearance": {"enabled": False}},
        "image_storage": {"enabled": False},
        "backup": {"enabled": False, "interval_minutes": 1440, "rotation_keep": 10},
        "third_party_apps": {"infinite_canvas": {"enabled": False, "url": ""}},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "GenBoxLocalUiMock/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        for key, value in JSON_HEADERS.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def request_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def is_authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TEST_BEARER}"

    def require_authorized(self) -> bool:
        if self.is_authorized():
            return True
        self.send_json(HTTPStatus.UNAUTHORIZED, {"detail": {"error": "Local test bearer required."}})
        return False

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self.send_json(HTTPStatus.OK, {"ok": True, "mode": "local-ui-mock"})
            return
        if path == "/auth/status":
            self.send_json(HTTPStatus.OK, {
                "authenticated": self.is_authorized(),
                "role": "admin" if self.is_authorized() else "",
                "subject_id": "local-ui-mock" if self.is_authorized() else "",
                "name": "Local UI test" if self.is_authorized() else "",
            })
            return
        if not self.require_authorized():
            return
        if path == "/version":
            self.send_json(HTTPStatus.OK, {"tag": "local-ui-mock"})
        elif path == "/api/settings":
            self.send_json(HTTPStatus.OK, {"config": compact_settings()})
        elif path == "/api/settings/third-party-apps":
            self.send_json(HTTPStatus.OK, {"infinite_canvas": {"enabled": False, "url": ""}})
        elif path == "/api/images":
            query = parse_qs(urlparse(self.path).query)
            start = query.get("start_date", [""])[0]
            end = query.get("end_date", [""])[0]
            items = [item for item in SYNTHETIC_IMAGES if (not start or item["created_at"][:10] >= start) and (not end or item["created_at"][:10] <= end)]
            self.send_json(HTTPStatus.OK, {
                "items": items,
                "total": len(items),
                "total_size": sum(item["size"] for item in items),
                "retention_days": 15,
                "counts": {"all": len(items), "image": len(items), "video": 0, "music": 0},
                "page": 1,
                "page_size": 24,
                "page_count": 1,
            })
        elif path == "/api/images/tags":
            self.send_json(HTTPStatus.OK, {"tags": ["synthetic", "sunrise", "mountain", "river"]})
        elif path == "/api/images/storage":
            self.send_json(HTTPStatus.OK, {"disk_total_mb": 1024, "disk_used_mb": 128, "disk_free_mb": 896, "image_count": 3, "image_size_mb": 1, "image_size_bytes": 6144})
        elif path == "/api/genbox-push/settings":
            self.send_json(HTTPStatus.OK, {"settings": STATE.settings})
        elif path == "/api/genbox-push/schedule":
            self.send_json(HTTPStatus.OK, {"schedule": STATE.schedule})
        elif path.startswith("/api/genbox-push/batches/"):
            batch_id = path.rsplit("/", 1)[-1]
            batch = STATE.batches.get(batch_id)
            if batch is None:
                self.send_json(HTTPStatus.NOT_FOUND, {"detail": {"error": "Synthetic batch not found."}})
            else:
                self.send_json(HTTPStatus.OK, {"batch": STATE.batch_view(batch)})
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"detail": {"error": "No local mock route."}})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/auth/login":
            if self.is_authorized():
                self.send_json(HTTPStatus.OK, {"ok": True})
            else:
                self.send_json(HTTPStatus.UNAUTHORIZED, {"detail": {"error": "Local test bearer required."}})
            return
        if not self.require_authorized():
            return
        body = self.request_json()
        if path == "/api/genbox-push/batches":
            paths = body.get("paths")
            if not isinstance(paths, list) or not paths or not all(isinstance(path, str) and path.startswith("synthetic/") for path in paths):
                self.send_json(HTTPStatus.BAD_REQUEST, {"detail": {"error": "Synthetic image paths are required."}})
                return
            self.send_json(HTTPStatus.OK, {"batch": STATE.create_batch(paths)})
        elif path == "/api/genbox-push/batches/preview-date-range":
            start = str(body.get("start_date") or "")
            end = str(body.get("end_date") or "")
            paths = [item["path"] for item in SYNTHETIC_IMAGES if start <= item["created_at"][:10] <= end]
            self.send_json(HTTPStatus.OK, {"preview": {"start_date": start, "end_date": end, "eligible_count": len(paths), "skipped_count": 0, "samples": paths[:2], "paths": paths}})
        elif path.endswith("/cancel"):
            batch = STATE.cancel_batch(path.split("/")[-2])
            self.send_json(HTTPStatus.OK if batch else HTTPStatus.NOT_FOUND, {"batch": batch} if batch else {"detail": {"error": "Synthetic batch not found."}})
        elif path.endswith("/retry-failed"):
            batch = STATE.retry_failed(path.split("/")[-2])
            self.send_json(HTTPStatus.OK if batch else HTTPStatus.NOT_FOUND, {"batch": batch} if batch else {"detail": {"error": "Synthetic batch not found."}})
        elif path == "/api/genbox-push/settings":
            STATE.settings.update({key: value for key, value in body.items() if key in {"enabled", "base_url", "source_id", "timeout_secs"}})
            STATE.settings["has_push_key"] = True
            self.send_json(HTTPStatus.OK, {"settings": STATE.settings})
        elif path == "/api/genbox-push/probe":
            self.send_json(HTTPStatus.OK, {"result": {"ok": True, "contract_version": "v1", "max_image_bytes": 1024 * 1024}})
        elif path == "/api/genbox-push/schedule/run-now":
            STATE.schedule.update({"last_run_at": NOW, "queued": 1, "succeeded": 0, "failed": 0, "last_error": ""})
            self.send_json(HTTPStatus.OK, {"schedule": STATE.schedule})
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"detail": {"error": "No local mock route."}})

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self.require_authorized():
            return
        if path != "/api/genbox-push/schedule":
            self.send_json(HTTPStatus.NOT_FOUND, {"detail": {"error": "No local mock route."}})
            return
        body = self.request_json()
        STATE.schedule.update({key: body.get(key, STATE.schedule[key]) for key in ("enabled", "weekday", "time", "start_date", "end_date")})
        self.send_json(HTTPStatus.OK, {"schedule": STATE.schedule})


def build_server(port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the isolated local UI mock API.")
    parser.add_argument("--port", type=int, default=8893)
    args = parser.parse_args()
    server = build_server(args.port)
    print(f"Local UI mock listening at http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
