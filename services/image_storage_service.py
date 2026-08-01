from __future__ import annotations

import hashlib
import io
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import quote, urlparse

from curl_cffi import requests
from fastapi import HTTPException
from PIL import Image

from services.config import DATA_DIR, config
from services.json_file import read_json_object, write_json_file
from utils.timezone import beijing_datetime_from_timestamp, beijing_now, beijing_now_str

IMAGE_INDEX_FILE = DATA_DIR / "image_index.json"
IMAGE_INDEX_LOCK = Lock()
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


class ImageStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredImage:
    rel: str
    url: str
    storage: str
    size: int


@dataclass(frozen=True)
class VerifiedDeleteResult:
    status: str
    reason: str
    size_bytes: int = 0


@dataclass
class _OpenedCleanupTarget:
    relative_path: str
    candidate: Path
    descriptor: int
    parent_descriptor: int | None
    parent_name: str
    file_stat: os.stat_result
    digest: str
    size_bytes: int


def _clean(value: object) -> str:
    return str(value or "").strip()


def _now_iso() -> str:
    return beijing_now_str()


def _mtime_date(path: Path) -> str:
    return beijing_datetime_from_timestamp(path.stat().st_mtime).strftime("%Y-%m-%d")


def _mtime_datetime(path: Path) -> str:
    return beijing_datetime_from_timestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")


def _safe_relative_path(path: str) -> str:
    value = str(path or "").strip().replace("\\", "/").lstrip("/")
    if not value:
        raise HTTPException(status_code=404, detail="image not found")
    parts = Path(value).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise HTTPException(status_code=404, detail="image not found")
    return Path(*parts).as_posix()


def _cleanup_relative_path(path: object) -> str:
    """Validate the already-normalized path used by destructive cleanup.

    The normal image API accepts a few browser-friendly aliases. Cleanup must
    be stricter: an absolute path, alternate separator, traversal component, or
    empty component is never an acceptable deletion target.
    """
    value = str(path or "")
    if not value or "\\" in value or "\x00" in value or value.startswith("/"):
        raise ImageStorageError("cleanup path is not a normalized relative path")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ImageStorageError("cleanup path is not a normalized relative path")
    candidate = Path(value)
    if candidate.is_absolute() or candidate.as_posix() != value:
        raise ImageStorageError("cleanup path is not a normalized relative path")
    return value


def _is_filesystem_alias(file_stat: os.stat_result) -> bool:
    if stat.S_ISLNK(file_stat.st_mode):
        return True
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    attributes = int(getattr(file_stat, "st_file_attributes", 0) or 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _same_cleanup_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return bool(
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and not _is_filesystem_alias(left)
        and not _is_filesystem_alias(right)
        and int(getattr(left, "st_nlink", 1) or 1) == 1
        and int(getattr(right, "st_nlink", 1) or 1) == 1
    )


def _image_dimensions(payload: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            return image.size
    except Exception:
        return None


def _is_image_rel(path: str) -> bool:
    try:
        safe_rel = _safe_relative_path(path)
    except HTTPException:
        return False
    return Path(safe_rel).suffix.lower() in IMAGE_EXTENSIONS


def _local_image_path(relative_path: str) -> Path:
    rel = _safe_relative_path(relative_path)
    root = config.images_dir.resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="image not found") from exc
    return path


def _read_json_object(path: Path) -> dict[str, object]:
    data = read_json_object(path, name=path.name)
    return data if isinstance(data, dict) else {}


def _write_json_object(path: Path, data: dict[str, object]) -> None:
    write_json_file(path, data)


class WebDAVClient:
    def __init__(self, settings: dict[str, object]):
        self.url = _clean(settings.get("webdav_url")).rstrip("/")
        self.username = _clean(settings.get("webdav_username"))
        self.password = _clean(settings.get("webdav_password"))
        self.root_path = _clean(settings.get("webdav_root_path")).strip("/")
        self.session = requests.Session()

    def _auth_kwargs(self) -> dict[str, object]:
        return {"auth": (self.username, self.password)} if self.username or self.password else {}

    def _request(self, method: str, url: str, **kwargs):
        response = self.session.request(method, url, timeout=30, **self._auth_kwargs(), **kwargs)
        if response.status_code >= 400 and not (method == "MKCOL" and response.status_code in {405}):
            raise ImageStorageError(f"WebDAV {method} failed: HTTP {response.status_code}")
        return response

    def remote_url(self, rel: str = "") -> str:
        parts = [part for part in [self.root_path, _safe_relative_path(rel) if rel else ""] if part]
        encoded = "/".join(quote(part, safe="") for item in parts for part in item.split("/") if part)
        return f"{self.url}/{encoded}" if encoded else self.url

    def ensure_dirs(self, rel: str) -> None:
        parts = [part for part in [self.root_path, Path(_safe_relative_path(rel)).parent.as_posix()] if part and part != "."]
        current = self.url
        for item in "/".join(parts).split("/"):
            if not item:
                continue
            current = f"{current}/{quote(item, safe='')}"
            response = self.session.request("MKCOL", current, timeout=30, **self._auth_kwargs())
            if response.status_code in {201, 405}:
                continue
            if response.status_code >= 400:
                raise ImageStorageError(f"WebDAV MKCOL failed: HTTP {response.status_code}")

    def put(self, rel: str, payload: bytes, content_type: str = "image/png") -> str:
        self.ensure_dirs(rel)
        url = self.remote_url(rel)
        self._request("PUT", url, data=payload, headers={"Content-Type": content_type})
        return url

    def get(self, rel: str) -> bytes:
        response = self._request("GET", self.remote_url(rel))
        return bytes(response.content)

    def delete(self, rel: str) -> bool:
        response = self.session.request("DELETE", self.remote_url(rel), timeout=30, **self._auth_kwargs())
        if response.status_code in {200, 202, 204, 404}:
            return response.status_code != 404
        raise ImageStorageError(f"WebDAV DELETE failed: HTTP {response.status_code}")

    def test(self) -> dict[str, object]:
        if not self.url:
            return {"ok": False, "status": 0, "error": "WebDAV URL is required"}
        if urlparse(self.url).scheme not in {"http", "https"}:
            return {"ok": False, "status": 0, "error": "invalid WebDAV URL"}
        test_rel = ".chatgpt2api_webdav_test.txt"
        try:
            self.put(test_rel, b"chatgpt2api webdav test\n", content_type="text/plain")
            self.delete(test_rel)
            return {"ok": True, "status": 200, "error": None}
        except ImageStorageError as exc:
            return {"ok": False, "status": 0, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "status": 0, "error": str(exc) or exc.__class__.__name__}
        finally:
            self.session.close()


class ImageStorageService:
    def __init__(self, index_file: Path = IMAGE_INDEX_FILE):
        self.index_file = index_file
        self._index_lock = IMAGE_INDEX_LOCK

    def settings(self) -> dict[str, object]:
        return config.get_image_storage_settings()

    def local_root(self) -> Path:
        return config.images_dir

    def mode(self) -> str:
        return _clean(self.settings().get("mode")) or "local"

    def _load_index(self) -> dict[str, dict[str, object]]:
        raw = _read_json_object(self.index_file)
        items = raw.get("items")
        if not isinstance(items, dict):
            return {}
        return {str(key): value for key, value in items.items() if isinstance(value, dict)}

    def _load_clean_index(self) -> dict[str, dict[str, object]]:
        items = self._load_index()
        return {rel: item for rel, item in items.items() if _is_image_rel(rel)}

    def _save_index(self, items: dict[str, dict[str, object]]) -> None:
        _write_json_object(self.index_file, {"items": items})

    def _public_url(self, rel: str, base_url: str | None = None) -> str:
        settings = self.settings()
        public_base_url = _clean(settings.get("public_base_url"))
        if public_base_url:
            return f"{public_base_url.rstrip('/')}/{_safe_relative_path(rel)}"
        return f"{(base_url or config.base_url).rstrip('/')}/images/{_safe_relative_path(rel)}"

    def make_relative_path(self, image_data: bytes) -> str:
        file_hash = hashlib.md5(image_data).hexdigest()
        filename = f"{int(time.time())}_{file_hash}.png"
        now = beijing_now()
        relative_dir = Path(now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"))
        return f"{relative_dir.as_posix()}/{filename}"

    def save(self, image_data: bytes, base_url: str | None = None) -> StoredImage:
        config.cleanup_old_images()
        rel = self.make_relative_path(image_data)
        mode = self.mode()
        if mode not in {"local", "webdav", "both"}:
            mode = "local"
        stored_local = False
        stored_webdav = False
        remote_url = ""

        if mode in {"local", "both"}:
            path = _local_image_path(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(image_data)
            stored_local = True

        if mode in {"webdav", "both"}:
            remote_url = WebDAVClient(self.settings()).put(rel, image_data)
            stored_webdav = True

        dimensions = _image_dimensions(image_data)
        item = {
            "rel": rel,
            "path": rel,
            "name": Path(rel).name,
            "date": "-".join(rel.split("/")[:3]),
            "size": len(image_data),
            "created_at": _now_iso(),
            "storage": "both" if stored_local and stored_webdav else ("webdav" if stored_webdav else "local"),
            "local": stored_local,
            "webdav": stored_webdav,
            "remote_url": remote_url,
        }
        if dimensions:
            item["width"], item["height"] = dimensions
        with self._index_lock:
            items = self._load_clean_index()
            items[rel] = item
            self._save_index(items)
        return StoredImage(rel=rel, url=self._public_url(rel, base_url), storage=str(item["storage"]), size=len(image_data))

    def get_bytes(self, rel: str) -> bytes:
        safe_rel = _safe_relative_path(rel)
        if not _is_image_rel(safe_rel):
            raise HTTPException(status_code=404, detail="image not found")
        path = _local_image_path(safe_rel)
        if path.is_file():
            return path.read_bytes()
        item = self._load_clean_index().get(safe_rel, {})
        if item.get("webdav"):
            return WebDAVClient(self.settings()).get(safe_rel)
        raise HTTPException(status_code=404, detail="image not found")

    def exists(self, rel: str) -> bool:
        safe_rel = _safe_relative_path(rel)
        if not _is_image_rel(safe_rel):
            return False
        if _local_image_path(safe_rel).is_file():
            return True
        item = self._load_clean_index().get(safe_rel, {})
        return bool(item.get("webdav"))

    def has_local(self, rel: str) -> bool:
        safe_rel = _safe_relative_path(rel)
        return _is_image_rel(safe_rel) and _local_image_path(safe_rel).is_file()

    def list_items(
        self,
        base_url: str,
        start_date: str = "",
        end_date: str = "",
        *,
        refresh_index: bool = True,
        verify_existing: bool = True,
    ) -> list[dict[str, object]]:
        with self._index_lock:
            indexed = self._load_clean_index()
            root = config.images_dir
            changed = False
            if refresh_index:
                for path in root.rglob("*"):
                    if not path.is_file() or not _is_image_rel(path.name):
                        continue
                    rel = path.relative_to(root).as_posix()
                    if rel in indexed:
                        continue
                    dimensions = None
                    try:
                        dimensions = _image_dimensions(path.read_bytes())
                    except Exception:
                        dimensions = None
                    indexed[rel] = {
                        "rel": rel,
                        "path": rel,
                        "name": path.name,
                        "date": "-".join(rel.split("/")[:3]) if len(rel.split("/")) >= 4 else _mtime_date(path),
                        "size": path.stat().st_size,
                        "created_at": _mtime_datetime(path),
                        "storage": "local",
                        "local": True,
                        "webdav": False,
                        **({"width": dimensions[0], "height": dimensions[1]} if dimensions else {}),
                    }
                    changed = True

            items: list[dict[str, object]] = []
            for rel, item in list(indexed.items()):
                if not _is_image_rel(rel):
                    indexed.pop(rel, None)
                    changed = True
                    continue
                if verify_existing:
                    local = _local_image_path(rel).is_file()
                    webdav = bool(item.get("webdav"))
                    if not local and not webdav:
                        indexed.pop(rel, None)
                        changed = True
                        continue
                    storage = "both" if local and webdav else ("webdav" if webdav else "local")
                    if item.get("local") != local or item.get("storage") != storage:
                        item = {
                            **item,
                            "local": local,
                            "storage": storage,
                        }
                        indexed[rel] = item
                        changed = True
                day = str(item.get("date") or "")
                if start_date and day < start_date:
                    continue
                if end_date and day > end_date:
                    continue
                items.append({
                    **item,
                    "rel": rel,
                    "path": rel,
                    "url": self._public_url(rel, base_url),
                })
            if changed:
                self._save_index(indexed)
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return items

    @staticmethod
    def _read_open_digest(descriptor: int) -> str:
        hasher = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return hasher.hexdigest()
            hasher.update(chunk)

    @staticmethod
    def _open_cleanup_descriptor(candidate: Path) -> int:
        if os.name != "nt":
            nofollow = int(getattr(os, "O_NOFOLLOW", 0))
            return os.open(candidate, os.O_RDONLY | nofollow)

        # Python's O_TEMPORARY enables delete sharing by marking the file for
        # deletion on close, which would violate retention on a failed check.
        # Use CreateFileW instead: share DELETE without delete-on-close, and
        # reject a final reparse point at open time.
        import ctypes
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            str(candidate),
            0x80010000,  # GENERIC_READ | DELETE
            0x00000007,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
            None,
            3,  # OPEN_EXISTING
            0x00200080,  # FILE_FLAG_OPEN_REPARSE_POINT | FILE_ATTRIBUTE_NORMAL
            None,
        )
        invalid = ctypes.c_void_p(-1).value
        if handle in {None, invalid}:
            error = ctypes.get_last_error()
            raise OSError(error, "CreateFileW failed")
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDONLY | int(getattr(os, "O_BINARY", 0)))
        except Exception:
            kernel32.CloseHandle(handle)
            raise

    @staticmethod
    def _cleanup_stat(target: _OpenedCleanupTarget) -> os.stat_result:
        if target.parent_descriptor is not None:
            return os.stat(target.parent_name, dir_fd=target.parent_descriptor, follow_symlinks=False)
        return target.candidate.lstat()

    @staticmethod
    def _close_cleanup_target(target: _OpenedCleanupTarget) -> None:
        try:
            os.close(target.descriptor)
        finally:
            if target.parent_descriptor is not None:
                os.close(target.parent_descriptor)

    def _open_verified_cleanup_target(
        self,
        rel: str,
        expected_sha256: str,
        *,
        expected_size: int | None = None,
    ) -> _OpenedCleanupTarget | dict[str, object]:
        """Open and hash a source while retaining its identity handle.

        The returned handle and its parent directory descriptor stay open until
        the caller completes the final identity check and unlink. This closes
        the cooperative Push/cleanup race and gives POSIX callers a stable
        directory anchor for the unlink operation.
        """
        try:
            safe_rel = _cleanup_relative_path(rel)
        except ImageStorageError as exc:
            return {"ok": False, "reason": "path-invalid", "detail": str(exc)}
        digest = str(expected_sha256 or "").strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            return {"ok": False, "reason": "hash-invalid"}
        root = Path(self.local_root())
        descriptor: int | None = None
        parent_descriptor: int | None = None
        try:
            root_stat = root.lstat()
            if not stat.S_ISDIR(root_stat.st_mode) or _is_filesystem_alias(root_stat):
                return {"ok": False, "reason": "storage-root-invalid"}
            candidate = root / safe_rel
            relative_check = candidate.relative_to(root)
            if relative_check.as_posix() != safe_rel:
                return {"ok": False, "reason": "path-outside-root"}
            current = root
            components = Path(safe_rel).parts
            file_stat = None
            for index, component in enumerate(components):
                current = current / component
                component_stat = current.lstat()
                if _is_filesystem_alias(component_stat):
                    return {"ok": False, "reason": "path-alias"}
                if index < len(components) - 1 and not stat.S_ISDIR(component_stat.st_mode):
                    return {"ok": False, "reason": "source-missing"}
                file_stat = component_stat
        except (OSError, ValueError):
            return {"ok": False, "reason": "source-missing"}
        if file_stat is None:
            return {"ok": False, "reason": "source-missing"}
        if not stat.S_ISREG(file_stat.st_mode):
            return {"ok": False, "reason": "source-not-regular"}
        if int(getattr(file_stat, "st_nlink", 1) or 1) != 1:
            return {"ok": False, "reason": "path-alias"}
        if expected_size is not None and int(file_stat.st_size) != int(expected_size):
            return {"ok": False, "reason": "source-changed", "size_bytes": int(file_stat.st_size)}
        try:
            descriptor = self._open_cleanup_descriptor(candidate)
            opened_stat = os.fstat(descriptor)
            if not _same_cleanup_identity(opened_stat, file_stat):
                os.close(descriptor)
                descriptor = None
                return {"ok": False, "reason": "source-changed"}
            actual = self._read_open_digest(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
        except FileNotFoundError:
            if descriptor is not None:
                os.close(descriptor)
            return {"ok": False, "reason": "source-missing"}
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            return {"ok": False, "reason": "source-unreadable"}
        if actual != digest:
            os.close(descriptor)
            return {"ok": False, "reason": "source-changed", "size_bytes": int(file_stat.st_size)}

        # Keep a parent directory descriptor where the platform supports it;
        # unlinking through that descriptor avoids a root-path replacement.
        if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
            try:
                parent_descriptor = os.open(
                    candidate.parent,
                    os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | nofollow,
                )
            except OSError:
                os.close(descriptor)
                return {"ok": False, "reason": "source-unreadable"}

        target = _OpenedCleanupTarget(
            relative_path=safe_rel,
            candidate=candidate,
            descriptor=descriptor,
            parent_descriptor=parent_descriptor,
            parent_name=candidate.name,
            file_stat=file_stat,
            digest=actual,
            size_bytes=int(file_stat.st_size),
        )
        try:
            if not _same_cleanup_identity(self._cleanup_stat(target), file_stat):
                self._close_cleanup_target(target)
                return {"ok": False, "reason": "source-changed"}
        except OSError:
            self._close_cleanup_target(target)
            return {"ok": False, "reason": "source-changed"}
        return target

    def verify_local_identity(
        self,
        rel: str,
        expected_sha256: str,
        *,
        expected_size: int | None = None,
    ) -> dict[str, object]:
        """Inspect one local source without following aliases."""
        target = self._open_verified_cleanup_target(rel, expected_sha256, expected_size=expected_size)
        if isinstance(target, dict):
            return target
        try:
            return {
                "ok": True,
                "relative_path": target.relative_path,
                "sha256": target.digest,
                "size_bytes": target.size_bytes,
                "stat": target.file_stat,
            }
        finally:
            self._close_cleanup_target(target)

    def _before_verified_unlink(self, _target: _OpenedCleanupTarget) -> None:
        """Test seam for deterministic replacement-race coverage."""

    def _unlink_open_cleanup_target(self, target: _OpenedCleanupTarget) -> VerifiedDeleteResult:
        try:
            os.lseek(target.descriptor, 0, os.SEEK_SET)
            current_digest = self._read_open_digest(target.descriptor)
            os.lseek(target.descriptor, 0, os.SEEK_SET)
        except OSError:
            return VerifiedDeleteResult("retained", "source-unreadable", target.size_bytes)
        if current_digest != target.digest:
            return VerifiedDeleteResult("retained", "source-changed", target.size_bytes)
        try:
            latest_stat = self._cleanup_stat(target)
        except FileNotFoundError:
            return VerifiedDeleteResult("retained", "source-missing", target.size_bytes)
        except OSError:
            return VerifiedDeleteResult("retained", "source-changed", target.size_bytes)
        if not _same_cleanup_identity(latest_stat, target.file_stat):
            return VerifiedDeleteResult("retained", "source-changed", target.size_bytes)
        try:
            if os.name == "nt":
                # Delete the exact opened file handle rather than resolving the
                # path again. This prevents a replacement path from becoming
                # the object that receives the delete request.
                import ctypes
                import msvcrt

                class _FileDispositionInfo(ctypes.Structure):
                    _fields_ = [("DeleteFile", ctypes.c_ubyte)]

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                set_file_info = kernel32.SetFileInformationByHandle
                set_file_info.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    ctypes.c_uint32,
                ]
                set_file_info.restype = ctypes.c_int
                info = _FileDispositionInfo(1)
                handle = ctypes.c_void_p(msvcrt.get_osfhandle(target.descriptor))
                if not set_file_info(handle, 4, ctypes.byref(info), ctypes.sizeof(info)):
                    error = ctypes.get_last_error()
                    raise OSError(error, "SetFileInformationByHandle failed")
            elif target.parent_descriptor is not None:
                os.unlink(target.parent_name, dir_fd=target.parent_descriptor)
            else:
                target.candidate.unlink()
        except FileNotFoundError:
            return VerifiedDeleteResult("retained", "source-missing", target.size_bytes)
        except OSError:
            return VerifiedDeleteResult("delete_failed", "unlink-failed", target.size_bytes)
        return VerifiedDeleteResult("deleted", "deleted", target.size_bytes)

    def delete_verified_local(
        self,
        rel: str,
        expected_sha256: str,
        *,
        expected_size: int | None = None,
        claim_held: bool = False,
    ) -> VerifiedDeleteResult:
        """Delete one unchanged local source after storage-owned checks.

        The cleanup service holds the shared source claim through its durable
        terminal record. Direct callers get the same claim automatically.
        WebDAV-only entries are retained: this primitive is deliberately local
        to the sender's image root and never turns a cleanup request into an
        unrelated remote delete.
        """
        from services.source_claim import source_claim

        safe_rel = str(rel or "")
        lock = None
        if not claim_held:
            lock = source_claim(safe_rel, str(expected_sha256 or "").strip().lower())
            if not lock.acquire(timeout_secs=0):
                return VerifiedDeleteResult("retained", "source-busy")
        target: _OpenedCleanupTarget | None = None
        try:
            opened = self._open_verified_cleanup_target(
                safe_rel,
                expected_sha256,
                expected_size=expected_size,
            )
            if isinstance(opened, dict):
                return VerifiedDeleteResult(
                    "retained",
                    str(opened.get("reason") or "source-unverified"),
                    int(opened.get("size_bytes") or 0),
                )
            target = opened
            # The handle remains open through the final identity check and
            # unlink. The hook exists only to make replacement races
            # deterministic in focused storage tests.
            self._before_verified_unlink(target)
            deletion = self._unlink_open_cleanup_target(target)
            if deletion.status != "deleted":
                return deletion
            try:
                with self._index_lock:
                    items = self._load_clean_index()
                    item = items.get(safe_rel)
                    if item is not None:
                        if item.get("webdav"):
                            items[safe_rel] = {
                                **item,
                                "local": False,
                                "storage": "webdav",
                            }
                        else:
                            items.pop(safe_rel, None)
                        self._save_index(items)
            except Exception:
                # The source is already gone, but the durable cleanup record
                # remains in ``deleting`` so recovery can report ambiguity.
                return VerifiedDeleteResult("delete_unknown", "index-write-failed", target.size_bytes)
            return deletion
        finally:
            if target is not None:
                self._close_cleanup_target(target)
            if lock is not None:
                lock.release()

    def delete_verified(
        self,
        rel: str,
        expected_sha256: str,
        *,
        expected_size: int | None = None,
        claim_held: bool = False,
    ) -> VerifiedDeleteResult:
        """Compatibility name for the storage-owned cleanup primitive."""
        return self.delete_verified_local(
            rel,
            expected_sha256,
            expected_size=expected_size,
            claim_held=claim_held,
        )

    def delete(self, rel: str) -> bool:
        safe_rel = _safe_relative_path(rel)
        removed = False
        path = _local_image_path(safe_rel)
        if path.is_file():
            path.unlink()
            removed = True
        with self._index_lock:
            items = self._load_clean_index()
            item = items.get(safe_rel, {})
            if item.get("webdav"):
                try:
                    removed = WebDAVClient(self.settings()).delete(safe_rel) or removed
                except ImageStorageError:
                    if not removed:
                        raise
            if safe_rel in items:
                items.pop(safe_rel, None)
                self._save_index(items)
        return removed

    def sync_all(self) -> dict[str, int]:
        settings = self.settings()
        if self.mode() not in {"webdav", "both"}:
            raise ImageStorageError("WebDAV 图片存储未启用")
        uploaded = 0
        skipped = 0
        failed = 0
        with self._index_lock:
            items = self._load_clean_index()
            client = WebDAVClient(settings)
            for path in sorted(config.images_dir.rglob("*")):
                if not path.is_file() or not _is_image_rel(path.name):
                    continue
                rel = path.relative_to(config.images_dir).as_posix()
                item = items.get(rel, {})
                if item.get("webdav"):
                    skipped += 1
                    continue
                try:
                    payload = path.read_bytes()
                    remote_url = client.put(rel, payload)
                    dimensions = _image_dimensions(payload)
                    items[rel] = {
                        **item,
                        "rel": rel,
                        "path": rel,
                        "name": path.name,
                        "date": "-".join(rel.split("/")[:3]) if len(rel.split("/")) >= 4 else _mtime_date(path),
                        "size": len(payload),
                        "created_at": str(item.get("created_at") or _mtime_datetime(path)),
                        "storage": "both",
                        "local": True,
                        "webdav": True,
                        "remote_url": remote_url,
                        **({"width": dimensions[0], "height": dimensions[1]} if dimensions else {}),
                    }
                    uploaded += 1
                except Exception:
                    failed += 1
            self._save_index(items)
        return {"uploaded": uploaded, "skipped": skipped, "failed": failed}

    def test_webdav(self) -> dict[str, object]:
        return WebDAVClient(self.settings()).test()


image_storage_service = ImageStorageService()
