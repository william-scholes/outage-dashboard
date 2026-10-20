from __future__ import annotations

from outage_automation.ingest.crowdstrike import CrowdStrikeAdapter
from outage_automation.ingest.kordia import KordiaAdapter
from outage_automation.ingest.pia import PiaAdapter
from outage_automation.ingest.nbn import NbnAdapter
from outage_automation.ingest.avepoint import AvePointAdapter


def test_authenticated_stubs_graceful_unconfigured():
    """Verify that all authenticated stubs return ok=True with empty events when unconfigured."""
    adapters = [
        CrowdStrikeAdapter(),
        KordiaAdapter(),
        PiaAdapter(),
        NbnAdapter(),
        AvePointAdapter(),
    ]
    for adapter in adapters:
        res = adapter.fetch()
        assert res.ok is True, f"{adapter.name} fetch failed"
        assert res.events == [], f"{adapter.name} should have empty events without API key"


def test_stubs_parse_contract():
    """Verify parse() contract for mock payloads."""
    cs = CrowdStrikeAdapter()
    cs_events = cs.parse({"resources": [{"id": "cs-1", "name": "Falcon Cloud Degradation"}]})
    assert len(cs_events) == 1
    assert cs_events[0].service == "crowdstrike"

    kordia = KordiaAdapter()
    k_events = kordia.parse({"incidents": [{"id": "k-1", "title": "Auckland Backhaul Maintenance"}]})
    assert len(k_events) == 1
    assert k_events[0].service == "kordia"

    pia = PiaAdapter()
    pia_events = pia.parse({"incidents": [{"id": "p-1", "title": "aiDesk Sync Queue Delay"}]})
    assert len(pia_events) == 1
    assert pia_events[0].service == "pia"

    nbn = NbnAdapter()
    nbn_events = nbn.parse({"outages": [{"id": "nbn-1", "title": "Sydney POI Maintenance"}]})
    assert len(nbn_events) == 1
    assert nbn_events[0].service == "nbn"
