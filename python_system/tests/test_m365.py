"""Unit tests for M365Adapter."""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from outage_automation.ingest.m365 import (
    M365Adapter,
    _infer_severity,
    _infer_regions,
    _infer_services,
    _augment_title,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "m365-api.json"


def _make_item(title: str, description: str = "", status: str = "") -> ET.Element:
    item = ET.Element("item")
    t = ET.SubElement(item, "title")
    t.text = title
    d = ET.SubElement(item, "description")
    d.text = description
    if status:
        s = ET.SubElement(item, "status")
        s.text = status
    return item


class TestM365SeverityParsing(unittest.TestCase):
    _make_item = staticmethod(_make_item)

    def setUp(self) -> None:
        self.adapter = M365Adapter()

    def test_explicit_service_degradation_with_partial_resolved_in_body(self) -> None:
        # Regression test for active incident containing 'Resolved' section in HTML body
        desc = (
            "<div>User impact: Users may experience issues when utilizing multiple M365 services.</div>"
            "<div>Resolved</div>"
            "<div>Microsoft Purview: Users can now access the portal.</div>"
            "<div>Current status: Most users should see recovery, though some scenarios remain degraded.</div>"
        )
        item = self._make_item("Microsoft Admin Center", desc, status="ServiceDegradation")
        self.assertEqual(self.adapter.parse_severity(item), "warning")

    def test_explicit_status_available(self) -> None:
        item = self._make_item("Microsoft Admin Center", "All systems operational.", status="Available")
        self.assertEqual(self.adapter.parse_severity(item), "resolved")

    def test_explicit_status_service_interruption(self) -> None:
        item = self._make_item("Exchange Online", "Total outage", status="ServiceInterruption")
        self.assertEqual(self.adapter.parse_severity(item), "critical")

    def test_title_resolved_keyword(self) -> None:
        item = self._make_item("Resolved: Teams sign-in issues", "The issue has been addressed.")
        self.assertEqual(self.adapter.parse_severity(item), "resolved")

    def test_investigating_with_critical_keywords(self) -> None:
        item = self._make_item(
            "Microsoft Teams Service Outage",
            "Teams service is unavailable. Users are unable to sign in.",
            status="Investigating",
        )
        self.assertEqual(self.adapter.parse_severity(item), "critical")

    def test_investigating_with_warning_keywords(self) -> None:
        item = self._make_item(
            "Exchange Online Delays",
            "Intermittent delays sending emails in APAC.",
            status="Investigating",
        )
        self.assertEqual(self.adapter.parse_severity(item), "warning")

    def test_info_fallback(self) -> None:
        item = self._make_item("Advisory on upcoming update", "Planned maintenance notice.", status="Advisory")
        self.assertEqual(self.adapter.parse_severity(item), "info")


class TestM365RegionAndServiceParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = M365Adapter()

    def _make_item(self, title: str, description: str = "") -> ET.Element:
        item = ET.Element("item")
        t = ET.SubElement(item, "title")
        t.text = title
        d = ET.SubElement(item, "description")
        d.text = description
        return item

    def test_region_inference_australia_and_apac(self) -> None:
        item = self._make_item("Issue in Sydney", "Affecting users across Australia and APAC.")
        regions = self.adapter.parse_regions(item)
        self.assertIn("global", regions)
        self.assertIn("australia", regions)

    def test_region_inference_new_zealand(self) -> None:
        item = self._make_item("Issue in Auckland", "Users in New Zealand experiencing delays.")
        regions = self.adapter.parse_regions(item)
        self.assertIn("global", regions)
        self.assertIn("new-zealand", regions)

    def test_service_extraction_and_title_augmentation(self) -> None:
        item = self._make_item(
            "Microsoft Admin Center",
            "Impact includes Exchange Online, Microsoft Teams, and OneDrive.",
        )
        augmented = self.adapter.parse_title(item)
        self.assertIn("Exchange Online", augmented)
        self.assertIn("Microsoft Teams", augmented)
        self.assertIn("OneDrive", augmented)

    def test_sub_services_defender_and_teams_calling(self) -> None:
        item = _make_item(
            "Service issue with Teams Phone and Direct Routing",
            "Users are experiencing PSTN calling drops. Microsoft Defender for Endpoint alerts also delayed."
        )
        adapter = M365Adapter()
        subs = adapter.parse_sub_services(item)
        self.assertIn("teams-calling", subs)
        self.assertIn("defender-endpoint", subs)


class TestM365FixtureParsing(unittest.TestCase):
    def test_parse_fixture(self) -> None:
        raw_xml = FIXTURE_PATH.read_text(encoding="utf-8")
        root = ET.fromstring(raw_xml)
        adapter = M365Adapter()
        events = adapter.parse(root)

        self.assertEqual(len(events), 3)
        # Event 1: Available -> resolved
        self.assertEqual(events[0].severity, "resolved")
        # Event 2: Intermittent delays -> warning
        self.assertEqual(events[1].severity, "warning")
        self.assertIn("australia", events[1].regions)
        # Event 3: Service Outage -> critical
        self.assertEqual(events[2].severity, "critical")
        self.assertIn("australia", events[2].regions)


if __name__ == "__main__":
    unittest.main()
