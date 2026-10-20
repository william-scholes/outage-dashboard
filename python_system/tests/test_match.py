"""Unit tests for match.py — ISS-019, #21."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from outage_automation.model import Customer, OutageEvent
from outage_automation.match import match_customers, region_match, sub_service_match


def _outage(raw_id: str, service: str, regions: list[str], sub_services: list[str] | None = None) -> OutageEvent:
    return OutageEvent(
        raw_id=raw_id,
        service=service,
        title=f"outage {raw_id}",
        description="",
        severity="warning",
        regions=regions,
        sub_services=list(sub_services or []),
        link="",
        published_at=datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc),
    )


def _customer(
    customer_id: str,
    name: str,
    subscriptions: dict[str, list[str]],
    sub_services: dict[str, list[str]] | None = None,
) -> Customer:
    return Customer(
        customer_id=customer_id,
        name=name,
        subscriptions=subscriptions,
        sub_services=sub_services or {},
    )


class TestRegionMatch(unittest.TestCase):
    def test_global_outage_matches_global_subscription(self) -> None:
        """A customer subscribed to ['global'] matches an outage with regions=['global']."""
        self.assertTrue(region_match(["global"], ["global"]))

    def test_global_outage_matches_nonempty_subscription(self) -> None:
        """An outage with ['global'] matches any customer subscription that includes 'global'."""
        self.assertTrue(region_match(["global"], ["global", "us-east-1"]))
        self.assertTrue(region_match(["global"], ["us-east-1", "global"]))

    def test_non_global_exact_match(self) -> None:
        """Non-global region lists match on exact intersection."""
        self.assertTrue(region_match(["us-east-1"], ["us-east-1"]))
        self.assertTrue(region_match(["us-east-1", "eu-west-1"], ["us-east-1"]))

    def test_no_intersection_returns_false(self) -> None:
        """Disjoint non-global region sets return False."""
        self.assertFalse(region_match(["us-east-1"], ["eu-west-1"]))
        # Note: region_match short-circuits on 'global' — ['australia-east'] vs ['global'] returns True
        # because a 'global' subscription covers all specific regions.
        self.assertTrue(region_match(["australia-east"], ["global"]))

    def test_empty_returns_false(self) -> None:
        """Empty region lists return False (no match on empty)."""
        self.assertFalse(region_match([], ["global"]))
        self.assertFalse(region_match(["global"], []))


class TestMatchCustomers(unittest.TestCase):
    def test_global_subscription_matches_global_outage(self) -> None:
        """A customer subscribed to ['global'] for azure matches an azure outage with regions=['global']."""
        customer = _customer("c1", "TestCo", {"azure": ["global"]})
        outage = _outage("o1", "azure", ["global"])
        matches = match_customers(outage, [customer])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0].name, "TestCo")

    def test_specific_region_subscription_matches_specific_outage(self) -> None:
        """A customer subscribed to ['australia-east'] matches an outage in that region."""
        customer = _customer("c1", "TestCo", {"azure": ["australia-east"]})
        outage = _outage("o1", "azure", ["australia-east"])
        matches = match_customers(outage, [customer])
        self.assertEqual(len(matches), 1)

    def test_global_outage_matches_specific_subscription(self) -> None:
        """An outage with ['global'] matches a customer with a specific-region subscription
        because region_match short-circuits on 'global' in the customer set."""
        customer = _customer("c1", "TestCo", {"azure": ["australia-east"]})
        outage = _outage("o1", "azure", ["global"])
        matches = match_customers(outage, [customer])
        self.assertEqual(len(matches), 1)

    def test_no_match_different_service(self) -> None:
        """A customer's azure subscription does not match an aws outage."""
        customer = _customer("c1", "TestCo", {"aws": ["global"]})
        outage = _outage("o1", "azure", ["global"])
        matches = match_customers(outage, [customer])
        self.assertEqual(len(matches), 0)

    def test_no_match_no_subscription(self) -> None:
        """A customer with no azure subscription is not matched."""
        customer = _customer("c1", "TestCo", {"aws": ["global"]})
        outage = _outage("o1", "azure", ["global"])
        matches = match_customers(outage, [customer])
        self.assertEqual(len(matches), 0)


class TestSubServiceMatch(unittest.TestCase):
    """TODO #21 — sub_services refine the per-service match.

    Rules: if either side is empty, no sub-service check is applied
    (the customer opts in to "all sub-services" or the outage doesn't
    carry the axis). When both sides are non-empty, the intersection
    must be non-empty.
    """

    def test_either_side_empty_passes(self) -> None:
        self.assertTrue(sub_service_match([], ["zia"]))
        self.assertTrue(sub_service_match(["zia"], []))
        self.assertTrue(sub_service_match([], []))

    def test_both_sides_overlap(self) -> None:
        self.assertTrue(sub_service_match(["zia", "zpa"], ["zia"]))
        self.assertTrue(sub_service_match(["zia"], ["zia", "zdx"]))

    def test_both_sides_disjoint(self) -> None:
        self.assertFalse(sub_service_match(["zia"], ["zpa"]))
        self.assertFalse(sub_service_match(["zia", "zpa"], ["zdx", "zcc"]))

    def test_case_insensitive(self) -> None:
        self.assertTrue(sub_service_match(["ZIA"], ["zia"]))


class TestMatchCustomersWithSubServices(unittest.TestCase):
    """TODO #21 — sub_services filter is applied to Zscaler matches."""

    def test_no_sub_services_either_side_matches(self) -> None:
        """Backward compat: no sub_services on either side → match."""
        c = _customer("c1", "TestCo", {"zscaler": ["global"]})
        o = _outage("o1", "zscaler", ["global"], sub_services=["zia"])
        self.assertEqual(len(match_customers(o, [c])), 1)

    def test_customer_subs_filtered_out(self) -> None:
        """Customer subscribed to zia only — outage on zpa should not match."""
        c = _customer("c1", "TestCo", {"zscaler": ["global"]}, sub_services={"zscaler": ["zia"]})
        o = _outage("o1", "zscaler", ["global"], sub_services=["zpa"])
        self.assertEqual(len(match_customers(o, [c])), 0)

    def test_customer_subs_match(self) -> None:
        """Customer subscribed to zia — outage on zia should match."""
        c = _customer("c1", "TestCo", {"zscaler": ["global"]}, sub_services={"zscaler": ["zia"]})
        o = _outage("o1", "zscaler", ["global"], sub_services=["zia"])
        self.assertEqual(len(match_customers(o, [c])), 1)

    def test_outage_with_no_sub_services_matches_subscribed_customer(self) -> None:
        """A generic Zscaler outage (no sub_services) matches a customer
        who subscribed to a specific sub-product. The customer would
        rather know about a generic issue than miss it."""
        c = _customer("c1", "TestCo", {"zscaler": ["global"]}, sub_services={"zscaler": ["zia"]})
        o = _outage("o1", "zscaler", ["global"], sub_services=[])
        self.assertEqual(len(match_customers(o, [c])), 1)

    def test_multiple_customers_with_mixed_subs(self) -> None:
        """One outage; one customer subscribes to zia, one to zpa.
        Only the zia customer should match a zia outage."""
        c1 = _customer("c1", "ZiaCo", {"zscaler": ["global"]}, sub_services={"zscaler": ["zia"]})
        c2 = _customer("c2", "ZpaCo", {"zscaler": ["global"]}, sub_services={"zscaler": ["zpa"]})
        o = _outage("o1", "zscaler", ["global"], sub_services=["zia"])
        matches = match_customers(o, [c1, c2])
        self.assertEqual([m[0].customer_id for m in matches], ["c1"])

    def test_subs_dont_leak_to_other_services(self) -> None:
        """A customer's zscaler sub_services must not affect azure matches."""
        c = _customer("c1", "TestCo", {"azure": ["global"], "zscaler": ["global"]}, sub_services={"zscaler": ["zia"]})
        o = _outage("o1", "azure", ["global"], sub_services=[])
        # Azure match should work even though the customer has Zscaler subs.
        self.assertEqual(len(match_customers(o, [c])), 1)


class TestCustomerFromDictSubServices(unittest.TestCase):
    """TODO #21 — `sub_services` is an optional top-level field on
    customers.json. `Customer.from_dict` must accept it (defaulting to
    `{}` when missing) and normalise service keys + sub-service codes.
    """

    def test_missing_sub_services_defaults_to_empty(self) -> None:
        c = Customer.from_dict({
            "customer_id": "c1", "name": "X",
            "subscriptions": {"azure": ["global"]},
        })
        self.assertEqual(c.sub_services, {})

    def test_parses_sub_services(self) -> None:
        c = Customer.from_dict({
            "customer_id": "c1", "name": "X",
            "subscriptions": {"zscaler": ["global"]},
            "sub_services": {"zscaler": ["zia", "zpa"]},
        })
        self.assertEqual(c.sub_services, {"zscaler": ["zia", "zpa"]})

    def test_normalises_case(self) -> None:
        c = Customer.from_dict({
            "customer_id": "c1", "name": "X",
            "subscriptions": {"ZSCALER": ["GLOBAL"]},
            "sub_services": {"Zscaler": ["ZIA"]},
        })
        self.assertEqual(c.subscriptions, {"zscaler": ["global"]})
        self.assertEqual(c.sub_services, {"zscaler": ["zia"]})

    def test_dedupes_sub_service_codes(self) -> None:
        c = Customer.from_dict({
            "customer_id": "c1", "name": "X",
            "subscriptions": {"zscaler": ["global"]},
            "sub_services": {"zscaler": ["zia", "zia", "zpa"]},
        })
        self.assertEqual(c.sub_services, {"zscaler": ["zia", "zpa"]})