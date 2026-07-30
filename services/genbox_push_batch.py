from __future__ import annotations

import hashlib
import random
import threading
import uuid
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from services.config import DATA_DIR
from services.genbox_push_service import GenBoxPushService, genbox_push_service
from services.genbox_push_transfer import GenBoxPushTransferCoordinator, genbox_push_transfer_coordinator
from services.image_storage_service import image_storage_service
from services.json_file import read_json_object, write_json_file
from services.process_file_lock import ProcessFileLock, ProcessReentrantLock
from utils.timezone import beijing_now, beijing_now_str


BATCH_FILE = DATA_DIR / "genbox_push_batches.json"
BATCH_ITEM_STATUSES = {"queued", "sending", "succeeded", "already-imported", "failed", "cancelled"}
MAX_AUTOMATIC_ATTEMPTS = 3
SENDING_RECOVERY_GRACE_SECONDS = 10


class GenBoxPushBatchService:
    """Durable, manually-created batches of existing local images.

    A batch deliberately contains only image transfer identities. User prompt
    text and destination credentials remain outside this persisted state.
    """

    def __init__(
        self,
        *,
        state_file: Path = BATCH_FILE,
        push_service: GenBoxPushService | Any = genbox_push_service,
        transfer_coordinator: GenBoxPushTransferCoordinator = genbox_push_transfer_coordinator,
        image_reader: Callable[[str], bytes] = image_storage_service.get_bytes,
        image_exists: Callable[[str], bool] = image_storage_service.exists,
        image_lister: Callable[..., list[dict[str, object]]] = image_storage_service.list_items,
        worker_factory: Callable[..., threading.Thread] = threading.Thread,
        retry_delay_seconds: Callable[[int], float] | None = None,
    ) -> None:
        self.state_file = state_file
        self.push_service = push_service
        self.transfer_coordinator = transfer_coordinator
        self.image_reader = image_reader
        self.image_exists = image_exists
        self.image_lister = image_lister
        self.worker_factory = worker_factory
        self.retry_delay_seconds = retry_delay_seconds or self._default_retry_delay_seconds
        self._lock = ProcessReentrantLock(state_file.with_suffix(state_file.suffix + ".state.lock"))
        self._worker: threading.Thread | None = None
        self._retry_wakeup = threading.Event()

    @staticmethod
    def _clean_path(value: object) -> str:
        return str(value or "").strip().replace("\\", "/").lstrip("/")

    def _item_lock(self, batch_id: str, item_id: str) -> ProcessFileLock:
        return ProcessFileLock(
            self.state_file.with_name(f".{self.state_file.name}.{batch_id}.{item_id}.item.lock")
        )

    @staticmethod
    def _safe_error(_: Exception | None = None) -> str:
        return "GenBox Push failed; the source image was retained. Check the destination and retry failed images."

    @staticmethod
    def _default_retry_delay_seconds(attempts: int) -> float:
        return min(30.0, float(2 ** max(0, attempts - 1))) + random.uniform(0.0, 1.0)

    @staticmethod
    def _retry_due(item: dict[str, Any]) -> bool:
        retry_at = str(item.get("next_retry_at") or "")
        if not retry_at:
            return True
        try:
            return datetime.fromisoformat(retry_at) <= beijing_now()
        except ValueError:
            return True

    def _retry_at(self, attempts: int) -> str:
        return (beijing_now() + timedelta(seconds=self.retry_delay_seconds(attempts))).isoformat()

    @staticmethod
    def _sending_is_stale(item: dict[str, Any]) -> bool:
        try:
            updated_at = datetime.fromisoformat(str(item.get("updated_at") or ""))
        except ValueError:
            return True
        if updated_at.tzinfo is not None:
            updated_at = updated_at.astimezone(beijing_now().tzinfo).replace(tzinfo=None)
        now = beijing_now().replace(tzinfo=None)
        return updated_at <= now - timedelta(seconds=SENDING_RECOVERY_GRACE_SECONDS)

    def _load_locked(self) -> dict[str, dict[str, Any]]:
        raw = read_json_object(self.state_file, name=self.state_file.name)
        source = raw.get("batches") if isinstance(raw.get("batches"), dict) else {}
        batches: dict[str, dict[str, Any]] = {}
        for batch_id, candidate in source.items():
            if not isinstance(candidate, dict) or not isinstance(candidate.get("items"), dict):
                continue
            items = {
                str(item_id): dict(item)
                for item_id, item in candidate["items"].items()
                if isinstance(item, dict) and str(item.get("status") or "") in BATCH_ITEM_STATUSES
            }
            if items:
                batches[str(batch_id)] = {**candidate, "items": items}
        changed = False
        for batch_id, batch in batches.items():
            batch_changed = False
            for item_id, item in batch["items"].items():
                if item.get("status") != "sending" or not self._sending_is_stale(item):
                    continue
                item_lock = self._item_lock(batch_id, item_id)
                if item_lock.acquire(timeout_secs=0):
                    try:
                        item.update({"status": "queued", "updated_at": beijing_now_str()})
                        changed = True
                        batch_changed = True
                    finally:
                        item_lock.release()
            if batch_changed:
                batch["updated_at"] = beijing_now_str()
        if changed:
            self._save_locked(batches)
        return batches

    def _save_locked(self, batches: dict[str, dict[str, Any]]) -> None:
        write_json_file(self.state_file, {"batches": batches})
        for path in (self.state_file, self.state_file.with_suffix(self.state_file.suffix + ".bak")):
            try:
                path.chmod(0o600)
            except OSError:
                pass

    @staticmethod
    def _status(items: Iterable[dict[str, Any]]) -> str:
        statuses = {str(item.get("status") or "") for item in items}
        if not statuses or statuses == {"cancelled"}:
            return "cancelled"
        if "sending" in statuses:
            return "sending"
        if "queued" in statuses:
            return "queued"
        if "failed" in statuses:
            return "failed"
        if "cancelled" in statuses:
            return "cancelled"
        return "succeeded"

    @classmethod
    def _public(cls, batch_id: str, batch: dict[str, Any]) -> dict[str, object]:
        items = list(batch["items"].values())
        public_items = [
            {
                "id": item_id,
                "path": str(item.get("path") or ""),
                "status": str(item.get("status") or "queued"),
                "attempts": max(0, int(item.get("attempts") or 0)),
                "updated_at": str(item.get("updated_at") or ""),
                "error": str(item.get("error") or ""),
                "receipt_status": str(item.get("receipt_status") or ""),
                "retryable": item.get("retryable") is True,
                "next_retry_at": str(item.get("next_retry_at") or ""),
                "source_retained": True,
            }
            for item_id, item in batch["items"].items()
        ]
        return {
            "id": batch_id,
            "status": cls._status(items),
            "created_at": str(batch.get("created_at") or ""),
            "updated_at": str(batch.get("updated_at") or ""),
            "total": len(public_items),
            "queued": sum(item["status"] == "queued" for item in public_items),
            "sending": sum(item["status"] == "sending" for item in public_items),
            "succeeded": sum(item["status"] == "succeeded" for item in public_items),
            "already_imported": sum(item["status"] == "already-imported" for item in public_items),
            "retrying": sum(bool(item["status"] == "queued" and item["next_retry_at"]) for item in public_items),
            "failed": sum(item["status"] == "failed" for item in public_items),
            "cancelled": sum(item["status"] == "cancelled" for item in public_items),
            "items": public_items,
            "source_retained": True,
        }

    def create(self, paths: Iterable[str], *, start_worker: bool = True) -> dict[str, object]:
        normalized: list[str] = []
        for value in paths:
            path = self._clean_path(value)
            if not path:
                raise ValueError("A generated image path is required.")
            if path not in normalized:
                normalized.append(path)
        if not normalized:
            raise ValueError("Select at least one generated image.")

        items: dict[str, dict[str, Any]] = {}
        for path in normalized:
            try:
                if not self.image_exists(path):
                    raise ValueError("missing source")
                source_sha256 = hashlib.sha256(self.image_reader(path)).hexdigest()
            except Exception as exc:
                raise ValueError("A selected image is unavailable; no batch was created.") from exc
            items[uuid.uuid4().hex] = {
                "path": path,
                "source_sha256": source_sha256,
                "status": "queued",
                "attempts": 0,
                "retryable": False,
                "next_retry_at": "",
                "updated_at": beijing_now_str(),
            }

        now = beijing_now_str()
        batch_id = uuid.uuid4().hex
        with self._lock:
            batches = self._load_locked()
            batches[batch_id] = {"created_at": now, "updated_at": now, "items": items}
            self._save_locked(batches)
            if start_worker:
                self._ensure_worker_locked()
            return self._public(batch_id, batches[batch_id])

    def preview_date_range(self, start_date: str, end_date: str) -> dict[str, object]:
        start = str(start_date or "").strip()
        end = str(end_date or "").strip()
        if not start or not end:
            raise ValueError("Choose both a start date and an end date.")
        try:
            if date.fromisoformat(end) < date.fromisoformat(start):
                raise ValueError("The end date must not be earlier than the start date.")
        except ValueError as exc:
            if str(exc).startswith("The end date"):
                raise
            raise ValueError("Use YYYY-MM-DD for the date range.") from exc

        candidates = self.image_lister("", start_date=start, end_date=end, refresh_index=True, verify_existing=True)
        paths: list[str] = []
        skipped = 0
        for item in candidates:
            path = self._clean_path(item.get("path") if isinstance(item, dict) else "")
            if not path or not self.image_exists(path):
                skipped += 1
                continue
            if path not in paths:
                paths.append(path)
        if len(paths) > 200:
            raise ValueError("This date range contains more than 200 images. Narrow the range and try again.")
        return {
            "start_date": start,
            "end_date": end,
            "eligible_count": len(paths),
            "skipped_count": skipped,
            "samples": paths[:5],
            # These are server-indexed relative image paths, not filesystem paths.
            "paths": paths,
        }

    def get(self, batch_id: str) -> dict[str, object] | None:
        with self._lock:
            batch = self._load_locked().get(str(batch_id or "").strip())
            return self._public(str(batch_id).strip(), batch) if batch is not None else None

    def get_latest_recoverable(self) -> dict[str, object] | None:
        """Return the newest active or failed batch for Gallery refresh recovery."""
        with self._lock:
            batches = self._load_locked()
            candidates = [
                (batch_id, batch)
                for batch_id, batch in batches.items()
                if self._status(batch["items"].values()) in {"queued", "sending", "failed"}
            ]
            if not candidates:
                return None
            batch_id, batch = max(
                candidates,
                key=lambda entry: (str(entry[1].get("updated_at") or ""), str(entry[0])),
            )
            return self._public(batch_id, batch)

    def cancel(self, batch_id: str) -> dict[str, object] | None:
        with self._lock:
            batches = self._load_locked()
            batch_id = str(batch_id or "").strip()
            batch = batches.get(batch_id)
            if batch is None:
                return None
            changed = False
            for item in batch["items"].values():
                if item.get("status") == "queued":
                    item.update({"status": "cancelled", "updated_at": beijing_now_str()})
                    changed = True
            if changed:
                batch["updated_at"] = beijing_now_str()
                self._save_locked(batches)
                self._retry_wakeup.set()
            return self._public(batch_id, batch)

    def retry_failed(self, batch_id: str, *, start_worker: bool = True) -> dict[str, object] | None:
        with self._lock:
            batches = self._load_locked()
            batch_id = str(batch_id or "").strip()
            batch = batches.get(batch_id)
            if batch is None:
                return None
            changed = False
            for item in batch["items"].values():
                if item.get("status") == "failed":
                    item.update({
                        "status": "queued",
                        "error": "",
                        "retryable": False,
                        "next_retry_at": "",
                        "updated_at": beijing_now_str(),
                    })
                    changed = True
            if changed:
                batch["updated_at"] = beijing_now_str()
                self._save_locked(batches)
                self._retry_wakeup.set()
                if start_worker:
                    self._ensure_worker_locked()
            return self._public(batch_id, batch)

    def resume(self, *, start_worker: bool = True) -> None:
        with self._lock:
            batches = self._load_locked()
            if start_worker and any(
                item.get("status") in {"queued", "sending"}
                for batch in batches.values()
                for item in batch["items"].values()
            ):
                self._ensure_worker_locked()

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = self.worker_factory(target=self._drain, name="genbox-push-batches", daemon=True)
        self._worker.start()

    def _claim_next(self) -> tuple[str, str, dict[str, Any], ProcessFileLock] | None:
        with self._lock:
            batches = self._load_locked()
            candidates = [
                (batch_id, item_id, item)
                for batch_id, batch in batches.items()
                for item_id, item in batch["items"].items()
                if item.get("status") == "queued" and self._retry_due(item)
            ]
            if not candidates:
                return None
            batch_id, item_id, item = min(candidates, key=lambda entry: str(entry[2].get("updated_at") or ""))
            item_lock = self._item_lock(batch_id, item_id)
            if not item_lock.acquire(timeout_secs=0):
                return None
            try:
                item.update({"status": "sending", "attempts": int(item.get("attempts") or 0) + 1, "updated_at": beijing_now_str()})
                batches[batch_id]["updated_at"] = beijing_now_str()
                self._save_locked(batches)
                return batch_id, item_id, dict(item), item_lock
            except Exception:
                item_lock.release()
                raise

    def _finish(
        self,
        batch_id: str,
        item_id: str,
        *,
        error: str = "",
        receipt_status: str = "",
        retryable: bool = False,
    ) -> None:
        with self._lock:
            batches = self._load_locked()
            batch = batches.get(batch_id)
            if batch is None or item_id not in batch["items"]:
                return
            item = batch["items"][item_id]
            attempts = int(item.get("attempts") or 0)
            retry_later = bool(error) and retryable and attempts < MAX_AUTOMATIC_ATTEMPTS
            status = "queued" if retry_later else "failed" if error else "already-imported" if receipt_status == "already-imported" else "succeeded"
            item.update({
                "status": status,
                "error": error,
                "receipt_status": receipt_status,
                "retryable": retry_later,
                "next_retry_at": self._retry_at(attempts) if retry_later else "",
                "updated_at": beijing_now_str(),
            })
            batch["updated_at"] = beijing_now_str()
            self._save_locked(batches)

    def _drain(self) -> None:
        while True:
            claimed = self._claim_next()
            if claimed is None:
                wait_for_recovery = False
                with self._lock:
                    batches = self._load_locked()
                    pending = any(
                        item.get("status") == "queued"
                        for batch in batches.values()
                        for item in batch["items"].values()
                    )
                    retrying = any(
                        item.get("status") == "queued" and str(item.get("next_retry_at") or "")
                        for batch in batches.values()
                        for item in batch["items"].values()
                    )
                    sending = any(
                        item.get("status") == "sending"
                        for batch in batches.values()
                        for item in batch["items"].values()
                    )
                    if pending and (retrying or sending):
                        self._retry_wakeup.clear()
                        wait_for_recovery = True
                    elif pending:
                        continue
                    elif self._worker is threading.current_thread():
                        self._worker = None
                    else:
                        return
                if wait_for_recovery:
                    self._retry_wakeup.wait(1.0)
                    continue
                return
            batch_id, item_id, item, item_lock = claimed
            try:
                # A gallery item may have changed since the user selected it.
                # Reject that race before joining a shared transfer.
                if hashlib.sha256(self.image_reader(str(item["path"]))).hexdigest() != item["source_sha256"]:
                    raise ValueError("source changed")
                receipt = self.transfer_coordinator.push_image(
                    self.push_service,
                    str(item["path"]),
                    str(item["source_sha256"]),
                )
            except Exception as exc:
                self._finish(
                    batch_id,
                    item_id,
                    error=self._safe_error(exc),
                    retryable=bool(getattr(exc, "retryable", False)),
                )
            else:
                self._finish(batch_id, item_id, receipt_status=str(receipt.get("status") or ""))
            finally:
                item_lock.release()


genbox_push_batch_service = GenBoxPushBatchService()
