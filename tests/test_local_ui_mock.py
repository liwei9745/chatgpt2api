import importlib.util
import json
import sys
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_ui_mock.py"
SPEC = importlib.util.spec_from_file_location("local_ui_mock_test", MODULE_PATH)
assert SPEC and SPEC.loader
mock = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mock
SPEC.loader.exec_module(mock)


class LocalUiMockTests(unittest.TestCase):
    def setUp(self):
        mock.STATE = mock.MockState()
        self.server = mock.build_server(0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def request(self, method, path, payload=None, authorized=True):
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = f"Bearer {mock.TEST_BEARER}"
        body = json.dumps(payload).encode() if payload is not None else None
        connection = HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode())
        connection.close()
        return response.status, data

    def test_rejects_non_test_bearer(self):
        status, _ = self.request("GET", "/api/images", authorized=False)
        self.assertEqual(status, 401)

    def test_batch_cancel_and_retry_are_synthetic_and_in_memory(self):
        status, created = self.request("POST", "/api/genbox-push/batches", {"paths": [item["path"] for item in mock.SYNTHETIC_IMAGES]})
        self.assertEqual(status, 200)
        batch_id = created["batch"]["id"]
        self.assertEqual(created["batch"]["failed"], 1)

        status, cancelled = self.request("POST", f"/api/genbox-push/batches/{batch_id}/cancel", {})
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["batch"]["cancelled"], 2)

        status, retried = self.request("POST", f"/api/genbox-push/batches/{batch_id}/retry-failed", {})
        self.assertEqual(status, 200)
        self.assertEqual(retried["batch"]["succeeded"], 1)
        self.assertEqual(retried["batch"]["failed"], 0)

    def test_schedule_and_date_preview_use_only_fixed_samples(self):
        status, preview = self.request("POST", "/api/genbox-push/batches/preview-date-range", {"start_date": "2026-07-21", "end_date": "2026-07-22"})
        self.assertEqual(status, 200)
        self.assertEqual(preview["preview"]["eligible_count"], 2)

        status, schedule = self.request("PUT", "/api/genbox-push/schedule", {"enabled": True, "weekday": 2, "time": "10:30", "start_date": "", "end_date": ""})
        self.assertEqual(status, 200)
        self.assertTrue(schedule["schedule"]["enabled"])
        self.assertEqual(schedule["schedule"]["weekday"], 2)


if __name__ == "__main__":
    unittest.main()
