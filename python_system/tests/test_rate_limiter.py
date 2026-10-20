"""Unit and integration tests for dashboard brute-force rate limiter and IP lockout."""
from __future__ import annotations

import base64
import os
import shutil
import socket
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from outage_automation.dashboard.server import (
    RATE_LIMITER,
    RateLimiter,
    run_server,
)
from outage_automation.paths import resolve_paths


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestRateLimiterLogic(unittest.TestCase):
    def setUp(self) -> None:
        self.limiter = RateLimiter(max_failures=3, window_seconds=2.0, lockout_seconds=5.0)

    def test_record_failures_and_lockout(self) -> None:
        ip = "192.168.1.100"
        self.assertFalse(self.limiter.is_locked(ip)[0])

        # Attempt 1 & 2: not locked
        locked, _ = self.limiter.record_failure(ip, "alice")
        self.assertFalse(locked)
        locked, _ = self.limiter.record_failure(ip, "alice")
        self.assertFalse(locked)
        self.assertFalse(self.limiter.is_locked(ip)[0])

        # Attempt 3: threshold reached -> locked out!
        locked, remaining = self.limiter.record_failure(ip, "alice")
        self.assertTrue(locked)
        self.assertGreater(remaining, 0)

        # Inquiring is_locked is now True
        is_locked, rem2 = self.limiter.is_locked(ip)
        self.assertTrue(is_locked)
        self.assertGreater(rem2, 0)

    def test_record_success_resets(self) -> None:
        ip = "10.0.0.5"
        self.limiter.record_failure(ip, "bob")
        self.limiter.record_success(ip)
        self.assertFalse(self.limiter.is_locked(ip)[0])
        # Can fail up to 2 more times without hitting threshold
        self.limiter.record_failure(ip, "bob")
        self.limiter.record_failure(ip, "bob")
        self.assertFalse(self.limiter.is_locked(ip)[0])


class TestDashboardServerLockout(unittest.TestCase):
    def setUp(self) -> None:
        RATE_LIMITER.reset()
        # Set max_failures to 3 for fast test
        RATE_LIMITER.max_failures = 3
        RATE_LIMITER.lockout_seconds = 10.0

        self.tmp = Path(tempfile.mkdtemp(prefix="test_lockout_"))
        self.paths = resolve_paths(str(self.tmp))
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        (self.paths.state_dir / "alert_history.json").write_text("[]", encoding="utf-8")
        (self.paths.state_dir / "customers.json").write_text("[]", encoding="utf-8")

        self.port = _free_port()
        self.auth = ("admin", "correctpassword")
        self.server = run_server(
            paths=self.paths,
            host="127.0.0.1",
            port=self.port,
            auth=self.auth,
            block=False,
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        RATE_LIMITER.reset()
        RATE_LIMITER.max_failures = 5
        RATE_LIMITER.lockout_seconds = 900.0

    def _request(self, user: str, pw: str) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}/api/dashboard"
        req = urllib.request.Request(url)
        token = base64.b64encode(f"{user}:{pw}".encode("utf-8")).decode("ascii")
        req.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers)

    def test_loopback_lockout_after_failures(self) -> None:
        # Loopback is explicitly subject to lockout logic
        # 1st bad request -> 401
        code1, _ = self._request("admin", "bad1")
        self.assertEqual(code1, 401)

        # 2nd bad request -> 401
        code2, _ = self._request("admin", "bad2")
        self.assertEqual(code2, 401)

        # 3rd bad request -> 429 (hits max_failures=3)
        code3, headers3 = self._request("admin", "bad3")
        self.assertEqual(code3, 429)
        self.assertTrue("Retry-After" in headers3 or "retry-after" in headers3)

        # Subsequent request even with correct password is still locked out
        code4, headers4 = self._request("admin", "correctpassword")
        self.assertEqual(code4, 429)

        # Resetting rate limiter clears the lockout
        RATE_LIMITER.reset()
        code5, _ = self._request("admin", "correctpassword")
        self.assertEqual(code5, 200)

    def test_unauthenticated_request_does_not_trigger_lockout(self) -> None:
        url = f"http://127.0.0.1:{self.port}/api/dashboard"
        # Browsers initially send requests with no Authorization header to receive the 401 challenge.
        # Sending multiple unauthenticated requests must NOT lock out the IP.
        for _ in range(10):
            try:
                urllib.request.urlopen(url, timeout=5)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 401)
                self.assertIn("WWW-Authenticate", e.headers)

        # Confirm IP is not locked out and can authenticate successfully
        code, _ = self._request("admin", "correctpassword")
        self.assertEqual(code, 200)

    def test_form_login_lockout(self) -> None:
        import urllib.parse
        url = f"http://127.0.0.1:{self.port}/login"
        bad_payload = urllib.parse.urlencode({"username": "admin", "password": "wrong"}).encode("utf-8")

        # 1st & 2nd fail -> 401
        for _ in range(2):
            req = urllib.request.Request(url, data=bad_payload, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.fail("Expected HTTPError")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 401)

        # 3rd fail -> hits threshold -> 429
        req = urllib.request.Request(url, data=bad_payload, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.fail("Expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 429)
            body = e.read().decode("utf-8")
            self.assertIn("Too many failed attempts", body)


if __name__ == "__main__":
    unittest.main()

