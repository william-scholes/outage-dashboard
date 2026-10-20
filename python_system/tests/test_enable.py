from __future__ import annotations

import json
from pathlib import Path
from outage_automation.ingest.enable import EnableAdapter, extract_enable_regions

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "enable-api.json"


def test_enable_adapter_fixture_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    payload = json.loads(raw_data.decode("utf-8"))

    adapter = EnableAdapter()
    events = adapter.parse(payload)

    assert len(events) >= 1
    assert all(e.service == "enable" for e in events)
    for ev in events:
        assert ev.link.startswith("https://")
        assert "new-zealand" in ev.regions
        assert "canterbury" in ev.regions


def test_enable_regions_inference():
    inc = {
        "id": "inc-enable-1",
        "name": "Fibre cut in Christchurch Central",
        "status": "investigating",
        "impact": "major",
        "created_at": "2026-09-20T00:00:00.000Z",
        "shortlink": "https://stspg.io/en1",
        "incident_updates": [{"body": "Fibre cut affecting Rolleston and Christchurch."}],
        "components": [],
    }
    regions = extract_enable_regions(inc)
    assert "new-zealand" in regions
    assert "christchurch" in regions
    assert "canterbury" in regions
