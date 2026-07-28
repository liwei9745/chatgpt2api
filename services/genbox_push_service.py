from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from curl_cffi import requests

from services.config import DATA_DIR
from services.image_storage_service import image_storage_service
from services.json_file import read_json_object, write_json_file


PUSH_CONTRACT_VERSION = "v1"
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SUCCESS_STATUSES = {"imported", "already-imported", "duplicate-local"}


class GenBoxPushError(RuntimeError):
    pass


@dataclass(frozen=True)
class GenBoxPushSettings:
    enabled: bool
    base_url: str
    source_id: str
    push_key: str
    timeout_secs: int


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
    ) -> None:
        self.settings_file = settings_file or DATA_DIR / "genbox_push_settings.json"
        self.state_file = state_file or DATA_DIR / "genbox_push_state.json"
        self.image_reader = image_reader or image_storage_service.get_bytes
        self.session_factory = session_factory or requests.Session
        self._lock = threading.RLock()

    def _load_settings(self) -> GenBoxPushSettings:
        raw = read_json_object(self.settings_file, name=self.settings_file.name)
        return GenBoxPushSettings(
            enabled=bool(raw.get("enabled", False)),
            base_url=_normalize_base_url(raw.get("base_url")),
            source_id=_normalize_source_id(raw.get("source_id")),
            push_key=_clean(raw.get("push_key")),
            timeout_secs=_normalize_timeout(raw.get("timeout_secs")),
        )

    @staticmethod
    def _public_settings(settings: GenBoxPushSettings) -> dict[str, object]:
        return {
            "enabled": settings.enabled,
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
                base_url=_normalize_base_url(payload.get("base_url", current.base_url)),
                source_id=_normalize_source_id(payload.get("source_id", current.source_id)),
                push_key=push_key,
                timeout_secs=_normalize_timeout(payload.get("timeout_secs", current.timeout_secs)),
            )
            if settings.enabled and (not settings.base_url or not settings.source_id or not settings.push_key):
                raise ValueError("启用 GenBox 推送前必须填写地址、来源标识和推送密钥")
            write_json_file(self.settings_file, {
                "enabled": settings.enabled,
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
            raise GenBoxPushError("无法连接到 GenBox，请检查私网或地址") from exc
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
    ) -> dict[str, object]:
        with self._lock:
            settings = self._configured_settings()
            probe = self._probe(settings)
            payload = self.image_reader(relative_path)
            digest = hashlib.sha256(payload).hexdigest()
            max_bytes = int(probe.get("max_image_bytes") or 0)
            if max_bytes and len(payload) > max_bytes:
                raise GenBoxPushError("图片超过 GenBox 当前允许的大小")
            session = self.session_factory()
            try:
                response = session.post(
                    f"{settings.base_url}/api/sync/push",
                    headers=self._headers(settings),
                    files={"image": (Path(relative_path).name, payload, self._content_type(relative_path))},
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
                raise GenBoxPushError("图片尚未发送成功，源图已保留") from exc
            finally:
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
            return result


genbox_push_service = GenBoxPushService()
