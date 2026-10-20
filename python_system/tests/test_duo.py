from __future__ import annotations

import json
from pathlib import Path
from outage_automation.ingest.duo import DuoAdapter, extract_duo_regions, extract_duo_sub_services

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "duo-api.json"


def test_duo_adapter_fixture_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    payload = json.loads(raw_data.decode("utf-8"))

    adapter = DuoAdapter()
    events = adapter.parse(payload)

    assert len(events) == 50
    assert all(e.service == "duo" for e in events)
    for ev in events:
        assert ev.link.startswith("https://")
        assert "global" in ev.regions


def test_duo_regions_and_sub_services():
    inc = {
        "id": "inc-duo-1",
        "name": "Issues with Push Delivery in Australia",
        "status": "investigating",
        "impact": "minor",
        "created_at": "2026-09-20T00:00:00.000Z",
        "shortlink": "https://stspg.io/duo1",
        "incident_updates": [{"body": "Investigating push delivery latency."}],
        "components": [{"name": "Push Delivery"}],
    }
    regions = extract_duo_regions(inc)
    assert "australia" in regions
    assert "global" in regions

    subs = extract_duo_sub_services(inc)
    assert "push" in subs
