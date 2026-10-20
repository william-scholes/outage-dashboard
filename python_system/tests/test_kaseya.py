from __future__ import annotations

import json
from pathlib import Path
from outage_automation.ingest.kaseya import (
    KaseyaAdapter,
    extract_kaseya_regions,
    extract_kaseya_sub_services,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "kaseya-api.json"


def test_kaseya_adapter_fixture_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    payload = json.loads(raw_data.decode("utf-8"))

    adapter = KaseyaAdapter()
    events = adapter.parse(payload)

    assert len(events) == 50
    assert all(e.service == "kaseya" for e in events)
    for ev in events:
        assert ev.link.startswith("https://")
        assert "global" in ev.regions


def test_kaseya_regions_and_sub_services():
    inc = {
        "id": "inc-kaseya-1",
        "name": "Degraded Performance on IT Glue Syrah (APAC)",
        "status": "investigating",
        "impact": "minor",
        "created_at": "2026-09-20T00:00:00.000Z",
        "shortlink": "https://stspg.io/kas1",
        "incident_updates": [{"body": "Investigating search latency in IT Glue."}],
        "components": [{"name": "Syrah (APAC)"}, {"name": "IT Glue"}],
    }
    regions = extract_kaseya_regions(inc)
    assert "australia" in regions
    assert "apac" in regions
    assert "global" in regions

    subs = extract_kaseya_sub_services(inc)
    assert "it-glue" in subs
