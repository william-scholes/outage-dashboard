"""Re-parse every fixture in `parser-tests/inputs/` through the registered
adapters. Reports per-fixture event counts and surfaces any provider
that throws on its own fixture (i.e. a schema change has likely happened).

This is a "did the upstream API change shape under us?" detector. Run it
whenever a provider's status page is suspected of having updated, or
when bumping the dependency that emits the fixture.
"""
from __future__ import annotations

import os
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ._fixture_server import start_fixture_server


# Convention: filenames in parser-tests/inputs/ encode the provider.
# Cloudflare + ConnectWise + M365 use JSON, Azure + AWS + Zscaler use RSS XML.
# We map filename -> registered provider name.
_FILENAME_TO_PROVIDER = {
    "cloudflare-api.json": "cloudflare",
    "connectwise-api.json": "connectwise",
    "m365-api.json": "m365",
    "azure-rss.xml": "azure",
    "aws-rss.xml": "aws",
    "zscaler-rss.xml": "zscaler",
    "chorus-api.json": "chorus",
    "googleworkspace-api.json": "googleworkspace",
    "vector-api.json": "vector",
    "atlassian-api.json": "atlassian",
    "wiz-rss.xml": "wiz",
    "xero-api.json": "xero",
    "halopsa-api.json": "halopsa",
    "ninjaone-api.json": "ninjaone",
    "duo-api.json": "duo",
    "kaseya-api.json": "kaseya",
    "enable-api.json": "enable",
    "mimecast-api.json": "mimecast",
    "lightwire-api.json": "lightwire",
    "myob-api.json": "myob",
    "rewst-api.json": "rewst",
    "wasabi-api.json": "wasabi",
    "tenable-api.json": "tenable",
    "eset-api.json": "eset",
    "googleai-api.json": "googleai",
}


@dataclass
class FixtureResult:
    path: Path
    provider: str
    event_count: int = 0
    first_service: Optional[str] = None
    error: Optional[str] = None


@dataclass
class VerifyReport:
    results: List[FixtureResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.error is None and r.event_count > 0 for r in self.results)

    def render(self) -> str:
        lines: List[str] = []
        if not self.results:
            lines.append("No fixtures found.")
            return "\n".join(lines)
        lines.append(f"Verified {len(self.results)} fixture(s):")
        lines.append("")
        for r in sorted(self.results, key=lambda r: r.path.name):
            status = "OK" if (r.error is None and r.event_count > 0) else "FAIL"
            detail = (
                f"{r.event_count} event(s), first service={r.first_service!r}"
                if r.error is None
                else f"error: {r.error}"
            )
            lines.append(f"  [{status}] {r.path.name}  ({r.provider})  {detail}")
        return "\n".join(lines)


def fixtures_dir() -> str:
    """Default fixtures directory: `<repo>/parser-tests/inputs/`.

    Falls back to `OA_FIXTURES_DIR` env var if set, then to a relative
    walk from the package install location.
    """
    override = os.environ.get("OA_FIXTURES_DIR")
    if override:
        return override
    here = Path(__file__).resolve()
    # <repo>/python_system/outage_automation/verify_fixtures.py
    # -> <repo>/parser-tests/inputs
    repo_root = here.parents[2] if len(here.parents) >= 3 else here.parent
    return str(repo_root / "parser-tests" / "inputs")


def _read_fixture(path: Path) -> bytes:
    return path.read_bytes()


def _build_file_url(path: Path) -> str:
    """Build a URL the way `urllib.request.urlopen` expects on
    this platform.

    If `OA_FIXTURE_BASE_URL` is set (e.g. an HTTP fixture server in the e2e),
    return `<base>/<repo-relative-path>`. Otherwise fall back to a `file://`
    URL (works on POSIX; on Windows needs the leading slash + forward slashes).
    """
    base = os.environ.get("OA_FIXTURE_BASE_URL", "").rstrip("/")
    if base:
        # Express the fixture path relative to the repo root so the fixture
        # server can map it back to disk.
        try:
            repo_root = Path(__file__).resolve().parents[2]
            rel = path.resolve().relative_to(repo_root)
            return f"{base}/{str(rel).replace(os.sep, '/')}"
        except ValueError:
            raise ValueError(
                f"fixture path is outside the repo ({repo_root}): {path}"
            )
    p = str(path.resolve())
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + p.replace("\\", "/")


def verify_fixtures(fixtures_root: str | Path) -> VerifyReport:
    fixtures_root = Path(fixtures_root)
    if not fixtures_root.is_dir():
        return VerifyReport(
            results=[FixtureResult(
                path=fixtures_root, provider="?",
                error=f"not a directory: {fixtures_root}",
            )]
        )

    # Late import so the import surface stays small for `oa` invocations
    # that don't need providers.
    from .ingest.registry import get_adapter

    report = VerifyReport()
    for fixture_path in sorted(fixtures_root.iterdir()):
        if not fixture_path.is_file():
            continue
        # Skip non-fixture files (README, hidden files, .DS_Store, etc.).
        if fixture_path.name.startswith(".") or not (
            fixture_path.suffix == ".json" or fixture_path.suffix == ".xml"
        ):
            continue
        provider_name = _FILENAME_TO_PROVIDER.get(fixture_path.name)
        if not provider_name:
            # Unknown file — record it as an unknown fixture but don't fail.
            report.results.append(FixtureResult(
                path=fixture_path, provider="?",
                error="unknown fixture (not mapped to a provider)",
            ))
            continue
        result = FixtureResult(path=fixture_path, provider=provider_name)
        try:
            # Construct the adapter with the file:// URL of the fixture.
            url = _build_file_url(fixture_path)
            adapter = get_adapter(provider_name, url=url)
        except Exception as e:
            result.error = f"adapter not found: {e!r}"
            report.results.append(result)
            continue
        try:
            ir = adapter.fetch()
        except Exception as e:
            result.error = f"fetch raised: {e!r}"
            report.results.append(result)
            continue
        if ir.error:
            result.error = f"adapter error: {ir.error}"
            report.results.append(result)
            continue
        result.event_count = len(ir.events)
        if ir.events:
            result.first_service = ir.events[0].service
        report.results.append(result)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    # Start the local fixture server so file:// URLs are replaced by HTTP.
    port = start_fixture_server()
    os.environ["OA_FIXTURE_BASE_URL"] = f"http://127.0.0.1:{port}"
    report = verify_fixtures(fixtures_dir())
    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
