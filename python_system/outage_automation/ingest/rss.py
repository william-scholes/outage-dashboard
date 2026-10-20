"""Shared RSS 2.0 adapter.

Used by M365, Azure, AWS, Zscaler. Subclasses provide:
  - `name`, `default_url`, `url_env_var`
  - `parse_severity(item) -> str` — provider-specific severity inference
  - `parse_regions(item) -> list[str]` — provider-specific region inference
  - `parse_title(item) -> str` (optional, default = raw `<title>` text)
  - `parse_description(item) -> str` (optional, default = raw `<description>` text)
  - `parse_link(item) -> str` (optional, default = raw `<link>` text)
  - `parse_guid(item) -> str` (optional, default = `<guid>` text or fallback to link/title)
  - `parse_published(item) -> datetime` (optional, default = RFC 822 `pubDate`)

The base class handles:
  - HTTP fetch with retries (via `fetch_url`)
  - XML parsing (via `xml.etree.ElementTree`)
  - ISO-8601 normalization of `pubDate` (via `email.utils.parsedate_to_datetime`)
  - `raw_id` fallback when `<guid>` is missing
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

from ..model import OutageEvent
from ..validate import parse_datetime
from .base import (
    IngestError,
    IngestResult,
    ProviderAdapter,
    fetch_url,
    maybe_capture_raw,
)


def _text(elem: Optional[ET.Element]) -> str:
    if elem is None or elem.text is None:
        return ""
    return str(elem.text).strip()


def _parse_rfc822_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clean_description(value: str) -> str:
    """Strip the most common RSS description wrappers (HTML / CDATA)."""
    if not value:
        return ""
    s = value
    # Strip a simple wrapping <div>...</div>
    if s.startswith("<") and s.endswith(">"):
        # Very rough HTML strip — drop tags but keep text.
        import re
        s = re.sub(r"<[^>]+>", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
    return s[:500]


def _hash_id(*parts: str) -> str:
    import hashlib
    h = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return h[:24]


class RssAdapter(ProviderAdapter):
    """Base class for any provider exposing an RSS 2.0 feed."""

    name: str = ""
    default_url: str = ""
    url_env_var: str = ""
    # Path to the `<item>` collection within the parsed tree.
    # Defaults to `rss.channel.item`. Override if the structure differs.
    item_path: List[str] = ["rss", "channel", "item"]

    def __init__(self, url: Optional[str] = None) -> None:
        env_url = (
            os.environ.get(self.url_env_var) if self.url_env_var else None
        )
        self.url = url or env_url or self.default_url

    # -- subclass hooks ---------------------------------------------------
    def parse_severity(self, item: ET.Element) -> str:
        return "info"

    def parse_regions(self, item: ET.Element) -> List[str]:
        return ["global"]

    def parse_sub_services(self, item: ET.Element) -> List[str]:
        """Override to emit per-provider sub-product codes (e.g.
        zia/zpa/zdx/zcc for Zscaler). Default returns []; the new
        sub-service axis is opt-in per adapter.
        """
        return []

    def parse_title(self, item: ET.Element) -> str:
        return _text(_find_child(item, "title"))

    def parse_description(self, item: ET.Element) -> str:
        return _clean_description(_text(_find_child(item, "description")))

    def parse_link(self, item: ET.Element) -> str:
        return _text(_find_child(item, "link"))

    def parse_guid(self, item: ET.Element) -> str:
        return _text(_find_child(item, "guid"))

    def parse_published(self, item: ET.Element) -> Optional[datetime]:
        raw = _text(_find_child(item, "pubDate"))
        # Try ISO-8601 first, then RFC 822 (RSS standard).
        dt = parse_datetime(raw)
        if dt is not None:
            return dt
        return _parse_rfc822_date(raw)

    # -- main entrypoint --------------------------------------------------
    def fetch(self, state_dir=None) -> IngestResult:
        result = IngestResult(provider=self.name)
        result.fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            data = fetch_url(self.url)
        except IngestError as e:
            result.error = str(e)
            return result
        result.raw_bytes = len(data)
        try:
            text = data.decode("utf-8")
            root = ET.fromstring(text)
        except Exception as e:
            result.error = f"invalid RSS XML: {e!r}"
            return result
        if state_dir is not None:
            cap = maybe_capture_raw(self.name, {"xml": text}, result.raw_bytes, state_dir)
            if cap:
                result.raw_payload = cap
        result.events = self.parse(root)
        return result

    def parse(self, root: ET.Element) -> List[OutageEvent]:
        items = _find_all(root, self.item_path)
        out: List[OutageEvent] = []
        for it in items:
            ev = self._item_to_event(it)
            if ev is not None:
                out.append(ev)
        return out

    def _item_to_event(self, item: ET.Element) -> Optional[OutageEvent]:
        published_at = self.parse_published(item)
        if published_at is None:
            return None
        title = self.parse_title(item) or "(untitled item)"
        description = self.parse_description(item)
        link = self.parse_link(item)
        guid = self.parse_guid(item) or _hash_id(title, link, str(published_at))
        if not guid:
            return None
        severity = self.parse_severity(item) or "info"
        regions_in = self.parse_regions(item)
        if not isinstance(regions_in, list):
            # Defensive: a subclass returning a bare string would otherwise
            # be iterated character-by-character (`list("global")` == ['g',
            # 'l', 'o', 'b', 'a', 'l']), producing nonsense region tokens.
            regions_in = ["global"] if regions_in else ["global"]
        regions = list(regions_in)
        # Normalize regions: lowercase + dedupe + always keep at least one.
        seen = set()
        norm_regions: List[str] = []
        for r in regions:
            k = str(r or "").strip().lower()
            if not k or k in seen:
                continue
            seen.add(k)
            norm_regions.append(str(r).strip())

        # Sub-services (zia/zpa/zdx/zcc etc.) are an orthogonal axis to
        # regions. Default-empty; only specific adapters (Zscaler) override.
        sub_services_in = self.parse_sub_services(item)
        if not isinstance(sub_services_in, list):
            sub_services_in = []
        norm_sub_services: List[str] = []
        seen_s: set = set()
        for s in sub_services_in:
            k = str(s or "").strip().lower()
            if not k or k in seen_s:
                continue
            seen_s.add(k)
            norm_sub_services.append(str(s).strip())

        # If the provider is AWS or Azure, and the parsed regions list is empty,
        # discard the event as it is not relevant to our target regions (Australia/Global).
        if self.name in ("aws", "azure") and not norm_regions:
            return None

        return OutageEvent(
            raw_id=str(guid),
            service=self.name,
            title=title,
            description=description,
            severity=str(severity).lower(),
            regions=norm_regions,
            sub_services=norm_sub_services,
            link=link,
            published_at=published_at,
        )


# -- tree helpers --------------------------------------------------------
def _find_child(parent: ET.Element, name: str) -> Optional[ET.Element]:
    for child in list(parent):
        if _local_name(child.tag) == name:
            return child
    return None


def _find_all(root: ET.Element, path: List[str]) -> List[ET.Element]:
    """Walk to the **parent** of the target elements and return its
    direct children matching the final path component.

    For `path = ["rss", "channel", "item"]`, this walks to `<channel>`
    and returns all `<item>` children of it.

    If `path[0]` matches the root's local name, we treat it as a no-op
    (the root itself) and continue. This lets callers write
    `["rss", "channel", "item"]` without having to know whether the
    parser root is `<rss>` or something else.
    """
    if not path:
        return []
    node: Optional[ET.Element] = root
    if _local_name(root.tag) == path[0]:
        path = path[1:]
    # Walk all but the last component (so `node` ends up at the parent).
    for name in path[:-1]:
        if node is None:
            return []
        node = _find_child(node, name)
    if node is None:
        return []
    last = path[-1]
    return [c for c in list(node) if _local_name(c.tag) == last]


def _local_name(tag: str) -> str:
    """Strip XML namespace from a tag name, e.g.
    `{http://www.w3.org/2005/Atom}link` → `link`.
    """
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag
