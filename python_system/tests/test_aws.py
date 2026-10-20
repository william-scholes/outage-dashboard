from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from outage_automation.ingest.aws import AwsAdapter

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO / "parser-tests" / "inputs" / "aws-rss.xml"


def test_aws_fixture_parsing():
    raw_xml = FIXTURE_PATH.read_text(encoding="utf-8")
    root = ET.fromstring(raw_xml)

    adapter = AwsAdapter()
    events = adapter.parse(root)

    assert len(events) == 4
    assert all(e.service == "aws" for e in events)

    # Event 0: S3 Sydney
    ev0 = events[0]
    assert "ap-southeast-2" in ev0.regions
    assert "s3" in ev0.sub_services

    # Event 1: EC2 Melbourne
    ev1 = events[1]
    assert "ap-southeast-4" in ev1.regions
    assert "ec2" in ev1.sub_services

    # Event 2: Lambda Sydney resolved
    ev2 = events[2]
    assert ev2.severity == "resolved"
    assert "ap-southeast-2" in ev2.regions

    # Event 3: CloudFront global
    ev3 = events[3]
    assert "global" in ev3.regions
    assert "cloudfront" in ev3.sub_services


def test_aws_health_json_parsing():
    adapter = AwsAdapter()
    mock_payload = {
        "cloudfront": [
            {
                "summary": "[RESOLVED] Change Propagation Delays",
                "arn": "arn:aws:health:global::event/CLOUDFRONT/test1",
                "status": "1",
                "date": "1770758100",
                "event_log": [{"message": "CloudFront delay resolved."}],
            }
        ],
        "ec2-ap-southeast-2": [
            {
                "summary": "Elevated Error Rates in Sydney",
                "arn": "arn:aws:health:ap-southeast-2::event/EC2/test2",
                "status": "2",
                "date": "1770758200",
                "event_log": [{"message": "Investigating instance launch errors."}],
            }
        ],
        "ec2-ap-southeast-6": [
            {
                "summary": "Service Disruption in Auckland",
                "arn": "arn:aws:health:ap-southeast-6::event/EC2/test3",
                "status": "3",
                "date": "1770758300",
                "event_log": [{"message": "Major power disruption in NZ region."}],
            }
        ],
        "ec2-us-east-1": [
            {
                "summary": "US East Outage",
                "arn": "arn:aws:health:us-east-1::event/EC2/test4",
                "status": "3",
            }
        ]
    }

    events = adapter.parse(mock_payload)
    # Should parse cloudfront (global), ap-southeast-2, and ap-southeast-6 (new-zealand), skipping us-east-1
    assert len(events) == 3

    cf = next(e for e in events if "cloudfront" in e.sub_services)
    assert cf.regions == ["global"]
    assert cf.severity == "resolved"

    syd = next(e for e in events if "ap-southeast-2" in e.regions)
    assert syd.severity == "warning"

    nz = next(e for e in events if "ap-southeast-6" in e.regions)
    assert "new-zealand" in nz.regions
    assert nz.severity == "critical"
