from __future__ import annotations

import json
from pathlib import Path
from outage_automation.ingest.rewst import RewstAdapter

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "rewst-api.json"


def test_rewst_adapter_fixture_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    payload = json.loads(raw_data.decode("utf-8"))

    adapter = RewstAdapter()
    events = adapter.parse(payload)

    assert len(events) >= 1
    assert all(e.service == "rewst" for e in events)

    ev0 = events[0]
    assert ev0.raw_id == "rewst-inc-1"
    assert ev0.severity == "warning"
    assert "australia" in ev0.regions
    assert "workflows" in ev0.sub_services
    assert ev0.link.startswith("https://status-au.rewst.io")
