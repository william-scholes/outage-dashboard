from __future__ import annotations

import json
from pathlib import Path
from outage_automation.ingest.wasabi import WasabiAdapter, extract_wasabi_regions

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "wasabi-api.json"


def test_wasabi_adapter_fixture_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    payload = json.loads(raw_data.decode("utf-8"))

    adapter = WasabiAdapter()
    events = adapter.parse(payload)

    assert len(events) > 0
    assert all(e.service == "wasabi" for e in events)
    for ev in events:
        assert ev.link.startswith("https://")
        assert "global" in ev.regions


def test_wasabi_regions_inference():
    inc = {
        "id": "inc-syd-1",
        "name": "Intermittent Errors in AP-Southeast-2 (Sydney)",
        "status": "investigating",
        "impact": "minor",
        "created_at": "2026-09-20T00:00:00.000Z",
        "shortlink": "https://stspg.io/wasabi1",
        "incident_updates": [{"body": "Investigating storage node in Sydney ap-southeast-2 region."}],
        "components": [{"name": "AP-Southeast-2 (Sydney)"}],
    }
    regions = extract_wasabi_regions(inc)
    assert "ap-southeast-2" in regions
    assert "global" in regions
