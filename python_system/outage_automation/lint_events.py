"""Data-quality checks against the local state files.

The linter reads `state/alert_history.json` and `state/events.ndjson`
and flags rows that look wrong: missing fields, future timestamps,
non-canonical severities, empty regions without a global fallback,
duplicate `raw_id`s.

Returns a `LintReport` with per-issue counts. Exit code 0 = clean,
1 = issues found, 2 = could not read state.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .model import _SEVERITIES
from .validate import parse_datetime


_CANONICAL_SEVERITIES = _SEVERITIES  # {"critical", "warning", "info", "resolved"}


@dataclass
class LintIssue:
    kind: str           # e.g. "missing_link"
    raw_id: str         # empty if the issue is at the file level
    detail: str

    def render(self) -> str:
        if self.raw_id:
            return f"[{self.kind}] {self.raw_id}: {self.detail}"
        return f"[{self.kind}] {self.detail}"


@dataclass
class LintReport:
    issues: List[LintIssue] = field(default_factory=list)
    rows_scanned: int = 0
    files_scanned: List[str] = field(default_factory=list)

    def add(self, kind: str, raw_id: str = "", detail: str = "") -> None:
        self.issues.append(LintIssue(kind=kind, raw_id=raw_id, detail=detail))

    @property
    def ok(self) -> bool:
        return len(self.issues) == 0

    def by_kind(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for issue in self.issues:
            out[issue.kind] = out.get(issue.kind, 0) + 1
        return out

    def render(self) -> str:
        lines: List[str] = []
        files_str = ", ".join(self.files_scanned) if self.files_scanned else "no files"
        lines.append(
            f"Scanned {self.rows_scanned} row(s) across "
            f"{len(self.files_scanned)} file(s): {files_str}"
        )
        if not self.issues:
            lines.append("OK: no issues found.")
            return "\n".join(lines)
        lines.append(f"Found {len(self.issues)} issue(s):")
        by = self.by_kind()
        for kind, n in sorted(by.items()):
            lines.append(f"  {kind}: {n}")
        lines.append("")
        # Cap rendered rows to keep the output readable.
        sample = self.issues[:50]
        for issue in sample:
            lines.append("  " + issue.render())
        if len(self.issues) > len(sample):
            lines.append(f"  ... and {len(self.issues) - len(sample)} more")
        return "\n".join(lines)


def _check_row(row: Dict[str, Any], report: LintReport, now: datetime) -> None:
    raw_id = str(row.get("raw_id") or "").strip()
    service = str(row.get("service") or "").strip().lower()

    # Severity canonical check.
    severity = str(row.get("severity") or "").strip().lower()
    if severity and severity not in _CANONICAL_SEVERITIES:
        report.add("bad_severity", raw_id, f"severity={severity!r}")
    elif not severity:
        report.add("missing_severity", raw_id, "severity is empty")

    # Title / description.
    if not str(row.get("title") or "").strip():
        report.add("empty_title", raw_id, "title is empty")
    if not str(row.get("description") or "").strip():
        report.add("empty_description", raw_id, "description is empty")

    # Link.
    if not str(row.get("link") or "").strip():
        report.add("missing_link", raw_id, "link is empty")

    # Regions — empty AND no 'global' / 'worldwide' indicator in title.
    regions = row.get("regions")
    if not isinstance(regions, list) or not regions:
        # Deliberate empty lists are allowed for all valid services due to noise filtering.
        if service in ("azure", "aws", "cloudflare", "connectwise", "m365", "zscaler", "chorus") and isinstance(regions, list) and len(regions) == 0:
            pass
        else:
            title_lower = str(row.get("title") or "").lower()
            if not any(k in title_lower for k in ("global", "worldwide", "all regions")):
                report.add("empty_regions", raw_id, "no regions and no global indicator")

    # published_at in the future.
    pub_raw = row.get("published_at") or row.get("published")
    pub_dt = parse_datetime(pub_raw)
    if pub_dt is None:
        report.add("bad_published_at", raw_id, f"published_at={pub_raw!r}")
    else:
        # Anything more than 5 minutes in the future is suspicious.
        if pub_dt > now + timedelta(minutes=5):
            report.add("future_published_at", raw_id,
                       f"published_at={pub_dt.isoformat()} is in the future")

    # Service tag (informational, just check it looks like a service name).
    if not service:
        report.add("missing_service", raw_id, "service is empty")


def _scan_alert_history(path: Path, report: LintReport, now: datetime) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        report.add("unreadable_alert_history", "", f"{path}: {e!r}")
        return
    if not isinstance(data, list):
        report.add("bad_alert_history_shape", "", f"expected list, got {type(data).__name__}")
        return
    report.files_scanned.append(str(path))
    raw_ids: Dict[str, int] = {}
    for row in data:
        if not isinstance(row, dict):
            report.add("non_dict_row", "", f"row is {type(row).__name__}")
            continue
        _check_row(row, report, now)
        rid = str(row.get("raw_id") or "").strip()
        if rid:
            raw_ids[rid] = raw_ids.get(rid, 0) + 1
        report.rows_scanned += 1
    # Duplicate raw_id check.
    for rid, n in raw_ids.items():
        if n > 1:
            report.add("duplicate_raw_id", rid, f"appears {n} times in alert_history")


def _scan_events_ndjson(path: Path, report: LintReport, now: datetime, *, max_lines: Optional[int] = None) -> None:
    if not path.exists():
        return
    report.files_scanned.append(str(path))
    if max_lines is None:
        try:
            raw_limit = os.environ.get("OA_LINT_MAX_NDJSON_LINES", "50000")
            max_lines = int(raw_limit) if raw_limit else None
        except ValueError:
            max_lines = 50000
    try:
        size = path.stat().st_size
        lines: Iterable[str]
        if max_lines and max_lines > 0 and size > 2 * 1024 * 1024:
            read_bytes = min(size, max_lines * 1200)
            with path.open("rb") as bf:
                bf.seek(max(0, size - read_bytes))
                chunk = bf.read().decode("utf-8", errors="replace")
                all_lines = chunk.splitlines()
                if size > read_bytes and len(all_lines) > 1:
                    all_lines = all_lines[1:]
                lines = all_lines[-max_lines:]
        else:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
                lines = all_lines[-max_lines:] if (max_lines and max_lines > 0) else all_lines

        for lineno, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                envelope = json.loads(line)
            except Exception as e:
                report.add("bad_events_ndjson_line", "",
                           f"line {lineno}: {e!r}")
                continue
            if not isinstance(envelope, dict):
                report.add("non_dict_row", "",
                           f"events.ndjson line {lineno}: not a dict")
                continue
            # events.ndjson rows are wrapped: {"ts": ..., "event": {...}}.
            # The "event" key is the actual OutageEvent; everything else is metadata.
            row = envelope.get("event") if isinstance(envelope.get("event"), dict) else envelope
            _check_row(row, report, now)
            report.rows_scanned += 1
    except Exception as e:
        report.add("unreadable_events_ndjson", "", f"{path}: {e!r}")
    # NOTE: we deliberately do NOT check for duplicate_raw_id in
    # events.ndjson — it's an append-only log, so the same event
    # legitimately appears multiple times when re-ingested.
    # Duplicate detection only makes sense for the materialized
    # alert_history.json.


def lint_state(state_dir: Path, *, now: Optional[datetime] = None) -> LintReport:
    """Lint the alert_history.json and events.ndjson under `state_dir`."""
    if now is None:
        now = datetime.now(timezone.utc)
    report = LintReport()
    if not state_dir.is_dir():
        report.add("missing_state_dir", "", f"no such directory: {state_dir}")
        return report
    _scan_alert_history(state_dir / "alert_history.json", report, now)
    _scan_events_ndjson(state_dir / "events.ndjson", report, now)
    return report


def exit_code_for(report: LintReport) -> int:
    """0 = clean, 1 = issues found, 2 = unreadable state."""
    if any(issue.kind.startswith("missing_state_dir") or
           issue.kind.startswith("unreadable") or
           issue.kind.startswith("bad_") and
           ("_shape" in issue.kind or "_line" in issue.kind)
           for issue in report.issues):
        return 2
    return 0 if report.ok else 1
