from __future__ import annotations

import hashlib
import io
import os
import stat
import time
import uuid
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
PROTECTED_CLEANUP_STAGING_ENV = "GENBOX_CLEANUP_PROTECTED_STAGING_ROOT"


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
    # Opaque operator-facing detail (for example quarantined entry names).
    detail: str = ""


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
    write_guard_held: bool = False


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


_CLEANUP_SOURCE_IDENTITY_FIELDS = {
    "device",
    "inode",
    "size_bytes",
}


def _cleanup_source_identity(file_stat: os.stat_result) -> dict[str, int]:
    return {
        "device": int(file_stat.st_dev),
        "inode": int(file_stat.st_ino),
        "size_bytes": int(file_stat.st_size),
    }


def _valid_cleanup_source_identity(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict) or set(value) != _CLEANUP_SOURCE_IDENTITY_FIELDS:
        return None
    try:
        identity = {field: int(value[field]) for field in _CLEANUP_SOURCE_IDENTITY_FIELDS}
    except (TypeError, ValueError, OverflowError):
        return None
    if any(number < 0 for number in identity.values()):
        return None
    return identity


def _same_cleanup_inode(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare an inode without requiring a single hard-link count."""
    return bool(
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and not _is_filesystem_alias(left)
        and not _is_filesystem_alias(right)
    )


def _quarantine_detail(names: list[str]) -> str:
    """Summarize quarantined directory entries for the cleanup audit trail."""
    return f"quarantined:{';'.join(names)}" if names else ""


def _cleanup_transaction_token(value: object) -> str:
    token = str(value or "").strip().lower()
    if len(token) in {32, 64} and all(char in "0123456789abcdef" for char in token):
        return token
    return ""


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
            0x00000005,  # FILE_SHARE_READ | FILE_SHARE_DELETE; deny new writers
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
    def _descriptor_mount_id(descriptor: int) -> int | None:
        try:
            for line in Path(f"/proc/self/fdinfo/{descriptor}").read_text(encoding="ascii").splitlines():
                if line.startswith("mnt_id:"):
                    return int(line.split(":", 1)[1].strip())
        except (OSError, UnicodeError, ValueError):
            return None
        return None

    def _cleanup_parent_is_anchored(self, target: _OpenedCleanupTarget) -> bool:
        """Prove the retained parent fd still names the authorized root path."""
        if target.parent_descriptor is None:
            try:
                parent_stat = target.candidate.parent.lstat()
            except OSError:
                return False
            return stat.S_ISDIR(parent_stat.st_mode) and not _is_filesystem_alias(parent_stat)

        root = Path(self.local_root())
        nofollow = int(getattr(os, "O_NOFOLLOW", 0))
        directory_flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | nofollow
        descriptor: int | None = None
        try:
            root_stat = root.lstat()
            if not stat.S_ISDIR(root_stat.st_mode) or _is_filesystem_alias(root_stat):
                return False
            descriptor = os.open(root, directory_flags)
            opened_root = os.fstat(descriptor)
            if opened_root.st_dev != root_stat.st_dev or opened_root.st_ino != root_stat.st_ino:
                return False
            for component in Path(target.relative_path).parts[:-1]:
                component_stat = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(component_stat.st_mode) or _is_filesystem_alias(component_stat):
                    return False
                next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
                opened_component = os.fstat(next_descriptor)
                if (
                    opened_component.st_dev != component_stat.st_dev
                    or opened_component.st_ino != component_stat.st_ino
                ):
                    os.close(next_descriptor)
                    return False
                os.close(descriptor)
                descriptor = next_descriptor
            current_parent = os.fstat(descriptor)
            retained_parent = os.fstat(target.parent_descriptor)
            if (
                current_parent.st_dev != retained_parent.st_dev
                or current_parent.st_ino != retained_parent.st_ino
            ):
                return False
            current_mount = self._descriptor_mount_id(descriptor)
            retained_mount = self._descriptor_mount_id(target.parent_descriptor)
            # An unreadable mount identity is not evidence of continuity.
            # Treat it as a boundary failure instead of accepting a path that
            # may have crossed mounts or been replaced behind the descriptor.
            return (
                current_mount is not None
                and retained_mount is not None
                and current_mount == retained_mount
            )
        except OSError:
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _acquire_posix_write_guard(descriptor: int) -> bool:
        """Acquire a kernel-enforced write lease or fail closed.

        The application claim coordinates GenBox workers. The lease closes the
        remaining cross-process window by rejecting an already-open writer and
        blocking new opens for write until the exact-delete operation ends.
        """
        if os.name == "nt":
            return False
        try:
            import fcntl
            import signal

            required = ("F_SETLEASE", "F_GETLEASE", "F_WRLCK", "F_UNLCK", "F_SETSIG")
            if any(not hasattr(fcntl, name) for name in required):
                return False
            fcntl.fcntl(descriptor, fcntl.F_SETSIG, signal.SIGURG)
            fcntl.fcntl(descriptor, fcntl.F_SETLEASE, fcntl.F_WRLCK)
            return fcntl.fcntl(descriptor, fcntl.F_GETLEASE) == fcntl.F_WRLCK
        except (ImportError, OSError, ValueError):
            return False

    @staticmethod
    def _posix_write_guard_active(descriptor: int) -> bool:
        if os.name == "nt":
            return False
        try:
            import fcntl

            return fcntl.fcntl(descriptor, fcntl.F_GETLEASE) == fcntl.F_WRLCK
        except (ImportError, OSError, ValueError, AttributeError):
            return False

    @staticmethod
    def _close_cleanup_target(target: _OpenedCleanupTarget) -> None:
        try:
            if target.write_guard_held and os.name != "nt":
                try:
                    import fcntl

                    fcntl.fcntl(target.descriptor, fcntl.F_SETLEASE, fcntl.F_UNLCK)
                except (ImportError, OSError, ValueError, AttributeError):
                    pass
            os.close(target.descriptor)
        finally:
            if target.parent_descriptor is not None:
                os.close(target.parent_descriptor)

    def _open_verified_posix_cleanup_target(
        self,
        root: Path,
        safe_rel: str,
        digest: str,
        *,
        expected_size: int | None,
        expected_identity: dict[str, int] | None,
        write_guard: bool,
    ) -> _OpenedCleanupTarget | dict[str, object]:
        """Open a cleanup target through anchored directory descriptors.

        This avoids a second pathname walk after validating a directory. Each
        component is opened relative to the descriptor for its already-opened
        parent and compared with the no-follow stat observed immediately before
        it. Any replacement or alias therefore becomes a retention decision.
        """
        nofollow = int(getattr(os, "O_NOFOLLOW", 0))
        directory_flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | nofollow
        parent_descriptor: int | None = None
        descriptor: int | None = None
        try:
            root_stat = root.lstat()
            if not stat.S_ISDIR(root_stat.st_mode) or _is_filesystem_alias(root_stat):
                return {"ok": False, "reason": "storage-root-invalid"}
            parent_descriptor = os.open(root, directory_flags)
            opened_root = os.fstat(parent_descriptor)
            if (
                opened_root.st_dev != root_stat.st_dev
                or opened_root.st_ino != root_stat.st_ino
                or not stat.S_ISDIR(opened_root.st_mode)
            ):
                return {"ok": False, "reason": "storage-root-invalid"}

            parts = Path(safe_rel).parts
            for component in parts[:-1]:
                component_stat = os.stat(component, dir_fd=parent_descriptor, follow_symlinks=False)
                if _is_filesystem_alias(component_stat) or not stat.S_ISDIR(component_stat.st_mode):
                    return {"ok": False, "reason": "path-alias"}
                next_descriptor = os.open(component, directory_flags, dir_fd=parent_descriptor)
                opened_component = os.fstat(next_descriptor)
                if (
                    opened_component.st_dev != component_stat.st_dev
                    or opened_component.st_ino != component_stat.st_ino
                    or not stat.S_ISDIR(opened_component.st_mode)
                ):
                    os.close(next_descriptor)
                    return {"ok": False, "reason": "source-changed"}
                os.close(parent_descriptor)
                parent_descriptor = next_descriptor

            parent_name = parts[-1]
            file_stat = os.stat(parent_name, dir_fd=parent_descriptor, follow_symlinks=False)
            if _is_filesystem_alias(file_stat):
                return {"ok": False, "reason": "path-alias"}
            if not stat.S_ISREG(file_stat.st_mode):
                return {"ok": False, "reason": "source-not-regular"}
            if int(getattr(file_stat, "st_nlink", 1) or 1) != 1:
                return {"ok": False, "reason": "path-alias"}
            if expected_size is not None and int(file_stat.st_size) != int(expected_size):
                return {"ok": False, "reason": "source-changed", "size_bytes": int(file_stat.st_size)}
            if expected_identity is not None and _cleanup_source_identity(file_stat) != expected_identity:
                return {"ok": False, "reason": "source-changed", "size_bytes": int(file_stat.st_size)}
            descriptor = os.open(
                parent_name,
                (os.O_RDWR if write_guard else os.O_RDONLY) | nofollow,
                dir_fd=parent_descriptor,
            )
            if write_guard and not self._acquire_posix_write_guard(descriptor):
                return {"ok": False, "reason": "source-busy"}
            opened_stat = os.fstat(descriptor)
            if not _same_cleanup_identity(opened_stat, file_stat):
                return {"ok": False, "reason": "source-changed"}
            actual = self._read_open_digest(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            if actual != digest:
                return {"ok": False, "reason": "source-changed", "size_bytes": int(file_stat.st_size)}
            latest_stat = os.stat(parent_name, dir_fd=parent_descriptor, follow_symlinks=False)
            if not _same_cleanup_identity(latest_stat, file_stat):
                return {"ok": False, "reason": "source-changed"}
            target = _OpenedCleanupTarget(
                relative_path=safe_rel,
                candidate=root / safe_rel,
                descriptor=descriptor,
                parent_descriptor=parent_descriptor,
                parent_name=parent_name,
                file_stat=file_stat,
                digest=actual,
                size_bytes=int(file_stat.st_size),
                write_guard_held=write_guard,
            )
            descriptor = None
            parent_descriptor = None
            return target
        except FileNotFoundError:
            return {"ok": False, "reason": "source-missing"}
        except OSError:
            return {"ok": False, "reason": "source-unreadable"}
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)

    def _open_verified_cleanup_target(
        self,
        rel: str,
        expected_sha256: str,
        *,
        expected_size: int | None = None,
        expected_identity: dict[str, int] | None = None,
        write_guard: bool = False,
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
        if os.name != "nt" and hasattr(os, "O_DIRECTORY") and getattr(os, "O_NOFOLLOW", 0):
            return self._open_verified_posix_cleanup_target(
                root,
                safe_rel,
                digest,
                expected_size=expected_size,
                expected_identity=expected_identity,
                write_guard=write_guard,
            )
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
        if expected_identity is not None and _cleanup_source_identity(file_stat) != expected_identity:
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
            nofollow = int(getattr(os, "O_NOFOLLOW", 0))
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
        expected_identity: dict[str, int] | None = None,
    ) -> dict[str, object]:
        """Inspect one local source without following aliases."""
        normalized_identity = None
        if expected_identity is not None:
            normalized_identity = _valid_cleanup_source_identity(expected_identity)
            if normalized_identity is None:
                return {"ok": False, "reason": "source-identity-invalid"}
        target = self._open_verified_cleanup_target(
            rel,
            expected_sha256,
            expected_size=expected_size,
            expected_identity=normalized_identity,
            write_guard=False,
        )
        if isinstance(target, dict):
            return target
        try:
            return {
                "ok": True,
                "relative_path": target.relative_path,
                "sha256": target.digest,
                "size_bytes": target.size_bytes,
                "stat": target.file_stat,
                "source_identity": _cleanup_source_identity(target.file_stat),
            }
        finally:
            self._close_cleanup_target(target)

    def _before_verified_unlink(self, _target: _OpenedCleanupTarget) -> None:
        """Test seam for deterministic replacement-race coverage."""

    def _before_final_unlink(self, _target: _OpenedCleanupTarget) -> None:
        """Test seam for the final directory-entry identity check."""

    def _after_final_identity_check(self, _target: _OpenedCleanupTarget) -> None:
        """Test seam immediately before the platform deletion primitive."""

    def _before_posix_exchange(self, _target: _OpenedCleanupTarget) -> None:
        """Test seam between the pre-exchange link check and the exchange."""

    def _after_posix_tombstone(self, _target: _OpenedCleanupTarget) -> None:
        """Test seam after the source name is atomically tombstoned."""

    def _after_posix_exchange(self, _target: _OpenedCleanupTarget) -> None:
        """Test seam immediately after the atomic directory-entry exchange."""

    @staticmethod
    def _open_protected_cleanup_staging(parent_fd: int) -> int | None:
        """Open a pre-provisioned staging boundary owned by another uid."""
        configured = str(os.environ.get(PROTECTED_CLEANUP_STAGING_ENV) or "").strip()
        if not configured or not os.path.isabs(configured):
            return None
        staging = Path(configured)
        try:
            source_stat = os.fstat(parent_fd)
            current = staging
            while True:
                item = current.lstat()
                writable_ancestor = bool(int(item.st_mode) & 0o022)
                sticky_shared_root = bool(
                    writable_ancestor
                    and int(item.st_mode) & stat.S_ISVTX
                    and int(item.st_uid) == 0
                )
                if _is_filesystem_alias(item) or not stat.S_ISDIR(item.st_mode) or (
                    writable_ancestor and not sticky_shared_root
                ):
                    return None
                if current == current.parent:
                    break
                current = current.parent
            staging_stat = staging.lstat()
            if int(staging_stat.st_uid) == int(source_stat.st_uid):
                return None
            flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
            descriptor = os.open(staging, flags)
            opened = os.fstat(descriptor)
            if (
                opened.st_dev != staging_stat.st_dev
                or opened.st_ino != staging_stat.st_ino
                or int(opened.st_uid) == int(source_stat.st_uid)
                or int(opened.st_mode) & 0o022
            ):
                os.close(descriptor)
                return None
            probe = f".genbox-staging-probe-{os.getpid()}-{uuid.uuid4().hex}"
            try:
                probe_fd = os.open(
                    probe,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_NOFOLLOW", 0)),
                    0o600,
                    dir_fd=descriptor,
                )
                os.close(probe_fd)
                os.unlink(probe, dir_fd=descriptor)
            except OSError:
                os.close(descriptor)
                return None
            return descriptor
        except OSError:
            return None

    def _unlink_posix_protected_staging(
        self,
        target: _OpenedCleanupTarget,
        *,
        cleanup_token: str | None = None,
    ) -> VerifiedDeleteResult:
        """Transfer the verified inode into a protected namespace first."""
        parent_fd = target.parent_descriptor
        if parent_fd is None:
            return VerifiedDeleteResult("retained", "protected-staging-unavailable", target.size_bytes)
        initial_parent = os.fstat(parent_fd)
        initial_parent_generation = (
            int(initial_parent.st_dev),
            int(initial_parent.st_ino),
            int(getattr(initial_parent, "st_ctime_ns", 0)),
            int(getattr(initial_parent, "st_mtime_ns", 0)),
        )
        if not self._cleanup_parent_is_anchored(target):
            return VerifiedDeleteResult(
                "retained", "source-changed", target.size_bytes, detail="restored-original-entry",
            )
        rechecked_parent = os.fstat(parent_fd)
        if initial_parent_generation != (
            int(rechecked_parent.st_dev),
            int(rechecked_parent.st_ino),
            int(getattr(rechecked_parent, "st_ctime_ns", 0)),
            int(getattr(rechecked_parent, "st_mtime_ns", 0)),
        ):
            return VerifiedDeleteResult("retained", "source-parent-changed", target.size_bytes)
        staging_fd = self._open_protected_cleanup_staging(parent_fd)
        if staging_fd is None:
            return VerifiedDeleteResult("retained", "protected-staging-unavailable", target.size_bytes)
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        linkat = getattr(libc, "linkat", None)
        renameat2 = getattr(libc, "renameat2", None)
        if linkat is None or renameat2 is None:
            os.close(staging_fd)
            return VerifiedDeleteResult("retained", "protected-staging-unavailable", target.size_bytes)
        linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        linkat.restype = ctypes.c_int
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        at_empty_path = 0x1000
        rename_noreplace = 0x1
        rename_exchange = 0x2
        suffix = _cleanup_transaction_token(cleanup_token) or f"{os.getpid()}-{uuid.uuid4().hex}"
        temp_name = f".genbox-cleanup-{suffix}.tmp"
        staged_original = f".genbox-cleanup-{suffix}.staged"
        staged_observed = f".genbox-cleanup-{suffix}.observed"

        def syscall_error(message: str) -> OSError:
            return OSError(ctypes.get_errno(), message)

        def stat_source(name: str) -> os.stat_result:
            return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)

        def stat_staging(name: str) -> os.stat_result:
            return os.stat(name, dir_fd=staging_fd, follow_symlinks=False)

        def rename(source_fd: int, source: str, destination_fd: int, destination: str, flags: int) -> None:
            if renameat2(
                source_fd,
                os.fsencode(source),
                destination_fd,
                os.fsencode(destination),
                flags,
            ) != 0:
                raise syscall_error("renameat2 failed")

        def unlink(fd: int, name: str) -> None:
            os.unlink(name, dir_fd=fd)

        def unlink_staged_exact(name: str, expected: os.stat_result) -> bool:
            """Remove a staging entry only after an atomic identity handoff."""
            # Keep the deletion tomb name independent from the entry being
            # removed, so a watcher keyed to the transaction name cannot
            # substitute a different object at the final unlink boundary.
            tomb = f".genbox-staging-delete-{suffix}-{uuid.uuid4().hex}"
            rename(staging_fd, name, staging_fd, tomb, rename_noreplace)
            try:
                moved = stat_staging(tomb)
                if not _same_cleanup_inode(moved, expected):
                    try:
                        rename(staging_fd, tomb, staging_fd, name, rename_noreplace)
                    except OSError:
                        pass
                    return False
                # Re-check immediately before the destructive primitive. A
                # replacement is then left as an artifact rather than being
                # removed under the original transaction name.
                moved_again = stat_staging(tomb)
                if not _same_cleanup_inode(moved_again, expected):
                    try:
                        rename(staging_fd, tomb, staging_fd, name, rename_noreplace)
                    except OSError:
                        pass
                    return False
                unlink(staging_fd, tomb)
                return True
            except OSError:
                try:
                    rename(staging_fd, tomb, staging_fd, name, rename_noreplace)
                except OSError:
                    pass
                return False

        def digest_opened() -> str:
            os.lseek(target.descriptor, 0, os.SEEK_SET)
            value = self._read_open_digest(target.descriptor)
            os.lseek(target.descriptor, 0, os.SEEK_SET)
            return value

        def parent_generation() -> tuple[int, int, int, int]:
            current = os.fstat(parent_fd)
            return (
                int(current.st_dev),
                int(current.st_ino),
                int(getattr(current, "st_ctime_ns", 0)),
                int(getattr(current, "st_mtime_ns", 0)),
            )

        def drop_owned_temp() -> None:
            try:
                if _same_cleanup_inode(stat_source(temp_name), target.file_stat):
                    unlink(parent_fd, temp_name)
            except OSError:
                pass

        step = "start"
        try:
            step = "link"
            initial_nlink = int(getattr(os.fstat(target.descriptor), "st_nlink", 0) or 0)
            if initial_nlink == 0:
                return VerifiedDeleteResult("retained", "source-changed", target.size_bytes)
            if initial_nlink > 1:
                return VerifiedDeleteResult(
                    "retained", "path-alias", target.size_bytes, detail="restored-original-entry",
                )
            if linkat(target.descriptor, b"", parent_fd, os.fsencode(temp_name), at_empty_path) != 0:
                raise syscall_error("linkat(AT_EMPTY_PATH) failed")
            if int(getattr(os.fstat(target.descriptor), "st_nlink", 0) or 0) != initial_nlink + 1:
                drop_owned_temp()
                return VerifiedDeleteResult(
                    "retained", "path-alias", target.size_bytes, detail="restored-original-entry",
                )
            step = "before-exchange"
            before_exchange_generation = parent_generation()
            self._before_posix_exchange(target)
            after_hook_generation = parent_generation()
            if (
                after_hook_generation[:2] != before_exchange_generation[:2]
                or (
                    after_hook_generation[3] == before_exchange_generation[3]
                    and after_hook_generation[2] != before_exchange_generation[2]
                )
                or not self._cleanup_parent_is_anchored(target)
            ):
                # Entry changes update mtime and ctime together; a ctime-only
                # change means the parent object itself was renamed or replaced,
                # so the anchor is gone. Drop only the service-owned link and
                # keep the verified source untouched.
                drop_owned_temp()
                return VerifiedDeleteResult(
                    "retained", "source-parent-changed", target.size_bytes,
                )
            post_hook_nlink = int(getattr(os.fstat(target.descriptor), "st_nlink", 0) or 0)
            if post_hook_nlink > initial_nlink + 1 or post_hook_nlink < max(0, initial_nlink - 1):
                drop_owned_temp()
                return VerifiedDeleteResult(
                    "retained", "path-alias", target.size_bytes, detail="restored-original-entry",
                )
            if digest_opened() != target.digest or not self._posix_write_guard_active(target.descriptor):
                drop_owned_temp()
                return VerifiedDeleteResult("retained", "source-changed", target.size_bytes)
            step = "exchange"
            rename(parent_fd, temp_name, parent_fd, target.parent_name, rename_exchange)
            after_exchange_generation = parent_generation()
            self._after_posix_exchange(target)
            if parent_generation() != after_exchange_generation:
                return VerifiedDeleteResult(
                    "delete_unknown", "source-parent-changed", target.size_bytes, detail=temp_name,
                )
            step = "identify"
            temp_stat = stat_source(temp_name)
            try:
                target_stat = stat_source(target.parent_name)
            except FileNotFoundError:
                target_stat = None
            temp_is_original = _same_cleanup_inode(temp_stat, target.file_stat)
            target_is_original = bool(target_stat is not None and _same_cleanup_inode(target_stat, target.file_stat))
            if not temp_is_original and not target_is_original:
                return VerifiedDeleteResult("retained", "source-changed", target.size_bytes)
            original_name = temp_name if temp_is_original else target.parent_name
            observed_name = target.parent_name if original_name == temp_name else temp_name
            step = "stage-original"
            rename(parent_fd, original_name, staging_fd, staged_original, rename_noreplace)
            try:
                step = "stage-observed"
                rename(parent_fd, observed_name, staging_fd, staged_observed, rename_noreplace)
            except FileNotFoundError:
                pass
            step = "after-tombstone"
            self._after_posix_tombstone(target)
            if not self._cleanup_parent_is_anchored(target):
                return VerifiedDeleteResult(
                    "delete_unknown", "source-parent-changed", target.size_bytes,
                    detail=staged_original,
                )
            if digest_opened() != target.digest:
                # The opened inode changed after the source name was moved
                # into staging. Restore a surviving entry before reporting
                # anything. When both staging names still reference the same
                # inode, discard only the duplicate link; when a replacement
                # differs, restore it and retain the changed original as an
                # explicit unknown artifact.
                try:
                    original_stat = stat_staging(staged_original)
                except FileNotFoundError:
                    return VerifiedDeleteResult(
                        "delete_unknown", "source-changed", target.size_bytes, detail=staged_original,
                    )
                try:
                    observed_stat = stat_staging(staged_observed)
                except FileNotFoundError:
                    observed_stat = None
                if observed_stat is not None and _same_cleanup_inode(observed_stat, original_stat):
                    try:
                        rename(staging_fd, staged_observed, parent_fd, target.parent_name, rename_noreplace)
                        if not unlink_staged_exact(staged_original, original_stat):
                            return VerifiedDeleteResult(
                                "delete_unknown", "staging-identity-ambiguous", target.size_bytes,
                                detail=staged_original,
                            )
                    except OSError:
                        return VerifiedDeleteResult(
                            "delete_unknown", "replacement-restore-ambiguous", target.size_bytes,
                            detail=staged_original,
                        )
                    return VerifiedDeleteResult(
                        "retained", "source-changed", target.size_bytes, detail="restored-original-entry",
                    )
                if observed_stat is not None:
                    try:
                        rename(staging_fd, staged_observed, parent_fd, target.parent_name, rename_noreplace)
                    except OSError:
                        return VerifiedDeleteResult(
                            "delete_unknown", "replacement-restore-ambiguous", target.size_bytes,
                            detail=staged_original,
                        )
                    return VerifiedDeleteResult(
                        "delete_unknown", "source-changed", target.size_bytes, detail=staged_original,
                    )
                try:
                    rename(staging_fd, staged_original, parent_fd, target.parent_name, rename_noreplace)
                except OSError:
                    return VerifiedDeleteResult(
                        "delete_unknown", "source-restore-ambiguous", target.size_bytes, detail=staged_original,
                    )
                return VerifiedDeleteResult(
                    "retained", "source-changed", target.size_bytes, detail="restored-original-entry",
                )
            original_stat = stat_staging(staged_original)
            if not _same_cleanup_inode(original_stat, target.file_stat):
                return VerifiedDeleteResult("delete_unknown", "staging-identity-ambiguous", target.size_bytes)
            try:
                observed_stat = stat_staging(staged_observed)
            except FileNotFoundError:
                observed_stat = None
            if observed_stat is not None and _same_cleanup_inode(observed_stat, target.file_stat):
                if not self._cleanup_parent_is_anchored(target):
                    return VerifiedDeleteResult(
                        "delete_unknown", "source-parent-changed", target.size_bytes,
                        detail=staged_original,
                    )
                if not unlink_staged_exact(staged_observed, observed_stat):
                    return VerifiedDeleteResult(
                        "delete_unknown", "staging-identity-ambiguous", target.size_bytes,
                        detail=staged_observed,
                    )
                if not unlink_staged_exact(staged_original, original_stat):
                    return VerifiedDeleteResult(
                        "delete_unknown", "staging-identity-ambiguous", target.size_bytes,
                        detail=staged_original,
                    )
                return VerifiedDeleteResult("deleted", "deleted", target.size_bytes)
            if observed_stat is not None:
                try:
                    step = "restore-observed"
                    rename(staging_fd, staged_observed, parent_fd, target.parent_name, rename_noreplace)
                except OSError:
                    return VerifiedDeleteResult(
                        "delete_unknown", "replacement-restore-ambiguous", target.size_bytes, detail=staged_observed,
                    )
            return VerifiedDeleteResult(
                "retained", "source-changed", target.size_bytes, detail=staged_original,
            )
        except (FileNotFoundError, OSError) as exc:
            return VerifiedDeleteResult(
                "delete_unknown",
                "protected-staging-operation-failed",
                target.size_bytes,
                detail=f"{staged_original}:{step}:{type(exc).__name__}:{getattr(exc, 'errno', '')}",
            )
        finally:
            os.close(staging_fd)

    def _unlink_posix_exact(
        self,
        target: _OpenedCleanupTarget,
        *,
        cleanup_token: str | None = None,
    ) -> VerifiedDeleteResult:
        """Remove only the opened inode using Linux atomic directory moves.

        POSIX has no unlink-if-inode primitive. On Linux, two hard links plus
        ``renameat2`` let us exchange and tombstone the current directory entry
        atomically. If the entry changed, the current object is restored and
        every unexpected object is quarantined instead of being deleted. When
        the required syscalls are unavailable, cleanup fails closed.
        """
        return self._unlink_posix_protected_staging(target, cleanup_token=cleanup_token)

    def _unlink_open_cleanup_target(
        self,
        target: _OpenedCleanupTarget,
        *,
        cleanup_token: str | None = None,
    ) -> VerifiedDeleteResult:
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
        # A cooperating sender worker can only mutate a source while holding
        # the shared claim. Re-read the opened bytes and the directory entry
        # after the final test seam as well, so the replacement window between
        # the last observation and the platform unlink primitive is covered by
        # an explicit fail-closed check in the supported application boundary.
        try:
            self._before_final_unlink(target)
            os.lseek(target.descriptor, 0, os.SEEK_SET)
            final_digest = self._read_open_digest(target.descriptor)
            os.lseek(target.descriptor, 0, os.SEEK_SET)
            final_stat = self._cleanup_stat(target)
        except FileNotFoundError:
            return VerifiedDeleteResult("retained", "source-missing", target.size_bytes)
        except OSError:
            return VerifiedDeleteResult("retained", "source-changed", target.size_bytes)
        if final_digest != target.digest or not _same_cleanup_identity(final_stat, target.file_stat):
            return VerifiedDeleteResult("retained", "source-changed", target.size_bytes)
        self._after_final_identity_check(target)
        # The test seam above represents a real watcher racing after the
        # earlier observation. Re-establish the final proof immediately before
        # the platform delete: the opened object, current directory entry, and
        # content must still be the single verified source.
        try:
            os.lseek(target.descriptor, 0, os.SEEK_SET)
            delete_digest = self._read_open_digest(target.descriptor)
            os.lseek(target.descriptor, 0, os.SEEK_SET)
            delete_handle_stat = os.fstat(target.descriptor)
            delete_path_stat = self._cleanup_stat(target)
        except FileNotFoundError:
            return VerifiedDeleteResult("retained", "source-missing", target.size_bytes)
        except OSError:
            return VerifiedDeleteResult("retained", "source-changed", target.size_bytes)
        if (
            int(getattr(delete_handle_stat, "st_nlink", 1) or 1) != 1
            or int(getattr(delete_path_stat, "st_nlink", 1) or 1) != 1
        ):
            return VerifiedDeleteResult("retained", "path-alias", target.size_bytes)
        if (
            delete_digest != target.digest
            or not _same_cleanup_identity(delete_handle_stat, target.file_stat)
            or not _same_cleanup_identity(delete_path_stat, target.file_stat)
        ):
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
                return self._unlink_posix_exact(target, cleanup_token=cleanup_token)
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
        expected_identity: dict[str, int] | None = None,
        claim_held: bool = False,
        cleanup_token: str | None = None,
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
        normalized_identity = _valid_cleanup_source_identity(expected_identity)
        if normalized_identity is None:
            return VerifiedDeleteResult("retained", "source-identity-missing")
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
                expected_identity=normalized_identity,
                write_guard=True,
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
            deletion = self._unlink_open_cleanup_target(target, cleanup_token=cleanup_token)
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
        expected_identity: dict[str, int] | None = None,
        claim_held: bool = False,
        cleanup_token: str | None = None,
    ) -> VerifiedDeleteResult:
        """Compatibility name for the storage-owned cleanup primitive."""
        return self.delete_verified_local(
            rel,
            expected_sha256,
            expected_size=expected_size,
            expected_identity=expected_identity,
            claim_held=claim_held,
            cleanup_token=cleanup_token,
        )

    def inspect_verified_delete_artifacts(
        self,
        rel: str,
        expected_sha256: str,
        cleanup_token: str,
        *,
        expected_size: int | None = None,
    ) -> dict[str, object]:
        """Locate only the deterministic artifacts for one interrupted delete.

        Recovery is read-only: it never removes, relinks, or renames an entry.
        The opaque names make an interrupted atomic transaction explainable to
        an operator without exposing an arbitrary filesystem path.
        """
        token = _cleanup_transaction_token(cleanup_token)
        digest = str(expected_sha256 or "").strip().lower()
        try:
            safe_rel = _cleanup_relative_path(rel)
        except ImageStorageError:
            return {"artifacts": [], "matching_artifacts": [], "target_matches": False, "reason": "path-invalid"}
        if not token or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            return {"artifacts": [], "matching_artifacts": [], "target_matches": False, "reason": "transaction-invalid"}
        names = [
            f".genbox-cleanup-{token}.tmp",
            f".genbox-cleanup-{token}.hold",
            f".genbox-cleanup-{token}.tomb",
        ]
        root = Path(self.local_root())
        artifacts: list[str] = []
        matching: list[str] = []
        target_matches = False
        parent_descriptor: int | None = None
        descriptor: int | None = None
        staging_descriptor: int | None = None
        try:
            if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
                return {"artifacts": [], "matching_artifacts": [], "target_matches": False, "reason": "not-posix"}
            nofollow = int(getattr(os, "O_NOFOLLOW", 0))
            directory_flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | nofollow
            parent_descriptor = os.open(root, directory_flags)
            for component in Path(safe_rel).parts[:-1]:
                next_descriptor = os.open(component, directory_flags, dir_fd=parent_descriptor)
                os.close(parent_descriptor)
                parent_descriptor = next_descriptor
            target_name = Path(safe_rel).parts[-1]
            try:
                target_stat = os.stat(target_name, dir_fd=parent_descriptor, follow_symlinks=False)
                if stat.S_ISREG(target_stat.st_mode) and not _is_filesystem_alias(target_stat):
                    descriptor = os.open(target_name, os.O_RDONLY | nofollow, dir_fd=parent_descriptor)
                    opened_target = os.fstat(descriptor)
                    if (
                        opened_target.st_dev == target_stat.st_dev
                        and opened_target.st_ino == target_stat.st_ino
                        and opened_target.st_size == target_stat.st_size
                        and (expected_size is None or int(opened_target.st_size) == int(expected_size))
                        and self._read_open_digest(descriptor) == digest
                    ):
                        target_matches = True
                    os.close(descriptor)
                    descriptor = None
            except FileNotFoundError:
                pass
            for name in names:
                try:
                    file_stat = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(file_stat.st_mode) or _is_filesystem_alias(file_stat):
                    artifacts.append(name)
                    continue
                artifacts.append(name)
                descriptor = os.open(name, os.O_RDONLY | nofollow, dir_fd=parent_descriptor)
                opened_stat = os.fstat(descriptor)
                if (
                    opened_stat.st_dev == file_stat.st_dev
                    and opened_stat.st_ino == file_stat.st_ino
                    and opened_stat.st_size == file_stat.st_size
                    and (expected_size is None or int(opened_stat.st_size) == int(expected_size))
                ):
                    actual = self._read_open_digest(descriptor)
                    if actual == digest:
                        matching.append(name)
                os.close(descriptor)
                descriptor = None
            staging_root = str(os.environ.get(PROTECTED_CLEANUP_STAGING_ENV) or "").strip()
            if staging_root and os.path.isabs(staging_root):
                try:
                    staging_descriptor = os.open(staging_root, directory_flags)
                    prefix = f".genbox-cleanup-{token}."
                    for name in os.listdir(staging_descriptor):
                        if not name.startswith(prefix):
                            continue
                        try:
                            file_stat = os.stat(name, dir_fd=staging_descriptor, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        display_name = f"protected-staging/{name}"
                        artifacts.append(display_name)
                        if not stat.S_ISREG(file_stat.st_mode) or _is_filesystem_alias(file_stat):
                            continue
                        descriptor = os.open(name, os.O_RDONLY | nofollow, dir_fd=staging_descriptor)
                        opened_stat = os.fstat(descriptor)
                        if (
                            opened_stat.st_dev == file_stat.st_dev
                            and opened_stat.st_ino == file_stat.st_ino
                            and opened_stat.st_size == file_stat.st_size
                            and (expected_size is None or int(opened_stat.st_size) == int(expected_size))
                            and self._read_open_digest(descriptor) == digest
                        ):
                            matching.append(display_name)
                        os.close(descriptor)
                        descriptor = None
                except OSError:
                    pass
        except OSError:
            return {
                "artifacts": artifacts,
                "matching_artifacts": matching,
                "target_matches": target_matches,
                "reason": "artifact-inspection-failed",
            }
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)
            if staging_descriptor is not None:
                os.close(staging_descriptor)
        return {
            "artifacts": artifacts,
            "matching_artifacts": matching,
            "target_matches": target_matches,
            "reason": "inspected",
        }

    def delete(self, rel: str) -> bool:
        # Generic/manual deletion must not bypass a receipt-tracked source.
        # Phase 6 cleanup uses delete_verified_local(), which owns the receipt,
        # policy, intent, audit, and identity checks.
        from services.config import config as runtime_config
        protected = runtime_config.receipt_protected_image_paths()
        safe_rel = _safe_relative_path(rel)
        if safe_rel in protected or "*" in protected:
            return False
        removed = False
        path = _local_image_path(safe_rel)
        claim = None
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            from services.source_claim import source_claim

            claim = source_claim(safe_rel, digest)
            if not claim.acquire(timeout_secs=0):
                return False
        try:
            # Re-read the protected projection after claiming the source. A
            # concurrent Push that registered a receipt before this point wins
            # over a generic deletion request.
            protected = runtime_config.receipt_protected_image_paths()
            if safe_rel in protected or "*" in protected:
                return False
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
        finally:
            if claim is not None:
                claim.release()

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
