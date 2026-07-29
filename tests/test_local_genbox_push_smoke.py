from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_genbox_push_smoke.py"
SPEC = importlib.util.spec_from_file_location("local_genbox_push_smoke", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


class LocalGenBoxPushSmokeTests(unittest.TestCase):
    def test_requires_explicit_prebuilt_image_tags(self) -> None:
        args = smoke.parse_args([
            "--sender-image", "sender:test",
            "--receiver-image", "receiver:test",
        ])

        self.assertEqual(args.sender_image, "sender:test")
        self.assertEqual(args.receiver_image, "receiver:test")

    def test_sender_job_asserts_coordination_idempotency_and_source_retention(self) -> None:
        job = smoke._sender_job_source()

        self.assertIn('Image.new("RGB", (2, 2), (18, 52, 86))', job)
        self.assertIn("GenBoxPushTransferCoordinator", job)
        self.assertIn("assert gated.calls == 1", job)
        self.assertIn('assert first["status"] == "imported"', job)
        self.assertIn('assert second["status"] == "already-imported"', job)
        self.assertIn('assert first["sha256"] == hashlib.sha256(payload).hexdigest()', job)
        self.assertIn('assert image_path.is_file()', job)
        self.assertNotIn("unlink(", job)

    def test_runtime_uses_inspected_image_ids_without_pulling(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")

        self.assertGreaterEqual(source.count('"--pull=never"'), 2)
        self.assertIn("receiver_image_id,", source)
        self.assertIn("sender_image_id,", source)

    def test_generated_resource_names_are_isolated(self) -> None:
        first = smoke.LocalSmokeResources()
        second = smoke.LocalSmokeResources()

        self.assertNotEqual(first.network_name, second.network_name)
        self.assertTrue(first.network_name.startswith("genbox-local-smoke-net-"))
        self.assertTrue(first.receiver_name.startswith("genbox-local-smoke-receiver-"))

    def test_output_sanitization_hides_test_credentials(self) -> None:
        self.assertEqual(smoke._sanitize("key=temporary-secret", ["temporary-secret"]), "key=[redacted]")

    def test_cleanup_refuses_resource_without_both_required_labels(self) -> None:
        resources = smoke.LocalSmokeResources(suffix="test-run")
        resources.receiver_created = True
        original_run = smoke._run
        commands: list[list[str]] = []

        def fake_run(command, **_kwargs):
            commands.append(list(command))
            return subprocess.CompletedProcess(command, 0, "false|test-run\n")

        smoke._run = fake_run
        try:
            with self.assertRaisesRegex(smoke.SmokeError, "refused to remove container"):
                resources.cleanup()
        finally:
            smoke._run = original_run

        self.assertNotIn(["docker", "rm", "-f", resources.receiver_name], commands)

    def test_cleanup_accepts_sender_already_removed_by_docker(self) -> None:
        resources = smoke.LocalSmokeResources(suffix="test-run")
        resources.sender_created = True
        original_run = smoke._run

        def fake_run(command, **_kwargs):
            return subprocess.CompletedProcess(command, 1, "No such container")

        smoke._run = fake_run
        try:
            resources.cleanup()
        finally:
            smoke._run = original_run

    def test_rejects_implicit_or_latest_image_references(self) -> None:
        with self.assertRaises(smoke.SmokeError):
            smoke._require_local_image("sender:latest")
        with self.assertRaises(smoke.SmokeError):
            smoke._require_local_image("sender")


if __name__ == "__main__":
    unittest.main()
