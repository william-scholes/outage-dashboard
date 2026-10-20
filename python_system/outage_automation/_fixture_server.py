"""Local HTTP fixture server for verify-fixtures and e2e tests.

Single source of truth for the ISS-011 fixture server infrastructure.
Serves files from the repo root so the ISS-003 scheme allowlist can
fetch them over HTTP instead of file:// URLs.

Used by:
- python_system/examples/test_e2e.py  (imports start_fixture_server, _FIXTURE_PORT)
- python_system/outage_automation/cli.py  (cmd_verify_fixtures starts the server)
"""
from __future__ import annotations

import socket
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# Repo root: <repo>/python_system/outage_automation/_fixture_server.py
# Use the same computation as verify_fixtures.py for consistency.
_here = Path(__file__).resolve()
REPO = _here.parents[2] if len(_here.parents) >= 3 else _here.parent

_FIXTURE_PORT: int | None = None
_FIXTURE_THREAD: threading.Thread | None = None
_FIXTURE_HTTPd: HTTPServer | None = None


class _FixtureHandler(SimpleHTTPRequestHandler):
    """Serves files from REPO. Used to expose parser-tests/inputs over HTTP
    so the ISS-003 scheme allowlist can fetch them."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO), **kwargs)

    def log_message(self, format, *args):
        pass  # silence

    def do_GET(self):
        path = self.path.lstrip("/")
        # Prevent directory traversal.
        try:
            full = (REPO / path).resolve()
            full.relative_to(REPO.resolve())
        except (ValueError, OSError):
            self.send_error(403, "Forbidden")
            return
        if not full.is_file():
            self.send_error(404, "Not found")
            return
        self.send_response(200)
        if path.endswith(".json"):
            self.send_header("Content-Type", "application/json")
        elif path.endswith(".xml"):
            self.send_header("Content-Type", "application/xml")
        else:
            self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(full.stat().st_size))
        self.end_headers()
        try:
            self.wfile.write(full.read_bytes())
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        # Support POST requests for RPC-based fixture testing (e.g. googleai)
        return self.do_GET()


def start_fixture_server() -> int:
    """Start the fixture HTTP server on an OS-chosen port. Returns the port.
    Safe to call multiple times — subsequent calls return the existing port."""
    global _FIXTURE_PORT, _FIXTURE_THREAD, _FIXTURE_HTTPd
    if _FIXTURE_HTTPd is not None:
        return _FIXTURE_PORT
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    _FIXTURE_HTTPd = HTTPServer(("127.0.0.1", port), _FixtureHandler)
    _FIXTURE_PORT = port
    _FIXTURE_THREAD = threading.Thread(target=_FIXTURE_HTTPd.serve_forever, daemon=True)
    _FIXTURE_THREAD.start()
    return port
