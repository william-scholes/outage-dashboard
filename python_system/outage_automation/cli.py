from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import ingest as ingest_pkg
from .config import load_region_rules
from .lock import FileLock, read_lock_info
from .logging_setup import get_logger
from .model import Customer, OutageEvent
from .paths import resolve_paths
from .processor import (
    build_alert_history_rows,
    compute_customer_impact,
    filter_active_window,
)
from .report import make_report, new_run_id, read_latest_report, write_report
from .store import append_ndjson, read_json, read_ndjson, write_json
from .validate import validate_customers, validate_event_batch


def _parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="outage-automation")
    p.add_argument("--root", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")

    run = sub.add_parser("run")
    run.add_argument("--events", default=None)

    dash = sub.add_parser("dashboard-data")
    dash.add_argument("--out", default=None)

    sub.add_parser("lint-customers")

    import_p = sub.add_parser("import-customers", help="import customer subscriptions from CSV")
    import_p.add_argument("csv_file", help="path to CSV file")
    import_p.add_argument("--replace", action="store_true", help="replace existing customer records rather than merging")

    inv_p = sub.add_parser("import-invoices", help="import customer subscriptions from billing invoice export CSV")
    inv_p.add_argument("csv_file", help="path to invoice CSV file")
    inv_p.add_argument("--no-backup", action="store_true", help="do not backup customers.json before overwriting")

    sub.add_parser(
        "validate-regions",
        help="validate approved-regions.csv (parses 5 columns + checks duplicates / service keys)",
    )

    sub.add_parser("list-providers")

    ingest_p = sub.add_parser("ingest")
    ingest_p.add_argument("provider")
    ingest_p.add_argument("--apply", action="store_true",
                          help="merge ingested events into alert_history.json (default: dry run)")
    ingest_p.add_argument("--url", default=None,
                          help="override provider URL (e.g. local fixture for testing)")

    replay = sub.add_parser("replay")
    replay.add_argument("--ttl-days", type=int, default=None,
                        help="override OA_TTL_DAYS for this replay")
    replay.add_argument("--dry-run", action="store_true",
                        help="print summary but do not write alert_history.json")

    serve_p = sub.add_parser(
        "serve",
        help="start the dashboard HTTP server (basic auth supported)",
    )
    serve_p.add_argument("--user", default=None,
                         help="HTTP Basic Auth username (or set OA_DASHBOARD_USER)")
    serve_p.add_argument("--password", default=None,
                         help="HTTP Basic Auth password (or set OA_DASHBOARD_PASSWORD "
                              "/ OA_DASHBOARD_PASSWORD_FILE). Mutually exclusive with "
                              "--password-file.")
    serve_p.add_argument("--password-file", default=None,
                         help="Path to a file containing the Basic Auth password. "
                              "Useful for systemd EnvironmentFile. Overrides "
                              "OA_DASHBOARD_PASSWORD and --password if set.")
    serve_p.add_argument("--allow-insecure-bind", action="store_true",
                         help="Allow binding to a non-loopback host without auth. "
                              "Refused by default; only set this if the dashboard "
                              "is behind another auth layer (e.g. reverse proxy).")
    serve_p.add_argument("--ssl", action="store_true",
                         help="Enable HTTPS serving (or set OA_DASHBOARD_SSL=1).")
    serve_p.add_argument("--ssl-cert", default=None,
                         help="Path to TLS certificate file (or set OA_DASHBOARD_SSL_CERT).")
    serve_p.add_argument("--ssl-key", default=None,
                         help="Path to TLS private key file (or set OA_DASHBOARD_SSL_KEY).")
    serve_p.add_argument("--host", default=None,
                         help="Host address to bind the HTTP server to (default: 127.0.0.1 or OA_DASHBOARD_HOST)")
    serve_p.add_argument("--port", type=int, default=None,
                         help="Port to listen on (default: 8765 or OA_DASHBOARD_PORT)")
    serve_p.add_argument("--redirect-port", type=int, default=None,
                         help="Optional HTTP port to listen on and issue 301 redirects to HTTPS "
                              "(or set OA_DASHBOARD_REDIRECT_PORT).")

    cert_p = sub.add_parser(
        "generate-cert",
        help="generate a self-signed SAN TLS certificate and private key",
    )
    cert_p.add_argument("--cert", default=None,
                        help="path to write certificate (default: /etc/outage-automation/dashboard.crt or state/ssl/dashboard.crt)")
    cert_p.add_argument("--key", default=None,
                        help="path to write private key (default: /etc/outage-automation/dashboard.key or state/ssl/dashboard.key)")
    cert_p.add_argument("--host", default="127.0.0.1",
                        help="hostname or IP for Subject Alternative Name (default: 127.0.0.1)")
    cert_p.add_argument("--days", type=int, default=3650,
                        help="validity duration in days (default: 3650 / 10 years)")

    run_all = sub.add_parser("run-all")
    run_all.add_argument("--providers", default=None,
                         help="comma-separated list; default = all registered")
    run_all.add_argument("--skip-apply", action="store_true",
                         help="ingest dry-run only; do not write to state")
    run_all.add_argument("--report-out", default=None,
                         help="write run report to this path instead of state/reports/")

    sub.add_parser("status")

    lint_events_p = sub.add_parser("lint-events")
    lint_events_p.add_argument("--strict", action="store_true",
                               help="exit 1 on any issue (default: exit 0 with report)")

    verify_fixtures_p = sub.add_parser("verify-fixtures")
    verify_fixtures_p.add_argument("--strict", action="store_true",
                                   help="exit 1 on any failure (default: exit 0 with report)")

    fixtures_dir = sub.add_parser("fixtures-dir")

    prune_events_p = sub.add_parser(
        "prune-events",
        help="prune events.ndjson entries older than retention window (default: 60 days)",
    )
    prune_events_p.add_argument(
        "--days", type=int, default=None,
        help="retention window in days (default: 60 or OA_EVENTS_RETENTION_DAYS)",
    )
    prune_events_p.add_argument(
        "--dry-run", action="store_true",
        help="print prune statistics without modifying events.ndjson",
    )

    return p.parse_args(argv)


def cmd_init(root: str | None) -> int:
    paths = resolve_paths(root)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    # Clear any stale lock from a prior crash so the next run can proceed
    # without the operator having to `rm state/.lock` by hand.
    _lock_paths(paths).unlink(missing_ok=True)
    if not paths.customers_path.exists():
        write_json(paths.customers_path, [])
    if not paths.alert_history_path.exists():
        write_json(paths.alert_history_path, [])
    if not paths.events_path.exists():
        paths.events_path.write_text("", encoding="utf-8", newline="\n")
    return 0


def _load_customers(paths) -> List[Customer]:
    raw = read_json(paths.customers_path, [])
    out: List[Customer] = []
    if isinstance(raw, list):
        for d in raw:
            if isinstance(d, dict):
                try:
                    out.append(Customer.from_dict(d))
                except Exception:
                    continue
    return out


def _lock_paths(paths):
    return paths.state_dir / ".lock"


def _lock_error_message(lock_path: Path) -> str:
    info = read_lock_info(lock_path)
    if info and info.pid:
        return f"locked by pid {info.pid} since {datetime.fromtimestamp(info.created_at, tz=timezone.utc).isoformat()}"
    return "locked"


def _events_to_dicts_and_stamp(events: List[OutageEvent]) -> tuple[List[Dict[str, Any]], str]:
    """Serialize a list of OutageEvents to dicts and produce a single timestamp.

    Returns (as_dicts, ts_now). Used by `cmd_ingest` and `cmd_run_all` so the
    shape stays consistent across both code paths.
    """
    ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    as_dicts: List[Dict[str, Any]] = []
    for e in events:
        as_dicts.append({
            "raw_id": e.raw_id,
            "service": e.service,
            "title": e.title,
            "description": e.description,
            "severity": e.severity,
            "regions": list(e.regions),
            "sub_services": list(e.sub_services),
            "link": e.link,
            "published_at": e.published_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    return as_dicts, ts_now


def _parse_last_seen(row: Dict[str, Any]) -> datetime:
    """Parse `last_seen_at` from an alert_history row.

    Handles ISO format with 'Z' suffix and missing/empty values.
    Returns epoch (datetime.min) for malformed timestamps so such rows
    are conservatively pruned.
    """
    val = row.get("last_seen_at") or ""
    if not val:
        return datetime.min
    # Strip trailing 'Z' and parse as UTC.
    if val.endswith("Z"):
        val = val[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(val).astimezone(timezone.utc)
    except ValueError:
        return datetime.min


def _merge_events_into_history(
    paths,
    incoming_events: List[OutageEvent],
    ttl_days: int,
    provider: Optional[str] = None,
) -> Dict[str, int]:
    retention_days = int(os.environ.get("OA_HISTORY_RETENTION_DAYS") or "30")
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    existing = read_json(paths.alert_history_path, [])
    existing_by_raw_id: Dict[str, Dict[str, Any]] = {}
    if isinstance(existing, list):
        for r in existing:
            if isinstance(r, dict) and r.get("raw_id"):
                existing_by_raw_id[str(r["raw_id"])] = r

    new_rows = build_alert_history_rows(
        incoming_events, existing_by_raw_id, ttl_days=ttl_days
    )
    merged: Dict[str, Dict[str, Any]] = dict(existing_by_raw_id)
    for r in new_rows:
        merged[r.raw_id] = r.to_dict()

    if provider:
        incoming_ids = {ev.raw_id for ev in incoming_events if ev.raw_id}
        ts_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        resolved_appends = []
        for raw_id, row in merged.items():
            if row.get("service") == provider and row.get("severity") != "resolved":
                if raw_id not in incoming_ids:
                    row["severity"] = "resolved"
                    event_data = {
                        "raw_id": row.get("raw_id"),
                        "service": row.get("service"),
                        "title": row.get("title"),
                        "description": row.get("description"),
                        "severity": "resolved",
                        "regions": list(row.get("regions") or []),
                        "sub_services": list(row.get("sub_services") or []),
                        "link": row.get("link"),
                        "published_at": row.get("published_at"),
                    }
                    resolved_appends.append({
                        "ts": ts_now,
                        "source": provider,
                        "event": event_data,
                    })
        if resolved_appends:
            append_ndjson(paths.events_path, resolved_appends)

    # Drop rows older than the retention window; keep the rest in `retained`.
    pruned_count = 0
    retained: Dict[str, Dict[str, Any]] = {}
    for k, v in merged.items():
        if _parse_last_seen(v) >= cutoff:
            retained[k] = v
        else:
            pruned_count += 1

    write_json(paths.alert_history_path, list(retained.values()))
    return {
        "existing": len(existing_by_raw_id),
        "incoming": len(incoming_events),
        "new_rows": len(new_rows),
        "history_size": len(retained),
        "pruned": pruned_count,
    }


def cmd_run(root: str | None, events_path: str | None) -> int:
    paths = resolve_paths(root)
    lock_path = _lock_paths(paths)
    ttl_days = int(os.environ.get("OA_TTL_DAYS") or "7")

    try:
        with FileLock(lock_path):
            raw_events = read_json(Path(events_path), []) if events_path else []
            incoming, errs = validate_event_batch(raw_events)
            if errs:
                for e in errs[:50]:
                    sys.stderr.write(f"{e.path}: {e.message}\n")
                if len(errs) > 50:
                    sys.stderr.write(f"... {len(errs) - 50} more\n")
                return 1

            stamped = [
                {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "event": e}
                for e in incoming
            ]
            if stamped:
                append_ndjson(paths.events_path, stamped)

            events: List[OutageEvent] = [
                OutageEvent.from_dict(e) for e in incoming
            ]
            summary = _merge_events_into_history(paths, events, ttl_days)
            sys.stdout.write(json.dumps({"status": "ok", **summary}) + "\n")
        return 0
    except RuntimeError:
        sys.stderr.write(_lock_error_message(lock_path) + "\n")
        return 2


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "customer"


def _parse_customer_csv(csv_path: Path) -> List[Dict[str, Any]]:
    import csv
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    customers_by_id: Dict[str, Dict[str, Any]] = {}
    with open(csv_path, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return []

        field_map = {fn.strip().lower(): fn for fn in reader.fieldnames if fn}
        is_tabular = "service" in field_map

        for row in reader:
            if not is_tabular:
                name = (
                    row.get("Customer Name")
                    or row.get("customer_name")
                    or row.get("Name")
                    or ""
                ).strip()
                if not name:
                    continue
                cid = (
                    row.get("Customer ID")
                    or row.get("customer_id")
                    or row.get("ID")
                    or _slugify(name)
                ).strip()
                subs: Dict[str, List[str]] = {}
                sub_svcs: Dict[str, List[str]] = {}

                for col, val in row.items():
                    if not col or not val:
                        continue
                    col_l = col.strip().lower()
                    if col_l in ("customer name", "customer id", "name", "id", "teams team id", "teams channel id"):
                        continue
                    svc = ""
                    if "azure" in col_l:
                        svc = "azure"
                    elif "aws" in col_l:
                        svc = "aws"
                    elif "zscaler" in col_l:
                        svc = "zscaler"
                    elif "cloudflare" in col_l:
                        svc = "cloudflare"
                    elif "connectwise" in col_l:
                        svc = "connectwise"
                    elif "m365" in col_l:
                        svc = "m365"
                    elif "google" in col_l:
                        svc = "googleworkspace"
                    elif "vector" in col_l:
                        svc = "vector"
                    elif "chorus" in col_l:
                        svc = "chorus"
                    elif "atlassian" in col_l:
                        svc = "atlassian"
                    elif "wiz" in col_l:
                        svc = "wiz"
                    elif "xero" in col_l:
                        svc = "xero"
                    elif "halopsa" in col_l or "halo" in col_l:
                        svc = "halopsa"
                    elif "ninja" in col_l:
                        svc = "ninjaone"
                    elif "duo" in col_l:
                        svc = "duo"
                    elif "kaseya" in col_l or "datto" in col_l or "itglue" in col_l or "it-glue" in col_l:
                        svc = "kaseya"
                    elif "enable" in col_l:
                        svc = "enable"
                    elif "mimecast" in col_l:
                        svc = "mimecast"
                    elif "lightwire" in col_l:
                        svc = "lightwire"
                    elif "myob" in col_l:
                        svc = "myob"
                    elif "rewst" in col_l:
                        svc = "rewst"
                    elif "crowdstrike" in col_l:
                        svc = "crowdstrike"
                    elif "kordia" in col_l:
                        svc = "kordia"
                    elif "pia" in col_l or "aidesk" in col_l:
                        svc = "pia"
                    elif "nbn" in col_l:
                        svc = "nbn"
                    elif "avepoint" in col_l:
                        svc = "avepoint"
                    elif "wasabi" in col_l:
                        svc = "wasabi"
                    elif "tenable" in col_l:
                        svc = "tenable"
                    elif "eset" in col_l:
                        svc = "eset"

                    if not svc:
                        continue

                    val_items = [v.strip().lower() for v in val.split(",") if v.strip()]
                    if "products" in col_l:
                        sub_svcs[svc] = val_items
                    else:
                        norm_regs = [re.sub(r"[^a-z0-9]+", "-", v).strip("-") for v in val_items]
                        subs[svc] = norm_regs

                entry = customers_by_id.setdefault(cid, {
                    "customer_id": cid,
                    "name": name,
                    "subscriptions": {},
                    "sub_services": {},
                })
                entry["name"] = name
                entry["subscriptions"].update(subs)
                if sub_svcs:
                    entry["sub_services"].update(sub_svcs)

            else:
                name = (row.get("customer_name") or row.get("Customer Name") or row.get("name") or "").strip()
                if not name:
                    continue
                cid = (row.get("customer_id") or row.get("Customer ID") or _slugify(name)).strip()
                svc = (row.get("service") or row.get("Service") or "").strip().lower()
                regs_raw = (row.get("regions") or row.get("Regions") or "").strip()
                subs_raw = (row.get("sub_services") or row.get("Sub Services") or "").strip()

                entry = customers_by_id.setdefault(cid, {
                    "customer_id": cid,
                    "name": name,
                    "subscriptions": {},
                    "sub_services": {},
                })
                entry["name"] = name
                if svc:
                    if regs_raw:
                        regs = [re.sub(r"[^a-z0-9]+", "-", r.strip().lower()).strip("-") for r in regs_raw.split(",") if r.strip()]
                        entry["subscriptions"][svc] = regs
                    if subs_raw:
                        sub_list = [s.strip().lower() for s in subs_raw.split(",") if s.strip()]
                        entry["sub_services"][svc] = sub_list

    for c in customers_by_id.values():
        if not c.get("sub_services"):
            c.pop("sub_services", None)

    return list(customers_by_id.values())


def cmd_import_customers(root: str | None, csv_path: str, replace: bool) -> int:
    paths = resolve_paths(root)
    csv_file = Path(csv_path)
    if not csv_file.exists():
        sys.stderr.write(f"error: CSV file does not exist: {csv_path}\n")
        return 1

    try:
        new_customers = _parse_customer_csv(csv_file)
    except Exception as e:
        sys.stderr.write(f"error parsing CSV: {e}\n")
        return 1

    if not new_customers:
        sys.stderr.write("warning: no valid customer rows found in CSV\n")
        return 0

    rules = load_region_rules(paths.approved_regions_csv)
    errs = validate_customers(new_customers, rules=rules)
    if errs:
        sys.stderr.write("validation errors in CSV customer data:\n")
        for e in errs[:20]:
            sys.stderr.write(f"  {e.path}: {e.message}\n")
        if len(errs) > 20:
            sys.stderr.write(f"  ... {len(errs) - 20} more errors\n")
        return 1

    lock_path = _lock_paths(paths)
    try:
        with FileLock(lock_path):
            existing = read_json(paths.customers_path, [])
            if not isinstance(existing, list):
                existing = []

            if replace:
                final_customers = new_customers
                updated_count = len(new_customers)
                added_count = 0
            else:
                cust_map = {c.get("customer_id"): c for c in existing if isinstance(c, dict)}
                added_count = 0
                updated_count = 0
                for nc in new_customers:
                    cid = nc.get("customer_id")
                    if cid in cust_map:
                        cust_map[cid].update(nc)
                        updated_count += 1
                    else:
                        cust_map[cid] = nc
                        added_count += 1
                final_customers = list(cust_map.values())

            write_json(paths.customers_path, final_customers)
    except RuntimeError as e:
        sys.stderr.write(f"could not acquire lock: {e}\n")
        return 2

    action = "replaced" if replace else "merged"
    sys.stdout.write(
        f"success: {action} {len(new_customers)} customer(s) "
        f"({updated_count} updated, {added_count} added). Total: {len(final_customers)}\n"
    )
    return 0


def cmd_import_invoices(root: str | None, csv_path: str, backup: bool = True) -> int:
    from .tools.import_invoices import import_invoices_to_state
    paths = resolve_paths(root)
    return import_invoices_to_state(Path(csv_path), paths, backup=backup)


def cmd_lint_customers(root: str | None) -> int:
    paths = resolve_paths(root)
    raw = read_json(paths.customers_path, [])
    rules = load_region_rules(paths.approved_regions_csv)
    errs = validate_customers(raw, rules=rules)
    if errs:
        for e in errs[:100]:
            sys.stderr.write(f"{e.path}: {e.message}\n")
        if len(errs) > 100:
            sys.stderr.write(f"... {len(errs) - 100} more\n")
        return 1
    return 0


def cmd_validate_regions(root: str | None) -> int:
    """Validate the approved-regions CSV: required columns, no duplicate
    (service, region_code) pairs, valid service keys, well-formed
    region codes, non-empty display names.

    Reads the same `OA_APPROVED_REGIONS_CSV` path the rest of the system
    uses (resolved via `paths.resolve_paths`), so the editor and the CLI
    always agree on the source of truth.
    """
    from .dashboard.regions_store import load_regions_csv, validate_regions
    paths = resolve_paths(root)
    rows = load_regions_csv(paths.approved_regions_csv)
    errs = validate_regions(rows)
    if errs:
        for e in errs[:100]:
            sys.stderr.write(f"{e.path}: {e.message}\n")
        if len(errs) > 100:
            sys.stderr.write(f"... {len(errs) - 100} more\n")
        return 1
    sys.stdout.write(f"{len(rows)} region row(s) OK\n")
    return 0


def cmd_list_providers() -> int:
    for name in ingest_pkg.list_providers():
        sys.stdout.write(name + "\n")
    return 0


def cmd_ingest(root: str | None, provider: str, apply: bool, url: str | None) -> int:
    paths = resolve_paths(root)
    kwargs: Dict[str, Any] = {}
    if url:
        kwargs["url"] = url
    try:
        result = ingest_pkg.run_provider(provider, state_dir=paths.state_dir, **kwargs)
    except KeyError as e:
        sys.stderr.write(str(e) + "\n")
        return 1

    if not result.ok:
        sys.stderr.write(f"[{provider}] ingest failed: {result.error}\n")
        return 1

    sys.stdout.write(
        f"[{provider}] fetched {result.raw_bytes} bytes, parsed {len(result.events)} events\n"
    )

    if not apply:
        sys.stdout.write(json.dumps(result.to_dict(), indent=2) + "\n")
        return 0

    # Apply under the file lock so we cannot race with `run` / `replay`.
    lock_path = _lock_paths(paths)
    ttl_days = int(os.environ.get("OA_TTL_DAYS") or "7")
    try:
        with FileLock(lock_path):
            as_dicts, ts_now = _events_to_dicts_and_stamp(result.events)
            for d in as_dicts:
                append_ndjson(
                    paths.events_path,
                    [{"ts": ts_now, "source": provider, "event": d}],
                )
            evs: List[OutageEvent] = [OutageEvent.from_dict(d) for d in as_dicts]
            summary = _merge_events_into_history(paths, evs, ttl_days, provider=provider)
            sys.stdout.write(json.dumps({"status": "ok", **summary}) + "\n")
        return 0
    except RuntimeError:
        sys.stderr.write(_lock_error_message(lock_path) + "\n")
        return 2


def cmd_replay(root: str | None, ttl_days: int | None, dry_run: bool) -> int:
    """Rebuild `alert_history.json` from `events.ndjson`.

    This is useful when you change TTL/retention semantics and want to apply
    them to the existing dataset without re-fetching from providers.
    """
    paths = resolve_paths(root)
    if ttl_days is None:
        ttl_days = int(os.environ.get("OA_TTL_DAYS") or "7")
    log = read_ndjson(paths.events_path)
    if not log:
        sys.stderr.write("events.ndjson is empty; nothing to replay\n")
        return 1

    all_events: List[OutageEvent] = []
    skipped = 0
    for row in log:
        # Two shapes are supported: {"event": {...}} (from `run`) and
        # {"event": <dict>, "source": ...} (from `ingest --apply`).
        ev_data = row.get("event") if isinstance(row, dict) else None
        if not isinstance(ev_data, dict):
            skipped += 1
            continue
        try:
            all_events.append(OutageEvent.from_dict(ev_data))
        except Exception:
            skipped += 1
            continue

    summary = {
        "log_rows": len(log),
        "parsed_events": len(all_events),
        "skipped": skipped,
        "ttl_days": ttl_days,
        "dry_run": dry_run,
    }

    if dry_run:
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
        return 0

    lock_path = _lock_paths(paths)
    try:
        with FileLock(lock_path):
            summary_out = _merge_events_into_history(paths, all_events, ttl_days)
        summary.update(summary_out)
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
        return 0
    except RuntimeError:
        sys.stderr.write(_lock_error_message(lock_path) + "\n")
        return 2


def _prune_events_ndjson(paths, days: int) -> Dict[str, Any]:
    """Prune entries in paths.events_path older than `days`.

    Uses a streaming tempfile to process logs of arbitrary size with O(1) memory usage.
    Returns a dictionary summarizing existing, retained, and pruned event counts.
    """
    if not paths.events_path.exists():
        return {"existing": 0, "retained": 0, "pruned": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Check filesystem free space against required headroom (at least 1.2x events_path size)
    try:
        free_bytes = shutil.disk_usage(paths.events_path.parent).free
        file_bytes = paths.events_path.stat().st_size
        required_bytes = int(file_bytes * 1.2)
        if free_bytes < required_bytes:
            sys.stderr.write(
                f"Insufficient disk headroom to prune events safely: "
                f"{free_bytes / (1024**3):.2f} GB available, need at least "
                f"{required_bytes / (1024**3):.2f} GB.\n"
            )
            return {
                "existing": 0,
                "retained": 0,
                "pruned": 0,
                "cutoff": cutoff_str,
                "aborted": True,
                "reason": "insufficient_headroom",
            }
    except OSError:
        pass

    pruned_count = 0
    total_count = 0
    retained_count = 0

    tmp_path = paths.events_path.with_suffix(".prune.tmp")

    try:
        with paths.events_path.open("r", encoding="utf-8") as f_in, \
             tmp_path.open("w", encoding="utf-8", newline="\n") as f_out:
            for line in f_in:
                if not line.strip():
                    continue
                total_count += 1
                ts_str = None
                if line.startswith('{"ts": "') and len(line) >= 28:
                    ts_str = line[8:28]
                else:
                    try:
                        row = json.loads(line)
                        ts_str = row.get("ts") if isinstance(row, dict) else None
                    except Exception:
                        pass

                if ts_str and ts_str < cutoff_str:
                    pruned_count += 1
                    continue

                retained_count += 1
                f_out.write(line if line.endswith("\n") else line + "\n")

        if pruned_count > 0:
            from .store import _safe_replace_or_copy
            _safe_replace_or_copy(tmp_path, paths.events_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    return {
        "existing": total_count,
        "retained": retained_count,
        "pruned": pruned_count,
        "cutoff": cutoff_str,
    }


def cmd_prune_events(root: str | None, days: int | None, dry_run: bool) -> int:
    """Prune `events.ndjson` to retain events within the specified retention window."""
    paths = resolve_paths(root)
    lock_path = _lock_paths(paths)

    if not paths.events_path.exists():
        sys.stderr.write("events.ndjson does not exist; nothing to prune\n")
        return 0

    if days is None:
        days = int(os.environ.get("OA_EVENTS_RETENTION_DAYS") or "90")

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        with FileLock(lock_path):
            if dry_run:
                total_count = 0
                pruned_count = 0
                retained_count = 0
                with paths.events_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        total_count += 1
                        ts_str = None
                        if line.startswith('{"ts": "') and len(line) >= 28:
                            ts_str = line[8:28]
                        else:
                            try:
                                row = json.loads(line)
                                ts_str = row.get("ts") if isinstance(row, dict) else None
                            except Exception:
                                pass

                        if ts_str and ts_str < cutoff_str:
                            pruned_count += 1
                            continue

                        retained_count += 1
                sys.stdout.write(
                    json.dumps({
                        "status": "dry-run",
                        "retention_days": days,
                        "total_events": total_count,
                        "retained_events": retained_count,
                        "pruned_events": pruned_count,
                        "cutoff": cutoff_str,
                    }) + "\n"
                )
                return 0

            summary = _prune_events_ndjson(paths, days)
            if summary.get("aborted"):
                sys.stdout.write(json.dumps({"status": "aborted", "retention_days": days, **summary}) + "\n")
                return 1
            sys.stdout.write(json.dumps({"status": "ok", "retention_days": days, **summary}) + "\n")
            return 0
    except RuntimeError:
        sys.stderr.write(_lock_error_message(lock_path) + "\n")
        return 2


def cmd_run_all(
    root: str | None,
    providers: str | None,
    skip_apply: bool,
    report_out: str | None,
) -> int:
    """Run the full ingest cycle for every (or specified) provider, and
    write a run report.

    Sequence:
      1. Lock the state dir.
      2. For each provider: fetch → parse → if not --skip-apply, merge
         into alert_history.json (and append to events.ndjson).
      3. Compute customer impact at request time.
      4. Write a run report (JSON) and a one-line human summary.
    """
    log = get_logger("run-all")
    paths = resolve_paths(root)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_paths(paths)
    ttl_days = int(os.environ.get("OA_TTL_DAYS") or "7")
    run_id = new_run_id()
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    provider_names: List[str] = (
        [p.strip() for p in providers.split(",") if p.strip()]
        if providers
        else ingest_pkg.list_providers()
    )
    log.info(
        "starting run-all",
        run_id=run_id,
        providers=",".join(provider_names),
        skip_apply=skip_apply,
    )

    if not provider_names:
        log.error("no providers registered; nothing to do")
        return 1

    provider_results: List[Dict[str, Any]] = []
    rc_overall = 0
    fetched_data = []
    for name in provider_names:
        t0 = time.monotonic()
        result = ingest_pkg.run_provider(name, state_dir=paths.state_dir)
        t_ms = int((time.monotonic() - t0) * 1000)
        fetched_data.append((name, result, t_ms))
        if not result.ok:
            log.error(
                f"[{name}] ingest failed",
                err=result.error,
                duration_ms=t_ms,
            )
            provider_results.append({
                "provider": name,
                "ok": False,
                "event_count": 0,
                "raw_bytes": result.raw_bytes,
                "duration_ms": t_ms,
                "error": result.error,
            })
            rc_overall = 1
        else:
            log.info(
                f"[{name}] ingested",
                events=len(result.events),
                bytes=result.raw_bytes,
                duration_ms=t_ms,
            )
            if skip_apply:
                provider_results.append({
                    "provider": name,
                    "ok": True,
                    "applied": False,
                    "event_count": len(result.events),
                    "raw_bytes": result.raw_bytes,
                    "duration_ms": t_ms,
                })

    if not skip_apply:
        try:
            with FileLock(lock_path):
                for name, result, t_ms in fetched_data:
                    if not result.ok:
                        continue
                    # Append to event log + merge into alert history.
                    as_dicts, ts_now = _events_to_dicts_and_stamp(result.events)
                    for d in as_dicts:
                        append_ndjson(
                            paths.events_path,
                            [{"ts": ts_now, "source": name, "event": d}],
                        )
                    evs: List[OutageEvent] = [OutageEvent.from_dict(d) for d in as_dicts]
                    summary = _merge_events_into_history(paths, evs, ttl_days, provider=name)
                    provider_results.append({
                        "provider": name,
                        "ok": True,
                        "applied": True,
                        "event_count": len(result.events),
                        "raw_bytes": result.raw_bytes,
                        "duration_ms": t_ms,
                        "history": summary,
                    })
        except RuntimeError as e:
            log.error(f"could not acquire lock: {e}")
            return 2

    # Recompute impact (no lock — alert_history.json is fully written
    # by the time we get here).
    rows = read_json(paths.alert_history_path, [])
    customers = _load_customers(paths)
    row_list: List[Dict[str, Any]] = rows if isinstance(rows, list) else []
    visible = filter_active_window(row_list, ttl_days=ttl_days)
    impact = compute_customer_impact(visible, customers)

    finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    history_summary = _history_summary(paths)
    report = make_report(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        providers=provider_results,
        history=history_summary,
        impact=impact,
    )

    if report_out:
        out_path = Path(report_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(f"report written to {out_path}")
    else:
        written = write_report(paths.state_dir, report)
        log.info(f"report written to {written}")

    # Emit a final single-line summary (always on stdout) for cron
    # to capture if needed.
    summary_line = (
        f"run-all {run_id} ok={rc_overall == 0} "
        f"providers={len(provider_results)} "
        f"events={sum(p.get('event_count', 0) for p in provider_results)} "
        f"history_size={history_summary.get('history_size', 0)} "
        f"impacted_customers={len(impact.get('by_customer', {}))}"
    )
    sys.stdout.write(summary_line + "\n")
    return rc_overall


def _history_summary(paths) -> Dict[str, int]:
    rows = read_json(paths.alert_history_path, [])
    if not isinstance(rows, list):
        return {"history_size": 0}
    active = sum(1 for r in rows if isinstance(r, dict) and str(r.get("severity", "")).lower() != "resolved")
    return {
        "history_size": len(rows),
        "active_count": active,
    }


def cmd_status(root: str | None) -> int:
    paths = resolve_paths(root)
    log = get_logger("status")
    summary = _history_summary(paths)
    customers = _load_customers(paths)
    latest = read_latest_report(paths.state_dir) if _safe_has_reports(paths) else None
    payload = {
        "state_dir": str(paths.state_dir),
        "history": summary,
        "customer_count": len(customers),
        "ttl_days": int(os.environ.get("OA_TTL_DAYS") or "7"),
        "last_run": latest,
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    log.info("status", **summary)
    return 0


def _safe_has_reports(paths) -> bool:
    return (paths.state_dir / "reports").is_dir()


def _resolve_dashboard_auth(
    cli_user: str | None,
    cli_password: str | None,
    cli_password_file: str | None,
) -> Optional[Tuple[str, str]]:
    """Resolve dashboard Basic Auth credentials from CLI + env vars.

    Precedence (highest first):
      - --password-file (CLI)
      - OA_DASHBOARD_PASSWORD_FILE (env)
      - --password (CLI)
      - OA_DASHBOARD_PASSWORD (env)
      - --user / OA_DASHBOARD_USER

    Returns `(user, password)` tuple, or `None` if neither is set.

    Exits the process with a clear error if only one of user/password is
    provided — that's almost always a typo.
    """
    user = cli_user or os.environ.get("OA_DASHBOARD_USER")

    # 1. Decide which password file (if any) to read. CLI wins over env.
    password_file_path: str | None = None
    if cli_password_file:
        password_file_path = cli_password_file
    else:
        password_file_path = os.environ.get("OA_DASHBOARD_PASSWORD_FILE")

    # 2. Resolve the actual password value.
    password: str | None = None
    password_source: str | None = None
    if password_file_path:
        try:
            password = Path(password_file_path).read_text(encoding="utf-8").rstrip("\r\n")
            password_source = f"file:{password_file_path}"
        except OSError as e:
            sys.stderr.write(
                f"[serve] could not read password file {password_file_path}: {e}\n"
            )
            raise SystemExit(2)
    elif cli_password:
        password = cli_password
        password_source = "CLI --password"
    else:
        env_pw = os.environ.get("OA_DASHBOARD_PASSWORD")
        if env_pw:
            password = env_pw
            password_source = "env OA_DASHBOARD_PASSWORD"

    if user and not password:
        sys.stderr.write(
            "[serve] OA_DASHBOARD_USER / --user is set but no password was provided. "
            "Set --password, --password-file, OA_DASHBOARD_PASSWORD, or "
            "OA_DASHBOARD_PASSWORD_FILE.\n"
        )
        raise SystemExit(2)
    if password and not user:
        sys.stderr.write(
            "[serve] a password was provided ({}: ...) but OA_DASHBOARD_USER / --user is not set.\n"
            .format(password_source or "source")
        )
        raise SystemExit(2)

    if user and password:
        return (user, password)
    return None


def cmd_serve(
    root: str | None,
    user: str | None = None,
    password: str | None = None,
    password_file: str | None = None,
    allow_insecure_bind: bool = False,
    ssl: bool = False,
    ssl_cert: str | None = None,
    ssl_key: str | None = None,
    redirect_port: int | None = None,
    host: str | None = None,
    port: int | None = None,
) -> int:
    # Late import so the rest of the CLI is usable without http.server deps
    # (it is stdlib, but this keeps the import surface small during tests).

    paths = resolve_paths(root)
    from .dashboard.server import InsecureBindError, run_server

    resolved_host = host or os.environ.get("OA_DASHBOARD_HOST", "127.0.0.1")
    resolved_port = port or int(os.environ.get("OA_DASHBOARD_PORT") or "8765")
    auth = _resolve_dashboard_auth(user, password, password_file)

    env_ssl = os.environ.get("OA_DASHBOARD_SSL", "").strip().lower() in ("1", "true", "yes")
    ssl_enabled = ssl or env_ssl or bool(ssl_cert) or bool(os.environ.get("OA_DASHBOARD_SSL_CERT"))
    resolved_cert = ssl_cert or os.environ.get("OA_DASHBOARD_SSL_CERT")
    resolved_key = ssl_key or os.environ.get("OA_DASHBOARD_SSL_KEY")

    env_redirect = os.environ.get("OA_DASHBOARD_REDIRECT_PORT")
    resolved_redirect = redirect_port or (int(env_redirect) if env_redirect else None)

    try:
        run_server(
            paths=paths,
            host=resolved_host,
            port=resolved_port,
            auth=auth,
            allow_insecure_bind=allow_insecure_bind,
            ssl_enabled=ssl_enabled,
            ssl_cert=resolved_cert,
            ssl_key=resolved_key,
            redirect_port=resolved_redirect,
        )
        return 0
    except InsecureBindError as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1


def cmd_generate_cert(
    root: str | None,
    cert: str | None = None,
    key: str | None = None,
    host: str = "127.0.0.1",
    days: int = 3650,
) -> int:
    from .dashboard.server import ensure_self_signed_cert

    paths = resolve_paths(root)
    if cert and key:
        cert_path = Path(cert)
        key_path = Path(key)
    else:
        etc_dir = Path("/etc/outage-automation")
        if etc_dir.is_dir() and os.access(etc_dir, os.W_OK):
            cert_path = Path(cert) if cert else etc_dir / "dashboard.crt"
            key_path = Path(key) if key else etc_dir / "dashboard.key"
        else:
            default_ssl_dir = paths.state_dir / "ssl"
            cert_path = Path(cert) if cert else default_ssl_dir / "dashboard.crt"
            key_path = Path(key) if key else default_ssl_dir / "dashboard.key"

    if cert_path.exists():
        cert_path.unlink()
    if key_path.exists():
        key_path.unlink()

    ensure_self_signed_cert(cert_path, key_path, host=host, days=days)
    sys.stdout.write(f"Generated TLS certificate: {cert_path}\n")
    sys.stdout.write(f"Generated TLS private key:  {key_path} (mode 0600)\n")
    sys.stdout.write(f"Validity: {days} days for host/SAN: {host}\n")
    return 0


def cmd_dashboard_data(root: str | None, out_path: str | None) -> int:
    from .dashboard_payload import build_dashboard_payload
    paths = resolve_paths(root)
    payload = build_dashboard_payload(paths)
    if out_path:
        write_json(Path(out_path), payload)
    else:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


def cmd_lint_events(root: str | None, strict: bool) -> int:
    from .lint_events import lint_state, exit_code_for
    paths = resolve_paths(root)
    report = lint_state(paths.state_dir)
    print(report.render())
    code = exit_code_for(report)
    if strict and not report.ok:
        return 1
    return code


def cmd_verify_fixtures(root: str | None, strict: bool) -> int:
    # Delegate to the helper that already handles OA_FIXTURES_DIR override
    # and walks up from this package's install location. This avoids the
    # fragility of `paths.state_dir.parent.parent`, which miscounts parents
    # when the state dir is at `<root>/state` instead of
    # `<root>/python_system/state`.
    from .verify_fixtures import verify_fixtures, fixtures_dir
    from ._fixture_server import start_fixture_server
    paths = resolve_paths(root)
    port = start_fixture_server()
    os.environ["OA_FIXTURE_BASE_URL"] = f"http://127.0.0.1:{port}"
    report = verify_fixtures(fixtures_dir())
    print(report.render())
    if strict and not report.ok:
        return 1
    return 0 if report.ok else 1


def cmd_fixtures_dir() -> int:
    from .verify_fixtures import fixtures_dir
    print(fixtures_dir())
    return 0


def main(argv: List[str] | None = None) -> int:
    ns = _parse_args(argv or sys.argv[1:])
    if ns.cmd == "init":
        return cmd_init(ns.root)
    if ns.cmd == "run":
        return cmd_run(ns.root, ns.events)
    if ns.cmd == "lint-customers":
        return cmd_lint_customers(ns.root)
    if ns.cmd == "import-customers":
        return cmd_import_customers(ns.root, ns.csv_file, ns.replace)
    if ns.cmd == "import-invoices":
        return cmd_import_invoices(ns.root, ns.csv_file, backup=not ns.no_backup)
    if ns.cmd == "validate-regions":
        return cmd_validate_regions(ns.root)
    if ns.cmd == "lint-events":
        return cmd_lint_events(ns.root, ns.strict)
    if ns.cmd == "verify-fixtures":
        return cmd_verify_fixtures(ns.root, ns.strict)
    if ns.cmd == "fixtures-dir":
        return cmd_fixtures_dir()
    if ns.cmd == "dashboard-data":
        return cmd_dashboard_data(ns.root, ns.out)
    if ns.cmd == "list-providers":
        return cmd_list_providers()
    if ns.cmd == "ingest":
        return cmd_ingest(ns.root, ns.provider, ns.apply, ns.url)
    if ns.cmd == "replay":
        return cmd_replay(ns.root, ns.ttl_days, ns.dry_run)
    if ns.cmd == "prune-events":
        return cmd_prune_events(ns.root, ns.days, ns.dry_run)
    if ns.cmd == "generate-cert":
        return cmd_generate_cert(ns.root, ns.cert, ns.key, ns.host, ns.days)
    if ns.cmd == "serve":
        return cmd_serve(
            ns.root,
            user=ns.user,
            password=ns.password,
            password_file=ns.password_file,
            allow_insecure_bind=ns.allow_insecure_bind,
            ssl=ns.ssl,
            ssl_cert=ns.ssl_cert,
            ssl_key=ns.ssl_key,
            redirect_port=ns.redirect_port,
        )
    if ns.cmd == "run-all":
        return cmd_run_all(ns.root, ns.providers, ns.skip_apply, ns.report_out)
    if ns.cmd == "status":
        return cmd_status(ns.root)
    return 1
