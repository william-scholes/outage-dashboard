"""Unit tests for AzureAdapter."""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from outage_automation.ingest.azure import (
    AzureAdapter,
    _infer_severity,
    _infer_regions,
    _infer_guid,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "azure-rss.xml"


class TestAzureSeverityParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = AzureAdapter()

    def _make_item(self, title: str, description: str = "", status: str = "") -> ET.Element:
        item = ET.Element("item")
        t = ET.SubElement(item, "title")
        t.text = title
        d = ET.SubElement(item, "description")
        d.text = description
        if status:
            s = ET.SubElement(item, "status")
            s.text = status
        return item

    def test_title_critical_keywords(self) -> None:
        self.assertEqual(self.adapter.parse_severity(self._make_item("Virtual Machines - Critical Outage")), "critical")
        self.assertEqual(self.adapter.parse_severity(self._make_item("Networking - Service Disruption in Australia East")), "critical")
        self.assertEqual(self.adapter.parse_severity(self._make_item("Azure Front Door - Service Unavailable")), "critical")

    def test_title_warning_keywords(self) -> None:
        self.assertEqual(self.adapter.parse_severity(self._make_item("Storage Accounts - Degraded Performance")), "warning")
        self.assertEqual(self.adapter.parse_severity(self._make_item("Cosmos DB - Intermittent Latency Issues")), "warning")
        self.assertEqual(self.adapter.parse_severity(self._make_item("App Service - Elevated Error Rates")), "warning")

    def test_title_resolved_keywords(self) -> None:
        self.assertEqual(self.adapter.parse_severity(self._make_item("App Service - Resolved in Australia Central")), "resolved")
        self.assertEqual(self.adapter.parse_severity(self._make_item("Virtual Machines - Mitigated")), "resolved")
        self.assertEqual(self.adapter.parse_severity(self._make_item("Azure SQL - Recovery Completed")), "resolved")

    def test_description_fallback_critical(self) -> None:
        item = self._make_item(
            "Azure Services Update - Australia East",
            "Customers may experience unavailable VM instances.",
        )
        self.assertEqual(self.adapter.parse_severity(item), "critical")

    def test_description_fallback_warning(self) -> None:
        item = self._make_item(
            "Incident Communication - Australia East",
            "Engineers are investigating elevated error rates and latency.",
        )
        self.assertEqual(self.adapter.parse_severity(item), "warning")

    def test_description_resolved_text_does_not_override_active_incident(self) -> None:
        # Partial recovery text in description should not mark active incident resolved
        item = self._make_item(
            "Incident Communication - Australia East",
            "Authentication has been resolved, but degraded performance continues across VMs.",
        )
        self.assertEqual(self.adapter.parse_severity(item), "warning")

    def test_explicit_status_tag(self) -> None:
        self.assertEqual(self.adapter.parse_severity(self._make_item("Update", "Desc", status="Available")), "resolved")
        self.assertEqual(self.adapter.parse_severity(self._make_item("Update", "Desc", status="Mitigated")), "resolved")
        self.assertEqual(self.adapter.parse_severity(self._make_item("Update", "Desc", status="ServiceInterruption")), "critical")
        self.assertEqual(self.adapter.parse_severity(self._make_item("Update", "Desc", status="ServiceDegradation")), "warning")

    def test_info_fallback(self) -> None:
        item = self._make_item("Advisory for Azure AD", "Informational maintenance notice.")
        self.assertEqual(self.adapter.parse_severity(item), "info")


class TestAzureRegionParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = AzureAdapter()

    def _make_item(self, title: str, description: str = "") -> ET.Element:
        item = ET.Element("item")
        t = ET.SubElement(item, "title")
        t.text = title
        d = ET.SubElement(item, "description")
        d.text = description
        return item

    def test_australia_east_region(self) -> None:
        item = self._make_item("Storage Degradation", "Affecting Australia East datacenter.")
        regions = self.adapter.parse_regions(item)
        self.assertIn("australia-east", regions)

    def test_australia_southeast_region(self) -> None:
        item = self._make_item("VM Outage in Australia Southeast", "Degradation in melbourne region.")
        regions = self.adapter.parse_regions(item)
        self.assertIn("australia-southeast", regions)

    def test_global_region(self) -> None:
        item = self._make_item("Active Directory Advisory", "Notice for customers worldwide across all regions.")
        regions = self.adapter.parse_regions(item)
        self.assertEqual(regions, ["global"])

    def test_non_anz_ignored(self) -> None:
        item = self._make_item("Storage Issue in East US", "Customers in East US and West Europe affected.")
        regions = self.adapter.parse_regions(item)
        self.assertEqual(regions, [])


class TestAzureFixtureParsing(unittest.TestCase):
    def test_parse_fixture(self) -> None:
        raw_xml = FIXTURE_PATH.read_text(encoding="utf-8")
        root = ET.fromstring(raw_xml)
        adapter = AzureAdapter()
        events = adapter.parse(root)

        self.assertEqual(len(events), 4)
        # Event 1: Critical Outage in Australia East
        self.assertEqual(events[0].severity, "critical")
        self.assertIn("australia-east", events[0].regions)

        # Event 2: Degraded Performance in Australia Southeast
        self.assertEqual(events[1].severity, "warning")
        self.assertIn("australia-southeast", events[1].regions)

        # Event 3: Resolved in Australia Central
        self.assertEqual(events[2].severity, "resolved")
        self.assertIn("australia-central", events[2].regions)

        # Event 4: Global Advisory
        self.assertEqual(events[3].severity, "info")
        self.assertEqual(events[3].regions, ["global"])


if __name__ == "__main__":
    unittest.main()
