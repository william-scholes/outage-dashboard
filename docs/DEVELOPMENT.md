# Local Development Guide

This guide covers setting up a local development environment, running tests, validating parser fixtures, and contributing new provider adapters to **Outage Automation**.

---

## 1. Prerequisites & Philosophy

- **Python 3.10+** (uses `match` syntax and `from __future__ import annotations`).
- **Standard Library Only:** The core system runtime has **zero external package dependencies**. Do not introduce external dependencies (`pip`, `requests`, `fastapi`, `pydantic`, etc.) into the `python_system/outage_automation/` package.
- **Platform:** Works on any standard POSIX environment (Linux, macOS) and local development shells.

---

## 2. Quick Setup

Clone the repository and initialize the local state directory:

```bash
git clone https://github.com/william-scholes/outage-dashboard.git
cd outage-dashboard

# Make CLI wrapper scripts executable
chmod +x python_system/bin/oa python_system/bin/oa-*.sh

# Initialize state directory (creates seed state/ files)
python3 python_system/run.py init

# Lint customer mappings against approved-regions.csv
python3 python_system/run.py lint-customers
```

---

## 3. Running the Test Suite

### Unit Tests
The test suite covers provider ingest adapters, normalization, customer matching, rate limiting, session authentication, and HTTP server handlers:

```bash
PYTHONPATH=python_system python3 -m unittest discover -s python_system/tests
```

### End-to-End Tests
Tests end-to-end CLI commands, state transitions, report generation, and data retention:

```bash
python3 python_system/examples/test_e2e.py
```

### Provider Fixture Verification
Validates that parser logic matches recorded real-world API fixture payloads under `parser-tests/inputs/`:

```bash
python3 python_system/run.py verify-fixtures --strict
```

### Headless Browser Tests (Optional / Playwright)
Browser-level automated tests for login flow, session cookies, filter toggling, and UI interaction:

```bash
# In a virtual environment equipped with Playwright
python3 -m unittest python_system/tests/test_browser_playwright.py
```

---

## 4. Local Interactive Workflow

```bash
# 1. Fetch live outage feeds from all registered providers
python3 python_system/run.py run-all

# 2. Dry-run ingest without saving state
python3 python_system/run.py run-all --skip-apply

# 3. Ingest from a single provider
python3 python_system/run.py ingest cloudflare --apply

# 4. Start local development dashboard (unauthenticated on 127.0.0.1)
python3 python_system/run.py serve
# Open http://127.0.0.1:8765 in your browser

# 5. Inspect current state and metrics
python3 python_system/run.py status
python3 python_system/run.py dashboard-data
```

You can also use the convenient bash aliases in `python_system/bin/`:
```bash
./python_system/bin/oa status
./python_system/bin/oa run-all
./python_system/bin/oa serve
```

---

## 5. Adding a New Cloud / SaaS Provider

Adding support for a new service is modular:

1. **Select or Create an Ingest Base:**
   - [`ingest/statuspage_v2.py`](../python_system/outage_automation/ingest/statuspage_v2.py) for Atlassian Statuspage APIs (e.g. Cloudflare, Atlassian).
   - [`ingest/status_io.py`](../python_system/outage_automation/ingest/status_io.py) for Status.io APIs (e.g. ConnectWise).
   - [`ingest/rss.py`](../python_system/outage_automation/ingest/rss.py) for RSS 2.0 / Atom XML feeds (e.g. AWS, Azure, M365, Zscaler, Wiz).
   - Custom parser class extending [`ingest/base.py`](../python_system/outage_automation/ingest/base.py) if the provider uses a custom REST API (e.g. Chorus, Vector, Google Workspace).

2. **Implement the Adapter:**
   - Create `python_system/outage_automation/ingest/<provider_name>.py`.
   - Implement `fetch()` returning a list of `OutageEvent` dataclasses.

3. **Register the Provider:**
   - Add the adapter to [`python_system/outage_automation/ingest/registry.py`](../python_system/outage_automation/ingest/registry.py).

4. **Add Region Mappings:**
   - Append standard region identifiers for the service to [`approved-regions.csv`](../approved-regions.csv).

5. **Add Fixture & Tests:**
   - Save a sample API response under `parser-tests/inputs/<provider>-api.json` (or `.xml`).
   - Add unit tests under `python_system/tests/test_ingest_<provider>.py`.
   - Run `python3 python_system/run.py verify-fixtures` to confirm.

## 6. Cutting a Public Release

To publish a sanitized, release-tagged snapshot to the public [`outage-dashboard`](https://github.com/william-scholes/outage-dashboard) repository:

```bash
./scripts/release-public.sh v1.0.0 "Initial public release"
```

The script runs automated pre-flight checks:
- Verifies that all unit and fixture tests pass.
- Audits files for accidental leaks of private internal IP addresses or private domains.
- Strips internal development archives.
- Tags and publishes a clean, atomic release commit to `main` on `william-scholes/outage-dashboard`.
- Creates a GitHub release and release notes.

