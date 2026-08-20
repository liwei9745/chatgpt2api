from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from services.config import DATA_DIR
from services.genbox_push_batch import GenBoxPushBatchService, genbox_push_batch_service
from services.genbox_push_service import GenBoxPushService, genbox_push_service
from services.image_storage_service import image_storage_service
from services.json_file import read_json_object, write_json_file
from utils.timezone import BEIJING_TZ, beijing_now


SCHEDULE_FILE = DATA_DIR / "genbox_push_schedule.json"
_FILE_LOCKS: dict[str, threading.RLock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _file_lock(path: Path) -> threading.RLock:
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(str(path.resolve()), threading.RLock())


class GenBoxPushScheduleService:
    """Durable weekly scans that create normal GenBox Push batches.

    The scheduler retains only image identities, batch identifiers and safe
    recovery state. It never stores prompts, credentials or image bytes.
    """

    def __init__(
        self,
        *,
        state_file: Path = SCHEDULE_FILE,
        batch_service: GenBoxPushBatchService | Any = genbox_push_batch_service,
        push_service: GenBoxPushService | Any = genbox_push_service,
        image_reader: Callable[[str], bytes] = image_storage_service.get_bytes,
        image_exists: Callable[[str], bool] = image_storage_service.exists,
        image_lister: Callable[..., list[dict[str, object]]] = image_storage_service.list_items,
        now: Callable[[], datetime] = beijing_now,
        worker_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        self.state_file = state_file
        self.batch_service = batch_service
        self.push_service = push_service
        self.image_reader = image_reader
        self.image_exists = image_exists
        self.image_lister = image_lister
        self.now = now
        self.worker_factory = worker_factory
        self._lock = _file_lock(state_file)
        self._lease_lock_file = state_file.with_suffix(state_file.suffix + ".lease-lock")
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

    @staticmethod
    def _default_schedule() -> dict[str, object]:
        return {
            "enabled": False,
            "weekday": 0,
            "time": "09:00",
            "start_date": "",
            "end_date": "",
            "overlap_days": 2,
            "last_scheduled_date": "",
            "last_run_at": "",
            "last_error": "",
        }

    @staticmethod
    def _clean_path(value: object) -> str:
        return str(value or "").strip().replace("\\", "/").lstrip("/")

    @staticmethod
    def _safe_error(_: Exception | None = None) -> str:
        return "Scheduled GenBox Push could not finish. Source images were retained; GenBox will retry a limited number of times."

    def _load_locked(self) -> dict[str, Any]:
        raw = read_json_object(self.state_file, name=self.state_file.name)
        schedule = self._default_schedule()
        if isinstance(raw.get("schedule"), dict):
            schedule.update(raw["schedule"])
        items = raw.get("items") if isinstance(raw.get("items"), dict) else {}
        return {
            "schedule": schedule,
            "cursor": str(raw.get("cursor") or ""),
            "lease": dict(raw.get("lease") or {}) if isinstance(raw.get("lease"), dict) else {},
            "items": {str(key): dict(value) for key, value in items.items() if isinstance(value, dict)},
        }

    def _save_locked(self, state: dict[str, Any]) -> None:
        write_json_file(self.state_file, state)
        for path in (self.state_file, self.state_file.with_suffix(self.state_file.suffix + ".bak")):
            try:
                path.chmod(0o600)
            except OSError:
                pass

    @contextmanager
    def _lease_file_guard(self):
        """Serialize lease changes between separate sender processes."""
        descriptor: int | None = None
        try:
            try:
                descriptor = os.open(self._lease_lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    # The guard is held only while reading or writing one JSON
                    # record. A stale file can therefore only be a crashed
                    # process, not a long-running Push operation.
                    if time.time() - self._lease_lock_file.stat().st_mtime > 30:
                        self._lease_lock_file.unlink()
                        descriptor = os.open(self._lease_lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except OSError:
                    pass
            yield descriptor is not None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                finally:
                    try:
                        self._lease_lock_file.unlink()
                    except OSError:
                        pass

    @staticmethod
    def _validate(payload: dict[str, object], current: dict[str, object]) -> dict[str, object]:
        schedule = {**current}
        for key in ("enabled", "start_date", "end_date", "delete_source_after_push"):
            if key in payload:
                if key == "enabled" or key == "delete_source_after_push":
                    schedule[key] = bool(payload[key])
                else:
                    schedule[key] = str(payload[key] or "").strip()
        try:
            weekday = int(payload.get("weekday", schedule["weekday"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("Choose a weekday for the automatic Push.") from exc
        if weekday not in range(7):
            raise ValueError("Choose a valid weekday for the automatic Push.")
        schedule["weekday"] = weekday
        time_value = str(payload.get("time", schedule["time"]) or "").strip()
        try:
            datetime.strptime(time_value, "%H:%M")
        except ValueError as exc:
            raise ValueError("Use HH:MM for the automatic Push time.") from exc
        schedule["time"] = time_value
        for key in ("start_date", "end_date"):
            if schedule[key]:
                try:
                    datetime.strptime(str(schedule[key]), "%Y-%m-%d")
                except ValueError as exc:
                    raise ValueError("Use YYYY-MM-DD for optional schedule dates.") from exc
        if schedule["start_date"] and schedule["end_date"] and schedule["end_date"] < schedule["start_date"]:
            raise ValueError("The schedule end date must not be before the start date.")
        return schedule

    @staticmethod
    def _public(state: dict[str, Any]) -> dict[str, object]:
        schedule = state["schedule"]
        items = list(state["items"].values())
        return {
            "enabled": bool(schedule.get("enabled")),
            "weekday": int(schedule.get("weekday") or 0),
            "time": str(schedule.get("time") or "09:00"),
            "start_date": str(schedule.get("start_date") or ""),
            "end_date": str(schedule.get("end_date") or ""),
            "delete_source_after_push": bool(schedule.get("delete_source_after_push")),
            "cursor": str(state.get("cursor") or ""),
            "last_run_at": str(schedule.get("last_run_at") or ""),
            "last_error": str(schedule.get("last_error") or ""),
            "queued": sum(item.get("status") in {"queued", "sending"} for item in items),
            "succeeded": sum(item.get("status") == "succeeded" for item in items),
            "already_imported": sum(item.get("status") == "already-imported" for item in items),
            "failed": sum(item.get("status") == "failed" for item in items),
            "source_retained": True,
        }

    def get_settings(self) -> dict[str, object]:
        # A manual scan creates normal batches that finish asynchronously. Refresh
        # their projections here so a disabled weekly schedule still reports the
        # completed outcome instead of leaving the Settings view at "queued".
        now = self.now().astimezone(BEIJING_TZ)
        with self._lease_file_guard() as guarded:
            with self._lock:
                state = self._load_locked()
                if guarded:
                    self._sync_batches_locked(state, now)
                    self._save_locked(state)
                return self._public(state)

    def update_settings(self, payload: dict[str, object]) -> dict[str, object]:
        with self._lock:
            state = self._load_locked()
            schedule = self._validate(payload, state["schedule"])
            if schedule["enabled"] and not bool(self.push_service.get_settings().get("enabled")):
                raise ValueError("Enable and save the GenBox destination before turning on automatic Push.")
            state["schedule"] = schedule
            self._save_locked(state)
            return self._public(state)

    def _acquire_lease(self, state: dict[str, Any], now: datetime) -> str | None:
        lease = state["lease"]
        expires = str(lease.get("expires_at") or "")
        if expires:
            try:
                if datetime.fromisoformat(expires) > now:
                    return None
            except ValueError:
                pass
        token = uuid.uuid4().hex
        state["lease"] = {"token": token, "expires_at": (now + timedelta(minutes=5)).isoformat()}
        self._save_locked(state)
        return token

    def _claim_lease(self, now: datetime) -> str | None:
        with self._lease_file_guard() as guarded:
            if not guarded:
                return None
            with self._lock:
                return self._acquire_lease(self._load_locked(), now)

    def _release_lease(self, token: str) -> None:
        with self._lease_file_guard() as guarded:
            if not guarded:
                return
            with self._lock:
                state = self._load_locked()
                if str(state["lease"].get("token") or "") == token:
                    state["lease"] = {}
                    self._save_locked(state)

    def _sync_batches_locked(self, state: dict[str, Any], now: datetime) -> None:
        for item in state["items"].values():
            batch_id = str(item.get("batch_id") or "")
            if not batch_id or item.get("status") not in {"queued", "sending"}:
                continue
            batch = self.batch_service.get(batch_id)
            if batch is None:
                item.update({"status": "failed", "error": self._safe_error(), "updated_at": now.isoformat()})
                continue
            status = str(batch.get("status") or "queued")
            if status in {"queued", "sending"}:
                item["status"] = status
            elif status == "succeeded":
                already_imported = int(batch.get("already_imported") or 0)
                succeeded = int(batch.get("succeeded") or 0)
                item.update({
                    "status": "already-imported" if already_imported and not succeeded else "succeeded",
                    "error": "",
                    "updated_at": now.isoformat(),
                })
            else:
                item.update({"status": "failed", "error": self._safe_error(), "updated_at": now.isoformat()})

    def _retry_failures_locked(self, state: dict[str, Any], now: datetime) -> None:
        for item in state["items"].values():
            if item.get("status") != "failed" or int(item.get("attempts") or 0) >= 3:
                continue
            retry_at = str(item.get("retry_at") or "")
            if retry_at:
                try:
                    if datetime.fromisoformat(retry_at) > now:
                        continue
                except ValueError:
                    pass
            batch_id = str(item.get("batch_id") or "")
            try:
                retried = self.batch_service.retry_failed(batch_id)
            except Exception:
                retried = None
            if retried is None:
                continue
            attempts = int(item.get("attempts") or 0) + 1
            status = str(retried.get("status") or "queued")
            item.update({
                "status": "already-imported" if status == "succeeded" and int(retried.get("already_imported") or 0) and not int(retried.get("succeeded") or 0) else "succeeded" if status == "succeeded" else "queued",
                "attempts": attempts,
                "retry_at": "" if status == "succeeded" else (now + timedelta(minutes=min(2 ** attempts, 30))).isoformat(),
                "error": "" if status == "succeeded" else self._safe_error(),
                "updated_at": now.isoformat(),
            })

    def run_now(self) -> dict[str, object]:
        now = self.now().astimezone(BEIJING_TZ)
        token = self._claim_lease(now)
        if token is None:
            raise ValueError("Another automatic Push scan is already running. Wait for it to finish.")
        try:
            with self._lock:
                state = self._load_locked()
                self._sync_batches_locked(state, now)
                self._retry_failures_locked(state, now)
                cursor = str(state.get("cursor") or "")
                overlap = int(state["schedule"].get("overlap_days") or 2)
                start = (now.date() - timedelta(days=overlap)).isoformat()
                if cursor:
                    try:
                        start = min(start, (datetime.strptime(cursor, "%Y-%m-%d").date() - timedelta(days=overlap)).isoformat())
                    except ValueError:
                        pass
                schedule = state["schedule"]
                if schedule.get("start_date"):
                    start = max(start, str(schedule["start_date"]))
                end = now.date().isoformat()
                if schedule.get("end_date"):
                    end = min(end, str(schedule["end_date"]))
                candidates = [] if end < start else self.image_lister("", start_date=start, end_date=end, refresh_index=True, verify_existing=True)
                for candidate in candidates:
                    path = self._clean_path(candidate.get("path") if isinstance(candidate, dict) else "")
                    if not path or not self.image_exists(path):
                        continue
                    digest = hashlib.sha256(self.image_reader(path)).hexdigest()
                    identity = f"{path}:{digest}"
                    if identity in state["items"]:
                        continue
                    try:
                        batch = self.batch_service.create(
                            [path],
                            delete_source_after_push=bool(schedule.get("delete_source_after_push")),
                        )
                        status = str(batch.get("status") or "queued")
                        state["items"][identity] = {
                            "path": path,
                            "source_sha256": digest,
                            "batch_id": str(batch.get("id") or ""),
                            "status": status if status in {"queued", "sending", "succeeded", "already-imported"} else "failed",
                            "attempts": 1,
                            "updated_at": now.isoformat(),
                            "error": "" if status != "failed" else self._safe_error(),
                        }
                    except Exception:
                        state["items"][identity] = {
                            "path": path,
                            "source_sha256": digest,
                            "batch_id": "",
                            "status": "failed",
                            "attempts": 1,
                            "retry_at": (now + timedelta(minutes=2)).isoformat(),
                            "updated_at": now.isoformat(),
                            "error": self._safe_error(),
                        }
                state["cursor"] = end
                state["schedule"].update({"last_run_at": now.isoformat(), "last_error": ""})
                self._save_locked(state)
                return self._public(state)
        except Exception as exc:
            with self._lock:
                state = self._load_locked()
                state["schedule"]["last_error"] = self._safe_error(exc)
                self._save_locked(state)
            raise
        finally:
            self._release_lease(token)

    def run_due_once(self) -> dict[str, object] | None:
        now = self.now().astimezone(BEIJING_TZ)
        with self._lease_file_guard() as guarded:
            if not guarded:
                return None
            with self._lock:
                state = self._load_locked()
                schedule = state["schedule"]
                if not schedule.get("enabled") or now.weekday() != int(schedule.get("weekday") or 0):
                    return None
                if now.strftime("%H:%M") < str(schedule.get("time") or "09:00"):
                    return None
                if str(schedule.get("last_scheduled_date") or "") == now.date().isoformat():
                    return None
                schedule["last_scheduled_date"] = now.date().isoformat()
                self._save_locked(state)
        try:
            return self.run_now()
        except Exception:
            with self._lock:
                state = self._load_locked()
                if state["schedule"].get("last_scheduled_date") == now.date().isoformat():
                    state["schedule"]["last_scheduled_date"] = ""
                    self._save_locked(state)
            raise

    def _resume_pending(self) -> None:
        now = self.now().astimezone(BEIJING_TZ)
        token = self._claim_lease(now)
        if token is None:
            return
        try:
            with self._lock:
                state = self._load_locked()
                self._sync_batches_locked(state, now)
                self._retry_failures_locked(state, now)
                self._save_locked(state)
        finally:
            self._release_lease(token)

    def resume(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = self.worker_factory(target=self._loop, name="genbox-push-schedule", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(60):
            try:
                if self.get_settings()["enabled"]:
                    self.run_due_once()
                    self._resume_pending()
            except Exception:
                pass


genbox_push_schedule_service = GenBoxPushScheduleService()
