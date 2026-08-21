from __future__ import annotations

import threading
import time
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from services.config import DATA_DIR
from services.genbox_push_cleanup import genbox_push_cleanup_service as _default_cleanup_service
from services.genbox_push_service import GenBoxPushService, genbox_push_service
from services.genbox_push_transfer import GenBoxPushTransferCoordinator, genbox_push_transfer_coordinator
from services.json_file import read_json_object, write_json_file
from services.process_file_lock import ProcessFileLock, ProcessReentrantLock
from utils.timezone import beijing_now_str


OUTBOX_FILE = DATA_DIR / "genbox_push_outbox.json"
OUTBOX_STATUSES = {"queued", "sending", "succeeded", "already-imported", "failed"}


class GenBoxPushOutbox:
    """Persist requested generation Pushes without blocking image storage."""

    def __init__(
        self,
        *,
        state_file: Path = OUTBOX_FILE,
        push_service: GenBoxPushService | Any = genbox_push_service,
        transfer_coordinator: GenBoxPushTransferCoordinator = genbox_push_transfer_coordinator,
        worker_factory: Callable[..., threading.Thread] = threading.Thread,
        cleanup_service: Any = None,
    ) -> None:
        self.state_file = state_file
        self.push_service = push_service
        self.transfer_coordinator = transfer_coordinator
        self.worker_factory = worker_factory
        self.cleanup_service: Any = cleanup_service or _default_cleanup_service
        # The outbox JSON is shared by all application workers.  A thread-only
        # lock allows two processes to claim the same queued item concurrently.
        self._lock = ProcessReentrantLock(state_file.with_suffix(state_file.suffix + ".state.lock"))
        self._worker: threading.Thread | None = None
        # Prompt text is intentionally memory-only. It can enrich an immediate
        # Push but is never written to durable transfer state or normal logs.
        self._metadata: dict[str, dict[str, str]] = {}

    @staticmethod
    def _clean(value: object) -> str:
        return str(value or "").strip()

    @classmethod
    def _entry_id(cls, relative_path: str, source_sha256: str) -> str:
        return f"{relative_path}:{source_sha256}"

    def _item_lock(self, entry_id: str) -> ProcessFileLock:
        lock_id = hashlib.sha256(entry_id.encode("utf-8")).hexdigest()
        return ProcessFileLock(self.state_file.with_name(f".{self.state_file.name}.{lock_id}.item.lock"))

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        del exc
        return "GenBox Push failed; the source image was retained. Check the destination and retry."

    def _load_locked(self) -> dict[str, dict[str, Any]]:
        raw = read_json_object(self.state_file, name=self.state_file.name)
        items = raw.get("items") if isinstance(raw.get("items"), dict) else {}
        items = {
            str(key): dict(value)
            for key, value in items.items()
            if isinstance(value, dict) and str(value.get("status") or "") in OUTBOX_STATUSES
        }
        changed = False
        for entry_id, item in items.items():
            if item.get("status") != "sending":
                continue
            item_lock = self._item_lock(entry_id)
            if not item_lock.acquire(timeout_secs=0):
                continue
            try:
                item.update({"status": "queued", "updated_at": beijing_now_str()})
                changed = True
            finally:
                item_lock.release()
        if changed:
            self._save_locked(items)
        return items

    def _save_locked(self, items: dict[str, dict[str, Any]]) -> None:
        write_json_file(self.state_file, {"items": items})
        try:
            self.state_file.chmod(0o600)
            self.state_file.with_suffix(self.state_file.suffix + ".bak").chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, object]:
        return {
            "status": str(item.get("status") or "queued"),
            "attempts": max(0, int(item.get("attempts") or 0)),
            "updated_at": str(item.get("updated_at") or ""),
            "result": dict(item.get("result") or {}) if isinstance(item.get("result"), dict) else None,
            "error": str(item.get("error") or ""),
            "source_retained": True,
        }

    def enqueue(
        self,
        relative_path: str,
        source_sha256: str,
        *,
        created_at: str = "",
        prompt: str = "",
        model: str = "",
        delete_source_after_push: bool = False,
        start_worker: bool = True,
    ) -> dict[str, object]:
        path = self._clean(relative_path).replace("\\", "/").lstrip("/")
        digest = self._clean(source_sha256).lower()
        if not path or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("Invalid generated image transfer identity.")
        entry_id = self._entry_id(path, digest)
        now = beijing_now_str()
        with self._lock:
            items = self._load_locked()
            item = items.get(entry_id)
            if item is None:
                item = {
                    "path": path,
                    "source_sha256": digest,
                    "created_at": self._clean(created_at),
                    "model": self._clean(model),
                    "delete_source_after_push": bool(delete_source_after_push),
                    "status": "queued",
                    "attempts": 0,
                    "updated_at": now,
                }
                items[entry_id] = item
                self._save_locked(items)
            self._metadata[entry_id] = {
                "prompt": self._clean(prompt),
                "model": self._clean(model),
                "created_at": self._clean(created_at),
            }
            result = self._public(item)
            if start_worker and result["status"] == "queued":
                self._ensure_worker_locked()
            return result

    def status_for_path(self, relative_path: str) -> dict[str, object] | None:
        path = self._clean(relative_path).replace("\\", "/").lstrip("/")
        if not path:
            return None
        with self._lock:
            items = self._load_locked()
            matches = [item for item in items.values() if item.get("path") == path]
            if not matches:
                return None
            latest = max(matches, key=lambda item: str(item.get("updated_at") or ""))
            return self._public(latest)

    def retry_path(self, relative_path: str) -> dict[str, object] | None:
        path = self._clean(relative_path).replace("\\", "/").lstrip("/")
        with self._lock:
            items = self._load_locked()
            matches = [(entry_id, item) for entry_id, item in items.items() if item.get("path") == path]
            if not matches:
                return None
            entry_id, item = max(matches, key=lambda pair: str(pair[1].get("updated_at") or ""))
            if item.get("status") != "failed":
                return None
            item.update({"status": "queued", "error": "", "updated_at": beijing_now_str()})
            items[entry_id] = item
            self._save_locked(items)
            self._ensure_worker_locked()
            return self._public(item)

    def resume(self, *, start_worker: bool = True) -> None:
        """Recover interrupted transfers and resume any durable queued work."""
        with self._lock:
            items = self._load_locked()
            if start_worker and any(item.get("status") == "queued" for item in items.values()):
                self._ensure_worker_locked()

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = self.worker_factory(target=self._drain, name="genbox-push-outbox", daemon=True)
        self._worker.start()

    def _claim_next(self) -> tuple[str, dict[str, Any], ProcessFileLock] | None:
        with self._lock:
            items = self._load_locked()
            candidates = sorted([
                (entry_id, item)
                for entry_id, item in items.items()
                if item.get("status") == "queued"
            ], key=lambda pair: str(pair[1].get("updated_at") or ""))
            for entry_id, item in candidates:
                item_lock = self._item_lock(entry_id)
                if not item_lock.acquire(timeout_secs=0):
                    continue
                try:
                    current = items.get(entry_id)
                    if current is None or current.get("status") != "queued":
                        item_lock.release()
                        continue
                    current.update({
                        "status": "sending",
                        "attempts": int(current.get("attempts") or 0) + 1,
                        "updated_at": beijing_now_str(),
                    })
                    items[entry_id] = current
                    self._save_locked(items)
                    return entry_id, dict(current), item_lock
                except Exception:
                    item_lock.release()
                    raise
            return None

    def _finish(self, entry_id: str, *, result: dict[str, object] | None = None, error: str = "") -> None:
        with self._lock:
            items = self._load_locked()
            item = items.get(entry_id)
            if item is None:
                self._metadata.pop(entry_id, None)
                return
            item.update({
                "status": (
                    "already-imported"
                    if result is not None and str(result.get("status") or "") in {"already-imported", "duplicate-local"}
                    else "succeeded" if result is not None else "failed"
                ),
                "updated_at": beijing_now_str(),
                "error": error,
            })
            if result is not None:
                item["result"] = {
                    "status": str(result.get("status") or ""),
                    "sha256": str(result.get("sha256") or ""),
                    "safe_to_delete_source": result.get("safe_to_delete_source") is True,
                    "source_retained": True,
                }
            items[entry_id] = item
            self._save_locked(items)
            # The prompt is only needed by the immediate outbound request. Do
            # not retain it in process memory after a terminal outcome.
            self._metadata.pop(entry_id, None)
        if (
            error == ""
            and result is not None
            and bool(item.get("delete_source_after_push"))
            and result.get("safe_to_delete_source") is True
            and str(item.get("status") or "") == "succeeded"
        ):
            record_key = str((result or {}).get("record_key") or "")
            if record_key:
                try:
                    self.cleanup_service.delete_selected({record_key})
                except Exception:
                    # Per-action delete is best-effort after a confirmed push;
                    # failure keeps the source and never affects the outbox item.
                    pass

    def _drain(self) -> None:
        while True:
            try:
                claimed = self._claim_next()
            except RuntimeError:
                # Another application process is updating the durable outbox.
                # Keep the worker alive and let that short critical section end.
                time.sleep(0.05)
                continue
            if claimed is None:
                # Clear the worker reference while holding the same lock used by
                # enqueue(). This closes the gap where a newly queued item could
                # otherwise be left behind as this worker exits.
                with self._lock:
                    items = self._load_locked()
                    if any(item.get("status") == "queued" for item in items.values()):
                        continue
                    if self._worker is threading.current_thread():
                        self._worker = None
                return
            entry_id, item, item_lock = claimed
            metadata = self._metadata.get(entry_id, {})
            try:
                result = self.transfer_coordinator.push_image(
                    self.push_service,
                    str(item["path"]),
                    str(item["source_sha256"]),
                    created_at=metadata.get("created_at") or str(item.get("created_at") or ""),
                    prompt=metadata.get("prompt", ""),
                    model=metadata.get("model") or str(item.get("model") or ""),
                )
            except Exception as exc:
                self._finish(entry_id, error=self._safe_error(exc))
            else:
                self._finish(entry_id, result=result)
            finally:
                item_lock.release()


genbox_push_outbox = GenBoxPushOutbox()
