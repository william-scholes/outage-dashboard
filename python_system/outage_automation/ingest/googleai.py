"""Google AI & Antigravity ingest adapter.

Source: https://aistudio.google.com/status
Powered by MakerSuite RPC API:
  POST https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/ListIncidentsHistory

Provides unified coverage for Google AI and Antigravity:
  - Antigravity (Desktop IDE, CLI agent endpoints, model serving)
  - Gemini API (Developer API, Batch, Context Caching, Keys)
  - AI Studio (Web UI, Build applets, Datasets, Billing)
  - Multimodal Live API (Realtime streaming)
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import (
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    RETRY_BACKOFF,
    IngestError,
    IngestResult,
    ProviderAdapter,
    maybe_capture_raw,
    parse_json_bytes,
)
from ..model import OutageEvent

SERVICE = "googleai"
DEFAULT_URL = (
    "https://alkalimakersuite-pa.clients6.google.com/$rpc/"
    "google.internal.alkali.applications.makersuite.v1.MakerSuiteService/ListIncidentsHistory"
)
DEFAULT_API_KEY = "AIzaSyDdP816MREB3SkjZO04QXbjsigfcI0GWOs"
DEFAULT_REFERER = "https://aistudio.google.com/"
CANONICAL_LINK = "https://aistudio.google.com/status"

URL_ENV_VAR = "OA_GOOGLEAI_URL"
API_KEY_ENV_VAR = "OA_GOOGLEAI_API_KEY"

_REGION_MAP = [
    (re.compile(r"\b(australia|sydney|melbourne|canberra|brisbane|perth|adelaide)\b", re.IGNORECASE), "australia"),
    (re.compile(r"\b(new zealand|new-zealand|auckland|wellington|christchurch)\b", re.IGNORECASE), "new-zealand"),
    (re.compile(r"\b(apac|asia pacific|asia-pacific|oceania|australasia)\b", re.IGNORECASE), "apac"),
    (re.compile(r"\b(united-states|united states|us|usa|us-east|us-west)\b", re.IGNORECASE), "us"),
    (re.compile(r"\b(europe|eu|uk|london|frankfurt)\b", re.IGNORECASE), "europe"),
]

# Component IDs returned by MakerSuite RPC
_COMPONENT_MAP = {
    1: "gemini-api",
    2: "live-api",
    3: "ai-studio",
}

# Keywords indicating impact on Antigravity (desktop IDE, CLI, code agent, model inference)
_ANTIGRAVITY_PATTERN = re.compile(
    r"\b(antigravity|agy|code assistant|code-assistant|agent|banana|gemini 3|gemini 2\.5|gemini 2\.0|gemini 1\.5|pro|flash|throttling|serving|inference)\b",
    re.IGNORECASE,
)


def extract_googleai_regions(text: str) -> List[str]:
    """Infer regions from text content. Always includes 'global'."""
    regions = {"global"}
    for pattern, code in _REGION_MAP:
        if pattern.search(text):
            regions.add(code)
    return sorted(regions)


def extract_googleai_sub_services(component_ids: List[int], text: str) -> List[str]:
    """Map component IDs and keywords to sub-services."""
    sub_services = set()
    for cid in component_ids:
        if cid in _COMPONENT_MAP:
            sub_services.add(_COMPONENT_MAP[cid])

    # If any mention of Antigravity or model capabilities powering Antigravity is present
    if _ANTIGRAVITY_PATTERN.search(text):
        sub_services.add("antigravity")

    # If nothing matched, default to gemini-api
    if not sub_services:
        sub_services.add("gemini-api")

    return sorted(sub_services)


def infer_googleai_severity(status_code: int, latest_update_text: str, title: str) -> str:
    """Infer OutageEvent severity from status code and description text.

    Upstream status codes:
      1: Detected / Ongoing
      2: Identified / Mitigations rolling out
      3: Mitigating / Fix submitted
      4: Resolved
      5: Mitigations underway / partially restored
    """
    combined = f"{title} {latest_update_text}".lower()

    if status_code == 4 or "resolved" in combined or "[resolved]" in combined:
        return "resolved"

    if any(k in combined for k in ("outage", "unavailable", "unavailability", "down")):
        return "critical"

    if any(k in combined for k in ("degraded", "disruption", "intermittent", "error rates", "latency", "throttling", "hanging")):
        return "warning"

    return "warning"


def parse_googleai_payload(payload: Any) -> List[OutageEvent]:
    """Parse JSON payload from ListIncidentsHistory RPC."""
    events: List[OutageEvent] = []
    if not isinstance(payload, list) or not payload:
        return events

    # Structure: data[0][0] is the list of incident tuples
    incidents_list = None
    if isinstance(payload[0], list):
        if payload[0] and isinstance(payload[0][0], list):
            incidents_list = payload[0][0]
        else:
            incidents_list = payload[0]
    else:
        incidents_list = payload

    for item in incidents_list:
        if not isinstance(item, (list, tuple)) or len(item) < 4:
            continue

        raw_id = str(item[0]).strip()
        title = str(item[1]).strip()
        status_num = int(item[2]) if isinstance(item[2], (int, float)) else 1
        updates_raw = item[3] if isinstance(item[3], list) else []
        component_ids = item[5] if len(item) > 5 and isinstance(item[5], list) else []

        if not raw_id:
            continue

        # Extract updates: each is [code, date_str, [unix_ts_str], body]
        update_texts = []
        timestamps = []
        latest_update_code = status_num
        latest_update_text = ""

        for upd in updates_raw:
            if isinstance(upd, (list, tuple)) and len(upd) >= 4:
                u_code = int(upd[0]) if isinstance(upd[0], (int, float)) else 1
                u_ts_list = upd[2]
                u_body = str(upd[3]).strip()
                latest_update_code = u_code
                latest_update_text = u_body
                update_texts.append(u_body)

                if isinstance(u_ts_list, list) and u_ts_list:
                    try:
                        ts = int(u_ts_list[0])
                        timestamps.append(datetime.fromtimestamp(ts, tz=timezone.utc))
                    except (ValueError, TypeError, OSError):
                        pass

        # Sort updates chronologically
        combined_text = f"{title}\n\n" + "\n\n".join(update_texts)
        severity = infer_googleai_severity(latest_update_code, latest_update_text, title)
        sub_services = extract_googleai_sub_services(component_ids, combined_text)
        regions = extract_googleai_regions(combined_text)

        # Published timestamp is the latest update or start time
        if timestamps:
            published_at = max(timestamps)
        else:
            published_at = datetime.now(timezone.utc)

        events.append(
            OutageEvent(
                raw_id=raw_id,
                service=SERVICE,
                title=title,
                description=latest_update_text or title,
                severity=severity,
                regions=regions,
                sub_services=sub_services,
                link=CANONICAL_LINK,
                published_at=published_at,
            )
        )

    return events


class GoogleAiAdapter(ProviderAdapter):
    """Adapter for Google AI & Antigravity status feed."""

    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def __init__(self, url: Optional[str] = None) -> None:
        self.url = url or os.environ.get(self.url_env_var) or self.default_url

    def fetch(self, state_dir=None) -> IngestResult:
        result = IngestResult(provider=self.name)
        result.fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Allow loading directly from local test fixtures if URL is a local path
        if self.url.startswith("/") or self.url.startswith("file://") or (not self.url.startswith("http://") and not self.url.startswith("https://") and Path(self.url).exists()):
            file_path = Path(self.url.replace("file://", ""))
            try:
                data = file_path.read_bytes()
                result.raw_bytes = len(data)
                payload = parse_json_bytes(data)
                result.events = self.parse(payload)
                result.raw_payload = maybe_capture_raw(self.name, payload, result.raw_bytes, state_dir)
                return result
            except Exception as e:
                result.error = f"failed to read fixture {file_path}: {e}"
                return result

        api_key = os.environ.get(API_KEY_ENV_VAR, DEFAULT_API_KEY)
        headers = {
            "Content-Type": "application/json+protobuf",
            "Referer": DEFAULT_REFERER,
            "x-goog-api-key": api_key,
            "User-Agent": "Mozilla/5.0 outage-automation/2.0",
        }

        req = urllib.request.Request(self.url, data=b"[]", headers=headers)
        last_err: Optional[str] = None
        data = None

        for attempt in range(DEFAULT_RETRIES + 1):
            try:
                with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                    data = resp.read()
                    break
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code} for {self.url}: {e.reason}"
            except urllib.error.URLError as e:
                reason = e.reason
                if isinstance(reason, socket.timeout):
                    last_err = f"timeout for {self.url} (>{DEFAULT_TIMEOUT}s)"
                else:
                    last_err = f"URL error for {self.url}: {reason}"
            except (TimeoutError, socket.timeout):
                last_err = f"timeout for {self.url} (>{DEFAULT_TIMEOUT}s)"
            except Exception as e:
                last_err = f"unexpected error for {self.url}: {e!r}"

            if attempt < DEFAULT_RETRIES:
                time.sleep(RETRY_BACKOFF * (attempt + 1))

        if data is None:
            result.error = last_err or f"failed to fetch {self.url}"
            return result

        result.raw_bytes = len(data)
        try:
            payload = parse_json_bytes(data)
        except IngestError as e:
            result.error = str(e)
            return result

        result.raw_payload = maybe_capture_raw(self.name, payload, result.raw_bytes, state_dir)
        try:
            result.events = self.parse(payload)
        except Exception as e:
            result.error = f"failed to parse payload: {e}"

        return result

    def parse(self, payload: Any) -> List[OutageEvent]:
        return parse_googleai_payload(payload)
