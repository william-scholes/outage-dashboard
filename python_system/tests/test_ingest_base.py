"""Unit tests for outage_automation.ingest.base."""
from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

from outage_automation.ingest.base import fetch_url


@contextmanager
def _fake_urlopen(read_data: bytes):
    """Return a fake urlopen that works as a context manager."""
    class FakeResp:
        def read(self):
            return read_data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    yield FakeResp()


class TestFetchUrlSchemeValidation(unittest.TestCase):
    """Scheme allowlist tests — ISS-003."""

    def test_file_scheme_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            fetch_url("file:///etc/passwd")
        self.assertIn("file", str(ctx.exception))
        self.assertIn("not allowed", str(ctx.exception))

    def test_ftp_scheme_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            fetch_url("ftp://example.com/")
        self.assertIn("ftp", str(ctx.exception))
        self.assertIn("not allowed", str(ctx.exception))

    def test_gopher_scheme_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            fetch_url("gopher://example.com/")
        self.assertIn("gopher", str(ctx.exception))
        self.assertIn("not allowed", str(ctx.exception))

    def test_http_scheme_allowed(self) -> None:
        fake_resp = b'{"ok": true}'
        with mock.patch(
            "outage_automation.ingest.base.urllib.request.urlopen",
            side_effect=lambda *args, **kwargs: _fake_urlopen(fake_resp),
        ) as mock_urlopen:
            result = fetch_url("http://example.invalid/api")
            self.assertEqual(result, fake_resp)
            mock_urlopen.assert_called_once()
            self.assertEqual(mock_urlopen.call_args[0][0].full_url, "http://example.invalid/api")

    def test_https_scheme_allowed(self) -> None:
        fake_resp = b'{"ok": true}'
        with mock.patch(
            "outage_automation.ingest.base.urllib.request.urlopen",
            side_effect=lambda *args, **kwargs: _fake_urlopen(fake_resp),
        ) as mock_urlopen:
            result = fetch_url("https://example.invalid/api")
            self.assertEqual(result, fake_resp)
            mock_urlopen.assert_called_once()
            self.assertEqual(mock_urlopen.call_args[0][0].full_url, "https://example.invalid/api")


if __name__ == "__main__":
    unittest.main()
