"""Unit tests for merge resolution logic (auto-resolving missing events)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from outage_automation.cli import _merge_events_into_history
from outage_automation.model import OutageEvent, AlertHistoryRow, iso_utc
from outage_automation.paths import resolve_paths
from outage_automation.store import read_json, write_json, read_ndjson


class TestMergeResolution(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = TemporaryDirectory()
        self.paths = resolve_paths(self.tmp_dir.name)
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        
        # Write empty state files to start
        write_json(self.paths.alert_history_path, [])
        self.paths.events_path.write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_resolve_missing_incident(self) -> None:
        # 1. Seed alert_history.json with an active connectwise incident
        # and an active aws incident.
        now_dt = datetime.now(timezone.utc)
        published = iso_utc(now_dt)
        expires = iso_utc(now_dt + timedelta(days=7))
        cw_event = {
            "raw_id": "cw-1",
            "service": "connectwise",
            "severity": "warning",
            "title": "ConnectWise Outage",
            "description": "ScreenConnect is down",
            "regions": ["emea"],
            "sub_services": [],
            "link": "https://status.connectwise.com/cw-1",
            "published_at": published,
            "last_seen_at": published,
            "expires_at": expires,
        }
        aws_event = {
            "raw_id": "aws-1",
            "service": "aws",
            "severity": "critical",
            "title": "AWS Outage",
            "description": "EC2 is down",
            "regions": ["us-east-1"],
            "sub_services": [],
            "link": "https://status.aws.com/aws-1",
            "published_at": published,
            "last_seen_at": published,
            "expires_at": expires,
        }
        write_json(self.paths.alert_history_path, [cw_event, aws_event])

        # 2. Merge with an empty connectwise incoming list, passing provider="connectwise"
        summary = _merge_events_into_history(
            self.paths,
            incoming_events=[],
            ttl_days=7,
            provider="connectwise",
        )

        # 3. Read back alert_history.json and assert:
        # - cw-1 is now resolved
        # - aws-1 remains critical
        history = read_json(self.paths.alert_history_path, [])
        cw_history = next((x for x in history if x.get("raw_id") == "cw-1"), None)
        aws_history = next((x for x in history if x.get("raw_id") == "aws-1"), None)

        self.assertIsNotNone(cw_history)
        self.assertEqual(cw_history["severity"], "resolved")

        self.assertIsNotNone(aws_history)
        self.assertEqual(aws_history["severity"], "critical")

        # 4. Assert events.ndjson was appended with the resolution event for cw-1
        log = read_ndjson(self.paths.events_path)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["source"], "connectwise")
        self.assertEqual(log[0]["event"]["raw_id"], "cw-1")
        self.assertEqual(log[0]["event"]["severity"], "resolved")

    def test_provider_none_does_not_resolve_missing(self) -> None:
        now_dt = datetime.now(timezone.utc)
        published = iso_utc(now_dt)
        expires = iso_utc(now_dt + timedelta(days=7))
        cw_event = {
            "raw_id": "cw-1",
            "service": "connectwise",
            "severity": "warning",
            "title": "ConnectWise Outage",
            "description": "ScreenConnect is down",
            "regions": ["emea"],
            "sub_services": [],
            "link": "https://status.connectwise.com/cw-1",
            "published_at": published,
            "last_seen_at": published,
            "expires_at": expires,
        }
        write_json(self.paths.alert_history_path, [cw_event])

        # Merge with empty incoming list, provider=None
        _merge_events_into_history(
            self.paths,
            incoming_events=[],
            ttl_days=7,
            provider=None,
        )

        # cw-1 should still be warning
        history = read_json(self.paths.alert_history_path, [])
        cw_history = next((x for x in history if x.get("raw_id") == "cw-1"), None)
        self.assertIsNotNone(cw_history)
        self.assertEqual(cw_history["severity"], "warning")

        # events.ndjson should be empty
        log = read_ndjson(self.paths.events_path)
        self.assertEqual(len(log), 0)


if __name__ == "__main__":
    unittest.main()
