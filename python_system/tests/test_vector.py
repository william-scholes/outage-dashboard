from __future__ import annotations

import json
from pathlib import Path
from outage_automation.ingest.vector import (
    VectorAdapter,
    _clean_slug,
    update_approved_regions_with_vector,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "vector-api.json"


def test_vector_adapter_json_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    incidents = json.loads(raw_data.decode("utf-8"))
    
    adapter = VectorAdapter()
    events = [adapter.parse_incident(inc) for inc in incidents]
    events = [e for e in events if e is not None]
    
    assert len(events) == 6
    # First event: Te Atatu Peninsula
    assert events[0].service == "vector"
    assert events[0].regions == ["te-atatu-peninsula"]
    assert "power" in events[0].sub_services
    assert "hv" in events[0].sub_services
    assert "Te Atatu Peninsula" in events[0].title
    assert "D-91565" in events[0].title


def test_vector_adapter_severity_inference():
    adapter = VectorAdapter()
    
    # Active / In progress
    active_inc = {
        "plannedOutageId": 12345,
        "switchingProposalNumber": "A-12345",
        "status": {"statusDescription": "Work in Progress"},
        "suburb": "Albany",
        "type": "LV",
        "plannedOutageBlocks": [
            {
                "advertisedStartDateTime": "2026-08-20T00:00:00Z",
                "advertisedEndDateTime": "2099-08-20T23:59:59Z",
            }
        ],
    }
    ev_active = adapter.parse_incident(active_inc)
    assert ev_active is not None
    assert ev_active.severity == "warning"
    assert "lv" in ev_active.sub_services
    assert "albany" in ev_active.regions

    # Cancelled
    cancelled_inc = {
        "plannedOutageId": 12346,
        "switchingProposalNumber": "A-12346",
        "status": {"statusDescription": "Cancelled"},
        "cancelledReason": "Weather postponement",
        "suburb": "Takapuna",
        "type": "HV",
    }
    ev_cancelled = adapter.parse_incident(cancelled_inc)
    assert ev_cancelled is not None
    assert ev_cancelled.severity == "resolved"


def test_vector_clean_slug():
    assert _clean_slug("Te Atatu Peninsula") == "te-atatu-peninsula"
    assert _clean_slug("Mount Albert") == "mount-albert"
    assert _clean_slug("O'Neill's Point") == "o-neill-s-point"
