"""Playwright browser tests for Outage Automation dashboard.

Run with:
    .venv-testing/bin/pytest python_system/tests/test_browser.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def dashboard_server(tmp_path_factory):
    scratch = tmp_path_factory.mktemp("browser_dash")
    env = os.environ.copy()
    env["OA_ROOT"] = str(scratch)
    env["OA_VECTOR_URL"] = (REPO / "parser-tests" / "inputs" / "vector-api.json").as_uri()

    # Initialize scratch repo
    init_cmd = [sys.executable, str(REPO / "python_system" / "run.py"), "init"]
    subprocess.run(init_cmd, check=True, env=env)
    
    # Run dashboard server on available port
    port = 58765
    env["OA_DASHBOARD_PORT"] = str(port)
    server_proc = subprocess.Popen(
        [sys.executable, str(REPO / "python_system" / "run.py"), "serve"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    
    # Wait for server ready
    import time
    import urllib.request
    
    url = f"http://127.0.0.1:{port}"
    ready = False
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.1)
            
    if not ready:
        server_proc.kill()
        raise RuntimeError("Failed to start dashboard server for browser test")
        
    yield url
    
    server_proc.terminate()
    server_proc.wait(timeout=5)


def test_dashboard_ui_renders(page, dashboard_server):
    page.goto(dashboard_server)
    page.wait_for_selector(".tab", timeout=5000)
    
    # Verify Tabs exist
    assert page.get_by_role("tab", name="Outages", exact=True).is_visible()
    assert page.get_by_role("tab", name="Customers").is_visible()
    assert page.get_by_role("tab", name="Regions").is_visible()


def test_customer_modal_has_vector(page, dashboard_server):
    page.goto(dashboard_server)
    page.get_by_role("tab", name="Customers").click()
    page.wait_for_timeout(300)
    page.get_by_text("+ Add customer", exact=False).click()
    page.wait_for_selector(".modal-backdrop:not(.hidden) .modal-body", timeout=3000)
    
    modal_text = page.inner_text(".modal-backdrop:not(.hidden) .modal-body").lower()
    assert "vector" in modal_text
    page.keyboard.press("Escape")


def test_region_modal_has_vector(page, dashboard_server):
    page.goto(dashboard_server)
    page.get_by_role("tab", name="Regions").click()
    page.wait_for_timeout(300)
    page.get_by_text("+ Add region", exact=False).click()
    page.wait_for_selector(".modal-backdrop:not(.hidden) .modal-body", timeout=3000)
    
    modal_text = page.inner_text(".modal-backdrop:not(.hidden) .modal-body").lower()
    assert "vector" in modal_text
    page.keyboard.press("Escape")


def test_m365_outage_renders_as_warning_not_resolved(page, tmp_path_factory):
    import socket
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    m365_xml = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Microsoft Admin Center Status</title>
    <link>https://status.cloud.microsoft/</link>
    <item>
      <guid isPermaLink="false">m365-test-degradation-1</guid>
      <title>Microsoft Admin Center</title>
      <description>&lt;div&gt;Impact on multiple services.&lt;div&gt;Resolved&lt;/div&gt;&lt;div&gt;Purview recovered.&lt;/div&gt;&lt;div&gt;Current status: Some scenarios remain degraded.&lt;/div&gt;&lt;/div&gt;</description>
      <pubDate>Wed, 02 Sep 2026 01:00:00 Z</pubDate>
      <status>ServiceDegradation</status>
    </item>
  </channel>
</rss>"""

    class _MockM365Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(m365_xml.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(m365_xml.encode("utf-8"))

    mock_httpd = HTTPServer(("127.0.0.1", 0), _MockM365Handler)
    mock_port = mock_httpd.server_address[1]
    mock_thread = threading.Thread(target=mock_httpd.serve_forever, daemon=True)
    mock_thread.start()

    scratch = tmp_path_factory.mktemp("browser_dash_m365")
    env = os.environ.copy()
    env["OA_ROOT"] = str(scratch)
    env["OA_M365_URL"] = f"http://127.0.0.1:{mock_port}/feed"

    init_cmd = [sys.executable, str(REPO / "python_system" / "run.py"), "init"]
    subprocess.run(init_cmd, check=True, env=env)

    # Ingest and apply to alert_history
    ingest_cmd = [sys.executable, str(REPO / "python_system" / "run.py"), "ingest", "m365", "--apply"]
    subprocess.run(ingest_cmd, check=True, env=env)

    port = 58766
    env["OA_DASHBOARD_PORT"] = str(port)
    server_proc = subprocess.Popen(
        [sys.executable, str(REPO / "python_system" / "run.py"), "serve"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    import time
    import urllib.request
    url = f"http://127.0.0.1:{port}"
    ready = False
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.1)

    try:
        assert ready, "Server failed to start"
        page.goto(url)
        page.wait_for_selector(".tab", timeout=5000)

        m365_group = page.locator('details[data-service-group="m365"]')
        assert m365_group.is_visible()
        assert m365_group.get_attribute("open") is not None

        summary_text = m365_group.locator("summary").inner_text()
        assert "1 warning" in summary_text
        assert "resolved" not in summary_text.lower()

        badge = m365_group.locator(".card-sev")
        assert badge.inner_text().strip().lower() == "warning"

        visible_text = page.locator("#visible-count").inner_text()
        assert "1 of 1" in visible_text

        # Verify severity filter counts: warning=1, resolved=0
        warning_count = page.locator('input.flt-sev[value="warning"] ~ .filter-count').inner_text()
        assert warning_count == "1"

        resolved_count = page.locator('input.flt-sev[value="resolved"] ~ .filter-count').inner_text()
        assert resolved_count == "0"
    finally:
        server_proc.terminate()
        server_proc.wait(timeout=5)
        mock_httpd.shutdown()


def test_azure_active_disruption_renders_as_critical(page, tmp_path_factory):
    import socket
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    azure_xml = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Azure Status</title>
    <link>https://status.azure.com/en-us/status</link>
    <item>
      <guid>azure-test-disruption-1</guid>
      <title>Virtual Machines - Service Disruption in Australia East</title>
      <description>Customers in Australia East may be experiencing connectivity issues with Virtual Machines. Engineering is engaged.</description>
      <pubDate>Wed, 02 Sep 2026 05:00:00 Z</pubDate>
    </item>
  </channel>
</rss>"""

    class _MockAzureHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(azure_xml.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(azure_xml.encode("utf-8"))

    mock_httpd = HTTPServer(("127.0.0.1", 0), _MockAzureHandler)
    mock_port = mock_httpd.server_address[1]
    mock_thread = threading.Thread(target=mock_httpd.serve_forever, daemon=True)
    mock_thread.start()

    scratch = tmp_path_factory.mktemp("browser_dash_azure")
    env = os.environ.copy()
    env["OA_ROOT"] = str(scratch)
    env["OA_AZURE_URL"] = f"http://127.0.0.1:{mock_port}/feed"

    init_cmd = [sys.executable, str(REPO / "python_system" / "run.py"), "init"]
    subprocess.run(init_cmd, check=True, env=env)

    # Ingest and apply to alert_history
    ingest_cmd = [sys.executable, str(REPO / "python_system" / "run.py"), "ingest", "azure", "--apply"]
    subprocess.run(ingest_cmd, check=True, env=env)

    port = 58767
    env["OA_DASHBOARD_PORT"] = str(port)
    server_proc = subprocess.Popen(
        [sys.executable, str(REPO / "python_system" / "run.py"), "serve"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    import time
    import urllib.request
    url = f"http://127.0.0.1:{port}"
    ready = False
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.1)

    try:
        assert ready, "Server failed to start"
        page.goto(url)
        page.wait_for_selector(".tab", timeout=5000)

        azure_group = page.locator('details[data-service-group="azure"]')
        assert azure_group.is_visible()
        assert azure_group.get_attribute("open") is not None

        summary_text = azure_group.locator("summary").inner_text()
        assert "1 critical" in summary_text

        badge = azure_group.locator(".card-sev")
        assert badge.inner_text().strip().lower() == "critical"

        visible_text = page.locator("#visible-count").inner_text()
        assert "1 of 1" in visible_text

        critical_count = page.locator('input.flt-sev[value="critical"] ~ .filter-count').inner_text()
        assert critical_count == "1"
    finally:
        server_proc.terminate()
        server_proc.wait(timeout=5)
        mock_httpd.shutdown()


