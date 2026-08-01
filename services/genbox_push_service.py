from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from curl_cffi import CurlMime, requests

from services.config import DATA_DIR
from services.genbox_push_cleanup import GenBoxPushCleanupService, genbox_push_cleanup_service
from services.image_storage_service import image_storage_service
from services.json_file import read_json_object, write_json_file
from services.source_claim import source_claim


PUSH_CONTRACT_VERSION = "v1"
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SUCCESS_STATUSES = {"imported", "already-imported", "duplicate-local"}
MAX_RECEIPT_BYTES = 1024 * 1024


class GenBoxPushError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class GenBoxPushSettings:
    enabled: bool
    cleanup_enabled: bool
    base_url: str
    source_id: str
    push_key: str
    timeout_secs: int


@dataclass(frozen=True)
class GenBoxPushTransferContext:
    """An in-memory configuration snapshot for one coordinated Push."""

    settings: GenBoxPushSettings
    scope: str


def _clean(value: object) -> str:
    return str(value or "").strip()


def _normalize_base_url(value: object) -> str:
    base_url = _clean(value).rstrip("/")
    if not base_url:
        return ""
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("GenBox 地址必须是有效的 HTTP 或 HTTPS 地址")
    if parsed.query or parsed.fragment:
        raise ValueError("GenBox 地址不能包含查询参数或片段")
    if parsed.params:
        raise ValueError("GenBox address cannot include path parameters")
    push_endpoint = "/api/sync/push"
    path = parsed.path or ""
    if push_endpoint in path and not path.endswith(push_endpoint):
        raise ValueError("GenBox Push address must end with /api/sync/push")
    if path.endswith(push_endpoint):
        base_url = parsed._replace(path=path[: -len(push_endpoint)].rstrip("/")).geturl().rstrip("/")
    return base_url


def _normalize_source_id(value: object) -> str:
    source_id = _clean(value)
    if source_id and not SOURCE_ID_PATTERN.fullmatch(source_id):
        raise ValueError("GenBox 来源标识只能使用字母、数字、点、下划线和连字符")
    return source_id


def _normalize_timeout(value: object, default: int = 20) -> int:
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = default
    return max(5, min(timeout, 120))


class GenBoxPushService:
    """Persist a single GenBox destination without exposing its Push key."""

    def __init__(
        self,
        *,
        settings_file: Path | None = None,
        state_file: Path | None = None,
        image_reader: Callable[[str], bytes] | None = None,
        session_factory: Callable[[], Any] | None = None,
        cleanup_service: GenBoxPushCleanupService | None = None,
    ) -> None:
        self.settings_file = settings_file or DATA_DIR / "genbox_push_settings.json"
        self.state_file = state_file or DATA_DIR / "genbox_push_state.json"
        self.image_reader = image_reader or image_storage_service.get_bytes
        self.session_factory = session_factory or requests.Session
        self.cleanup_service = cleanup_service or (
            genbox_push_cleanup_service
            if settings_file is None and state_file is None
            else GenBoxPushCleanupService(
                state_file=self.state_file.with_name("genbox_push_cleanup.json"),
                audit_file=self.state_file.with_name("genbox_push_cleanup_audit.json"),
                settings_file=self.settings_file,
            )
        )
        self._lock = threading.RLock()

    def _load_settings(self) -> GenBoxPushSettings:
        raw = read_json_object(self.settings_file, name=self.settings_file.name)
        return GenBoxPushSettings(
            enabled=bool(raw.get("enabled", False)),
            cleanup_enabled=raw.get("cleanup_enabled") is True,
            base_url=_normalize_base_url(raw.get("base_url")),
            source_id=_normalize_source_id(raw.get("source_id")),
            push_key=_clean(raw.get("push_key")),
            timeout_secs=_normalize_timeout(raw.get("timeout_secs")),
        )

    @staticmethod
    def _public_settings(settings: GenBoxPushSettings) -> dict[str, object]:
        return {
            "enabled": settings.enabled,
            "cleanup_enabled": settings.cleanup_enabled,
            "base_url": settings.base_url,
            "source_id": settings.source_id,
            "has_push_key": bool(settings.push_key),
            "timeout_secs": settings.timeout_secs,
        }

    def get_settings(self) -> dict[str, object]:
        with self._lock:
            return self._public_settings(self._load_settings())

    def update_settings(self, payload: dict[str, object]) -> dict[str, object]:
        with self._lock:
            current = self._load_settings()
            push_key = current.push_key
            if bool(payload.get("clear_push_key", False)):
                push_key = ""
            elif "push_key" in payload and _clean(payload.get("push_key")):
                push_key = _clean(payload.get("push_key"))
            settings = GenBoxPushSettings(
                enabled=bool(payload.get("enabled", current.enabled)),
                cleanup_enabled=(
                    current.cleanup_enabled
                    if payload.get("cleanup_enabled") is None
                    else bool(payload.get("cleanup_enabled"))
                ),
                base_url=_normalize_base_url(payload.get("base_url", current.base_url)),
                source_id=_normalize_source_id(payload.get("source_id", current.source_id)),
                push_key=push_key,
                timeout_secs=_normalize_timeout(payload.get("timeout_secs", current.timeout_secs)),
            )
            if settings.enabled and (not settings.base_url or not settings.source_id or not settings.push_key):
                raise ValueError("启用 GenBox 推送前必须填写地址、来源标识和推送密钥")
            write_json_file(self.settings_file, {
                "enabled": settings.enabled,
                "cleanup_enabled": settings.cleanup_enabled,
                "base_url": settings.base_url,
                "source_id": settings.source_id,
                "push_key": settings.push_key,
                "timeout_secs": settings.timeout_secs,
            })
            self._restrict_file_permissions(self.settings_file)
            self._restrict_file_permissions(self.settings_file.with_suffix(self.settings_file.suffix + ".bak"))
            return self._public_settings(settings)

    @staticmethod
    def _restrict_file_permissions(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _configured_settings(self) -> GenBoxPushSettings:
        settings = self._load_settings()
        if not settings.enabled:
            raise GenBoxPushError("GenBox 推送尚未启用")
        if not settings.base_url or not settings.source_id or not settings.push_key:
            raise GenBoxPushError("GenBox 推送配置不完整")
        return settings

    @staticmethod
    def _headers(settings: GenBoxPushSettings) -> dict[str, str]:
        return {
            "X-GenBox-Source": settings.source_id,
            "X-GenBox-Key": settings.push_key,
        }

    @staticmethod
    def _response_json(response: Any) -> dict[str, object]:
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)) and len(content) > MAX_RECEIPT_BYTES:
            raise GenBoxPushError("GenBox returned an oversized receipt; the source image was retained.")
        text = getattr(response, "text", None)
        if isinstance(text, str) and len(text.encode("utf-8")) > MAX_RECEIPT_BYTES:
            raise GenBoxPushError("GenBox returned an oversized receipt; the source image was retained.")
        try:
            payload = response.json()
        except Exception as exc:
            raise GenBoxPushError("GenBox 返回了无法识别的响应") from exc
        if not isinstance(payload, dict):
            raise GenBoxPushError("GenBox 返回了无法识别的响应")
        return payload

    @staticmethod
    def _request_error(response: Any) -> GenBoxPushError:
        status = int(getattr(response, "status_code", 0) or 0)
        if status in {401, 403}:
            return GenBoxPushError("GenBox 拒绝了推送身份，请检查来源标识或推送密钥")
        if status in {408, 429} or status >= 500:
            return GenBoxPushError(f"GenBox 暂时不可用（HTTP {status}）", retryable=True)
        if status:
            return GenBoxPushError(f"GenBox 拒绝了本次推送（HTTP {status}）")
        return GenBoxPushError("无法连接到 GenBox")

    @staticmethod
    def _reject_redirect(response: Any) -> None:
        status = int(getattr(response, "status_code", 0) or 0)
        if 300 <= status < 400:
            raise GenBoxPushError("GenBox 地址发生了重定向；为避免密钥泄露，推送已停止")

    def _probe(self, settings: GenBoxPushSettings) -> dict[str, object]:
        session = self.session_factory()
        try:
            response = session.get(
                f"{settings.base_url}/api/sync/push/status",
                headers=self._headers(settings),
                timeout=settings.timeout_secs,
                allow_redirects=False,
            )
        except Exception as exc:
            raise GenBoxPushError("无法连接到 GenBox，请检查私网或地址", retryable=True) from exc
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()
        self._reject_redirect(response)
        if not bool(getattr(response, "ok", False)):
            raise self._request_error(response)
        payload = self._response_json(response)
        if payload.get("ok") is not True or payload.get("contract_version") != PUSH_CONTRACT_VERSION:
            raise GenBoxPushError("GenBox 不支持当前推送协议")
        if payload.get("source_id") != settings.source_id:
            raise GenBoxPushError("GenBox 返回的来源标识不匹配")
        return payload

    def probe(self) -> dict[str, object]:
        with self._lock:
            payload = self._probe(self._configured_settings())
        return {
            "ok": True,
            "contract_version": payload["contract_version"],
            "max_image_bytes": int(payload.get("max_image_bytes") or 0),
        }

    def source_sha256(self, relative_path: str) -> str:
        """Return the current content identity without contacting GenBox."""
        return hashlib.sha256(self.image_reader(relative_path)).hexdigest()

    @staticmethod
    def _transfer_scope_for(settings: GenBoxPushSettings) -> str:
        identity = f"{settings.base_url}\n{settings.source_id}\n{settings.push_key}".encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    def capture_transfer_context(self) -> GenBoxPushTransferContext:
        """Capture the destination used to key and execute one coordinated Push."""
        with self._lock:
            settings = self._configured_settings()
            return GenBoxPushTransferContext(
                settings=settings,
                scope=self._transfer_scope_for(settings),
            )

    def transfer_scope(self) -> str:
        """Use only a one-way, non-secret destination identity for sharing."""
        return self.capture_transfer_context().scope

    @staticmethod
    def _content_type(relative_path: str) -> str:
        suffix = Path(relative_path).suffix.lower()
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(suffix, "image/png")

    def _save_result(self, relative_path: str, result: dict[str, object]) -> None:
        raw = read_json_object(self.state_file, name=self.state_file.name)
        items = raw.get("items") if isinstance(raw.get("items"), dict) else {}
        items[relative_path] = result
        write_json_file(self.state_file, {"items": items})
        self._restrict_file_permissions(self.state_file)
        self._restrict_file_permissions(self.state_file.with_suffix(self.state_file.suffix + ".bak"))

    def push_image(
        self,
        relative_path: str,
        *,
        created_at: str = "",
        prompt: str = "",
        model: str = "",
        expected_sha256: str | None = None,
        _transfer_context: GenBoxPushTransferContext | None = None,
    ) -> dict[str, object]:
        payload = self.image_reader(relative_path)
        digest = hashlib.sha256(payload).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise GenBoxPushError("The source image changed before it could be sent; it was retained.")
        claim = source_claim(relative_path, digest)
        if not claim.acquire(timeout_secs=0):
            raise GenBoxPushError(
                "The source image is busy; it was retained. Retry after the active transfer finishes.",
                retryable=True,
            )
        try:
            return self._push_image_unclaimed(
                relative_path,
                created_at=created_at,
                prompt=prompt,
                model=model,
                expected_sha256=digest,
                _transfer_context=_transfer_context,
            )
        finally:
            claim.release()

    def _push_image_unclaimed(
        self,
        relative_path: str,
        *,
        created_at: str = "",
        prompt: str = "",
        model: str = "",
        expected_sha256: str | None = None,
        _transfer_context: GenBoxPushTransferContext | None = None,
    ) -> dict[str, object]:
        with self._lock:
            settings = self._configured_settings()
            if _transfer_context is not None:
                context_scope = self._transfer_scope_for(_transfer_context.settings)
                if (
                    _transfer_context.scope != context_scope
                    or self._transfer_scope_for(settings) != _transfer_context.scope
                ):
                    raise GenBoxPushError(
                        "The GenBox Push configuration changed before this image could be sent; it was retained. Retry the send."
                    )
                settings = _transfer_context.settings
            payload = self.image_reader(relative_path)
            digest = hashlib.sha256(payload).hexdigest()
            if expected_sha256 is not None and digest != expected_sha256:
                raise GenBoxPushError("The source image changed before it could be sent; it was retained.")
            probe = self._probe(settings)
            max_bytes = int(probe.get("max_image_bytes") or 0)
            if max_bytes and len(payload) > max_bytes:
                raise GenBoxPushError("图片超过 GenBox 当前允许的大小")
            session = self.session_factory()
            multipart = CurlMime()
            multipart.addpart(
                name="image",
                filename=Path(relative_path).name,
                content_type=self._content_type(relative_path),
                data=payload,
            )
            try:
                response = session.post(
                    f"{settings.base_url}/api/sync/push",
                    headers=self._headers(settings),
                    multipart=multipart,
                    data={
                        "remote_path": relative_path,
                        "source_sha256": digest,
                        "created_at": _clean(created_at),
                        "prompt": _clean(prompt),
                        "model": _clean(model),
                    },
                    timeout=settings.timeout_secs,
                    allow_redirects=False,
                )
            except Exception as exc:
                raise GenBoxPushError("图片尚未发送成功，源图已保留", retryable=True) from exc
            finally:
                multipart.close()
                close = getattr(session, "close", None)
                if callable(close):
                    close()
            self._reject_redirect(response)
            if not bool(getattr(response, "ok", False)):
                raise self._request_error(response)
            receipt = self._response_json(response)
            if (
                receipt.get("ok") is not True
                or receipt.get("contract_version") != PUSH_CONTRACT_VERSION
                or receipt.get("source_id") != settings.source_id
                or receipt.get("sha256") != digest
                or receipt.get("status") not in SUCCESS_STATUSES
            ):
                raise GenBoxPushError("GenBox 回执校验失败，源图已保留")
            result = {
                "status": str(receipt["status"]),
                "sha256": digest,
                "safe_to_delete_source": receipt.get("safe_to_delete_source") is True,
                "source_retained": True,
            }
            self._save_result(relative_path, result)
            try:
                self.cleanup_service.record_receipt(
                    destination_scope=self._transfer_scope_for(settings),
                    source_id=settings.source_id,
                    remote_path=relative_path,
                    source_sha256=digest,
                    receipt_status=str(receipt["status"]),
                    safe_to_delete_source=receipt.get("safe_to_delete_source") is True,
                    size_bytes=len(payload),
                )
            except Exception as exc:
                raise GenBoxPushError(
                    "The receipt was accepted but could not be durably recorded; the source was retained."
                ) from exc
            return result


genbox_push_service = GenBoxPushService()
