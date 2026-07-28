from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path
from typing import Any

from services.config import DATA_DIR
from services.genbox_push_service import GenBoxPushService, genbox_push_service
from services.image_storage_service import image_storage_service
from services.json_file import read_json_object, write_json_file
from utils.timezone import beijing_now_str


BATCH_FILE = DATA_DIR / "genbox_push_batches.json"
BATCH_ITEM_STATUSES = {"queued", "sending", "succeeded", "failed", "cancelled"}


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
        image_reader: Callable[[str], bytes] = image_storage_service.get_bytes,
        image_exists: Callable[[str], bool] = image_storage_service.exists,
        image_lister: Callable[..., list[dict[str, object]]] = image_storage_service.list_items,
        worker_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        self.state_file = state_file
        self.push_service = push_service
        self.image_reader = image_reader
        self.image_exists = image_exists
        self.image_lister = image_lister
        self.worker_factory = worker_factory
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._recovered = False

    @staticmethod
    def _clean_path(value: object) -> str:
        return str(value or "").strip().replace("\\", "/").lstrip("/")

    @staticmethod
    def _safe_error(_: Exception | None = None) -> str:
        return "GenBox Push failed; the source image was retained. Check the destination and retry failed images."

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
        if not self._recovered:
            self._recovered = True
            changed = False
            for batch in batches.values():
                for item in batch["items"].values():
                    if item.get("status") == "sending":
                        item.update({"status": "queued", "updated_at": beijing_now_str()})
                        changed = True
                if changed:
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
                    item.update({"status": "queued", "error": "", "updated_at": beijing_now_str()})
                    changed = True
            if changed:
                batch["updated_at"] = beijing_now_str()
                self._save_locked(batches)
                if start_worker:
                    self._ensure_worker_locked()
            return self._public(batch_id, batch)

    def resume(self, *, start_worker: bool = True) -> None:
        with self._lock:
            batches = self._load_locked()
            if start_worker and any(
                item.get("status") == "queued"
                for batch in batches.values()
                for item in batch["items"].values()
            ):
                self._ensure_worker_locked()

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = self.worker_factory(target=self._drain, name="genbox-push-batches", daemon=True)
        self._worker.start()

    def _claim_next(self) -> tuple[str, str, dict[str, Any]] | None:
        with self._lock:
            batches = self._load_locked()
            candidates = [
                (batch_id, item_id, item)
                for batch_id, batch in batches.items()
                for item_id, item in batch["items"].items()
                if item.get("status") == "queued"
            ]
            if not candidates:
                return None
            batch_id, item_id, item = min(candidates, key=lambda entry: str(entry[2].get("updated_at") or ""))
            item.update({"status": "sending", "attempts": int(item.get("attempts") or 0) + 1, "updated_at": beijing_now_str()})
            batches[batch_id]["updated_at"] = beijing_now_str()
            self._save_locked(batches)
            return batch_id, item_id, dict(item)

    def _finish(self, batch_id: str, item_id: str, *, error: str = "") -> None:
        with self._lock:
            batches = self._load_locked()
            batch = batches.get(batch_id)
            if batch is None or item_id not in batch["items"]:
                return
            item = batch["items"][item_id]
            item.update({
                "status": "failed" if error else "succeeded",
                "error": error,
                "updated_at": beijing_now_str(),
            })
            batch["updated_at"] = beijing_now_str()
            self._save_locked(batches)

    def _drain(self) -> None:
        while True:
            claimed = self._claim_next()
            if claimed is None:
                with self._lock:
                    batches = self._load_locked()
                    pending = any(
                        item.get("status") == "queued"
                        for batch in batches.values()
                        for item in batch["items"].values()
                    )
                    if pending:
                        continue
                    if self._worker is threading.current_thread():
                        self._worker = None
                return
            batch_id, item_id, item = claimed
            try:
                # A gallery item may have changed since the user selected it.
                # Reject that race rather than send a different source image.
                if hashlib.sha256(self.image_reader(str(item["path"]))).hexdigest() != item["source_sha256"]:
                    raise ValueError("source changed")
                self.push_service.push_image(str(item["path"]))
            except Exception as exc:
                self._finish(batch_id, item_id, error=self._safe_error(exc))
            else:
                self._finish(batch_id, item_id)


genbox_push_batch_service = GenBoxPushBatchService()
