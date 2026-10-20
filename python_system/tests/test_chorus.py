"""Unit tests for Chorus adapter medium inference and incident parsing."""
import unittest
from datetime import datetime, timezone

from outage_automation.ingest.chorus import ChorusAdapter, infer_access_mediums


class TestChorusAdapter(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ChorusAdapter()

    def test_infer_access_mediums_gpon(self) -> None:
        inc = {
            "reference": "INC000045091196",
            "sites": [{"role": "GPON", "impact": 10}],
            "description": "Loss of Fibre Broadband Services",
        }
        self.assertEqual(infer_access_mediums(inc), ["fibre"])

    def test_infer_access_mediums_dsl(self) -> None:
        inc = {
            "reference": "INC000045057504",
            "sites": [{"role": "DSL", "impact": 10}],
            "description": "Loss of Broadband Services",
        }
        self.assertEqual(infer_access_mediums(inc), ["copper"])

    def test_infer_access_mediums_copper_plant(self) -> None:
        inc = {
            "reference": "FRE4945196",
            "sites": [
                {"role": "FRE_CABLEPAIR", "impact": 2},
                {"role": "FRE_TERMINAL", "impact": 5},
            ],
            "description": "Loss of Broadband Services",
        }
        self.assertEqual(infer_access_mediums(inc), ["copper"])

    def test_infer_access_mediums_mixed(self) -> None:
        inc = {
            "reference": "INC000045999999",
            "sites": [
                {"role": "GPON", "impact": 50},
                {"role": "DSL", "impact": 20},
            ],
            "description": "Loss of Services",
        }
        self.assertEqual(infer_access_mediums(inc), ["copper", "fibre"])

    def test_infer_access_mediums_desc_fallback_fibre(self) -> None:
        inc = {
            "reference": "INC000045091196",
            "sites": [],
            "description": "St Heliers - Loss of Fibre Broadband Services",
        }
        self.assertEqual(infer_access_mediums(inc), ["fibre"])

    def test_infer_access_mediums_desc_fallback_copper(self) -> None:
        inc = {
            "reference": "INC000045057504",
            "sites": [],
            "description": "Taumarunui - Loss of Broadband Services",
        }
        self.assertEqual(infer_access_mediums(inc), ["copper"])

    def test_parse_incident_st_heliers_regression(self) -> None:
        # The user's exact case: St Heliers INC000045091196
        inc = {
            "reference": "INC000045091196",
            "status": "N",
            "start_time": 1728018600,
            "description": "Loss of Fibre Broadband Services",
            "exchange": "St Heliers",
            "sites": [
                {
                    "impact": 25,
                    "role": "GPON",
                    "point": "{\"type\":\"Point\",\"coordinates\":[174.85,-36.85]}",
                }
            ],
            "updates": [
                {"timestamp": "04/10/2026 18:44 Event", "description": "Under investigation."},
                {"timestamp": "04/10/2026 19:04 Update", "description": "Technician assigned."},
            ],
        }
        event = self.adapter.parse_incident(inc)
        self.assertIsNotNone(event)
        self.assertEqual(event.raw_id, "INC000045091196")
        self.assertEqual(event.service, "chorus")
        self.assertIn("st-heliers", event.regions)
        self.assertEqual(event.sub_services, ["fibre"])
        self.assertEqual(event.severity, "warning")


if __name__ == "__main__":
    unittest.main()
