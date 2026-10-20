from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from outage_automation.ingest.xero import XeroAdapter, extract_xero_regions
from outage_automation.validate import validate_outage_event

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "xero-api.json"


def test_xero_adapter_fixture_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    payload = json.loads(raw_data.decode("utf-8"))

    adapter = XeroAdapter()
    events = adapter.parse(payload)

    assert len(events) == 5
    assert all(e.service == "xero" for e in events)

    # Validate each event against the schema
    for idx, ev in enumerate(events):
        d = dataclasses.asdict(ev)
        d["published_at"] = ev.published_at.isoformat()
        errs = validate_outage_event(d, idx)
        assert not errs, f"Validation errors on event {idx}: {errs}"


def test_xero_regions_inference():
    # Test AU extraction
    inc_au = {
        "id": "xero-au-1",
        "name": "AU: Bank Feeds Latency",
        "components": [{"name": "Bank Feeds - Australia"}],
        "incident_updates": [{"body": "Investigating connection timeouts with CommBank."}],
    }
    regs_au = extract_xero_regions(inc_au)
    assert "global" in regs_au
    assert "australia" in regs_au
    assert "new-zealand" not in regs_au

    # Test NZ extraction
    inc_nz = {
        "id": "xero-nz-1",
        "name": "NZ: Inland Revenue Filing Delays",
        "components": [{"name": "E-Filing New Zealand"}],
        "incident_updates": [{"body": "Payday filing for Kiwi employers is currently delayed."}],
    }
    regs_nz = extract_xero_regions(inc_nz)
    assert "global" in regs_nz
    assert "new-zealand" in regs_nz
    assert "australia" not in regs_nz
