from __future__ import annotations

import json
from pathlib import Path
from outage_automation.ingest.myob import (
    MyobAdapter,
    extract_myob_regions,
    extract_myob_sub_services,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "myob-api.json"


def test_myob_adapter_fixture_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    payload = json.loads(raw_data.decode("utf-8"))

    adapter = MyobAdapter()
    events = adapter.parse(payload)

    assert len(events) >= 10
    assert all(e.service == "myob" for e in events)

    # All events have valid link and regions
    for ev in events:
        assert ev.link.startswith("https://status.myob.com")
        assert "global" in ev.regions


def test_myob_regions_and_sub_services():
    regs_au = extract_myob_regions("Australian Taxation Office (ATO) Outage")
    assert "australia" in regs_au

    regs_nz = extract_myob_regions("PayGlobal Payroll New Zealand IRD Filing Delays")
    assert "new-zealand" in regs_nz

    subs = extract_myob_sub_services("Scheduled maintenance Hosted Compliance & Greentree")
    assert "greentree" in subs

    subs_arl = extract_myob_sub_services("AccountRight Live bank feed delays")
    assert "accountright" in subs_arl
