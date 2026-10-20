from __future__ import annotations

import json
from pathlib import Path
from outage_automation.ingest.lightwire import (
    LightwireAdapter,
    extract_lightwire_regions,
    extract_lightwire_severity,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "lightwire-api.json"


def test_lightwire_adapter_fixture_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    payload = json.loads(raw_data.decode("utf-8"))

    adapter = LightwireAdapter()
    events = adapter.parse(payload)

    assert len(events) == 50
    assert all(e.service == "lightwire" for e in events)

    # First event (voice incident)
    ev0 = events[0]
    assert ev0.raw_id == "1038"
    assert ev0.severity in ("warning", "critical")
    assert "new-zealand" in ev0.regions
    assert "voice" in ev0.sub_services
    assert ev0.link == "https://status.lightwire.co.nz/incidents/1038"

    # Fallback when permalink is missing or empty
    fallback_payload = {
        "data": [
            {
                "id": 9999,
                "name": "Fallback Test Outage",
                "message": "Testing fallback link logic",
                "status": 1,
                "created_at": "2026-10-14 12:00:00",
            }
        ]
    }
    fallback_events = adapter.parse(fallback_payload)
    assert len(fallback_events) == 1
    assert fallback_events[0].link == "https://status.lightwire.co.nz/incidents/9999"


def test_lightwire_regions_and_severity():
    regions = extract_lightwire_regions("Power outage at Cambridge tower in Waikato")
    assert "waikato" in regions
    assert "new-zealand" in regions

    assert extract_lightwire_severity(4, "Major core outage") == "resolved"
    assert extract_lightwire_severity(1, "Critical core fibre cut") == "critical"
    assert extract_lightwire_severity(1, "Investigating packet loss") == "warning"
