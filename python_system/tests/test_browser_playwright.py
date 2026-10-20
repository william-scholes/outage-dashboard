"""End-to-end browser test suite using Playwright.

Tests complete user journey:
1. Adding a new service and location in the Regions tab (e.g. wiz / us-west-2).
2. Adding a new customer subscribing to the new service/location.
3. Editing an existing customer.
4. Verifying Outages view displays cards with the new service/location and matched customer impact.
5. Verifying Graphs view renders native SVG charts and reflects all added values in filters and data.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    sync_playwright = None  # type: ignore[assignment]

from outage_automation.dashboard.server import run_server
from outage_automation.paths import resolve_paths


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright is not installed in this environment")
class TestPlaywrightBrowserE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="test_playwright_e2e_"))
        cls.paths = resolve_paths(str(cls.tmp))
        cls.paths.state_dir.mkdir(parents=True, exist_ok=True)

        # Copy approved-regions.csv to scratch directory
        repo_regions = Path(__file__).resolve().parents[2] / "approved-regions.csv"
        cls.paths.approved_regions_csv.parent.mkdir(parents=True, exist_ok=True)
        if repo_regions.exists():
            shutil.copy(repo_regions, cls.paths.approved_regions_csv)
        else:
            cls.paths.approved_regions_csv.write_text(
                "Service,Region Code,Display Name\n"
                "aws,global,Global\n"
                "aws,ap-southeast-2,Sydney\n"
                "wiz,global,Global\n",
                encoding="utf-8",
            )

        # Seed initial customer
        (cls.paths.customers_path).write_text(
            json.dumps([
                {
                    "customer_id": "seed-corp",
                    "name": "Seed Corp",
                    "subscriptions": {"aws": ["ap-southeast-2"]},
                }
            ], indent=2),
            encoding="utf-8",
        )

        # Seed initial outages including a Wiz outage
        cls.wiz_outage_id = "wiz-test-outage-001"
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        exp_iso = (now_dt + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        (cls.paths.alert_history_path).write_text(
            json.dumps([
                {
                    "raw_id": cls.wiz_outage_id,
                    "service": "wiz",
                    "title": "Wiz Services Disruption in Frankfurt",
                    "description": "Investigating issues affecting Wiz portal in eu-central-1.",
                    "severity": "critical",
                    "regions": ["eu-central-1"],
                    "sub_services": ["portal"],
                    "link": "https://status.wiz.io/incidents/test-wiz-1",
                    "published_at": now_iso,
                    "last_seen_at": now_iso,
                    "expires_at": exp_iso,
                },
                {
                    "raw_id": "aws-test-outage-001",
                    "service": "aws",
                    "title": "Amazon EC2 Network Performance",
                    "description": "Degraded network connectivity in ap-southeast-2.",
                    "severity": "warning",
                    "regions": ["ap-southeast-2"],
                    "sub_services": [],
                    "link": "https://status.aws.amazon.com/test",
                    "published_at": now_iso,
                    "last_seen_at": now_iso,
                    "expires_at": exp_iso,
                },
            ], indent=2),
            encoding="utf-8",
        )

        cls.port = _free_port()
        cls.server = run_server(
            paths=cls.paths,
            host="127.0.0.1",
            port=cls.port,
            auth=None,
            block=False,
        )

        # Launch Playwright headless browser
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.browser.close()
            cls.pw.stop()
        except Exception:
            pass
        try:
            cls.server.shutdown()
            cls.server.server_close()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_full_browser_lifecycle(self) -> None:
        context = self.browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        console_errors: list[str] = []
        failed_requests: list[str] = []
        bad_responses: list[str] = []

        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("requestfailed", lambda req: failed_requests.append(f"{req.method} {req.url}: {req.failure}"))
        page.on("response", lambda resp: bad_responses.append(f"{resp.request.method} {resp.url} -> {resp.status}") if resp.status >= 400 else None)

        base_url = f"http://127.0.0.1:{self.port}/dashboard"

        # -------------------------------------------------------------
        # STEP 1: Add new service/location in Regions tab
        # -------------------------------------------------------------
        page.goto(base_url, wait_until="networkidle")
        page.wait_for_selector("#app")

        # Verify "How it works" modal contains Wiz
        page.click("#help-btn")
        page.wait_for_selector("#help-backdrop:not(.hidden)")
        help_text = page.inner_text("#help-modal")
        self.assertIn("Wiz", help_text)
        self.assertIn("status.wiz.io", help_text)
        page.click("#help-close")
        page.wait_for_selector("#help-backdrop", state="hidden")

        # Click Regions tab
        page.click('#tabs-nav button[data-view="regions"]')
        page.wait_for_selector("#view-regions:not([hidden])")

        # Open Add Region modal
        page.click("#reg-add")
        page.wait_for_selector("#reg-edit-backdrop:not(.hidden)")

        # Fill Region form
        page.select_option("#reg-svc", "wiz")
        page.fill("#reg-code", "eu-central-1")
        page.fill("#reg-display", "EU Central 1 (Frankfurt)")

        # Save Region
        page.click("#reg-save")
        page.wait_for_selector("#reg-edit-backdrop", state="hidden")

        # Verify region appears in table
        page.wait_for_selector("#reg-table-wrap tr")
        table_text = page.inner_text("#reg-table-wrap")
        self.assertIn("wiz", table_text)
        self.assertIn("eu-central-1", table_text)
        self.assertIn("EU Central 1 (Frankfurt)", table_text)

        # -------------------------------------------------------------
        # STEP 2: Add new customer in Customers tab
        # -------------------------------------------------------------
        page.click('#tabs-nav button[data-view="customers"]')
        page.wait_for_selector("#view-customers:not([hidden])")

        # Open Add Customer modal
        page.click("#cust-add")
        page.wait_for_selector("#cust-edit-backdrop:not(.hidden)")

        # Fill Customer Name
        page.fill("#cust-name", "Playwright Global Corp")

        # Check wiz service toggle
        wiz_toggle = page.wait_for_selector('.svc-toggle[data-svc="wiz"]')
        if not wiz_toggle.is_checked():
            wiz_toggle.check()

        # Check eu-central-1 region checkbox for wiz
        wiz_region_cb = page.wait_for_selector('.region-check[data-svc="wiz"][value="eu-central-1"]')
        wiz_region_cb.check()

        # Save Customer
        page.click("#cust-save")
        page.wait_for_selector("#cust-edit-backdrop", state="hidden")

        # Verify customer appears in customer list
        page.wait_for_selector("#cust-list .customer-editor-row")
        cust_list_text = page.inner_text("#cust-list")
        self.assertIn("Playwright Global Corp", cust_list_text)

        # -------------------------------------------------------------
        # STEP 3: Edit existing customer and add WIZ > Global
        # -------------------------------------------------------------
        page.click('[data-cust-edit="seed-corp"]')
        page.wait_for_selector("#cust-edit-backdrop:not(.hidden)")

        # Edit name
        page.fill("#cust-name", "Seed Corp International")

        # Add WIZ service with Global region to existing customer
        seed_wiz_toggle = page.wait_for_selector('.svc-toggle[data-svc="wiz"]')
        if not seed_wiz_toggle.is_checked():
            seed_wiz_toggle.check()

        seed_wiz_global_cb = page.wait_for_selector('.region-check[data-svc="wiz"][value="global"]')
        if not seed_wiz_global_cb.is_checked():
            seed_wiz_global_cb.check()

        # Save updated customer
        page.click("#cust-save")
        page.wait_for_selector("#cust-edit-backdrop", state="hidden")

        # Verify updated name and new subscriptions are reflected
        page.wait_for_selector("#cust-list .customer-editor-row")
        updated_cust_list = page.inner_text("#cust-list")
        self.assertIn("Seed Corp International", updated_cust_list)
        self.assertIn("global", updated_cust_list)
        self.assertNotIn("Seed Corp\n", updated_cust_list)

        # -------------------------------------------------------------
        # STEP 4: Outages Dashboard Verification
        # -------------------------------------------------------------
        page.click('#tabs-nav button[data-view="outages"]')
        page.wait_for_selector("#view-outages:not([hidden])")

        # Wait for outage cards to render
        page.wait_for_selector(".card")
        outages_text = page.inner_text("#card-list")
        self.assertIn("Wiz Services Disruption in Frankfurt", outages_text)
        self.assertIn("eu-central-1", outages_text)

        # Verify customer impact badges match BOTH new customer and edited existing customer
        wiz_card = page.locator('.card[data-service="wiz"]').first
        self.assertTrue(wiz_card.is_visible())
        card_text = wiz_card.inner_text()
        self.assertIn("Playwright Global Corp", card_text)
        self.assertIn("Seed Corp International", card_text)

        # -------------------------------------------------------------
        # STEP 5: Graphs View Verification
        # -------------------------------------------------------------
        page.click('#tabs-nav button[data-view="graphs"]')
        page.wait_for_selector("#view-graphs:not([hidden])")

        # Verify Native SVG element is present and rendered
        page.wait_for_selector("#graph-svg")
        # Give graph animation/render a moment
        time.sleep(0.5)

        svg_content = page.inner_html("#graph-svg")
        self.assertGreater(len(svg_content), 50)
        self.assertTrue("<rect" in svg_content or "<path" in svg_content or "<g" in svg_content)

        # Verify added customer is in the Customer dropdown filter
        cust_dropdown_html = page.inner_html("#graph-customer-filter")
        self.assertIn("Playwright Global Corp", cust_dropdown_html)

        # Verify added provider is in provider filters
        provider_filters_html = page.inner_html("#graph-provider-filters")
        self.assertIn("wiz", provider_filters_html)

        # Verify graph summary bar rendered metrics
        summary_bar = page.inner_text("#graphs-summary-bar")
        self.assertTrue("Outages" in summary_bar or "Impact" in summary_bar or len(summary_bar) > 0)

        # Test switching chart type to grouped-bar and check SVG update
        page.select_option("#graph-chart-type", "grouped-bar")
        time.sleep(0.3)
        updated_svg = page.inner_html("#graph-svg")
        self.assertIn("<rect", updated_svg)

        # -------------------------------------------------------------
        # STEP 6: All Outages View Verification
        # -------------------------------------------------------------
        page.click('#tabs-nav button[data-view="all"]')
        page.wait_for_selector("#view-all:not([hidden])")

        # Verify headers
        self.assertTrue(page.locator('th[data-sort="published_at"]').is_visible())
        self.assertTrue(page.locator('th[data-sort="service"]').is_visible())
        self.assertTrue(page.locator('th[data-sort="severity"]').is_visible())
        self.assertTrue(page.locator('th[data-sort="title"]').is_visible())

        # Verify rows rendered
        all_rows = page.locator("#all-table-wrap tr.table-outage-row")
        self.assertGreater(all_rows.count(), 0)

        # Test search filter
        page.fill("#all-search", "wiz")
        time.sleep(0.3)
        filtered_rows = page.locator("#all-table-wrap tr.table-outage-row")
        self.assertGreater(filtered_rows.count(), 0)
        self.assertIn("wiz", filtered_rows.first.inner_text().lower())

        # Clear search and test sort
        page.fill("#all-search", "")
        time.sleep(0.2)
        page.click('th[data-sort="service"]')
        time.sleep(0.2)
        self.assertTrue(page.locator('th[data-sort="service"] .sort-indicator').is_visible())

        # Asset integrity & error assertions
        self.assertEqual(console_errors, [], f"Console errors detected: {console_errors}")
        self.assertEqual(failed_requests, [], f"Failed requests detected: {failed_requests}")
        self.assertEqual(bad_responses, [], f"Bad 4xx/5xx responses detected: {bad_responses}")

        page.close()
        context.close()


@unittest.skipUnless(HAS_PLAYWRIGHT, "playwright is not installed in this environment")
class TestPlaywrightHTTPSAndAuth(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="test_playwright_https_"))
        cls.paths = resolve_paths(str(cls.tmp))
        cls.paths.state_dir.mkdir(parents=True, exist_ok=True)
        (cls.paths.alert_history_path).write_text("[]", encoding="utf-8")
        (cls.paths.customers_path).write_text("[]", encoding="utf-8")

        cls.cert_path = cls.tmp / "test_dashboard.crt"
        cls.key_path = cls.tmp / "test_dashboard.key"
        from outage_automation.dashboard.server import ensure_self_signed_cert
        ensure_self_signed_cert(cls.cert_path, cls.key_path, host="127.0.0.1")

        cls.https_port = _free_port()
        cls.http_redirect_port = _free_port()
        cls.auth = ("viewer", "securepass123")

        cls.server = run_server(
            paths=cls.paths,
            host="127.0.0.1",
            port=cls.https_port,
            auth=cls.auth,
            ssl_enabled=True,
            ssl_cert=str(cls.cert_path),
            ssl_key=str(cls.key_path),
            redirect_port=cls.http_redirect_port,
            block=False,
        )

        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.browser.close()
            cls.pw.stop()
        except Exception:
            pass
        try:
            cls.server.shutdown()
            cls.server.server_close()
        except Exception:
            pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_http_to_https_redirect(self) -> None:
        context = self.browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{self.http_redirect_port}/dashboard")
        self.assertTrue(page.url.startswith(f"https://127.0.0.1:{self.https_port}/"))
        page.close()
        context.close()

    def test_unauthenticated_requests_do_not_lock_out(self) -> None:
        """Verify that multiple unauthenticated API requests receive 401 and are never locked out."""
        context = self.browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        for _ in range(6):
            resp = page.request.get(f"https://127.0.0.1:{self.https_port}/api/dashboard")
            self.assertEqual(resp.status, 401)

        page.close()
        context.close()

    def test_authenticated_https_session_and_asset_integrity(self) -> None:
        """Verify that all assets (HTML, CSS, JS, API, favicon) load cleanly with 200 and no console/network errors."""
        auth_header = "Basic " + base64.b64encode(b"viewer:securepass123").decode()
        context = self.browser.new_context(
            ignore_https_errors=True,
            extra_http_headers={"Authorization": auth_header},
        )
        page = context.new_page()

        console_errors: list[str] = []
        failed_requests: list[str] = []
        bad_responses: list[str] = []

        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("requestfailed", lambda req: failed_requests.append(f"{req.method} {req.url}: {req.failure}"))
        page.on("response", lambda resp: bad_responses.append(f"{resp.request.method} {resp.url} -> {resp.status}") if resp.status >= 400 else None)

        resp = page.goto(f"https://127.0.0.1:{self.https_port}/dashboard", wait_until="networkidle")
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status, 200)
        page.wait_for_selector("#app")

        # Verify favicon links exist in document head
        favicon_link = page.locator('link[rel*="icon"]').first
        self.assertTrue(favicon_link.count() > 0)

        # Verify favicon endpoint resolves directly with 200
        fav_resp = page.request.get(f"https://127.0.0.1:{self.https_port}/favicon.ico")
        self.assertEqual(fav_resp.status, 200)
        self.assertIn("image/", fav_resp.headers.get("content-type", ""))

        # Rapid reloads over HTTPS
        for _ in range(3):
            reload_resp = page.reload(wait_until="networkidle")
            self.assertIsNotNone(reload_resp)
            self.assertEqual(reload_resp.status, 200)

        # Assert no errors or failures
        self.assertEqual(console_errors, [], f"Console errors detected: {console_errors}")
        self.assertEqual(failed_requests, [], f"Failed requests detected: {failed_requests}")
        self.assertEqual(bad_responses, [], f"Bad 4xx/5xx responses detected: {bad_responses}")

        page.close()
        context.close()


if __name__ == "__main__":
    unittest.main()
