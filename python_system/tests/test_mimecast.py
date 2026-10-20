from __future__ import annotations

import json
from pathlib import Path
from outage_automation.ingest.mimecast import MimecastAdapter

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "mimecast-api.json"


def test_mimecast_adapter_fixture_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    payload = json.loads(raw_data.decode("utf-8"))

    adapter = MimecastAdapter()
    events = adapter.parse(payload)

    assert len(events) == 2
    assert all(e.service == "mimecast" for e in events)

    # Active Australian Grid outage
    ev0 = events[0]
    assert ev0.raw_id == "mc-inc-201"
    assert ev0.severity == "warning"
    assert "australia" in ev0.regions
    assert "status.mimecast.com" in ev0.link

    # Resolved US outage
    ev1 = events[1]
    assert ev1.raw_id == "mc-inc-200"
    assert ev1.severity == "resolved"
