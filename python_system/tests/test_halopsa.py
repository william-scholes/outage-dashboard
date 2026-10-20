from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from outage_automation.ingest.halopsa import HaloPsaAdapter
from outage_automation.validate import validate_outage_event

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "halopsa-api.json"


def test_halopsa_adapter_fixture_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    payload = json.loads(raw_data.decode("utf-8"))

    adapter = HaloPsaAdapter()
    events = adapter.parse(payload)

    assert len(events) == 2
    assert all(e.service == "halopsa" for e in events)

    # Active degraded event
    ev0 = events[0]
    assert ev0.raw_id == "halo-inc-101"
    assert ev0.severity == "warning"
    assert "australia" in ev0.regions or "Australia" in ev0.regions
    assert "Halo Core Application" in ev0.title
    assert "status.haloservicesolutions.com" in ev0.link

    # Resolved event
    ev1 = events[1]
    assert ev1.raw_id == "halo-inc-100"
    assert ev1.severity == "resolved"

    # Schema validation
    for idx, ev in enumerate(events):
        d = dataclasses.asdict(ev)
        d["published_at"] = ev.published_at.isoformat()
        errs = validate_outage_event(d, idx)
        assert not errs, f"Validation errors on event {idx}: {errs}"
