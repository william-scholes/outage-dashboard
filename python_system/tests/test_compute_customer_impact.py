"""Unit tests for compute_customer_impact with empty/edge inputs — ISS-009."""
from __future__ import annotations

import unittest

from outage_automation.model import Customer
from outage_automation.processor import compute_customer_impact


def _row(raw_id: str, service: str = "cloudflare", regions=None) -> dict:
    return {
        "raw_id": raw_id,
        "service": service,
        "regions": regions or [],
        "title": f"event {raw_id}",
        "description": "",
        "severity": "warning",
        "link": "",
        "published_at": "2026-06-09T12:00:00Z",
    }


def _customer(cid: str, name: str, subscriptions: dict[str, list[str]]) -> Customer:
    return Customer(customer_id=cid, name=name, subscriptions=subscriptions)


class TestComputeCustomerImpact(unittest.TestCase):
    """Edge cases for compute_customer_impact."""

    def test_empty_rows_returns_empty_structure(self) -> None:
        """No events, no customers affected."""
        result = compute_customer_impact([], [])
        self.assertEqual(result, {"by_customer": {}, "by_outage": {}})

    def test_empty_customers_no_matches(self) -> None:
        """Events exist but no customer subscriptions match any region."""
        rows = [_row("e1", regions=["us-east-1"])]
        customers = [_customer("c1", "Acme", {"cloudflare": ["eu-west-1"]})]
        result = compute_customer_impact(rows, customers)
        self.assertEqual(result["by_customer"], {})
        self.assertEqual(result["by_outage"], {"e1": []})

    def test_single_row_single_customer_match(self) -> None:
        """One event, one customer whose subscription matches."""
        rows = [_row("e1", service="cloudflare", regions=["us-east-1"])]
        customers = [_customer("c1", "Acme", {"cloudflare": ["us-east-1"]})]
        result = compute_customer_impact(rows, customers)
        self.assertIn("Acme", result["by_customer"])
        acme = result["by_customer"]["Acme"]
        self.assertEqual(acme["total"], 1)
        self.assertEqual(acme["by_service"], {"cloudflare": 1})
        self.assertEqual(acme["by_severity"], {"warning": 1})
        self.assertEqual(result["by_outage"]["e1"], ["Acme"])

    def test_multiple_events_aggregate_by_service(self) -> None:
        """Multiple events for the same customer should aggregate by service."""
        rows = [
            _row("e1", service="cloudflare", regions=["us-east-1"]),
            _row("e2", service="cloudflare", regions=["us-east-1"]),
            _row("e3", service="aws", regions=["us-east-1"]),
        ]
        customers = [_customer("c1", "Acme", {"cloudflare": ["us-east-1"], "aws": ["us-east-1"]})]
        result = compute_customer_impact(rows, customers)
        acme = result["by_customer"]["Acme"]
        self.assertEqual(acme["total"], 3)
        self.assertEqual(acme["by_service"], {"cloudflare": 2, "aws": 1})
        self.assertEqual(acme["by_severity"], {"warning": 3})

    def test_multiple_events_aggregate_by_severity(self) -> None:
        """Multiple events with different severities aggregate correctly."""
        rows = [
            _row("e1", service="cloudflare", regions=["us-east-1"]),
            _row("e2", service="cloudflare", regions=["us-east-1"]),
        ]
        # Modify the severity of e2
        rows[1]["severity"] = "critical"

        customers = [_customer("c1", "Acme", {"cloudflare": ["us-east-1"]})]
        result = compute_customer_impact(rows, customers)
        acme = result["by_customer"]["Acme"]
        self.assertEqual(acme["total"], 2)
        self.assertEqual(acme["by_severity"], {"warning": 1, "critical": 1})


    def test_garbage_rows_skipped(self) -> None:
        """Rows that fail OutageEvent.from_dict are silently skipped."""
        rows = [
            _row("e1", regions=["us-east-1"]),
            {"this": "is not a valid event"},  # from_dict will fail
            _row("e2", regions=["us-east-1"]),
        ]
        customers = [_customer("c1", "Acme", {"cloudflare": ["us-east-1"]})]
        result = compute_customer_impact(rows, customers)
        # Only the two valid rows should produce matches.
        self.assertEqual(set(result["by_outage"].keys()), {"e1", "e2"})
        self.assertEqual(result["by_customer"]["Acme"]["total"], 2)


if __name__ == "__main__":
    unittest.main()
