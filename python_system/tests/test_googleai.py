"""Tests for Google AI & Antigravity status ingest adapter."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from outage_automation.ingest.googleai import (
    GoogleAiAdapter,
    extract_googleai_regions,
    extract_googleai_sub_services,
    infer_googleai_severity,
    parse_googleai_payload,
)
from outage_automation.ingest.registry import get_adapter

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "googleai-api.json"


class TestGoogleAiIngest(unittest.TestCase):
    def test_fixture_parsing(self) -> None:
        raw_data = FIXTURE_PATH.read_bytes()
        payload = json.loads(raw_data.decode("utf-8"))

        adapter = GoogleAiAdapter()
        events = adapter.parse(payload)

        self.assertGreater(len(events), 0)
        self.assertTrue(all(e.service == "googleai" for e in events))

        # Check an event details
        for ev in events:
            self.assertTrue(ev.link.startswith("https://"))
            self.assertIn("global", ev.regions)
            self.assertIn(ev.severity, {"critical", "warning", "info", "resolved"})
            self.assertGreater(len(ev.sub_services), 0)

    def test_sub_service_inference(self) -> None:
        # Component 1 = gemini-api
        sub1 = extract_googleai_sub_services([1], "Gemini API batch requests delayed")
        self.assertIn("gemini-api", sub1)

        # Component 2 = live-api
        sub2 = extract_googleai_sub_services([2], "Realtime streaming latency")
        self.assertIn("live-api", sub2)

        # Component 3 = ai-studio
        sub3 = extract_googleai_sub_services([3], "AI Studio Build issues")
        self.assertIn("ai-studio", sub3)

        # Antigravity keyword or model inference mention
        sub_agy = extract_googleai_sub_services([1], "Antigravity agent requests timing out on model inference")
        self.assertIn("antigravity", sub_agy)
        self.assertIn("gemini-api", sub_agy)

        # Throttling on Gemini Pro/Flash models triggers antigravity sub-service
        sub_models = extract_googleai_sub_services([1], "Gemini 2.5 Pro and Flash Throttling Incident")
        self.assertIn("antigravity", sub_models)

    def test_severity_inference(self) -> None:
        self.assertEqual(infer_googleai_severity(4, "Issue has been resolved.", "API issue"), "resolved")
        self.assertEqual(infer_googleai_severity(1, "Outage affecting all requests", "Total outage"), "critical")
        self.assertEqual(infer_googleai_severity(1, "Increased error rates", "Latency issue"), "warning")

    def test_regions_inference(self) -> None:
        regs = extract_googleai_regions("Issue reported by users in Australia and New Zealand")
        self.assertIn("global", regs)
        self.assertIn("australia", regs)
        self.assertIn("new-zealand", regs)

    def test_registry_aliases(self) -> None:
        adapter1 = get_adapter("googleai")
        self.assertIsInstance(adapter1, GoogleAiAdapter)

        adapter2 = get_adapter("antigravity")
        self.assertIsInstance(adapter2, GoogleAiAdapter)

        adapter3 = get_adapter("google-ai")
        self.assertIsInstance(adapter3, GoogleAiAdapter)

        adapter4 = get_adapter("gemini")
        self.assertIsInstance(adapter4, GoogleAiAdapter)


if __name__ == "__main__":
    unittest.main()
