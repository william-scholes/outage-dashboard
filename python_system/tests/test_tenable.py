from __future__ import annotations

import json
from pathlib import Path
from outage_automation.ingest.tenable import TenableAdapter, extract_tenable_regions

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "tenable-api.json"


def test_tenable_adapter_fixture_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    payload = json.loads(raw_data.decode("utf-8"))

    adapter = TenableAdapter()
    events = adapter.parse(payload)

    assert len(events) > 0
    assert all(e.service == "tenable" for e in events)
    for ev in events:
        assert ev.link.startswith("https://")
        assert "global" in ev.regions


def test_tenable_regions_inference():
    inc = {
        "id": "inc-ten-1",
        "name": "Scan Processing Delays in Sydney",
        "status": "investigating",
        "impact": "minor",
        "created_at": "2026-09-20T00:00:00.000Z",
        "shortlink": "https://stspg.io/tenable1",
        "incident_updates": [{"body": "Investigating connector delays in Australia environment AP-SYD-1."}],
        "components": [{"name": "AP-SYD-1"}],
    }
    regions = extract_tenable_regions(inc)
    assert "australia" in regions
    assert "global" in regions
