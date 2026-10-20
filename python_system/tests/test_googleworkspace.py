from __future__ import annotations

import json
from pathlib import Path
from outage_automation.ingest.googleworkspace import (
    GoogleWorkspaceAdapter,
    _parse_google_incident_json,
    _parse_google_atom_xml,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "googleworkspace-api.json"


def test_google_workspace_adapter_json_parsing():
    raw_data = FIXTURE_PATH.read_bytes()
    incidents = json.loads(raw_data.decode("utf-8"))
    
    events = [_parse_google_incident_json(inc) for inc in incidents]
    events = [e for e in events if e is not None]
    
    assert len(events) == 3
    assert events[0].service == "googleworkspace"
    assert events[0].severity == "warning"
    assert "australia" in events[0].regions
    assert "google-drive" in events[0].sub_services
    
    assert events[1].severity == "resolved"
    assert events[2].severity == "resolved"


def test_google_workspace_adapter_atom_parsing():
    atom_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Google Workspace Status Dashboard updates</title>
  <updated>2026-08-19T13:05:47+00:00</updated>
  <entry>
    <title>RESOLVED: Incident at 18 Aug 2026, 11:58 pm UTC</title>
    <link href="https://www.google.com/appsstatus/dashboard/incidents/sjDiph2NXGAfkYYLfK7T" rel="alternate" type="text/html"></link>
    <id>tag:www.google.com,2026:feed:sjDiph2NXGAfkYYLfK7T.12345</id>
    <updated>2026-08-19T13:05:47+00:00</updated>
    <summary type="html">&lt;p&gt;Multiple customers in Australia may have experienced slowness.&lt;/p&gt;</summary>
  </entry>
</feed>"""
    events = _parse_google_atom_xml(atom_xml)
    assert len(events) == 1
    assert events[0].service == "googleworkspace"
    assert events[0].severity == "resolved"
    assert "australia" in events[0].regions


def test_google_workspace_adapter_nz_parsing():
    atom_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Google Workspace Status Dashboard updates</title>
  <updated>2026-08-19T13:05:47+00:00</updated>
  <entry>
    <title>Disruption in Gmail</title>
    <link href="https://www.google.com/appsstatus/dashboard/incidents/nz123" rel="alternate" type="text/html"></link>
    <id>tag:www.google.com,2026:feed:nz123.12345</id>
    <updated>2026-08-19T13:05:47+00:00</updated>
    <summary type="html">&lt;p&gt;Customers in New Zealand and Auckland may experience delays.&lt;/p&gt;</summary>
  </entry>
</feed>"""
    events = _parse_google_atom_xml(atom_xml)
    assert len(events) == 1
    assert "new-zealand" in events[0].regions
