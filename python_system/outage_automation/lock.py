from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LockInfo:
    pid: int
    created_at: float


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class FileLock:
    def __init__(self, lock_path: Path, timeout: float = 5.0):
        self._path = lock_path
        self._timeout = timeout
        self._acquired = False

    def acquire(self, timeout: float | None = None) -> None:
        if timeout is None:
            timeout = self._timeout
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "created_at": time.time()}).encode("utf-8")
        start_time = time.time()
        while True:
            try:
                fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                self._acquired = True
                return
            except FileExistsError:
                info = read_lock_info(self._path)
                if info is not None and not is_pid_alive(info.pid):
                    self._path.unlink(missing_ok=True)
                    continue
                if info is None:
                    # Stale or corrupt lock file (e.g. 0-byte from crash/disk full).
                    # If it has existed unreadable or empty for > 0.5s, reclaim it.
                    try:
                        mtime = self._path.stat().st_mtime
                        if (time.time() - mtime) > 0.5:
                            self._path.unlink(missing_ok=True)
                            continue
                    except (FileNotFoundError, OSError):
                        continue
                if timeout <= 0 or (time.time() - start_time) >= timeout:
                    raise RuntimeError(f"lock exists: {self._path}")
                time.sleep(0.05)

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            self._path.unlink(missing_ok=True)
        finally:
            self._acquired = False

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def read_lock_info(lock_path: Path) -> LockInfo | None:
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        d = json.loads(raw)
        pid = int(d.get("pid") or 0)
        created_at = float(d.get("created_at") or 0.0)
        if pid <= 0:
            return None
        return LockInfo(pid=pid, created_at=created_at)
    except Exception:
        return None

