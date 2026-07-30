#!/usr/bin/env python3
"""Run the GenBox Push v1 sender/receiver smoke test using local Docker only.

This script never builds or pulls images, publishes ports, or uses user supplied
credentials.  It starts a disposable GenBox receiver on an isolated Docker
network, then runs the supplied chatgpt2api sender image as a one-shot job.
Both containers receive generated, test-only credentials and are removed even
when an assertion fails.

Example:
    python scripts/local_genbox_push_smoke.py \
        --sender-image genbox/chatgpt2api:2.7.0-genbox-p4.0.1-dev \
        --receiver-image genbox-p4-local:current
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


SOURCE_ID = "local-smoke"
RECEIVER_HOSTNAME = "genbox-receiver"
RECEIVER_PORT = 8891
WAIT_TIMEOUT_SECONDS = 45

class SmokeError(RuntimeError):
    """Raised when the local-only smoke test cannot establish its contract."""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sender-image", required=True, help="Prebuilt local chatgpt2api image tag")
    parser.add_argument("--receiver-image", required=True, help="Prebuilt local GenBox receiver image tag")
    return parser.parse_args(argv)


def _run(command: Sequence[str], *, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SmokeError("Docker CLI is not available on this machine.") from exc
    except subprocess.TimeoutExpired as exc:
        output = str(exc.stdout or "")
        raise SmokeError(f"Timed out after {timeout:.0f}s: {' '.join(command[:3])}; {output[-500:]}") from exc

    if check and completed.returncode:
        output = completed.stdout.strip()
        raise SmokeError(f"Docker command failed ({completed.returncode}): {' '.join(command[:3])}; {output[-500:]}")
    return completed


def _require_local_image(image: str) -> str:
    image = image.strip()
    if not image:
        raise SmokeError("Image tag cannot be empty.")
    if any(character.isspace() for character in image) or image.startswith("-"):
        raise SmokeError("Image reference must be a single explicit local tag or digest.")
    if image.endswith(":latest") or ":" not in image.rsplit("/", 1)[-1] and "@sha256:" not in image:
        raise SmokeError("Image reference must be explicit and cannot use the latest tag.")
    inspected = _run(["docker", "image", "inspect", "--format", "{{.Id}}", image], timeout=30)
    image_id = inspected.stdout.strip()
    if not image_id.startswith("sha256:"):
        raise SmokeError(f"Could not determine a local image ID for {image}.")
    return image_id


def _sanitize(value: str, secrets_to_hide: Sequence[str]) -> str:
    sanitized = value
    for secret in secrets_to_hide:
        if secret:
            sanitized = sanitized.replace(secret, "[redacted]")
    return sanitized


@dataclass
class LocalSmokeResources:
    suffix: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    network_created: bool = False
    receiver_created: bool = False
    sender_created: bool = False
    receiver_env_file: str = ""
    sender_env_file: str = ""

    @property
    def network_name(self) -> str:
        return f"genbox-local-smoke-net-{self.suffix}"

    @property
    def receiver_name(self) -> str:
        return f"genbox-local-smoke-receiver-{self.suffix}"

    @property
    def sender_name(self) -> str:
        return f"genbox-local-smoke-sender-{self.suffix}"

    @property
    def run_label(self) -> str:
        return f"genbox.local-smoke.run={self.suffix}"

    def _has_required_labels(self, resource_type: str, resource_name: str) -> bool | None:
        labels = ".Config.Labels" if resource_type == "container" else ".Labels"
        template = (
            f"{{{{ index {labels} \"genbox.local-smoke\" }}}}|"
            f"{{{{ index {labels} \"genbox.local-smoke.run\" }}}}"
        )
        inspected = _run(
            ["docker", resource_type, "inspect", "--format", template, resource_name],
            check=False,
            timeout=15,
        )
        if inspected.returncode:
            # A --rm sender may have already removed itself.  There is no
            # resource left to delete, so cleanup can safely continue.
            output = inspected.stdout.lower()
            if "no such" in output or "not found" in output:
                return None
            raise SmokeError(f"could not inspect {resource_type} {resource_name} before cleanup")
        return inspected.stdout.strip() == f"true|{self.suffix}"

    def cleanup(self) -> None:
        # Cleanup is exact-name plus an inspect-time per-run label match.  It
        # never searches by prefix, labels, or Docker-wide prune operations.
        cleanup_failures: list[str] = []
        for resource_type, resource_name, created, command in (
            ("container", self.sender_name, self.sender_created, ["docker", "rm", "-f", self.sender_name]),
            ("container", self.receiver_name, self.receiver_created, ["docker", "rm", "-f", self.receiver_name]),
            ("network", self.network_name, self.network_created, ["docker", "network", "rm", self.network_name]),
        ):
            if not created:
                continue
            labels_match = self._has_required_labels(resource_type, resource_name)
            if labels_match is None:
                continue
            if not labels_match:
                cleanup_failures.append(f"refused to remove {resource_type} {resource_name}: labels did not match this local smoke run")
                continue
            removal = _run(command, check=False, timeout=30)
            if removal.returncode:
                cleanup_failures.append(f"could not remove {resource_type} {resource_name}")
        for env_file in (self.sender_env_file, self.receiver_env_file):
            if env_file:
                try:
                    Path(env_file).unlink(missing_ok=True)
                except OSError:
                    pass
        if cleanup_failures:
            raise SmokeError("; ".join(cleanup_failures))


def _write_env_file(values: dict[str, str]) -> str:
    # Docker's --env-file keeps generated secrets out of the command line,
    # terminal output, and repository workspace.  Values here are generated
    # for one run and deleted by LocalSmokeResources.cleanup().
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="genbox-local-smoke-", suffix=".env", delete=False)
    try:
        for name, value in values.items():
            if "\n" in value or "\r" in value:
                raise SmokeError(f"Local smoke environment value {name} is invalid.")
            handle.write(f"{name}={value}\n")
    finally:
        handle.close()
    return handle.name


def _wait_for_receiver(receiver_name: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        check = _run(
            [
                "docker",
                "exec",
                receiver_name,
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8891/api/setup/status', timeout=2)",
            ],
            check=False,
            timeout=10,
        )
        if check.returncode == 0:
            return
        last_error = check.stdout.strip()[-300:]
        time.sleep(1)
    raise SmokeError(f"Timed out waiting for the local GenBox receiver. {last_error}")


def _sender_job_source() -> str:
    return """
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import threading

from PIL import Image

from services.genbox_push_service import GenBoxPushService
from services.genbox_push_batch import GenBoxPushBatchService
from services.genbox_push_schedule import GenBoxPushScheduleService
from services.genbox_push_transfer import GenBoxPushTransferCoordinator
from utils.timezone import BEIJING_TZ

relative_path = "local-smoke/synthetic.png"
image_path = Path("/app/data/images") / relative_path
image_path.parent.mkdir(parents=True, exist_ok=True)
Image.new("RGB", (2, 2), (18, 52, 86)).save(image_path, format="PNG")
payload = image_path.read_bytes()

service = GenBoxPushService()
settings = service.update_settings({
    "enabled": True,
    "base_url": os.environ["LOCAL_SMOKE_RECEIVER_URL"] + "/api/sync/push",
    "source_id": os.environ["LOCAL_SMOKE_SOURCE_ID"],
    "push_key": os.environ["LOCAL_SMOKE_PUSH_KEY"],
    "timeout_secs": 20,
})
assert settings["has_push_key"] is True
assert "push_key" not in settings

probe = service.probe()

class GatedPushService:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def transfer_scope(self):
        return self.delegate.transfer_scope()

    def push_image(self, *args, **kwargs):
        self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=10), "coordinated Push was not released"
        return self.delegate.push_image(*args, **kwargs)

coordinator = GenBoxPushTransferCoordinator()
gated = GatedPushService(service)
digest = hashlib.sha256(payload).hexdigest()
coordinated_results = []
coordinated_errors = []

def coordinated_push():
    try:
        coordinated_results.append(coordinator.push_image(
            gated,
            relative_path,
            digest,
            prompt="local synthetic smoke image",
            model="local-smoke",
        ))
    except BaseException as exc:
        coordinated_errors.append(exc)

owner = threading.Thread(target=coordinated_push)
follower = threading.Thread(target=coordinated_push)
owner.start()
assert gated.started.wait(timeout=5), "coordinated Push did not start"
follower.start()
for _ in range(100):
    if any(transfer.waiters for transfer in coordinator._inflight.values()):
        break
    threading.Event().wait(0.01)
else:
    raise AssertionError("duplicate request did not join the coordinated Push")
gated.release.set()
owner.join(timeout=15)
follower.join(timeout=15)
assert not coordinated_errors, coordinated_errors
assert len(coordinated_results) == 2, coordinated_results
assert gated.calls == 1, gated.calls
first = coordinated_results[0]
second = service.push_image(relative_path, prompt="local synthetic smoke image", model="local-smoke")

# Simulate a process stop after the receiver has accepted the image but before
# the batch worker persisted its terminal status. Restart recovery must retry
# the durable item safely; the receiver then returns its idempotent outcome.
batch_state_file = Path("/tmp/local-smoke-batch-state.json")
batch_state_file.write_text(json.dumps({"batches": {"resume": {
    "created_at": "2026-07-29 00:00:00",
    "updated_at": "2026-07-29 00:00:00",
    "items": {"item": {
        "path": relative_path,
        "source_sha256": digest,
        "status": "sending",
        "attempts": 1,
        "updated_at": "2026-07-29 00:00:00",
    }},
}}}), encoding="utf-8")
resumed_batch = GenBoxPushBatchService(state_file=batch_state_file, push_service=service)
resumed_batch.resume(start_worker=False)
assert resumed_batch.get("resume")["queued"] == 1
resumed_batch._drain()
resumed = resumed_batch.get("resume")
assert resumed["status"] == "succeeded", resumed
assert resumed["succeeded"] + resumed["already_imported"] == 1, resumed
assert resumed["already_imported"] == 1, resumed
for path in (batch_state_file, batch_state_file.with_suffix(batch_state_file.suffix + ".bak")):
    path.unlink(missing_ok=True)

# Use the durable scheduler with the real sender service and receiver. Its
# overlap scan must discover a file that appears after the first scan without
# requeuing the file already confirmed in the first batch.
schedule_clock = datetime(2026, 7, 30, 10, 0, tzinfo=BEIJING_TZ)
scheduled_paths = []
for name, color in (("scheduled-first.png", (90, 22, 47)), ("scheduled-late.png", (44, 108, 35))):
    scheduled_path = Path("/app/data/images/local-smoke") / name
    Image.new("RGB", (2, 2), color).save(scheduled_path, format="PNG")

def scheduled_lister(_base_url, **_filters):
    return [{"path": path} for path in scheduled_paths]

scheduled_batches = GenBoxPushBatchService(
    state_file=Path("/tmp/local-smoke-scheduled-batches.json"),
    push_service=service,
)
scheduled = GenBoxPushScheduleService(
    state_file=Path("/tmp/local-smoke-schedule.json"),
    batch_service=scheduled_batches,
    push_service=service,
    image_lister=scheduled_lister,
    now=lambda: schedule_clock,
)
scheduled_paths.append("local-smoke/scheduled-first.png")

def run_schedule_until(expected_succeeded):
    deadline = threading.Event()
    result = scheduled.run_now()
    for _ in range(100):
        if result["succeeded"] == expected_succeeded:
            return result
        deadline.wait(0.05)
        result = scheduled.run_now()
    raise AssertionError(result)

first_schedule = run_schedule_until(1)
schedule_clock += timedelta(days=1)
scheduled_paths.append("local-smoke/scheduled-late.png")
second_schedule = run_schedule_until(2)
for path in (
    Path("/tmp/local-smoke-schedule.json"),
    Path("/tmp/local-smoke-schedule.json.bak"),
    Path("/tmp/local-smoke-scheduled-batches.json"),
    Path("/tmp/local-smoke-scheduled-batches.json.bak"),
):
    path.unlink(missing_ok=True)

assert image_path.is_file(), "sender source image was deleted"
assert first["status"] == "imported", first
assert second["status"] == "already-imported", second
assert first["sha256"] == hashlib.sha256(payload).hexdigest(), first
assert second["sha256"] == hashlib.sha256(payload).hexdigest(), second
assert first["source_retained"] is True
assert second["source_retained"] is True

print("LOCAL_SMOKE_RESULT=" + json.dumps({
    "probe": probe,
    "first_status": first["status"],
    "second_status": second["status"],
    "coordinated_physical_calls": gated.calls,
    "resumed_batch_status": resumed["status"],
    "resumed_batch_item_status": resumed["items"][0]["status"],
    "scheduled_late_item_count": second_schedule["succeeded"],
    "source_sha256": first["sha256"],
    "source_retained": first["source_retained"] and second["source_retained"],
}, sort_keys=True))
""".strip()


def _parse_sender_result(output: str) -> dict[str, object]:
    marker = "LOCAL_SMOKE_RESULT="
    lines = [line for line in output.splitlines() if line.startswith(marker)]
    if len(lines) != 1:
        raise SmokeError("Sender job did not emit a single smoke result marker.")
    try:
        result = json.loads(lines[0][len(marker):])
    except json.JSONDecodeError as exc:
        raise SmokeError("Sender job emitted an invalid smoke result.") from exc
    if not isinstance(result, dict):
        raise SmokeError("Sender job emitted a non-object smoke result.")
    return result


def _assert_receiver_import(receiver_name: str, source_sha256: str) -> None:
    if len(source_sha256) != 64 or any(character not in "0123456789abcdef" for character in source_sha256):
        raise SmokeError("Sender emitted an invalid source SHA-256.")
    receiver_check = (
        "import io; from pathlib import Path; from PIL import Image; "
        f"expected = '{source_sha256}'; "
        "files = [path for path in Path('/app/storage/gallery').rglob('*.png') if path.is_file()]; "
        "assert len(files) == 3, files; "
        "images = [Image.open(io.BytesIO(path.read_bytes())) for path in files]; "
        "assert all(image.size == (2, 2) for image in images); "
        "assert expected in {image.text.get('SourceSHA256') for image in images}"
    )
    check = _run(
        [
            "docker",
            "exec",
            receiver_name,
            "python",
            "-c",
            receiver_check,
        ],
        check=False,
        timeout=30,
    )
    if check.returncode:
        raise SmokeError("GenBox receiver did not retain the expected synthetic imports with their dimensions and source hash.")


def run_smoke(sender_image: str, receiver_image: str) -> dict[str, object]:
    _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
    sender_image_id = _require_local_image(sender_image)
    receiver_image_id = _require_local_image(receiver_image)

    resources = LocalSmokeResources()
    push_key = secrets.token_urlsafe(32)
    receiver_admin_key = secrets.token_urlsafe(32)
    sender_admin_key = secrets.token_urlsafe(32)
    primary_error: BaseException | None = None
    try:
        resources.receiver_created = True
        _run([
            "docker", "network", "create", "--internal",
            "--label", "genbox.local-smoke=true",
            "--label", resources.run_label,
            resources.network_name,
        ])
        resources.network_created = True

        push_keys_json = json.dumps({SOURCE_ID: push_key}, separators=(",", ":"))
        resources.receiver_env_file = _write_env_file({
            "APP_MODE": "prod",
            "ADMIN_KEY": receiver_admin_key,
            "GENBOX_PUSH_KEYS": push_keys_json,
        })
        _run([
            "docker", "run", "--pull=never", "-d", "--name", resources.receiver_name,
            "--label", "genbox.local-smoke=true",
            "--label", resources.run_label,
            "--network", resources.network_name,
            "--network-alias", RECEIVER_HOSTNAME,
            "--env-file", resources.receiver_env_file,
            receiver_image_id,
        ])
        _wait_for_receiver(resources.receiver_name, WAIT_TIMEOUT_SECONDS)

        resources.sender_env_file = _write_env_file({
            "LOCAL_SMOKE_RECEIVER_URL": f"http://{RECEIVER_HOSTNAME}:{RECEIVER_PORT}",
            "LOCAL_SMOKE_SOURCE_ID": SOURCE_ID,
            "LOCAL_SMOKE_PUSH_KEY": push_key,
            "CHATGPT2API_AUTH_KEY": sender_admin_key,
        })
        resources.sender_created = True
        sender = _run([
            "docker", "run", "--pull=never", "--rm", "--name", resources.sender_name,
            "--label", "genbox.local-smoke=true",
            "--label", resources.run_label,
            "--network", resources.network_name,
            "--env-file", resources.sender_env_file,
            "--entrypoint", "uv",
            sender_image_id,
            "run", "--no-sync", "python", "-c", _sender_job_source(),
        ], check=False, timeout=120)
        # --rm may remove the job before cleanup, which is expected.
        if sender.returncode:
            detail = _sanitize(str(sender.stdout or "")[-1000:], [push_key, receiver_admin_key, sender_admin_key])
            raise SmokeError(f"Local sender job failed ({sender.returncode}). {detail}")

        result = _parse_sender_result(sender.stdout)
        if result.get("first_status") != "imported" or result.get("second_status") != "already-imported":
            raise SmokeError("Sender result did not prove initial import and idempotent retry.")
        if result.get("coordinated_physical_calls") != 1:
            raise SmokeError("Sender result did not prove one physical Push for concurrent matching requests.")
        if result.get("resumed_batch_status") != "succeeded":
            raise SmokeError("Sender result did not prove interrupted batch recovery.")
        if result.get("scheduled_late_item_count") != 2:
            raise SmokeError("Sender result did not prove scheduled late-image discovery.")
        if result.get("source_retained") is not True:
            raise SmokeError("Sender result did not prove source retention.")
        probe = result.get("probe")
        if not isinstance(probe, dict) or probe.get("contract_version") != "v1":
            raise SmokeError("Sender result did not prove the GenBox Push v1 probe contract.")
        _assert_receiver_import(resources.receiver_name, str(result.get("source_sha256") or ""))
        return {**result, "sender_image_id": sender_image_id, "receiver_image_id": receiver_image_id}
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            resources.cleanup()
        except SmokeError as cleanup_error:
            if primary_error is not None:
                raise SmokeError(f"{primary_error}; cleanup also failed: {cleanup_error}") from primary_error
            raise


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_smoke(args.sender_image, args.receiver_image)
    except SmokeError as exc:
        print(f"LOCAL GenBox Push smoke: FAILED: {exc}", file=sys.stderr)
        return 1

    print(
        "LOCAL GenBox Push smoke: PASSED "
        f"(first={result['first_status']}, retry={result['second_status']}, source_retained=true, "
        f"sender={result['sender_image_id']}, receiver={result['receiver_image_id']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
