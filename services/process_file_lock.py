from __future__ import annotations

import os
import threading
import time
from pathlib import Path


_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _local_lock(path: Path) -> threading.Lock:
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(str(path.resolve()), threading.Lock())


class ProcessFileLock:
    """Short-lived, non-reentrant lock shared by sender processes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = _local_lock(path)
        self._handle = None

    def acquire(self, *, timeout_secs: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout_secs)
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if not self._local.acquire(timeout=remaining):
                return False
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = self.path.open("a+b")
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return True
            except OSError:
                if "handle" in locals():
                    handle.close()
                self._local.release()
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._local.release()


class ProcessReentrantLock:
    """Use an OS lock at the outermost scope and an RLock for nesting."""

    def __init__(self, path: Path) -> None:
        self._local = threading.RLock()
        self._process = ProcessFileLock(path)
        self._depth = threading.local()

    def __enter__(self) -> ProcessReentrantLock:
        self._local.acquire()
        depth = int(getattr(self._depth, "value", 0))
        if depth == 0 and not self._process.acquire():
            self._local.release()
            raise RuntimeError("GenBox Push state is busy; retry the request.")
        self._depth.value = depth + 1
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        depth = int(getattr(self._depth, "value", 1)) - 1
        self._depth.value = depth
        if depth == 0:
            self._process.release()
        self._local.release()
