"""Unit tests for WizAdapter."""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from outage_automation.dashboard.regions_store import load_regions_csv
from outage_automation.ingest.wiz import (
    WizAdapter,
    _infer_regions,
    _infer_severity,
    _infer_sub_services,
    update_approved_regions_with_wiz,
)


class TestWizAdapter(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = WizAdapter()
        self.fixture_path = Path(__file__).resolve().parents[2] / "parser-tests" / "inputs" / "wiz-rss.xml"

    def _make_item(self, title: str, description: str = "") -> ET.Element:
        item = ET.Element("item")
        t = ET.SubElement(item, "title")
        t.text = title
        d = ET.SubElement(item, "description")
        d.text = description
        return item

    def test_fixture_parsing(self) -> None:
        if not self.fixture_path.exists():
            self.skipTest("Fixture wiz-rss.xml not found")
        xml_text = self.fixture_path.read_text(encoding="utf-8")
        root = ET.fromstring(xml_text)
        events = self.adapter.parse(root)
        self.assertGreater(len(events), 0)
        for ev in events:
            self.assertEqual(ev.service, "wiz")
            self.assertTrue(ev.raw_id)
            self.assertTrue(ev.title)
            self.assertIn(ev.severity, ("resolved", "critical", "major", "warning", "info"))
            self.assertGreater(len(ev.regions), 0)

    def test_severity_resolved(self) -> None:
        item = self._make_item(
            "Service issue",
            "<p><strong>Resolved</strong> - This incident has been resolved.</p>",
        )
        self.assertEqual(_infer_severity(item), "resolved")

    def test_severity_critical(self) -> None:
        item = self._make_item(
            "AWS us-west-2 Service Disruption",
            "<p><strong>Investigating</strong> - Major cloud outage disrupting services.</p>",
        )
        self.assertEqual(_infer_severity(item), "critical")

    def test_severity_major(self) -> None:
        item = self._make_item(
            "Portal and API Service Degradation",
            "<p><strong>Investigating</strong> - Elevated error rates across portal.</p>",
        )
        self.assertEqual(_infer_severity(item), "major")

    def test_severity_warning(self) -> None:
        item = self._make_item(
            "Delays in sensor ATM ingestion",
            "<p><strong>Investigating</strong> - We are seeing processing delays.</p>",
        )
        self.assertEqual(_infer_severity(item), "warning")

    def test_region_inference_cloud_and_dc(self) -> None:
        item = self._make_item(
            "Issue affecting tenants hosted in AWS us-west-2 region",
            "Affected Wiz DC: prod-us2",
        )
        regions = _infer_regions(item)
        self.assertIn("us-west-2", regions)
        self.assertIn("us", regions)

    def test_region_inference_fallback_global(self) -> None:
        item = self._make_item("General platform maintenance", "Routine update")
        regions = _infer_regions(item)
        self.assertEqual(regions, ["global"])

    def test_sub_service_inference(self) -> None:
        item = self._make_item(
            "Intermittent login failures on portal",
            "Authentication and login service degraded",
        )
        subs = _infer_sub_services(item)
        self.assertIn("authentication", subs)
        self.assertIn("portal", subs)

    def test_update_approved_regions_dynamic(self) -> None:
        with TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "approved-regions.csv"
            csv_path.write_text("aws,global,Global\n", encoding="utf-8")

            update_approved_regions_with_wiz({"us-west-2", "me-central-1"}, csv_path=csv_path)

            rows = load_regions_csv(csv_path)
            wiz_codes = {r.region_code for r in rows if r.service == "wiz"}
            self.assertIn("global", wiz_codes)
            self.assertIn("us-west-2", wiz_codes)
            self.assertIn("me-central-1", wiz_codes)


if __name__ == "__main__":
    unittest.main()
