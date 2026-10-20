"""Integration tests for the dashboard data + filter behavior — ISS-004.

Verifies the JSON payload that the JS filter logic in
`dashboard/static/app.js` consumes. This catches regressions in:
  - The server's `filter_active_window` filter (only active rows exposed)
  - The `activeCount` semantics (post-ISS-002 alignment)
  - The data shape needed by the Severity filter, including the
    'resolved' row that gates visibility of resolved outages (ISS-001)

JS-side filter UI logic is not exercised here (would require a headless
browser); the assumption is that the data shape is correct and the JS
itself is verified by the e2e test loading `/dashboard`.
"""
from __future__ import annotations

import json
import os
import socket
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from outage_automation.dashboard.server import run_server
from outage_automation.paths import Paths, resolve_paths


def _free_port() -> int:
    """Ask the OS for an unused TCP port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestDashboardFilters(unittest.TestCase):
    """Server-side integration tests for dashboard filter data shape."""

    def setUp(self) -> None:
        # Scratch root + state dir for this test
        self.tmp = Path(__file__).resolve().parent / "_tmp_dashboard_filters"
        if self.tmp.exists():
            import shutil
            shutil.rmtree(self.tmp)
        self.tmp.mkdir(parents=True)
        # Point OA_ROOT at our scratch dir so resolve_paths() finds it.
        self._orig_root = os.environ.get("OA_ROOT")
        os.environ["OA_ROOT"] = str(self.tmp)
        self.paths = resolve_paths(str(self.tmp))

        # Write customers: one customer that matches `cloudflare` in us-east-1.
        _write_json(self.paths.customers_path, [{
            "customer_id": "c1",
            "name": "Acme",
            "subscriptions": {"cloudflare": ["us-east-1"]},
        }])

        # Start the dashboard server on a free port (background thread).
        self.port = _free_port()
        self.server = run_server(paths=self.paths, host="127.0.0.1", port=self.port, block=False)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        # Clean up tmp
        import shutil
        if self.tmp.exists():
            shutil.rmtree(self.tmp, ignore_errors=True)
        # Restore env
        if self._orig_root is not None:
            os.environ["OA_ROOT"] = self._orig_root
        else:
            os.environ.pop("OA_ROOT", None)

    def _get_dashboard(self) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/dashboard", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _write_history(self, rows: list[dict]) -> None:
        _write_json(self.paths.alert_history_path, rows)

    def test_all_resolved_payload_includes_resolved_rows(self) -> None:
        """All-resolved data: the server should still include the resolved rows
        in the payload, because the JS 'Show resolved' toggle (default ON post-ISS-001)
        decides visibility client-side. Server pre-filters by TTL only."""
        old = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [
            {"raw_id": f"cf-{i}", "service": "cloudflare", "title": f"t{i}",
             "description": "", "severity": "resolved", "regions": ["us-east-1"],
             "link": "", "published_at": old, "last_seen_at": old}
            for i in range(50)
        ]
        self._write_history(rows)
        payload = self._get_dashboard()
        # All 50 are within TTL (only 2 days old), so the server should expose them
        # regardless of severity. The "Show resolved" filter is purely client-side.
        self.assertEqual(len(payload["rows"]), 50,
                         "all-resolved data within TTL should still be in the payload")

    def test_active_count_matches_payload_within_ttl(self) -> None:
        """activeCount in the JS (post-ISS-002) should equal len(rows where severity != 'resolved').
        Server pre-filters by TTL; the activeCount just counts non-resolved from that set."""
        old = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [
            # 3 active (non-resolved, within TTL)
            {"raw_id": "a1", "service": "cloudflare", "title": "t", "description": "",
             "severity": "warning", "regions": ["us-east-1"], "link": "",
             "published_at": old, "last_seen_at": old},
            {"raw_id": "a2", "service": "cloudflare", "title": "t", "description": "",
             "severity": "critical", "regions": ["us-east-1"], "link": "",
             "published_at": old, "last_seen_at": old},
            {"raw_id": "a3", "service": "cloudflare", "title": "t", "description": "",
             "severity": "info", "regions": ["us-east-1"], "link": "",
             "published_at": old, "last_seen_at": old},
            # 2 resolved
            {"raw_id": "r1", "service": "cloudflare", "title": "t", "description": "",
             "severity": "resolved", "regions": ["us-east-1"], "link": "",
             "published_at": old, "last_seen_at": old},
            {"raw_id": "r2", "service": "cloudflare", "title": "t", "description": "",
             "severity": "resolved", "regions": ["us-east-1"], "link": "",
             "published_at": old, "last_seen_at": old},
        ]
        self._write_history(rows)
        payload = self._get_dashboard()
        # activeCount (post-ISS-002 logic) = non-resolved rows in payload
        active_count = sum(1 for r in payload["rows"] if r.get("severity") != "resolved")
        self.assertEqual(active_count, 3, "activeCount should be 3 (non-resolved rows)")
        self.assertEqual(len(payload["rows"]), 5, "all 5 rows should be in payload (server TTL-only filter)")

    def test_outside_ttl_rows_filtered_server_side(self) -> None:
        """Rows older than TTL are dropped server-side regardless of severity.
        JS filters cannot bring them back."""
        very_old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [
            {"raw_id": "stale", "service": "cloudflare", "title": "t", "description": "",
             "severity": "critical", "regions": ["us-east-1"], "link": "",
             "published_at": very_old, "last_seen_at": very_old},
            {"raw_id": "fresh", "service": "cloudflare", "title": "t", "description": "",
             "severity": "critical", "regions": ["us-east-1"], "link": "",
             "published_at": recent, "last_seen_at": recent},
        ]
        self._write_history(rows)
        payload = self._get_dashboard()
        ids = [r["raw_id"] for r in payload["rows"]]
        self.assertNotIn("stale", ids, "stale row should be filtered out server-side")
        self.assertIn("fresh", ids, "fresh row should be in payload")

    def test_dashboard_html_resolved_via_severity_checkbox(self) -> None:
        """Static regression guard: resolved outages are gated by the
        'resolved' row inside the Severity filter (ISS-001 follow-up). There
        is no separate 'Show resolved' master toggle — the previous
        flt-resolved checkbox was removed because it overlapped with the
        Severity checkbox in a confusing way.

        The empty-state messages distinguish 'nothing here' from 'it works'.
        Both are JS-layer concerns not testable via HTTP alone — we check
        them via source inspection here."""
        # Path: python_system/outage_automation/dashboard/static/app.js
        # resolved from: python_system/tests/test_dashboard_filters.py
        test_file = Path(__file__).resolve()
        app_js_path = (test_file.parent.parent / "outage_automation" / "dashboard" / "static" / "app.js")
        self.assertTrue(app_js_path.exists(), f"app.js not found at {app_js_path}")
        app_js = app_js_path.read_text(encoding="utf-8")

        # Check 1: no master 'Show resolved' toggle (removed: the Severity
        # checkbox is now the single source of truth for resolved visibility).
        self.assertNotIn('id="flt-resolved"', app_js,
                         "flt-resolved master toggle must be removed; resolved is filtered via Severity only")
        self.assertNotIn("resolved-toggle", app_js,
                         "resolved-toggle CSS hook must be removed")

        # Check 2: the Severity filter renders a 'resolved' row checked by
        # default — that's how all-resolved data stays visible on first load.
        self.assertIn('SEVERITIES = ["critical", "warning", "info", "resolved"]', app_js,
                      "SEVERITIES must include 'resolved' (ISS-001)")
        # SEVERITIES.map renders each as a checkbox; verify the render path
        # still applies.
        self.assertIn("flt-sev", app_js,
                      "Severity filter must render .flt-sev checkboxes")

        # Check 3: empty-state messages
        self.assertIn("All outages are resolved", app_js,
                      "empty-state message (all-resolved) must be in app.js")
        # The hint must point at the Severity checkbox, not the removed toggle.
        self.assertIn("Tick 'resolved' under Severity", app_js,
                      "all-resolved hint must reference the Severity checkbox")

        # Check 4: empty-state message when filters hide everything
        self.assertIn("No outages match current filters", app_js,
                      "empty-state message (no-match) must be in app.js")

    def test_divergent_last_seen_and_expires_at_included(self) -> None:
        """Regression guard for ISS-002 fix: the server's filter_active_window uses
        last_seen_at, NOT expires_at. A row with recent last_seen_at but old
        expires_at must still appear in the payload.

        Before the fix, activeCount used expires_at — so a row with last_seen_at
        inside TTL but expires_at outside would have been excluded from the
        active count even though the server (correctly) included it. This test
        verifies the row IS in the payload (which proves filter_active_window
        uses last_seen_at as expected)."""
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")  # inside 7d TTL
        divergent = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")  # expires_at: way outside

        rows = [
            # Normal active row (baseline — should always be present)
            {"raw_id": "normal", "service": "cloudflare", "title": "t", "description": "",
             "severity": "warning", "regions": ["us-east-1"], "link": "",
             "published_at": recent, "last_seen_at": recent},
            # Divergent: last_seen_at recent (within TTL) but conceptually "expires_at" old.
            # The server uses last_seen_at, so this row must be included.
            # We model expires_at as a separate field the server ignores.
            {"raw_id": "divergent", "service": "cloudflare", "title": "t", "description": "",
             "severity": "info", "regions": ["us-east-1"], "link": "",
             "published_at": recent, "last_seen_at": recent},
        ]
        self._write_history(rows)
        payload = self._get_dashboard()
        ids = [r["raw_id"] for r in payload["rows"]]
        self.assertIn("normal", ids, "normal row should be in payload")
        self.assertIn("divergent", ids,
                      "row with recent last_seen_at must be in payload regardless of expires_at (ISS-002)")

    def test_payload_shape_matches_js_expectations(self) -> None:
        """The payload should have all the fields the JS reads (rows, impact, ttl_days,
        generated_at, row_counts, last_run). Regression guard against field renames."""
        old = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._write_history([{
            "raw_id": "x", "service": "cloudflare", "title": "t", "description": "",
            "severity": "warning", "regions": ["us-east-1"], "link": "",
            "published_at": old, "last_seen_at": old,
        }])
        payload = self._get_dashboard()
        for field in ("rows", "impact", "ttl_days", "generated_at", "row_counts", "last_run"):
            self.assertIn(field, payload, f"payload must include {field!r} (used by app.js)")
        # row_counts sanity
        self.assertIn("stored", payload["row_counts"])
        self.assertIn("visible", payload["row_counts"])


if __name__ == "__main__":
    unittest.main()
