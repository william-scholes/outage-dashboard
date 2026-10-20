from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from outage_automation.lock import FileLock, read_lock_info


class TestFileLock(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="test_lock_"))
        self.lock_path = self.tmp / ".lock"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_normal_acquire_and_release(self) -> None:
        lock = FileLock(self.lock_path, timeout=1.0)
        self.assertFalse(self.lock_path.exists())
        with lock:
            self.assertTrue(self.lock_path.exists())
            info = read_lock_info(self.lock_path)
            self.assertIsNotNone(info)
            self.assertEqual(info.pid, os.getpid())
        self.assertFalse(self.lock_path.exists())

    def test_stale_dead_pid_recovery(self) -> None:
        self.lock_path.write_text('{"pid": 9999999, "created_at": 1000.0}', encoding="utf-8")
        lock = FileLock(self.lock_path, timeout=1.0)
        with lock:
            self.assertTrue(self.lock_path.exists())
            info = read_lock_info(self.lock_path)
            self.assertEqual(info.pid, os.getpid())

    def test_stale_zero_byte_lock_recovery(self) -> None:
        self.lock_path.write_bytes(b"")
        old_time = time.time() - 2.0
        os.utime(self.lock_path, (old_time, old_time))

        lock = FileLock(self.lock_path, timeout=1.0)
        with lock:
            self.assertTrue(self.lock_path.exists())
            info = read_lock_info(self.lock_path)
            self.assertIsNotNone(info)
            self.assertEqual(info.pid, os.getpid())

    def test_stale_corrupt_json_lock_recovery(self) -> None:
        self.lock_path.write_text("invalid-json{{{", encoding="utf-8")
        old_time = time.time() - 2.0
        os.utime(self.lock_path, (old_time, old_time))

        lock = FileLock(self.lock_path, timeout=1.0)
        with lock:
            self.assertTrue(self.lock_path.exists())
            info = read_lock_info(self.lock_path)
            self.assertIsNotNone(info)
            self.assertEqual(info.pid, os.getpid())


if __name__ == "__main__":
    unittest.main()
