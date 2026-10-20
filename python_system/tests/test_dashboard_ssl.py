"""Tests for native SSL/TLS dashboard serving, certificate generation, and HTTP redirection."""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import ssl
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from outage_automation.dashboard.server import (
    ensure_self_signed_cert,
    run_server,
)
from outage_automation.paths import resolve_paths


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestDashboardSSL(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="oa_test_ssl_"))
        self.paths = resolve_paths(str(self.tmp))
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        (self.paths.alert_history_path).write_text("[]", encoding="utf-8")
        (self.paths.customers_path).write_text("[]", encoding="utf-8")

        self.cert_path = self.tmp / "test_dashboard.crt"
        self.key_path = self.tmp / "test_dashboard.key"
        self.server = None

    def tearDown(self) -> None:
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ensure_self_signed_cert(self) -> None:
        """Test generating a self-signed SAN certificate and key."""
        ensure_self_signed_cert(self.cert_path, self.key_path, host="127.0.0.1", days=30)
        self.assertTrue(self.cert_path.exists())
        self.assertTrue(self.key_path.exists())
        self.assertGreater(self.cert_path.stat().st_size, 100)
        self.assertGreater(self.key_path.stat().st_size, 100)

        # Idempotency check: calling again does not overwrite or fail
        mtime_before = self.cert_path.stat().st_mtime
        ensure_self_signed_cert(self.cert_path, self.key_path, host="127.0.0.1", days=30)
        self.assertEqual(self.cert_path.stat().st_mtime, mtime_before)

    def test_ssl_health_endpoint(self) -> None:
        """Test HTTPS GET /api/health with SSL encryption."""
        ensure_self_signed_cert(self.cert_path, self.key_path, host="127.0.0.1")
        port = _free_port()
        self.server = run_server(
            paths=self.paths,
            host="127.0.0.1",
            port=port,
            ssl_enabled=True,
            ssl_cert=str(self.cert_path),
            ssl_key=str(self.key_path),
            block=False,
        )

        client_ctx = ssl.create_default_context(cafile=str(self.cert_path))
        req = urllib.request.Request(f"https://127.0.0.1:{port}/api/health")
        with urllib.request.urlopen(req, context=client_ctx, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data.get("status"), "ok")

    def test_ssl_auth_gate(self) -> None:
        """Test Basic Auth enforcement over HTTPS."""
        ensure_self_signed_cert(self.cert_path, self.key_path, host="127.0.0.1")
        port = _free_port()
        self.server = run_server(
            paths=self.paths,
            host="127.0.0.1",
            port=port,
            auth=("testuser", "secretpass99"),
            ssl_enabled=True,
            ssl_cert=str(self.cert_path),
            ssl_key=str(self.key_path),
            block=False,
        )

        client_ctx = ssl.create_default_context(cafile=str(self.cert_path))

        # Unauthenticated request -> 401
        req_no_auth = urllib.request.Request(f"https://127.0.0.1:{port}/api/dashboard")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_no_auth, context=client_ctx, timeout=5)
        self.assertEqual(ctx.exception.code, 401)
        self.assertIn("Basic", ctx.exception.headers.get("WWW-Authenticate", ""))

        # Authenticated request -> 200
        creds = base64.b64encode(b"testuser:secretpass99").decode("ascii")
        req_auth = urllib.request.Request(
            f"https://127.0.0.1:{port}/api/dashboard",
            headers={"Authorization": f"Basic {creds}"},
        )
        with urllib.request.urlopen(req_auth, context=client_ctx, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertIn("rows", payload)

    def test_plain_http_to_ssl_socket_fails(self) -> None:
        """Test that plain unencrypted HTTP requests to the TLS socket fail as expected."""
        ensure_self_signed_cert(self.cert_path, self.key_path, host="127.0.0.1")
        port = _free_port()
        self.server = run_server(
            paths=self.paths,
            host="127.0.0.1",
            port=port,
            ssl_enabled=True,
            ssl_cert=str(self.cert_path),
            ssl_key=str(self.key_path),
            block=False,
        )

        with self.assertRaises((urllib.error.URLError, ConnectionResetError, OSError)):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)

    def test_http_redirector(self) -> None:
        """Test that the companion HTTP redirector returns 301 to HTTPS."""
        ensure_self_signed_cert(self.cert_path, self.key_path, host="127.0.0.1")
        https_port = _free_port()
        http_port = _free_port()

        self.server = run_server(
            paths=self.paths,
            host="127.0.0.1",
            port=https_port,
            ssl_enabled=True,
            ssl_cert=str(self.cert_path),
            ssl_key=str(self.key_path),
            redirect_port=http_port,
            block=False,
        )

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            resp = opener.open(f"http://127.0.0.1:{http_port}/dashboard?tab=outages")
            status = resp.status
            loc = resp.headers.get("Location")
        except urllib.error.HTTPError as e:
            status = e.code
            loc = e.headers.get("Location")

        self.assertEqual(status, 301)
        self.assertEqual(loc, f"https://127.0.0.1:{https_port}/dashboard?tab=outages")


if __name__ == "__main__":
    unittest.main()
