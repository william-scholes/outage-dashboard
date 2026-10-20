"""Unit tests for ZscalerAdapter."""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from outage_automation.ingest.zscaler import ZscalerAdapter
from outage_automation.dashboard.regions_store import load_regions_csv


class TestZscalerRegionParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ZscalerAdapter()

    def _make_item(self, title: str, description: str = "") -> ET.Element:
        item = ET.Element("item")
        t = ET.SubElement(item, "title")
        t.text = title
        d = ET.SubElement(item, "description")
        d.text = description
        return item

    def test_parse_regions_exact_apac_match(self) -> None:
        # Melbourne II is in APAC
        item = self._make_item("Melbourne II Datacenter Scheduled Maintenance")
        regions = self.adapter.parse_regions(item)
        self.assertEqual(regions, ["melbourne-ii"])

    def test_parse_regions_base_apac_match(self) -> None:
        # Sydney matches base of Sydney III / Sydney V
        item = self._make_item("Sydney Datacenter Maintenance")
        regions = self.adapter.parse_regions(item)
        self.assertEqual(regions, ["sydney-iii", "sydney-v"])

    def test_parse_regions_multiple_apac_matches(self) -> None:
        # Sydney III, Melbourne II
        item = self._make_item("Urgent Maintenance on Sydney III and Melbourne II datacenters")
        regions = self.adapter.parse_regions(item)
        self.assertEqual(regions, ["melbourne-ii", "sydney-iii"])

    def test_parse_regions_non_apac_ignore(self) -> None:
        # Madrid IV is in EMEA (non-APAC)
        item = self._make_item("Madrid IV Datacenter Maintenance")
        regions = self.adapter.parse_regions(item)
        self.assertEqual(regions, [])

    def test_parse_regions_non_apac_region_keywords(self) -> None:
        # Scheduled Maintenance Extended - AMER Region
        # Scheduled Maintenance Extended - EMEA Region
        item_amer = self._make_item("Scheduled Maintenance Extended - AMER Region")
        regions_amer = self.adapter.parse_regions(item_amer)
        self.assertEqual(regions_amer, [])

        item_emea = self._make_item("Scheduled Maintenance Extended - EMEA Region")
        regions_emea = self.adapter.parse_regions(item_emea)
        self.assertEqual(regions_emea, [])

    def test_parse_regions_global_fallback(self) -> None:
        # General titles with no cities should fallback to global
        item = self._make_item("ZTW Control Gateway Connection Degradation")
        regions = self.adapter.parse_regions(item)
        self.assertEqual(regions, ["global"])


class TestZscalerSeverityParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ZscalerAdapter()

    def _make_item(self, title: str, description: str = "", extra_tags: dict = None) -> ET.Element:
        item = ET.Element("item")
        t = ET.SubElement(item, "title")
        t.text = title
        d = ET.SubElement(item, "description")
        d.text = description
        if extra_tags:
            for tag, val in extra_tags.items():
                child = ET.SubElement(item, tag)
                child.text = val
        return item

    def test_parse_severity_resolved_keyword(self) -> None:
        item = self._make_item("Sydney Datacenter Resolved", "We completed the maintenance")
        self.assertEqual(self.adapter.parse_severity(item), "resolved")

    def test_parse_severity_resolved_date(self) -> None:
        item = self._make_item("Data Center Expansions in ZPA", "New DC in Sydney V", {"ResolvedDate": "Wed, 15 Apr 2026 20:32:04 GMT"})
        self.assertEqual(self.adapter.parse_severity(item), "resolved")

    def test_parse_severity_completed_status(self) -> None:
        item = self._make_item("Data Center Expansions in ZPA", "New DC in Sydney V", {"Status": "Completed"})
        self.assertEqual(self.adapter.parse_severity(item), "resolved")

    def test_parse_severity_cancelled_status(self) -> None:
        item = self._make_item("Sydney Maintenance", "Cancelled", {"Status": "Cancelled"})
        self.assertEqual(self.adapter.parse_severity(item), "resolved")

    def test_parse_severity_active_maintenance(self) -> None:
        item = self._make_item("Sydney Maintenance", "Underway", {"Status": "In Progress"})
        # Should not map to resolved
        self.assertEqual(self.adapter.parse_severity(item), "info")


class TestZscalerDynamicUpdate(unittest.TestCase):
    def test_dynamic_update_regions(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            csv_path = Path(tmp_dir) / "approved-regions.csv"
            # Create a simple approved regions file
            csv_path.write_text("Service,Region Code,Display Name\nzscaler,global,Global\n", encoding="utf-8")
            
            with mock.patch("outage_automation.ingest.zscaler.resolve_paths") as mock_paths:
                from outage_automation.paths import Paths
                mock_paths.return_value = Paths(
                    root=Path(tmp_dir),
                    state_dir=Path(tmp_dir),
                    events_path=Path(tmp_dir) / "events.ndjson",
                    alert_history_path=Path(tmp_dir) / "alert_history.json",
                    customers_path=Path(tmp_dir) / "customers.json",
                    dashboard_path=Path(tmp_dir) / "dashboard.json",
                    approved_regions_csv=csv_path,
                    raw_dir=Path(tmp_dir) / "raw",
                )
                
                # Simulate fetch update
                from outage_automation.ingest.zscaler import update_approved_regions_with_zscaler
                update_approved_regions_with_zscaler({"Sydney III", "Sydney V", "Melbourne II"})
                
                # Load rows and verify they were appended
                rows = load_regions_csv(csv_path)
                regions = sorted([(r.region_code, r.display_name) for r in rows if r.service == "zscaler"])
                self.assertEqual(regions, [
                    ("global", "Global"),
                    ("melbourne-ii", "Melbourne II"),
                    ("sydney-iii", "Sydney III"),
                    ("sydney-v", "Sydney V"),
                ])


if __name__ == "__main__":
    unittest.main()
