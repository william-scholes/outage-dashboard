"""Unit tests for filter_active_window TTL boundary — ISS-008."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from outage_automation.processor import filter_active_window


def _row(last_seen_at: str, raw_id: str = "r1") -> dict:
    return {"raw_id": raw_id, "last_seen_at": last_seen_at}


class TestFilterActiveWindowBoundary(unittest.TestCase):
    """Boundary tests for `filter_active_window` around the TTL cutoff."""

    def setUp(self) -> None:
        # Fixed "now" for deterministic boundary testing.
        self.now = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
        self.ttl = 7  # days

    def test_just_inside_window_kept(self) -> None:
        """last_seen_at = now - 6 days 23h 59m is inside the 7-day window."""
        just_inside = (self.now - timedelta(days=6, hours=23, minutes=59)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [_row(just_inside)]
        out = filter_active_window(rows, now=self.now, ttl_days=self.ttl)
        self.assertEqual(len(out), 1, "row 1 second before cutoff should be kept")

    def test_exactly_at_window_kept(self) -> None:
        """last_seen_at = now - 7d is exactly at the cutoff. The filter
        uses `> ttl_days` (strict), so the boundary is kept."""
        at_cutoff = (self.now - timedelta(days=self.ttl)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [_row(at_cutoff)]
        out = filter_active_window(rows, now=self.now, ttl_days=self.ttl)
        self.assertEqual(len(out), 1, "row exactly at the TTL cutoff should be kept (strict >)")

    def test_just_outside_window_dropped(self) -> None:
        """last_seen_at = now - 7d 1m is just outside the 7-day window."""
        just_outside = (self.now - timedelta(days=self.ttl, minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [_row(just_outside)]
        out = filter_active_window(rows, now=self.now, ttl_days=self.ttl)
        self.assertEqual(out, [], "row 1 minute past the TTL cutoff should be dropped")

    def test_far_outside_window_dropped(self) -> None:
        """last_seen_at = now - 30d is far outside the window."""
        far_outside = (self.now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [_row(far_outside)]
        out = filter_active_window(rows, now=self.now, ttl_days=self.ttl)
        self.assertEqual(out, [], "row 30 days old should be dropped")

    def test_mixed_window_keeps_only_inside(self) -> None:
        """A list of 4 rows spanning the boundary returns only the inside ones."""
        rows = [
            _row((self.now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), raw_id="inside1"),
            _row((self.now - timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ"), raw_id="inside2"),
            _row((self.now - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ"), raw_id="outside1"),
            _row((self.now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), raw_id="outside2"),
        ]
        out = filter_active_window(rows, now=self.now, ttl_days=self.ttl)
        ids = [r["raw_id"] for r in out]
        self.assertEqual(ids, ["inside1", "inside2"], "only inside-window rows should be kept")

    def test_falls_back_to_published_at_when_last_seen_missing(self) -> None:
        """If `last_seen_at` is missing, `published_at` is used as the reference."""
        old = (self.now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent = (self.now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [
            {"raw_id": "old-via-published", "published_at": old},  # outside
            {"raw_id": "recent-via-published", "published_at": recent},  # inside
        ]
        out = filter_active_window(rows, now=self.now, ttl_days=self.ttl)
        ids = [r["raw_id"] for r in out]
        self.assertEqual(ids, ["recent-via-published"], "published_at fallback should be used")


if __name__ == "__main__":
    unittest.main()
