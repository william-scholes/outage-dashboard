# Contributing to Outage Automation

Thank you for your interest in contributing to **Outage Automation**! We welcome community contributions, bug reports, documentation improvements, and new provider feeds.

---

## 1. Core Design Philosophy

Before contributing code, please keep these core tenets in mind:

1. **Zero External Runtime Dependencies:**
   * The core system package (`python_system/outage_automation/`) is built **strictly using the Python standard library**.
   * Do not introduce third-party runtime dependencies (such as `requests`, `fastapi`, `pydantic`, `flask`, `sqlalchemy`, etc.).
   * Developer and testing tools (like `pytest`, `playwright`, `ruff`) belong exclusively in testing environments and are never required to run the production system.

2. **File-Backed Atomic Architecture:**
   * State is stored on disk (`events.ndjson`, `alert_history.json`, `customers.json`) with strict file locking (`state/.lock`) and atomic temporary-file renames.
   * Modifying state must always preserve this atomic write pattern.

3. **Deterministic Testing:**
   * All provider adapters have sample mock response fixtures under `parser-tests/inputs/`.
   * Unit tests must never make real outbound network calls.

---

## 2. How to Contribute

### Reporting Issues
* Search existing GitHub issues to verify that the bug or feature hasn't already been reported.
* Provide clear reproduction steps, Python version, operating system, and any relevant CLI output or log entries.
* Ensure you **do not include sensitive or proprietary information** (passwords, tokens, or private customer records) in issue descriptions.

### Adding New Cloud / SaaS Providers
Adding support for a new service is modular and straightforward:
1. **Choose an Ingest Base:**
   * [`ingest/statuspage_v2.py`](python_system/outage_automation/ingest/statuspage_v2.py) for Atlassian Statuspage feeds.
   * [`ingest/status_io.py`](python_system/outage_automation/ingest/status_io.py) for Status.io feeds.
   * [`ingest/rss.py`](python_system/outage_automation/ingest/rss.py) for RSS/Atom XML feeds.
   * Subclass [`ingest/base.py`](python_system/outage_automation/ingest/base.py) (`ProviderAdapter`) for custom JSON or REST APIs.
2. **Implement Adapter:** Place in `python_system/outage_automation/ingest/<provider>.py`.
3. **Register Adapter:** Add to `DEFAULT_SERVICES` in [`config.py`](python_system/outage_automation/config.py) and map in [`registry.py`](python_system/outage_automation/ingest/registry.py).
4. **Define Regions:** Add the provider's region codes to [`approved-regions.csv`](approved-regions.csv).
5. **Add Fixtures & Tests:**
   * Save a sample payload in `parser-tests/inputs/<provider>-api.json` (or `.xml`).
   * Add test cases in `python_system/tests/test_<provider>.py`.
   * Verify using `python3 python_system/run.py verify-fixtures`.

---

## 3. Local Development & Testing

### Prerequisites
* Python 3.10 or higher.
* Linux or macOS (or WSL2 on Windows).

### Setting Up Development Environment
```bash
# Clone the repository
git clone https://github.com/william-scholes/outage-dashboard.git
cd outage-dashboard

# Make CLI tools executable
chmod +x python_system/bin/oa python_system/bin/oa-*.sh

# Initialize state directory
python3 python_system/run.py init
```

### Running Tests
To run the full unit test suite without installing external test packages:
```bash
PYTHONPATH=python_system python3 -m unittest discover -s python_system/tests
```

If you use a virtual environment with `pytest`:
```bash
pytest python_system/tests
```

To run end-to-end retention and integration tests:
```bash
python3 python_system/examples/test_e2e.py
```

To verify provider parser fixtures:
```bash
python3 python_system/run.py verify-fixtures --strict
```

To run the linter and format checker:
```bash
./python_system/bin/oa-lint.sh
```

---

## 4. Pull Request Guidelines

1. **Branch Naming:** Create a focused feature branch (e.g. `feat/new-provider-datadog` or `fix/chorus-sub-service`).
2. **Atomic Commits:** Keep commits focused with clear, descriptive messages (e.g. `feat(ingest): add datadog status adapter`).
3. **Test Coverage:** Ensure every bug fix or new adapter includes corresponding unit tests.
4. **No Secrets / Private State:**
   * Never commit live credentials, API keys, or private customer data.
   * State files in `python_system/state/` are gitignored and must remain local.
5. **Run Verification:** Before opening a PR, ensure all tests pass cleanly:
   ```bash
   PYTHONPATH=python_system python3 -m unittest discover -s python_system/tests
   python3 python_system/run.py verify-fixtures --strict
   ```

---

## 5. Code of Conduct

We are committed to providing a welcoming, inclusive, and harassment-free experience for everyone. Please be respectful, constructive, and considerate when interacting on issues, discussions, and pull requests.
