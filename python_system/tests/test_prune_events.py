from datetime import datetime, timedelta, timezone
import json
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock

from outage_automation.cli import cmd_prune_events
from outage_automation.paths import resolve_paths


class TestPruneEvents(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp_dir = TemporaryDirectory()
        self.root = self.tmp_dir.name
        self.paths = resolve_paths(self.root)
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_prune_events_retention(self) -> None:
        now = datetime.now(timezone.utc)
        ts_recent = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts_old = (now - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")

        events = [
            {"ts": ts_old, "source": "aws", "event": {"raw_id": "old_1"}},
            {"ts": ts_recent, "source": "azure", "event": {"raw_id": "recent_1"}},
        ]

        with self.paths.events_path.open("w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        # Dry run check
        rc_dry = cmd_prune_events(self.root, days=90, dry_run=True)
        self.assertEqual(rc_dry, 0)

        # Actual prune
        rc_prune = cmd_prune_events(self.root, days=90, dry_run=False)
        self.assertEqual(rc_prune, 0)

        lines = self.paths.events_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(data["ts"], ts_recent)
        self.assertEqual(data["source"], "azure")

    def test_prune_events_default_90_days(self) -> None:
        now = datetime.now(timezone.utc)
        ts_80d = (now - timedelta(days=80)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts_100d = (now - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")

        events = [
            {"ts": ts_100d, "source": "aws", "event": {"raw_id": "old_1"}},
            {"ts": ts_80d, "source": "azure", "event": {"raw_id": "recent_1"}},
        ]

        with self.paths.events_path.open("w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        # When days is None, defaults to 90 days
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OA_EVENTS_RETENTION_DAYS", None)
            rc = cmd_prune_events(self.root, days=None, dry_run=False)
            self.assertEqual(rc, 0)

        lines = self.paths.events_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(data["ts"], ts_80d)

    def test_prune_events_cleanup_on_error(self) -> None:
        now = datetime.now(timezone.utc)
        ts_old = (now - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ts_recent = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self.paths.events_path.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts_old, "source": "aws", "event": {"raw_id": "old_1"}}) + "\n")
            f.write(json.dumps({"ts": ts_recent, "source": "azure", "event": {"raw_id": "recent_1"}}) + "\n")

        tmp_path = self.paths.events_path.with_suffix(".prune.tmp")

        # Simulate OSError during replace
        with patch("outage_automation.store._safe_replace_or_copy", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                from outage_automation.cli import _prune_events_ndjson
                _prune_events_ndjson(self.paths, days=90)

        # tmp_path must be unlinked in finally block
        self.assertFalse(tmp_path.exists())

    def test_prune_events_insufficient_headroom(self) -> None:
        now = datetime.now(timezone.utc)
        ts_recent = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self.paths.events_path.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts_recent, "source": "aws"}) + "\n")

        tmp_path = self.paths.events_path.with_suffix(".prune.tmp")

        # Mock shutil.disk_usage to return 10 bytes free
        fake_usage = MagicMock(total=10**9, used=10**9 - 10, free=10)
        with patch("shutil.disk_usage", return_value=fake_usage):
            rc = cmd_prune_events(self.root, days=90, dry_run=False)
            self.assertEqual(rc, 1)

        # tmp_path was never created or left behind
        self.assertFalse(tmp_path.exists())


if __name__ == "__main__":
    unittest.main()
