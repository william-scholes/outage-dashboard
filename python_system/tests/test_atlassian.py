from __future__ import annotations

import json
from pathlib import Path
from outage_automation.ingest.atlassian import (
    AtlassianAdapter,
    extract_atlassian_sub_services,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "atlassian-api.json"


def test_atlassian_adapter_fixture_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    payload = json.loads(raw_data.decode("utf-8"))

    adapter = AtlassianAdapter()
    events = adapter.parse(payload)

    assert len(events) == 50
    assert all(e.service == "atlassian" for e in events)

    # First event is the active critical outage
    ev0 = events[0]
    assert ev0.raw_id == "qq07180qzmpp"
    assert ev0.severity == "critical"
    assert "global" in ev0.regions
    assert "jira" in ev0.sub_services
    assert "jira-service-management" in ev0.sub_services
    assert "confluence" in ev0.sub_services
    assert ev0.link.startswith("https://")

    # Resolved events
    ev1 = events[1]
    assert ev1.severity == "resolved"


def test_atlassian_sub_services_extraction():
    inc = {
        "name": "Degraded Bitbucket pipelines and Trello webhooks",
        "incident_updates": [
            {"body": "Investigating reports of delays in Jira Service Management and Confluence."}
        ]
    }
    subs = extract_atlassian_sub_services(inc)
    assert "bitbucket" in subs
    assert "trello" in subs
    assert "jira-service-management" in subs
    assert "confluence" in subs


def test_atlassian_regions_inference():
    adapter = AtlassianAdapter()
    inc = {
        "id": "test-inc-1",
        "name": "Access issues in Jira Cloud",
        "status": "investigating",
        "impact": "major",
        "created_at": "2026-08-27T04:00:00.000Z",
        "shortlink": "https://stspg.io/test1",
        "incident_updates": [
            {"body": "Users are experiencing errors loading boards."}
        ],
        "components": [
            {"name": "Jira Software"}
        ]
    }
    payload = {"incidents": [inc]}
    events = adapter.parse(payload)
    assert len(events) == 1
    ev = events[0]
    assert ev.severity == "critical"
    assert ev.regions == ["global"]
    assert "jira" in ev.sub_services
