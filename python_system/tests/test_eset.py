from __future__ import annotations

import json
from pathlib import Path
from outage_automation.ingest.eset import EsetAdapter, extract_eset_regions

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "eset-api.json"


def test_eset_adapter_fixture_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    payload = json.loads(raw_data.decode("utf-8"))

    adapter = EsetAdapter()
    events = adapter.parse(payload)

    assert len(events) > 0
    assert all(e.service == "eset" for e in events)
    for ev in events:
        assert ev.link.startswith("https://")
        assert "global" in ev.regions


def test_eset_regions_inference():
    inc = {
        "id": "inc-eset-1",
        "name": "LiveGrid Outage in Oceania",
        "status": "investigating",
        "impact": "minor",
        "created_at": "2026-09-20T00:00:00.000Z",
        "shortlink": "https://stspg.io/eset1",
        "incident_updates": [{"body": "LiveGrid lookup delays reported across Australia and New Zealand."}],
        "components": [{"name": "Americas, Oceania"}],
    }
    regions = extract_eset_regions(inc)
    assert "australia" in regions
    assert "global" in regions
