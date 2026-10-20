from __future__ import annotations

import json
from pathlib import Path
from outage_automation.ingest.ninjaone import NinjaOneAdapter, extract_ninjaone_regions

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "ninjaone-api.json"


def test_ninjaone_adapter_fixture_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    payload = json.loads(raw_data.decode("utf-8"))

    adapter = NinjaOneAdapter()
    events = adapter.parse(payload)

    assert len(events) == 50
    assert all(e.service == "ninjaone" for e in events)
    for ev in events:
        assert ev.link.startswith("https://")
        assert "global" in ev.regions


def test_ninjaone_regions_inference():
    inc = {
        "id": "inc-oc-1",
        "name": "Degraded Performance in Australia (OC)",
        "status": "investigating",
        "impact": "minor",
        "created_at": "2026-09-20T00:00:00.000Z",
        "shortlink": "https://stspg.io/oc1",
        "incident_updates": [{"body": "Investigating slow agent check-ins in Sydney."}],
        "components": [{"name": "Australia (OC)"}],
    }
    regions = extract_ninjaone_regions(inc)
    assert "australia" in regions
    assert "global" in regions
