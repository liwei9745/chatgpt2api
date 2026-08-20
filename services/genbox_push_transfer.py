from __future__ import annotations

import threading
import hmac
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.genbox_push_service import GenBoxPushError


@dataclass
class _Transfer:
    completed: threading.Event = field(default_factory=threading.Event)
    metadata_fingerprint: str = ""
    metadata_is_rich: bool = False
    waiters: int = 0
    result: dict[str, object] | None = None
    error: GenBoxPushError | None = None


class GenBoxPushTransferCoordinator:
    """Share one in-process physical Push for a matching in-flight request."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._inflight: dict[str, _Transfer] = {}
        self._metadata_salt = secrets.token_bytes(32)

    @staticmethod
    def normalize_relative_path(value: object) -> str:
        path = str(value or "").strip().replace("\\", "/").lstrip("/")
        if not path:
            raise GenBoxPushError("A generated image path is required.")
        parts = Path(path).parts
        if any(part in {"", ".", ".."} for part in parts):
            raise GenBoxPushError("The generated image path is invalid.")
        return Path(*parts).as_posix()

    @staticmethod
    def _capture_context(push_service: Any) -> tuple[object | None, str]:
        capture = getattr(push_service, "capture_transfer_context", None)
        if callable(capture):
            context = capture()
            scope = str(getattr(context, "scope", ""))
            if not scope:
                raise GenBoxPushError("The GenBox Push destination is unavailable; retry after checking its configuration.")
            return context, scope

        scope = getattr(push_service, "transfer_scope", None)
        if callable(scope):
            destination_scope = str(scope())
        else:
            # Injected test services have no non-secret destination identity.
            # Their object identity still lets all local entry points share one
            # coordinator without claiming a cross-instance guarantee.
            destination_scope = f"service:{id(push_service)}"
        return None, destination_scope

    @staticmethod
    def _safe_failure(*, retryable: bool = False) -> GenBoxPushError:
        return GenBoxPushError(
            "GenBox Push failed; the source image was retained. Check the destination and retry.",
            retryable=retryable,
        )

    def _metadata_fingerprint(self, *, created_at: str, prompt: str, model: str) -> tuple[str, bool]:
        metadata = "\n".join((created_at, prompt, model))
        return hmac.digest(self._metadata_salt, metadata.encode("utf-8"), "sha256").hex(), bool(
            created_at or prompt or model
        )

    @staticmethod
    def _metadata_conflict() -> GenBoxPushError:
        return GenBoxPushError(
            "This image is already being sent with different metadata. It was retained; retry after the other send finishes."
        )

    def push_image(
        self,
        push_service: Any,
        relative_path: str,
        source_sha256: str,
        *,
        created_at: str = "",
        prompt: str = "",
        model: str = "",
    ) -> dict[str, object]:
        """Send once or reuse an in-process confirmed result for this content."""
        path = self.normalize_relative_path(relative_path)
        digest = str(source_sha256 or "").strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise GenBoxPushError("The generated image content identity is invalid.")
        fingerprint, metadata_is_rich = self._metadata_fingerprint(
            created_at=created_at,
            prompt=prompt,
            model=model,
        )
        context, destination_scope = self._capture_context(push_service)
        key = "\n".join((destination_scope, path, digest))
        owner = False
        with self._lock:
            transfer = self._inflight.get(key)
            if transfer is None:
                transfer = _Transfer(
                    metadata_fingerprint=fingerprint,
                    metadata_is_rich=metadata_is_rich,
                )
                self._inflight[key] = transfer
                owner = True

        if not owner:
            with self._lock:
                if transfer.metadata_fingerprint != fingerprint and metadata_is_rich:
                    raise self._metadata_conflict()
                transfer.waiters += 1
            try:
                transfer.completed.wait()
                if transfer.result is not None:
                    return dict(transfer.result)
                raise transfer.error or self._safe_failure()
            finally:
                with self._lock:
                    transfer.waiters -= 1

        try:
            kwargs: dict[str, object] = {
                "created_at": created_at,
                "prompt": prompt,
                "model": model,
                "expected_sha256": digest,
            }
            if context is not None:
                kwargs["_transfer_context"] = context
            result = push_service.push_image(path, **kwargs)
            if str(result.get("sha256") or "").lower() != digest:
                raise self._safe_failure()
            transfer.result = {
                "status": str(result.get("status") or ""),
                "sha256": digest,
                "safe_to_delete_source": result.get("safe_to_delete_source") is True,
                "record_key": str(result.get("record_key") or ""),
                "source_retained": True,
            }
            return dict(transfer.result)
        except GenBoxPushError as exc:
            transfer.error = self._safe_failure(retryable=exc.retryable)
            raise transfer.error
        except Exception:
            transfer.error = self._safe_failure()
            raise transfer.error
        finally:
            transfer.completed.set()
            # Failures are not cached, so a later retry owns a new physical send.
            with self._lock:
                if self._inflight.get(key) is transfer:
                    self._inflight.pop(key, None)


genbox_push_transfer_coordinator = GenBoxPushTransferCoordinator()
