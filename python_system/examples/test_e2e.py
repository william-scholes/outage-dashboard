"""End-to-end tests for the Python migration.

Covers:
- Idempotent `run` (applying the same batch twice keeps history_size)
- `lint-customers` exit codes
- `ingest --apply` from a local file:// URL
- `replay` is consistent with the live state
- `dashboard-data` returns valid JSON with the expected shape
- File lock prevents concurrent writers
- On-the-fly customer matching works (no matched_customers in state)

Run: `python examples/test_e2e.py`
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = ROOT
PY_SYSTEM = REPO / "python_system"
STATE = PY_SYSTEM / "state"
SCRATCH = REPO / "python_system" / "state_e2e_scratch"


# ---------------------------------------------------------------------------
# ISS-011: Local HTTP fixture server (replaces file:// URLs broken by ISS-003)
# ---------------------------------------------------------------------------
# Import from the single source of truth in outage_automation._fixture_server.
import sys as _sys
_sys.path.insert(0, str(REPO / "python_system"))
import outage_automation._fixture_server as _fixture_server

# Start the server immediately at import time so module-level URL constants
# (cf_url, aws_url, etc.) can embed the port without depending on main() running.
_fixture_server.start_fixture_server()


def http_fixture_url(abs_path: Path) -> str:
    """Return an HTTP URL for an absolute path inside REPO, served by the fixture server."""
    rel = abs_path.resolve().relative_to(REPO.resolve())
    return f"http://127.0.0.1:{_fixture_server._FIXTURE_PORT}/{str(rel).replace(os.sep, '/')}"


def stop_fixture_server() -> None:
    # Not called: the fixture server runs as a daemon thread and terminates
    # automatically at process exit. Kept for potential future use.
    if _fixture_server._FIXTURE_HTTPd is not None:
        _fixture_server._FIXTURE_HTTPd.shutdown()
        _fixture_server._FIXTURE_HTTPd.server_close()
        _fixture_server._FIXTURE_HTTPd = None


def state_dir_for(base: Path) -> Path:
    """Mirror `outage_automation.paths.resolve_paths` for tests."""
    nested = base / "python_system" / "state"
    direct = base / "state"
    if direct.is_dir() and not nested.is_dir():
        return direct
    return nested


def make_fixture_env(scratch: Path) -> dict[str, str]:
    """Generate an environment routing all fixture-backed providers to the local fixture server."""
    from outage_automation.ingest.registry import get_adapter
    from outage_automation.verify_fixtures import _FILENAME_TO_PROVIDER
    env = os.environ.copy()
    env["OA_ROOT"] = str(scratch)
    env.setdefault("OA_APPROVED_REGIONS_CSV", str(REPO / "approved-regions.csv"))
    for filename, provider in _FILENAME_TO_PROVIDER.items():
        fixture_file = REPO / "parser-tests" / "inputs" / filename
        if fixture_file.is_file():
            adapter_cls = get_adapter(provider)
            env_var = getattr(adapter_cls, "url_env_var", None)
            if env_var:
                env[env_var] = http_fixture_url(fixture_file).replace("\\", "/")
    return env


def run_cli(*args: str, root: Path | None = None) -> subprocess.CompletedProcess:
    """Run `python python_system/run.py ...` and return CompletedProcess.

    Uses `OA_ROOT` env var to override the resolved root directory, and
    points `OA_APPROVED_REGIONS_CSV` at the repo-level CSV so lint
    works in scratch dirs that don't have one.
    """
    cmd = [sys.executable, "python_system/run.py", *args]
    env = os.environ.copy()
    if root is not None:
        env["OA_ROOT"] = str(root)
    env.setdefault("OA_APPROVED_REGIONS_CSV", str(REPO / "approved-regions.csv"))
    return subprocess.run(
        cmd, cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env
    )


def assert_eq(label: str, actual, expected) -> bool:
    ok = actual == expected
    mark = "OK  " if ok else "FAIL"
    print(f"{mark} {label}: actual={actual!r} expected={expected!r}")
    return ok


def assert_true(label: str, cond: bool, detail: str = "") -> bool:
    mark = "OK  " if cond else "FAIL"
    suffix = f" ({detail})" if detail else ""
    print(f"{mark} {label}{suffix}")
    return cond


def setup_scratch() -> Path:
    """Reset scratch dir. State files live at `<scratch>/python_system/state/`."""
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    return SCRATCH


def write_customers(state_dir: Path) -> None:
    (state_dir / "customers.json").write_text(
        json.dumps([
            {
                "customer_id": "t-1",
                "name": "Test-Client",
                "subscriptions": {
                    "cloudflare": ["global"],
                    "aws": ["ap-southeast-2"],
                },
            }
        ]),
        encoding="utf-8",
    )


def write_events(scratch: Path) -> Path:
    events_path = scratch / "events.json"
    events_path.write_text(
        json.dumps([
            {
                "raw_id": "test-1",
                "service": "cloudflare",
                "title": "Test",
                "description": "test desc",
                "severity": "info",
                "regions": ["Global"],
                "link": "https://example.invalid",
                "published": "2026-06-01T00:00:00.000Z",
            }
        ]),
        encoding="utf-8",
    )
    return events_path


def main() -> int:
    failures = 0

    # --- 1. Idempotent `run` ---
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    events_path = write_events(scratch)
    failures += not assert_true("state dir created", state_dir.is_dir(), str(state_dir))

    r2 = run_cli("run", "--events", str(events_path), root=scratch)
    failures += not assert_true("first run exit 0", r2.returncode == 0, r2.stderr[-200:])
    after_first = json.loads((state_dir / "alert_history.json").read_text(encoding="utf-8"))
    failures += not assert_eq("after first run", len(after_first), 1)

    r3 = run_cli("run", "--events", str(events_path), root=scratch)
    failures += not assert_true("second run exit 0", r3.returncode == 0, r3.stderr[-200:])
    after_second = json.loads((state_dir / "alert_history.json").read_text(encoding="utf-8"))
    failures += not assert_eq("after second run (idempotent)", len(after_second), 1)

    # --- 2. lint-customers success ---
    r4 = run_cli("lint-customers", root=scratch)
    failures += not assert_true("lint-customers exit 0", r4.returncode == 0, r4.stderr[-200:])

    # --- 3. lint-customers failure (bad region) ---
    (state_dir / "customers.json").write_text(
        json.dumps([{
            "customer_id": "bad-1",
            "name": "BadClient",
            "subscriptions": {"azure": ["made-up-region-123"]}
        }]),
        encoding="utf-8",
    )
    r5 = run_cli("lint-customers", root=scratch)
    failures += not assert_eq("lint-customers detects bad region", r5.returncode, 1)
    failures += not assert_true(
        "error mentions region",
        "made-up-region-123" in r5.stderr,
        r5.stderr[-200:],
    )
    # Restore good customers.
    write_customers(state_dir)

    # --- 4. ingest --apply from a local file URL ---
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    cf_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "cloudflare-api.json").replace("\\", "/")
    r6 = run_cli(
        "ingest", "cloudflare", "--apply", "--url", cf_url, root=scratch,
    )
    failures += not assert_true("ingest --apply exit 0", r6.returncode == 0, r6.stderr[-200:])
    after_ingest = json.loads((state_dir / "alert_history.json").read_text(encoding="utf-8"))
    failures += not assert_eq("ingest applied 50 events", len(after_ingest), 50)

    # --- 5. on-the-fly customer matching ---
    r7 = run_cli("dashboard-data", root=scratch)
    failures += not assert_true("dashboard-data exit 0", r7.returncode == 0, r7.stderr[-200:])
    payload = json.loads(r7.stdout)
    first_row = payload["rows"][0]
    failures += not assert_true(
        "no matched_customers in state",
        "matched_customers" not in first_row,
    )
    failures += not assert_true(
        "customer impact has Test-Client",
        "Test-Client" in payload["impact"]["by_customer"],
    )

    # --- 6. Replay consistency ---
    r8 = run_cli("replay", "--dry-run", root=scratch)
    failures += not assert_true("replay dry-run exit 0", r8.returncode == 0, r8.stderr[-200:])
    failures += not assert_true("replay parses >0 events", '"parsed_events": ' in r8.stdout)
    r8b = run_cli("replay", root=scratch)
    failures += not assert_true("replay exit 0", r8b.returncode == 0, r8b.stderr[-200:])

    # --- 7. Lock prevents concurrent writers (active PID) ---
    scratch = setup_scratch()
    events_path = write_events(scratch)
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    active_pid = os.getpid()
    (state_dir / ".lock").write_text(
        json.dumps({"pid": active_pid, "created_at": time.time()}), encoding="utf-8"
    )
    r9 = run_cli("run", "--events", str(events_path), root=scratch)
    failures += not assert_eq("locked run returns 2", r9.returncode, 2)
    failures += not assert_true("locked stderr mentions pid", str(active_pid) in r9.stderr, r9.stderr[-200:])
    (state_dir / ".lock").unlink()

    # --- 7b. Stale lock (dead PID) is automatically reclaimed ---
    (state_dir / ".lock").write_text(
        json.dumps({"pid": 999999, "created_at": time.time()}), encoding="utf-8"
    )
    r9b = run_cli("run", "--events", str(events_path), root=scratch)
    failures += not assert_eq("stale lock recovered and exit 0", r9b.returncode, 0)
    failures += not assert_true("stale lock unlinked", not (state_dir / ".lock").exists() or json.loads((state_dir / ".lock").read_text())["pid"] != 999999)

    # --- 8. List providers ---
    r10 = run_cli("list-providers")
    failures += not assert_true("list-providers exit 0", r10.returncode == 0, r10.stderr[-200:])
    failures += not assert_true("cloudflare registered", "cloudflare" in r10.stdout, r10.stdout)
    failures += not assert_true("atlassian registered", "atlassian" in r10.stdout, r10.stdout)

    # --- 9. Unknown provider ---
    r11 = run_cli("ingest", "fake-provider")
    failures += not assert_eq("unknown provider exit 1", r11.returncode, 1)

    # --- 10. Timestamps consistent Z suffix (reuse ingested state) ---
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    cf_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "cloudflare-api.json").replace("\\", "/")
    run_cli("ingest", "cloudflare", "--apply", "--url", cf_url, root=scratch)
    r12 = run_cli("dashboard-data", root=scratch)
    sample = json.loads(r12.stdout)["rows"][0]
    failures += not assert_true("published_at has Z", sample["published_at"].endswith("Z"))
    failures += not assert_true("last_seen_at has Z", sample["last_seen_at"].endswith("Z"))
    failures += not assert_true("expires_at has Z", sample["expires_at"].endswith("Z"))
    failures += not assert_true("no microseconds", "." not in sample["published_at"])

    # --- 11. OA_RAW_CAPTURE persists a raw payload ---
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    env = os.environ.copy()
    env["OA_ROOT"] = str(scratch)
    env["OA_RAW_CAPTURE"] = "1"
    cf_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "cloudflare-api.json").replace("\\", "/")
    r13 = subprocess.run(
        [sys.executable, "python_system/run.py", "ingest", "cloudflare",
         "--apply", "--url", cf_url],
        cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env,
    )
    failures += not assert_true("raw capture ingest exit 0", r13.returncode == 0, r13.stderr[-200:])
    raw_dir = state_dir / "raw" / "cloudflare"
    files = list(raw_dir.glob("*.json")) if raw_dir.exists() else []
    failures += not assert_true("raw payload captured", len(files) > 0, str(raw_dir))

    # --- 12. run-all writes a run report ---
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    cf_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "cloudflare-api.json").replace("\\", "/")
    cw_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "connectwise-api.json").replace("\\", "/")
    m365_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "m365-api.json").replace("\\", "/")
    azure_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "azure-rss.xml").replace("\\", "/")
    aws_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "aws-rss.xml").replace("\\", "/")
    zscaler_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "zscaler-rss.xml").replace("\\", "/")
    chorus_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "chorus-api.json").replace("\\", "/")
    google_workspace_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "googleworkspace-api.json").replace("\\", "/")
    vector_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "vector-api.json").replace("\\", "/")
    atlassian_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "atlassian-api.json").replace("\\", "/")
    env = make_fixture_env(scratch)
    r14 = subprocess.run(
        [sys.executable, "python_system/run.py", "run-all"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env,
    )
    failures += not assert_true("run-all exit 0", r14.returncode == 0, r14.stderr[-200:])
    reports = list((state_dir / "reports").glob("*.json")) if (state_dir / "reports").is_dir() else []
    failures += not assert_true("run-all wrote a report", len(reports) == 1, str(reports))
    if reports:
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        failures += not assert_true("report has run_id", isinstance(report.get("run_id"), str))
        from outage_automation.config import DEFAULT_SERVICES
        failures += not assert_true(
            f"report has {len(DEFAULT_SERVICES)} providers",
            isinstance(report.get("providers"), list) and len(report["providers"]) == len(DEFAULT_SERVICES),
            f"actual={len(report.get('providers', []))} expected={len(DEFAULT_SERVICES)}",
        )
        # Find each provider
        cf_result = next((p for p in report["providers"] if p["provider"] == "cloudflare"), None)
        cw_result = next((p for p in report["providers"] if p["provider"] == "connectwise"), None)
        m365_result = next((p for p in report["providers"] if p["provider"] == "m365"), None)
        az_result = next((p for p in report["providers"] if p["provider"] == "azure"), None)
        aws_result = next((p for p in report["providers"] if p["provider"] == "aws"), None)
        zs_result = next((p for p in report["providers"] if p["provider"] == "zscaler"), None)
        chorus_result = next((p for p in report["providers"] if p["provider"] == "chorus"), None)
        gw_result = next((p for p in report["providers"] if p["provider"] == "googleworkspace"), None)
        vector_result = next((p for p in report["providers"] if p["provider"] == "vector"), None)
        atl_result = next((p for p in report["providers"] if p["provider"] == "atlassian"), None)
        wiz_result = next((p for p in report["providers"] if p["provider"] == "wiz"), None)
        failures += not assert_true("cloudflare in report", cf_result is not None)
        failures += not assert_true("connectwise in report", cw_result is not None)
        failures += not assert_true("m365 in report", m365_result is not None)
        failures += not assert_true("azure in report", az_result is not None)
        failures += not assert_true("aws in report", aws_result is not None)
        failures += not assert_true("zscaler in report", zs_result is not None)
        failures += not assert_true("chorus in report", chorus_result is not None)
        failures += not assert_true("googleworkspace in report", gw_result is not None)
        failures += not assert_true("vector in report", vector_result is not None)
        failures += not assert_true("atlassian in report", atl_result is not None)
        failures += not assert_true("wiz in report", wiz_result is not None)
        if cf_result:
            failures += not assert_true("cloudflare event_count=50", cf_result.get("event_count") == 50)
        if cw_result:
            failures += not assert_true("connectwise event_count=4", cw_result.get("event_count") == 4)
        if m365_result:
            failures += not assert_true("m365 event_count=3", m365_result.get("event_count") == 3)
        if az_result:
            failures += not assert_true("azure event_count=4", az_result.get("event_count") == 4)
        if aws_result:
            failures += not assert_true("aws event_count=4", aws_result.get("event_count") == 4)
        if zs_result:
            failures += not assert_true("zscaler event_count=5", zs_result.get("event_count") == 5)
        if chorus_result:
            failures += not assert_true("chorus event_count=3", chorus_result.get("event_count") == 3)
        if gw_result:
            failures += not assert_true("googleworkspace event_count=3", gw_result.get("event_count") == 3)
        if vector_result:
            failures += not assert_true("vector event_count=6", vector_result.get("event_count") == 6)
        if atl_result:
            failures += not assert_true("atlassian event_count=50", atl_result.get("event_count") == 50)
        if wiz_result:
            failures += not assert_true("wiz event_count=25", wiz_result.get("event_count") == 25)
        failures += not assert_true("impact_summary present", isinstance(report.get("impact_summary"), dict))
        failures += not assert_true("impact_summary has top customers",
                                    isinstance(report["impact_summary"].get("top_customers"), list))

    # --- 13. run-all --skip-apply does not modify state ---
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    env = make_fixture_env(scratch)
    r15 = subprocess.run(
        [sys.executable, "python_system/run.py", "run-all", "--skip-apply"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env,
    )
    failures += not assert_true("run-all --skip-apply exit 0", r15.returncode == 0, r15.stderr[-200:])
    after = json.loads((state_dir / "alert_history.json").read_text(encoding="utf-8"))
    failures += not assert_eq("skip-apply leaves state empty", len(after), 0)

    # --- 14. status reports last_run ---
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    env = make_fixture_env(scratch)
    r16 = subprocess.run(
        [sys.executable, "python_system/run.py", "run-all"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env,
    )
    failures += not assert_true("status: run-all prep exit 0", r16.returncode == 0, r16.stderr[-200:])
    r17 = run_cli("status", root=scratch)
    failures += not assert_true("status exit 0", r17.returncode == 0, r17.stderr[-200:])
    status = json.loads(r17.stdout)
    failures += not assert_true("status has last_run", status.get("last_run") is not None)
    failures += not assert_true(
        "status last_run includes cloudflare",
        status["last_run"] is not None
        and any(p["provider"] == "cloudflare" for p in status["last_run"]["providers"]),
    )

    # --- 15. /api/dashboard includes last_run ---
    import urllib.request
    sys.path.insert(0, str(REPO / "python_system"))
    from outage_automation.dashboard.server import run_server
    from outage_automation.paths import resolve_paths
    paths = resolve_paths(scratch)
    srv = run_server(paths, host="127.0.0.1", port=8766, block=False)
    try:
        time.sleep(0.4)
        body = urllib.request.urlopen("http://127.0.0.1:8766/api/dashboard", timeout=5).read()
        d = json.loads(body.decode("utf-8"))
        failures += not assert_true("/api/dashboard has last_run", d.get("last_run") is not None)
    finally:
        srv.shutdown()
        srv.server_close()

    # --- 16. Bin scripts exist and start with shebang ---
    for name in ("oa", "oa-init.sh", "oa-run-all.sh", "oa-serve.sh", "oa-status.sh", "oa-backup.sh", "oa-lint.sh"):
        p = REPO / "python_system" / "bin" / name
        failures += not assert_true(f"bin/{name} exists", p.is_file(), str(p))
        if p.is_file():
            head = p.read_bytes()[:2]
            failures += not assert_true(f"bin/{name} has shebang", head == b"#!", head)

    # --- 17. Phase 3a-i: ConnectWise ingester (Status.io) ---
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    cw_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "connectwise-api.json").replace("\\", "/")
    r18 = run_cli("ingest", "connectwise", "--apply", "--url", cw_url, root=scratch)
    if r18.returncode != 0:
        # `ingest <provider>` doesn't accept --url; use env var.
        env = os.environ.copy()
        env["OA_ROOT"] = str(scratch)
        env["OA_CONNECTWISE_URL"] = cw_url
        r18 = subprocess.run(
            [sys.executable, "python_system/run.py", "ingest",
             "connectwise", "--apply"],
            cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env,
        )
    failures += not assert_true("connectwise ingest exit 0", r18.returncode == 0, r18.stderr[-200:])
    cw_history = json.loads((state_dir / "alert_history.json").read_text(encoding="utf-8"))
    failures += not assert_eq("connectwise ingested 4 events", len(cw_history), 4)
    sevs = sorted(r["severity"] for r in cw_history)
    failures += not assert_eq("connectwise severities match", sevs, ["info", "resolved", "resolved", "warning"])
    services = sorted({r["service"] for r in cw_history})
    failures += not assert_eq("connectwise service tag", services, ["connectwise"])

    # --- 18. Phase 3a-ii: M365 ingester (RSS) ---
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    m365_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "m365-api.json").replace("\\", "/")
    env = os.environ.copy()
    env["OA_ROOT"] = str(scratch)
    env["OA_M365_URL"] = m365_url
    r19 = subprocess.run(
        [sys.executable, "python_system/run.py", "ingest", "m365", "--apply"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env,
    )
    failures += not assert_true("m365 ingest exit 0", r19.returncode == 0, r19.stderr[-200:])
    m365_history = json.loads((state_dir / "alert_history.json").read_text(encoding="utf-8"))
    failures += not assert_eq("m365 ingested 3 events", len(m365_history), 3)
    m365_sevs = sorted(r["severity"] for r in m365_history)
    failures += not assert_eq("m365 severities match", m365_sevs, ["critical", "resolved", "warning"])
    # The two non-resolved events should mention Australia (normalized to lowercase)
    aus_count = sum(1 for r in m365_history if "australia" in (r.get("regions") or []))
    failures += not assert_eq("m365 detected Australia region", aus_count, 2)
    # Service tag should be m365
    failures += not assert_eq("m365 service tag", {r["service"] for r in m365_history}, {"m365"})

    # --- 19. Phase 3a-iii: Azure ingester (RSS) ---
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    azure_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "azure-rss.xml").replace("\\", "/")
    env = os.environ.copy()
    env["OA_ROOT"] = str(scratch)
    env["OA_AZURE_URL"] = azure_url
    r20 = subprocess.run(
        [sys.executable, "python_system/run.py", "ingest", "azure", "--apply"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env,
    )
    failures += not assert_true("azure ingest exit 0", r20.returncode == 0, r20.stderr[-200:])
    azure_history = json.loads((state_dir / "alert_history.json").read_text(encoding="utf-8"))
    failures += not assert_eq("azure ingested 4 events", len(azure_history), 4)
    azure_sevs = sorted(r["severity"] for r in azure_history)
    failures += not assert_eq("azure severities match", azure_sevs, ["critical", "info", "resolved", "warning"])
    failures += not assert_eq("azure service tag", {r["service"] for r in azure_history}, {"azure"})
    # Check region detection
    aus_east_count = sum(1 for r in azure_history if "australia-east" in (r.get("regions") or []))
    failures += not assert_true("azure detected Australia East in at least 2 events", aus_east_count >= 2, str(aus_east_count))

    # --- 20. Phase 3a-iv: AWS ingester (RSS, guid region extraction) ---
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    aws_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "aws-rss.xml").replace("\\", "/")
    env = os.environ.copy()
    env["OA_ROOT"] = str(scratch)
    env["OA_AWS_URL"] = aws_url
    r21 = subprocess.run(
        [sys.executable, "python_system/run.py", "ingest", "aws", "--apply"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env,
    )
    failures += not assert_true("aws ingest exit 0", r21.returncode == 0, r21.stderr[-200:])
    aws_history = json.loads((state_dir / "alert_history.json").read_text(encoding="utf-8"))
    failures += not assert_eq("aws ingested 4 events", len(aws_history), 4)
    aws_sevs = sorted(r["severity"] for r in aws_history)
    failures += not assert_eq("aws severities match", aws_sevs, ["critical", "info", "resolved", "warning"])
    failures += not assert_eq("aws service tag", {r["service"] for r in aws_history}, {"aws"})
    # The eu-central-2 event should be discarded (not Australia-relevant)
    dynamodb = next((r for r in aws_history if "DynamoDB" in r["title"]), None)
    failures += not assert_true("aws eu-central-2 event is discarded",
                                dynamodb is None,
                                str(dynamodb.get("regions") if dynamodb else None))
    # Sydney and Melbourne events should have proper regions
    sydney = next((r for r in aws_history if "S3" in r["title"]), None)
    failures += not assert_true("aws S3 Sydney event has ap-southeast-2",
                                sydney is not None and "ap-southeast-2" in (sydney.get("regions") or []),
                                str(sydney.get("regions") if sydney else None))
    melbourne = next((r for r in aws_history if "EC2" in r["title"]), None)
    failures += not assert_true("aws EC2 Melbourne event has ap-southeast-4",
                                melbourne is not None and "ap-southeast-4" in (melbourne.get("regions") or []),
                                str(melbourne.get("regions") if melbourne else None))

    # --- 21. Phase 3a-v: Zscaler ingester (RSS, ZIA/ZPA/ZDX/ZCC detection) ---
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    zscaler_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "zscaler-rss.xml").replace("\\", "/")
    env = os.environ.copy()
    env["OA_ROOT"] = str(scratch)
    env["OA_ZSCALER_URL"] = zscaler_url
    r22 = subprocess.run(
        [sys.executable, "python_system/run.py", "ingest", "zscaler", "--apply"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env,
    )
    failures += not assert_true("zscaler ingest exit 0", r22.returncode == 0, r22.stderr[-200:])
    zscaler_history = json.loads((state_dir / "alert_history.json").read_text(encoding="utf-8"))
    failures += not assert_eq("zscaler ingested 5 events", len(zscaler_history), 5)
    zscaler_sevs = sorted(r["severity"] for r in zscaler_history)
    failures += not assert_eq("zscaler severities match", zscaler_sevs, ["critical", "info", "resolved", "warning", "warning"])
    failures += not assert_eq("zscaler service tag", {r["service"] for r in zscaler_history}, {"zscaler"})
    # Zscaler outages should have regions=['global'] and empty sub_services.
    zia = next((r for r in zscaler_history if "ZIA Service Outage" in r["title"]), None)
    failures += not assert_true("zscaler ZIA outage detected", zia is not None)
    failures += not assert_eq("zscaler ZIA row has empty sub_services", zia.get("sub_services") if zia else None, [])
    failures += not assert_true("zscaler ZIA row has regions=['global'] only",
                                zia is not None and (zia.get("regions") or []) == ["global"],
                                str(zia.get("regions") if zia else None))
    # The ZCC maintenance should be info
    zcc = next((r for r in zscaler_history if "ZCC" in r["title"]), None)
    failures += not assert_true("zscaler ZCC maintenance is info",
                                zcc is not None and zcc.get("severity") == "info",
                                str(zcc.get("severity") if zcc else None))
    # The mixed ZIA/ZPA event should also have empty sub_services
    mixed = next((r for r in zscaler_history if "ZIA and ZPA" in r["title"]), None)
    failures += not assert_true("zscaler mixed ZIA+ZPA detected", mixed is not None)
    failures += not assert_eq("zscaler mixed row has empty sub_services", mixed.get("sub_services") if mixed else None, [])

    # --- Chorus Ingest Test ---
    chorus_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "chorus-api.json").replace("\\", "/")
    env = os.environ.copy()
    env["OA_ROOT"] = str(scratch)
    env["OA_CHORUS_URL"] = chorus_url
    r_chorus = subprocess.run(
        [sys.executable, "python_system/run.py", "ingest", "chorus", "--apply"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env,
    )
    failures += not assert_true("chorus ingest exit 0", r_chorus.returncode == 0, r_chorus.stderr[-200:])
    chorus_history = json.loads((state_dir / "alert_history.json").read_text(encoding="utf-8"))
    failures += not assert_eq("chorus ingested events count", len(chorus_history), 8)
    chorus_only = [r for r in chorus_history if r["service"] == "chorus"]
    failures += not assert_eq("chorus only events", len(chorus_only), 3)
    chorus_sevs = sorted(r["severity"] for r in chorus_only)
    failures += not assert_eq("chorus severities match", chorus_sevs, ["critical", "info", "resolved"])
    
    kelburn = next((r for r in chorus_only if "Kelburn" in r["title"]), None)
    failures += not assert_true("chorus Kelburn outage detected", kelburn is not None)
    failures += not assert_eq("chorus Kelburn sub_services", kelburn.get("sub_services") if kelburn else None, ["fibre"])
    failures += not assert_eq("chorus Kelburn regions", kelburn.get("regions") if kelburn else None, ["kelburn"])

    # --- Atlassian Ingest Test ---
    atl_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "atlassian-api.json").replace("\\", "/")
    env = os.environ.copy()
    env["OA_ROOT"] = str(scratch)
    env["OA_ATLASSIAN_URL"] = atl_url
    r_atl = subprocess.run(
        [sys.executable, "python_system/run.py", "ingest", "atlassian", "--apply"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env,
    )
    failures += not assert_true("atlassian ingest exit 0", r_atl.returncode == 0, r_atl.stderr[-200:])
    atl_history = json.loads((state_dir / "alert_history.json").read_text(encoding="utf-8"))
    atl_only = [r for r in atl_history if r["service"] == "atlassian"]
    failures += not assert_eq("atlassian only events", len(atl_only), 50)
    atl_sevs = {r["severity"] for r in atl_only}
    failures += not assert_true("atlassian severities valid", atl_sevs.issubset({"critical", "warning", "info", "resolved"}))
    atl_active = next((r for r in atl_only if r["raw_id"] == "qq07180qzmpp"), None)
    failures += not assert_true("atlassian active outage detected", atl_active is not None)
    failures += not assert_eq("atlassian active severity is critical", atl_active.get("severity") if atl_active else None, "critical")
    failures += not assert_true("atlassian active has jira subservice", "jira" in (atl_active.get("sub_services") or []) if atl_active else False)

    # --- 23. Phase 3b: `lint-events` (clean state) ---
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    # Ingest cloudflare so we have some real rows to lint.
    cf_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "cloudflare-api.json").replace("\\", "/")
    env = os.environ.copy()
    env["OA_ROOT"] = str(scratch)
    env["OA_CLOUDFLARE_URL"] = cf_url
    subprocess.run(
        [sys.executable, "python_system/run.py", "ingest", "cloudflare", "--apply"],
        cwd=str(REPO), capture_output=True, text=True, timeout=60, env=env,
    )
    r_lint = run_cli("lint-events", root=scratch)
    failures += not assert_true("lint-events exit 0 on clean state", r_lint.returncode == 0, r_lint.stderr[-200:])
    failures += not assert_true("lint-events stdout says OK",
                                "OK" in r_lint.stdout or "no issues" in r_lint.stdout,
                                r_lint.stdout[:300])

    # --- 24. Phase 3b: `lint-events` (known-bad state) ---
    # Inject a bad row into alert_history.json and verify linter catches it.
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    bad_row = {
        "raw_id": "bad-test-1",
        "service": "",            # missing
        "severity": "fatal",      # not canonical
        "title": "",              # empty
        "description": "x",
        "regions": [],            # empty, no global indicator
        "link": "",               # missing
        "published_at": "2099-01-01T00:00:00Z",  # future
        "last_seen_at": "2099-01-01T00:00:00Z",
        "expires_at": "2099-01-08T00:00:00Z",
    }
    (state_dir / "alert_history.json").write_text(json.dumps([bad_row]), encoding="utf-8")
    r_lint_bad = run_cli("lint-events", root=scratch)
    # Without --strict, exit code is 1 (issues found) but 0 in non-strict mode actually... let me check.
    # Per cmd_lint_events: returns exit_code_for which is 1 for issues. So strict=exit 1.
    failures += not assert_true("lint-events exit 1 on bad state", r_lint_bad.returncode == 1, f"got {r_lint_bad.returncode}")
    expected_kinds = {"bad_severity", "empty_title", "missing_link", "empty_regions",
                      "future_published_at", "missing_service"}
    actual_kinds = set()
    for kind in ("bad_severity", "empty_title", "empty_description", "missing_link",
                 "empty_regions", "future_published_at", "missing_service", "missing_severity"):
        if kind in r_lint_bad.stdout:
            actual_kinds.add(kind)
    failures += not assert_true("lint-events found all expected issue kinds",
                                expected_kinds.issubset(actual_kinds),
                                f"missing kinds: {expected_kinds - actual_kinds}")

    # --- 25. Phase 3b: `verify-fixtures` (all 6 fixtures pass) ---
    env = os.environ.copy()
    env["OA_FIXTURE_BASE_URL"] = f"http://127.0.0.1:{_fixture_server._FIXTURE_PORT}"
    r_vf = subprocess.run(
        [sys.executable, "python_system/run.py", "verify-fixtures"],
        cwd=str(REPO), capture_output=True, text=True, timeout=60, env=env,
    )
    failures += not assert_true("verify-fixtures exit 0 on valid fixtures",
                                r_vf.returncode == 0,
                                f"got {r_vf.returncode}: {r_vf.stderr[-200:]}")
    for prov in ("cloudflare", "connectwise", "m365", "azure", "aws", "zscaler", "chorus", "googleworkspace", "vector", "atlassian"):
        failures += not assert_true(f"verify-fixtures: {prov} present in stdout",
                                    prov in r_vf.stdout, r_vf.stdout[:300])

    # --- 22. Phase 3c: `serve` command (background-thread test) ---
    import time as _time
    import urllib.error as _urllib_error
    import urllib.request as _urllib_request
    scratch = setup_scratch()
    run_cli("init", root=scratch)
    state_dir = state_dir_for(scratch)
    write_customers(state_dir)
    # Ingest something so the dashboard has rows.
    cf_url = http_fixture_url(REPO / "parser-tests" / "inputs" / "cloudflare-api.json").replace("\\", "/")
    env = os.environ.copy()
    env["OA_ROOT"] = str(scratch)
    env["OA_CLOUDFLARE_URL"] = cf_url
    subprocess.run(
        [sys.executable, "python_system/run.py", "ingest", "cloudflare", "--apply"],
        cwd=str(REPO), capture_output=True, text=True, timeout=60, env=env,
    )
    # Use a free port to avoid clashes.
    server_port = 18765
    sys.path.insert(0, str(REPO / "python_system"))
    from outage_automation.dashboard.server import run_server
    from outage_automation.paths import resolve_paths
    srv = run_server(
        resolve_paths(str(scratch)),
        host="127.0.0.1", port=server_port, block=False,
    )
    _time.sleep(0.4)
    try:
        # /api/health
        r_health = _urllib_request.urlopen(
            f"http://127.0.0.1:{server_port}/api/health", timeout=5
        )
        failures += not assert_true("serve: /api/health 200", r_health.status == 200, str(r_health.status))
        # /api/dashboard
        r_dash = _urllib_request.urlopen(
            f"http://127.0.0.1:{server_port}/api/dashboard", timeout=5
        )
        failures += not assert_true("serve: /api/dashboard 200", r_dash.status == 200, str(r_dash.status))
        body = json.loads(r_dash.read().decode("utf-8"))
        failures += not assert_true("serve: /api/dashboard has rows",
                                    isinstance(body.get("rows"), list) and len(body["rows"]) > 0,
                                    f"got {len(body.get('rows', []))} rows")
        # /dashboard (HTML)
        r_html = _urllib_request.urlopen(
            f"http://127.0.0.1:{server_port}/dashboard", timeout=5
        )
        html = r_html.read().decode("utf-8")
        failures += not assert_true("serve: /dashboard returns HTML", "<!DOCTYPE" in html or "<html" in html.lower(), html[:80])
        # /nope (404)
        try:
            _urllib_request.urlopen(f"http://127.0.0.1:{server_port}/nope", timeout=5)
            failures += not assert_true("serve: /nope 404", False, "got 200")
        except _urllib_error.HTTPError as e:
            failures += not assert_true("serve: /nope 404", e.code == 404, str(e.code))
    finally:
        srv.shutdown()
        srv.server_close()

    # --- 23. Phase 4: dashboard HTTP Basic Auth ---
    from outage_automation.dashboard.server import (
        InsecureBindError,
        run_server as run_server_with_auth,
    )
    auth_port = 18766
    auth = ("alice", "s3cr3t")
    srv_auth = run_server_with_auth(
        resolve_paths(str(scratch)),
        host="127.0.0.1", port=auth_port, block=False, auth=auth,
    )
    _time.sleep(0.4)
    try:
        # /api/health stays open even with auth on (for liveness probes)
        r_health = _urllib_request.urlopen(
            f"http://127.0.0.1:{auth_port}/api/health", timeout=5
        )
        failures += not assert_true(
            "auth: /api/health 200 (no creds needed)", r_health.status == 200,
            str(r_health.status),
        )

        # /api/dashboard without creds -> 401 + WWW-Authenticate
        try:
            _urllib_request.urlopen(
                f"http://127.0.0.1:{auth_port}/api/dashboard", timeout=5
            )
            failures += not assert_true("auth: /api/dashboard 401 without creds", False, "got 200")
        except _urllib_error.HTTPError as e:
            failures += not assert_true(
                "auth: /api/dashboard 401 without creds", e.code == 401, str(e.code),
            )
            www_auth = e.headers.get("WWW-Authenticate", "")
            failures += not assert_true(
                "auth: 401 carries WWW-Authenticate: Basic",
                www_auth.lower().startswith("basic"), repr(www_auth),
            )

        # /api/dashboard with wrong creds -> 401
        req_bad = _urllib_request.Request(
            f"http://127.0.0.1:{auth_port}/api/dashboard",
            headers={"Authorization": "Basic " + _b64("alice:wrong")},
        )
        try:
            _urllib_request.urlopen(req_bad, timeout=5)
            failures += not assert_true("auth: /api/dashboard 401 with wrong pw", False, "got 200")
        except _urllib_error.HTTPError as e:
            failures += not assert_true(
                "auth: /api/dashboard 401 with wrong pw", e.code == 401, str(e.code),
            )

        # /api/dashboard with right creds -> 200
        req_ok = _urllib_request.Request(
            f"http://127.0.0.1:{auth_port}/api/dashboard",
            headers={"Authorization": "Basic " + _b64("alice:s3cr3t")},
        )
        r_ok = _urllib_request.urlopen(req_ok, timeout=5)
        failures += not assert_true(
            "auth: /api/dashboard 200 with right creds", r_ok.status == 200,
            str(r_ok.status),
        )

        # /dashboard (HTML) redirects to /login without credentials
        r_dash = _urllib_request.urlopen(
            f"http://127.0.0.1:{auth_port}/dashboard", timeout=5
        )
        failures += not assert_true(
            "auth: /dashboard redirects to /login without creds",
            "/login" in r_dash.geturl() and r_dash.status == 200,
            f"url={r_dash.geturl()} status={r_dash.status}",
        )

        # /static/ also requires auth (so the bundle isn't leaked)
        try:
            _urllib_request.urlopen(
                f"http://127.0.0.1:{auth_port}/static/app.css", timeout=5
            )
            failures += not assert_true("auth: /static/ 401 without creds", False, "got 200")
        except _urllib_error.HTTPError as e:
            failures += not assert_true(
                "auth: /static/ 401 without creds", e.code == 401, str(e.code),
            )
    finally:
        srv_auth.shutdown()
        srv_auth.server_close()

    # --- 23b. InsecureBindError: refuse to bind off loopback without auth ---
    try:
        run_server_with_auth(
            resolve_paths(str(scratch)),
            host="0.0.0.0", port=18767, block=False, auth=None,
        )
        failures += not assert_true(
            "auth: refuses 0.0.0.0 bind without auth", False, "no error raised",
        )
    except InsecureBindError as e:
        failures += not assert_true(
            "auth: refuses 0.0.0.0 bind without auth",
            "auth" in str(e).lower() and "0.0.0.0" in str(e), str(e)[:120],
        )

    # --- 23c. allow_insecure_bind override works ---
    try:
        srv_override = run_server_with_auth(
            resolve_paths(str(scratch)),
            host="0.0.0.0", port=18768, block=False, auth=None,
            allow_insecure_bind=True,
        )
        _time.sleep(0.3)
        try:
            r = _urllib_request.urlopen(
                "http://127.0.0.1:18768/api/health", timeout=5
            )
            failures += not assert_true(
                "auth: allow_insecure_bind reaches /api/health",
                r.status == 200, str(r.status),
            )
        finally:
            srv_override.shutdown()
            srv_override.server_close()
    except Exception as e:
        failures += not assert_true(
            "auth: allow_insecure_bind override works", False, repr(e),
        )

    # --- 23d. _resolve_dashboard_auth resolution order ---
    from outage_automation.cli import _resolve_dashboard_auth as _resolve
    # Both unset -> None
    saved = {k: os.environ.pop(k, None) for k in (
        "OA_DASHBOARD_USER", "OA_DASHBOARD_PASSWORD", "OA_DASHBOARD_PASSWORD_FILE",
    )}
    try:
        failures += not assert_true(
            "auth: resolve returns None when nothing is set",
            _resolve(None, None, None) is None, "",
        )
        # Only user -> SystemExit
        try:
            _resolve("alice", None, None)
            failures += not assert_true("auth: resolve exits when only user set", False, "no exit")
        except SystemExit:
            failures += not assert_true("auth: resolve exits when only user set", True, "")
        # Only password -> SystemExit
        try:
            _resolve(None, "pw", None)
            failures += not assert_true("auth: resolve exits when only pw set", False, "no exit")
        except SystemExit:
            failures += not assert_true("auth: resolve exits when only pw set", True, "")
        # Both set -> tuple
        failures += not assert_eq(
            "auth: resolve returns (user, pw) tuple",
            _resolve("alice", "pw", None), ("alice", "pw"),
        )
        # Env var fallback
        os.environ["OA_DASHBOARD_USER"] = "bob"
        os.environ["OA_DASHBOARD_PASSWORD"] = "envpw"
        failures += not assert_eq(
            "auth: resolve reads OA_DASHBOARD_USER/PASSWORD",
            _resolve(None, None, None), ("bob", "envpw"),
        )
        # CLI overrides env
        failures += not assert_eq(
            "auth: CLI --user/--password override env",
            _resolve("cli-user", "cli-pw", None), ("cli-user", "cli-pw"),
        )
        # --password-file overrides --password and env
        pf = SCRATCH / "pw.txt"
        pf.write_text("filepw\n", encoding="utf-8")
        failures += not assert_eq(
            "auth: --password-file wins over --password",
            _resolve("alice", "ignored", str(pf)), ("alice", "filepw"),
        )
        # Env OA_DASHBOARD_PASSWORD_FILE
        os.environ["OA_DASHBOARD_PASSWORD_FILE"] = str(pf)
        failures += not assert_eq(
            "auth: OA_DASHBOARD_PASSWORD_FILE reads file",
            _resolve("alice", "ignored", None), ("alice", "filepw"),
        )
        # Missing --password-file -> SystemExit
        try:
            _resolve("alice", None, str(SCRATCH / "nope.txt"))
            failures += not assert_true("auth: missing --password-file exits", False, "no exit")
        except SystemExit:
            failures += not assert_true("auth: missing --password-file exits", True, "")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    print()
    if failures:
        print(f"{failures} test(s) failed")
        return 1
    print("All tests passed.")
    return 0


def main_with_dashboard_writes() -> int:
    """Combined runner: ingest/lint/serve suite + dashboard write endpoints.

    We import the dashboard writes module after the ingest suite so a
    failure in either is reported with the right context, and so the
    test counter is shared (caller cares about total failures, not
    per-module).
    """
    import test_dashboard_writes as dw
    failures = main()
    print()
    print("=== Dashboard write endpoints ===")
    dw_failures = dw.main()
    if failures or dw_failures:
        return 1
    return 0


def _b64(s: str) -> str:
    """Base64-encode a string (no bytes/string mixing in callers)."""
    import base64
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


if __name__ == "__main__":
    raise SystemExit(main_with_dashboard_writes())
