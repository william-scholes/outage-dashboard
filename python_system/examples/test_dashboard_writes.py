"""E2E tests for the dashboard write endpoints (customers + regions).

Mirrors `test_e2e.py`'s `run_cli()` / `setup_scratch()` / `assert_eq()`
helpers. Spawns the dashboard server via
`dashboard.server.run_server(..., block=False)` on a random port with auth
off (loopback), drives it with `urllib.request` (no extra deps).

Run standalone: `python python_system/examples/test_dashboard_writes.py`
Run via the existing suite: `python python_system/examples/test_e2e.py`
imports and calls this module's `main()`.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[2]
PY_SYSTEM = REPO / "python_system"
SCRATCH = REPO / "python_system" / "state_e2e_scratch"

# Make the package importable when this file is run directly.
sys.path.insert(0, str(PY_SYSTEM))


_failures = 0


def assert_eq(label: str, actual, expected) -> bool:
    global _failures
    ok = actual == expected
    mark = "OK  " if ok else "FAIL"
    print(f"{mark} {label}: actual={actual!r} expected={expected!r}")
    if not ok:
        _failures += 1
    return ok


def assert_true(label: str, cond: bool, detail: str = "") -> bool:
    global _failures
    mark = "OK  " if cond else "FAIL"
    suffix = f" ({detail})" if detail else ""
    print(f"{mark} {label}{suffix}")
    if not cond:
        _failures += 1
    return cond


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Client:
    """Tiny urllib wrapper that supports arbitrary methods + body."""

    def __init__(self, base: str):
        self.base = base

    def request(self, method: str, path: str, body: Any = None) -> Tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, raw.decode("utf-8", errors="replace")


def setup_scratch_with_csv() -> Tuple[Path, Path]:
    """Set up a scratch state dir + a copy of the production CSV.

    Returns (state_dir, scratch_csv). All edits stay inside the scratch
    dir; the live approved-regions.csv and
    state/ files are never touched.
    """
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    state_dir = SCRATCH / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "customers.json").write_text("[]", encoding="utf-8")
    # Always start with an empty events file so filter_active_window is happy.
    (state_dir / "events.ndjson").write_text("", encoding="utf-8")
    (state_dir / "alert_history.json").write_text("[]", encoding="utf-8")
    scratch_csv = SCRATCH / "approved-regions.csv"
    shutil.copy2(REPO / "approved-regions.csv", scratch_csv)
    return state_dir, scratch_csv


def start_dashboard(state_dir: Path, scratch_csv: Path) -> Tuple[HTTPServer, int]:
    """Start the dashboard on a free port with auth off. Returns (server, port)."""
    # Set env vars BEFORE importing / reloading server module so the
    # helper paths pick them up.
    os.environ["OA_ROOT"] = str(SCRATCH)
    os.environ["OA_APPROVED_REGIONS_CSV"] = str(scratch_csv)

    # Force a fresh import so module-level state doesn't leak between tests.
    import importlib
    import outage_automation.paths as paths_mod
    importlib.reload(paths_mod)
    from outage_automation.dashboard import server as srv
    importlib.reload(srv)
    from outage_automation.paths import resolve_paths
    from outage_automation.dashboard.server import run_server

    paths = resolve_paths(SCRATCH)
    port = _free_port()
    server = run_server(paths, host="127.0.0.1", port=port, block=False)
    return server, port


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_health_open(client: _Client) -> None:
    status, body = client.request("GET", "/api/health")
    assert_eq("health status", status, 200)
    assert_true("health body has status=ok", body and body.get("status") == "ok")


def test_regions_get_returns_3_columns(client: _Client) -> None:
    status, body = client.request("GET", "/api/regions")
    assert_eq("GET /api/regions status", status, 200)
    assert_true("regions is a list", isinstance(body, dict) and isinstance(body.get("regions"), list))
    assert_true("regions is non-empty", len(body["regions"]) > 0)
    sample = body["regions"][0]
    for k in ("service", "region_code", "display_name"):
        assert_true(f"region has {k!r}", k in sample, f"keys={list(sample)}")
    # The editor no longer surfaces `is_global` or `description` — those
    # fields are derived in the UI from `region_code == "global"`.
    for k in ("description", "is_global"):
        assert_true(f"region omits {k!r}", k not in sample, f"keys={list(sample)}")


def test_customers_post_create(client: _Client) -> None:
    status, body = client.request("POST", "/api/customers", {
        "name": "Acme Corp",
        "subscriptions": {"azure": ["australia-east", "global"]},
    })
    assert_eq("create status", status, 201)
    names = [c["name"] for c in body["customers"]]
    assert_true("Acme Corp in list", "Acme Corp" in names)
    # ID should be auto-slugified
    acme = next(c for c in body["customers"] if c["name"] == "Acme Corp")
    assert_eq("auto-slugified id", acme["customer_id"], "acme-corp")


def test_customers_post_dup_id(client: _Client) -> None:
    status, body = client.request("POST", "/api/customers", {
        "name": "Beta Co",
        "customer_id": "acme-corp",  # already exists from previous test
        "subscriptions": {},
    })
    assert_eq("dup id status", status, 409)
    assert_true("dup id error mentions id", "customer_id" in body.get("error", ""))


def test_customers_post_dup_name(client: _Client) -> None:
    status, body = client.request("POST", "/api/customers", {
        "name": "Acme Corp",  # already exists
        "subscriptions": {},
    })
    assert_eq("dup name status", status, 409)


def test_customers_post_validation_error(client: _Client) -> None:
    # Subscription to a region not in the allowlist — validate_customers
    # rejects it.
    status, body = client.request("POST", "/api/customers", {
        "name": "Bad Co",
        "subscriptions": {"azure": ["made-up-region-xyz"]},
    })
    assert_eq("validation status", status, 400)
    assert_true("validation has details", "details" in body)
    assert_true("validation mentions region", any("made-up-region-xyz" in d.get("message", "") for d in body.get("details", [])))


def test_customers_post_atomicity(client: _Client, state_dir: Path) -> None:
    # Bad input must not mutate the on-disk file.
    before = (state_dir / "customers.json").read_bytes()
    status, body = client.request("POST", "/api/customers", {
        "name": "",  # missing required name
        "subscriptions": {"azure": ["made-up-region-xyz"]},
    })
    after = (state_dir / "customers.json").read_bytes()
    assert_eq("validation rejects empty name", status, 400)
    assert_true("file unchanged on validation failure", before == after)


def test_customers_put_updates(client: _Client) -> None:
    status, body = client.request("PUT", "/api/customers/acme-corp", {
        "name": "Acme Inc",
        "subscriptions": {"aws": ["ap-southeast-2"]},
    })
    assert_eq("put status", status, 200)
    acme = next(c for c in body["customers"] if c["customer_id"] == "acme-corp")
    assert_eq("name updated", acme["name"], "Acme Inc")
    assert_eq("subscriptions replaced", acme["subscriptions"], {"aws": ["ap-southeast-2"]})


def test_customers_put_404(client: _Client) -> None:
    status, body = client.request("PUT", "/api/customers/nonexistent", {
        "name": "X",
        "subscriptions": {},
    })
    assert_eq("put 404 status", status, 404)


def test_customers_delete(client: _Client) -> None:
    # Create a throwaway customer so we don't disturb Acme.
    client.request("POST", "/api/customers", {
        "name": "Throwaway",
        "customer_id": "throwaway",
        "subscriptions": {},
    })
    status, body = client.request("DELETE", "/api/customers/throwaway")
    assert_eq("delete status", status, 200)
    assert_true(
        "throwaway gone",
        all(c["customer_id"] != "throwaway" for c in body["customers"]),
    )


def test_customers_delete_404(client: _Client) -> None:
    status, body = client.request("DELETE", "/api/customers/nope")
    assert_eq("delete 404 status", status, 404)


def test_regions_post_create(client: _Client) -> None:
    status, body = client.request("POST", "/api/regions", {
        "service": "azure",
        "region_code": "uk-south",
        "display_name": "UK South",
    })
    assert_eq("region create status", status, 201)
    assert_true(
        "uk-south in list",
        any(r["region_code"] == "uk-south" for r in body["regions"]),
    )


def test_regions_post_dup_409(client: _Client) -> None:
    # uk-south was just created; doing it again should 409.
    status, body = client.request("POST", "/api/regions", {
        "service": "azure",
        "region_code": "uk-south",
        "display_name": "UK South",
    })
    assert_eq("region dup status", status, 409)


def test_regions_post_bad_service_400(client: _Client) -> None:
    status, body = client.request("POST", "/api/regions", {
        "service": "not-a-service",
        "region_code": "us-east-1",
        "display_name": "X",
    })
    assert_eq("bad service status", status, 400)
    assert_true("bad service has details", "details" in body)


def test_regions_post_invalid_code_400(client: _Client) -> None:
    status, body = client.request("POST", "/api/regions", {
        "service": "azure",
        "region_code": "US_East_1",  # uppercase + underscore not allowed
        "display_name": "X",
    })
    assert_eq("invalid code status", status, 400)


def test_regions_put_updates_display_name(client: _Client) -> None:
    status, body = client.request("PUT", "/api/regions/azure/uk-south", {
        "display_name": "UK South (renamed)",
    })
    assert_eq("region put status", status, 200)
    row = next(r for r in body["regions"] if r["region_code"] == "uk-south")
    assert_eq("display_name updated", row["display_name"], "UK South (renamed)")


def test_regions_put_404(client: _Client) -> None:
    status, body = client.request("PUT", "/api/regions/azure/nope", {
        "display_name": "X",
    })
    assert_eq("region put 404", status, 404)


def test_regions_delete_in_use_blocked(client: _Client) -> None:
    # Acme Corp (created earlier) references aws/ap-southeast-2.
    status, body = client.request("DELETE", "/api/regions/aws/ap-southeast-2")
    assert_eq("in-use delete status", status, 409)
    assert_eq("in-use error", body.get("error"), "in use")
    assert_true("customer_count > 0", body.get("customer_count", 0) > 0)
    assert_true("customers listed", isinstance(body.get("customers"), list) and len(body["customers"]) > 0)


def test_regions_delete_force_succeeds(client: _Client) -> None:
    status, body = client.request("DELETE", "/api/regions/aws/ap-southeast-2?force=true")
    assert_eq("force-delete status", status, 200)
    assert_true(
        "ap-southeast-2 gone from aws",
        all(not (r["service"] == "aws" and r["region_code"] == "ap-southeast-2") for r in body["regions"]),
    )


def test_regions_delete_404(client: _Client) -> None:
    status, body = client.request("DELETE", "/api/regions/azure/nope")
    assert_eq("region delete 404", status, 404)


def test_locked_returns_503(state_dir: Path, port: int) -> None:
    """Pre-acquire the lock in this process, then try a write over HTTP."""
    from outage_automation.lock import FileLock
    lock = FileLock(state_dir / ".lock")
    # Retry a few times to avoid race condition with the previous request's lock release
    for _ in range(10):
        try:
            lock.acquire()
            break
        except RuntimeError:
            time.sleep(0.05)
    else:
        lock.acquire()  # final try to raise if still failing
    try:
        client = _Client(f"http://127.0.0.1:{port}")
        status, body = client.request("POST", "/api/customers", {
            "name": "Locked Out Co",
            "subscriptions": {},
        })
        assert_eq("locked status", status, 503)
        assert_eq("locked error", body.get("error"), "locked")
    finally:
        lock.release()


def test_concurrent_writes_succeed(state_dir: Path, port: int) -> None:
    """Briefly hold lock in a background thread and verify an HTTP write waits and succeeds."""
    import threading
    from outage_automation.lock import FileLock
    lock = FileLock(state_dir / ".lock")
    lock.acquire()

    def release_soon():
        time.sleep(0.1)
        lock.release()

    t = threading.Thread(target=release_soon)
    t.start()
    try:
        client = _Client(f"http://127.0.0.1:{port}")
        status, body = client.request("POST", "/api/customers", {
            "name": "Concurrent Co",
            "subscriptions": {"azure": ["global"]},
        })
        assert_eq("concurrent write status", status, 201)
    finally:
        t.join()


# ---------------------------------------------------------------------------
# Sub-services (TODO #21) — the customer editor exposes per-service
# sub_services, the server accepts them, the file is round-tripped.
# These tests are independent of the matcher logic (covered in
# tests/test_match.py); they cover the API surface.
# ---------------------------------------------------------------------------


def test_customer_sub_services_round_trip(client: _Client) -> None:
    """Create a customer with sub_services, then re-fetch and confirm
    the sub_services come back unchanged."""
    status, body = client.request("POST", "/api/customers", {
        "name": "ZiaCo",
        "customer_id": "ziaco",
        "subscriptions": {"zscaler": ["global"]},
        "sub_services": {"zscaler": ["zia", "zpa"]},
    })
    assert_eq("create with sub_services status", status, 201)
    ziaco = next(c for c in body["customers"] if c["customer_id"] == "ziaco")
    assert_eq("sub_services round-trip", ziaco.get("sub_services"), {"zscaler": ["zia", "zpa"]})


def test_customer_no_sub_services_omits_field(client: _Client) -> None:
    """A customer without sub_services in the body should not have
    the field set on the round-tripped record (keeps the on-disk
    format minimal)."""
    status, body = client.request("POST", "/api/customers", {
        "name": "PlainCo",
        "customer_id": "plainco",
        "subscriptions": {"cloudflare": ["global"]},
    })
    assert_eq("create without sub_services status", status, 201)
    plain = next(c for c in body["customers"] if c["customer_id"] == "plainco")
    assert_eq("sub_services absent", plain.get("sub_services"), {})


def test_customer_sub_services_validation(client: _Client) -> None:
    """sub_services must be an object (dict) when present; an empty
    list or string triggers a 400."""
    status, body = client.request("POST", "/api/customers", {
        "name": "BadCo",
        "customer_id": "badco",
        "subscriptions": {"zscaler": ["global"]},
        "sub_services": "not-a-dict",
    })
    assert_eq("non-dict sub_services status", status, 400)
    assert_true("non-dict sub_services has details", "details" in body)
    assert_true(
        "non-dict sub_services mentions sub_services",
        any(d.get("path") == "sub_services" for d in body.get("details", [])),
    )


def test_customer_sub_services_unknown_service(client: _Client) -> None:
    """sub_services with an unknown service key is rejected."""
    status, body = client.request("POST", "/api/customers", {
        "name": "BadSvc",
        "customer_id": "badsvc",
        "subscriptions": {"zscaler": ["global"]},
        "sub_services": {"not-a-service": ["foo"]},
    })
    assert_eq("unknown sub_services service status", status, 400)


def test_customer_sub_services_empty_codes(client: _Client) -> None:
    """A sub_services entry with an empty string is rejected."""
    status, body = client.request("POST", "/api/customers", {
        "name": "EmptyCode",
        "customer_id": "emptycode",
        "subscriptions": {"zscaler": ["global"]},
        "sub_services": {"zscaler": [""]},
    })
    assert_eq("empty sub_service code status", status, 400)


def test_customer_sub_services_update(client: _Client) -> None:
    """PUT can replace the sub_services (full-replace semantics)."""
    # Set up.
    client.request("POST", "/api/customers", {
        "name": "UpCo",
        "customer_id": "upco",
        "subscriptions": {"zscaler": ["global"]},
        "sub_services": {"zscaler": ["zia"]},
    })
    # Replace.
    status, body = client.request("PUT", "/api/customers/upco", {
        "name": "UpCo",
        "subscriptions": {"zscaler": ["global"]},
        "sub_services": {"zscaler": ["zpa", "zdx"]},
    })
    assert_eq("update with new sub_services status", status, 200)
    up = next(c for c in body["customers"] if c["customer_id"] == "upco")
    assert_eq("sub_services updated", up.get("sub_services"), {"zscaler": ["zpa", "zdx"]})

    # Clear by passing empty dict.
    status, body = client.request("PUT", "/api/customers/upco", {
        "name": "UpCo",
        "subscriptions": {"zscaler": ["global"]},
        "sub_services": {},
    })
    assert_eq("clear sub_services status", status, 200)
    up = next(c for c in body["customers"] if c["customer_id"] == "upco")
    # After clear, the field is absent (or empty) — round-tripped form
    # is canonical.
    assert_eq("sub_services cleared", up.get("sub_services", {}), {})


def test_customer_sub_services_omitted_in_update_preserves(client: _Client) -> None:
    """If a PUT body omits sub_services, the existing sub_services is
    preserved (backward compat: an older editor that doesn't know about
    sub_services can still update name + subscriptions without
    accidentally clearing sub_services)."""
    client.request("POST", "/api/customers", {
        "name": "KeepCo",
        "customer_id": "keepco",
        "subscriptions": {"azure": ["australia-east"]},
        "sub_services": {"zscaler": ["zia"]},
    })
    status, body = client.request("PUT", "/api/customers/keepco", {
        "name": "KeepCo",
        "subscriptions": {"azure": ["australia-east", "global"]},
    })
    assert_eq("update without sub_services status", status, 200)
    keep = next(c for c in body["customers"] if c["customer_id"] == "keepco")
    assert_eq("sub_services preserved when omitted", keep.get("sub_services"), {"zscaler": ["zia"]})


def test_body_too_large_413(client: _Client) -> None:
    big = "x" * (300 * 1024)  # > 256 KiB
    # Use a raw urlopen with a manually-sized body
    req = urllib.request.Request(
        client.base + "/api/customers",
        data=big.encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert_eq("body too large status", status, 413)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    global _failures
    _failures = 0

    state_dir, scratch_csv = setup_scratch_with_csv()
    server, port = start_dashboard(state_dir, scratch_csv)
    client = _Client(f"http://127.0.0.1:{port}")

    try:
        test_health_open(client)
        test_regions_get_returns_3_columns(client)
        test_customers_post_create(client)
        test_customers_post_dup_id(client)
        test_customers_post_dup_name(client)
        test_customers_post_validation_error(client)
        test_customers_post_atomicity(client, state_dir)
        test_customers_put_updates(client)
        test_customers_put_404(client)
        test_customers_delete(client)
        test_customers_delete_404(client)
        # Sub-services (TODO #21) — run before the regions-delete
        # tests remove ap-southeast-2, otherwise an existing customer
        # (with ap-southeast-2) fails validation when we add new ones.
        test_customer_sub_services_round_trip(client)
        test_customer_no_sub_services_omits_field(client)
        test_customer_sub_services_validation(client)
        test_customer_sub_services_unknown_service(client)
        test_customer_sub_services_empty_codes(client)
        test_customer_sub_services_update(client)
        test_customer_sub_services_omitted_in_update_preserves(client)
        test_concurrent_writes_succeed(state_dir, port)
        test_regions_post_create(client)
        test_regions_post_dup_409(client)
        test_regions_post_bad_service_400(client)
        test_regions_post_invalid_code_400(client)
        test_regions_put_updates_display_name(client)
        test_regions_put_404(client)
        test_regions_delete_in_use_blocked(client)
        test_regions_delete_force_succeeds(client)
        test_regions_delete_404(client)
        test_locked_returns_503(state_dir, port)
        test_body_too_large_413(client)
    finally:
        server.shutdown()
        server.server_close()
        # Best-effort cleanup of the scratch dir
        try:
            shutil.rmtree(SCRATCH)
        except OSError:
            pass

    print()
    if _failures:
        print(f"{_failures} test(s) FAILED")
        return 1
    print("All dashboard write tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
