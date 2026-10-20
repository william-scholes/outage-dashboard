"""Shared ingest plumbing: HTTP client, base adapter, and result types.

This module contains no third-party dependencies: it uses the Python
standard library so the system remains self-contained and runs on a
fresh Linux container with only Python installed.
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..model import OutageEvent

USER_AGENT = "outage-automation/1.0 (+python)"
DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 2
RETRY_BACKOFF = 0.75
RAW_PAYLOAD_MAX_BYTES = 1_500_000  # ~1.5 MB cap to keep debug payloads sane


class IngestError(RuntimeError):
    """Raised when a provider ingest fails after retries."""


@dataclass
class IngestResult:
    """Outcome of a single provider ingest run."""

    provider: str
    events: List[OutageEvent] = field(default_factory=list)
    raw_bytes: int = 0
    raw_payload: Optional[Dict[str, Any]] = None
    fetched_at: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "ok": self.ok,
            "event_count": len(self.events),
            "raw_bytes": self.raw_bytes,
            "fetched_at": self.fetched_at,
            "error": self.error,
        }


ALLOWED_SCHEMES = frozenset({"http", "https"})


def fetch_url(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    user_agent: str = USER_AGENT,
    headers: Optional[Dict[str, str]] = None,
) -> bytes:
    """GET `url` with a sane timeout, retry, and User-Agent.


    Raises `IngestError` if the request ultimately fails. The error message
    is short and actionable. Raises `ValueError` if the URL scheme is not
    http or https.
    """
    # Scheme allowlist — reject file://, ftp://, etc. before any network I/O
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ValueError(
            f"URL scheme '{parsed.scheme}' is not allowed; "
            f"only 'http' and 'https' are permitted. URL: {url}"
        )
    hdrs = {"User-Agent": user_agent, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    attempt = 0
    last_err: Optional[str] = None
    while attempt <= retries:
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} for {url}: {e.reason}"
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, socket.timeout):
                last_err = f"timeout for {url} (>{timeout}s)"
            else:
                last_err = f"URL error for {url}: {reason}"
        except (TimeoutError, socket.timeout):
            last_err = f"timeout for {url} (>{timeout}s)"
        except Exception as e:  # pragma: no cover - defensive
            last_err = f"unexpected error for {url}: {e!r}"
        attempt += 1
        if attempt <= retries:
            time.sleep(RETRY_BACKOFF * attempt)
    raise IngestError(last_err or f"failed to fetch {url}")


def parse_json_bytes(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        raise IngestError(f"invalid JSON in provider response: {e}")


def maybe_capture_raw(
    provider: str,
    payload: Any,
    raw_bytes: int,
    state_dir,
) -> Optional[Dict[str, Any]]:
    """Save a size-capped copy of the provider payload for troubleshooting.

    `state_dir/raw/<provider>/<timestamp>.json` is written when the payload
    fits within `RAW_PAYLOAD_MAX_BYTES` and the caller has indicated
    capture is desired (typically by setting `OA_RAW_CAPTURE=1`).
    """
    if not os.environ.get("OA_RAW_CAPTURE"):
        return None
    if raw_bytes > RAW_PAYLOAD_MAX_BYTES:
        return {"captured": False, "reason": "payload too large", "raw_bytes": raw_bytes}
    from datetime import datetime, timezone
    from pathlib import Path

    base = Path(state_dir) / "raw" / provider
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = base / f"{ts}.json"
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return {"captured": False, "reason": repr(e)}
    return {"captured": True, "path": str(path), "raw_bytes": raw_bytes}


class ProviderAdapter:
    """Base class for provider adapters.

    Subclasses should set `name` and implement `fetch(self) -> IngestResult`.
    The `to_events(...)` step should turn the raw payload into normalized
    `OutageEvent` objects.
    """

    name: str = ""

    def fetch(self, state_dir=None) -> IngestResult:  # pragma: no cover - interface
        raise NotImplementedError

    @staticmethod
    def normalize_region(regions_raw: List[str], service: str) -> List[str]:
        """Default region normalizer: lowercase + dedupe, preserve 'global'."""
        out: List[str] = []
        seen = set()
        for r in regions_raw or []:
            if not r:
                continue
            v = str(r).strip().lower()
            if not v or v in seen:
                continue
            seen.add(v)
            out.append(v)
        return out
