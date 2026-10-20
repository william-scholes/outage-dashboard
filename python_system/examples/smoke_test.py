"""Smoke test the dashboard server.

Starts the server in a background thread, hits each endpoint, asserts
the response is sensible, and shuts down cleanly.
"""
import sys
import time
import urllib.error
import urllib.request

# Allow running this file directly.
sys.path.insert(0, ".")
from outage_automation.paths import resolve_paths
from outage_automation.dashboard.server import run_server


def main() -> int:
    paths = resolve_paths(None)
    srv = run_server(paths, host="127.0.0.1", port=8765, block=False)
    time.sleep(0.4)

    failures = 0
    for path in [
        "/api/health",
        "/api/dashboard",
        "/api/customers",
        "/dashboard",
        "/static/app.js",
        "/static/app.css",
        "/static/missing.txt",
        "/nope",
    ]:
        try:
            r = urllib.request.urlopen(
                f"http://127.0.0.1:8765{path}", timeout=5
            )
            body = r.read()
            print(
                f"{path:25}  {r.status}  {len(body):>7} bytes  "
                f"ct={r.headers.get('Content-Type')}"
            )
        except urllib.error.HTTPError as e:
            print(f"{path:25}  HTTP {e.code}")
            if path not in ("/static/missing.txt", "/nope"):
                failures += 1
        except Exception as e:
            print(f"{path:25}  ERROR  {e!r}")
            failures += 1

    # Confirm /api/dashboard parses as JSON with expected keys
    r = urllib.request.urlopen("http://127.0.0.1:8765/api/dashboard", timeout=5)
    import json
    payload = json.loads(r.read().decode("utf-8"))
    expected = {"rows", "impact", "ttl_days", "generated_at", "row_counts"}
    missing = expected - set(payload.keys())
    if missing:
        print(f"FAIL: /api/dashboard missing keys: {missing}")
        failures += 1
    else:
        print(
            f"OK  /api/dashboard has {len(payload['rows'])} rows, "
            f"{len(payload['impact'].get('by_customer', {}))} customers impacted"
        )

    srv.shutdown()
    srv.server_close()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
