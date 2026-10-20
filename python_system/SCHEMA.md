# Outage System Data Model & Storage Schema

This document is the authoritative specification for the normalized outage data model, subscription contracts, and on-disk state files.

> [!NOTE]
> **Single Source of Truth Architecture:**
> This document defines system-wide data models, contracts, and storage invariants. Individual provider endpoints, regex rules, and severity heuristics are defined and documented directly in their respective adapter modules under [`outage_automation/ingest/<provider>.py`](python_system/outage_automation/ingest/). System services are registered dynamically in `config.DEFAULT_SERVICES` and `ingest.registry.PROVIDERS`.

---

## 1. Normalized Event Schema (`OutageEvent`)

The in-memory dataclass produced by all ingestion adapters and validated by `validate_outage_event`:

| Field | Type | Validation / Constraints | Description |
| :--- | :--- | :--- | :--- |
| `raw_id` | `str` | Non-empty string | Stable unique identifier assigned by the provider. Used as deduplication key. |
| `service` | `str` | Canonical registered service | Registered service code matching `config.DEFAULT_SERVICES` (e.g. `aws`, `azure`, `chorus`, `zscaler`). Lowercased. |
| `title` | `str` | String | Headline summary of the incident. |
| `description` | `str` | String | Detailed status update text (truncated to 500 chars for persistence). |
| `severity` | `str` | `critical` \| `warning` \| `info` \| `resolved` | Normalized severity level. Aliases like `warn` or `crit` are coerced. |
| `regions` | `List[str]` | Non-empty array of lowercased strings | Target region codes. Use `["global"]` for service-wide or cross-regional outages. Localized outages use canonical kebab-case codes (e.g. `ap-southeast-2`, `auckland`). |
| `sub_services` | `List[str]` | Array of lowercased strings | Optional sub-product or access medium codes (e.g. `["zia"]`, `["fibre"]`). Empty list if not applicable. |
| `link` | `str` | HTTP / HTTPS URL | Direct web URL to the provider's incident advisory. |
| `published_at` | `str` | ISO-8601 UTC timestamp | Published datetime string with trailing `Z` (e.g. `2026-10-07T04:15:00Z`). |

### Normalization Rules (`model.OutageEvent.from_dict`)

1. `raw_id` is whitespace-trimmed.
2. `service` is lowercased; must exist in registered services or validation fails.
3. `severity` is lowercased; `warn` → `warning`, `crit` → `critical`. Unrecognized values fail validation.
4. `regions` entries are lowercased and de-duplicated while preserving order.
5. `published_at` / `published` is parsed, converted to UTC, and formatted without microseconds.

---

## 2. Severity Taxonomy

Every provider's status levels are normalized to exactly one of four canonical severities:

| Severity | Color Code | Description | Typical Triggers |
| :--- | :--- | :--- | :--- |
| `critical` | Red | Major outage / complete service disruption | Total service down, data plane loss, major disruption |
| `warning` | Amber | Partial degradation / elevated error rates | Slow response, partial API failures, intermittent errors |
| `info` | Blue | Advisory / scheduled maintenance | Informational bulletins, planned maintenance windows |
| `resolved` | Green | Mitigated / closed incident | Incident marked resolved, completed, or mitigated |

---

## 3. Subscription & Matching Contract

Matching occurs dynamically in memory at request time (`match.py`):

### Region Matching
- An outage matches a customer if their subscribed regions for that service share at least one common region code, OR if either side specifies `"global"`.
- `"global"` functions as a universal wildcard matching all regions.

### Sub-Service Scoping
- If either the outage or the customer subscription has an empty `sub_services` list (`[]`), the sub-service check is considered a match.
- If both specify non-empty `sub_services`, they match if their sets intersect (e.g. customer subscribed to `["fibre"]` matches a Chorus outage tagged `["fibre"]` but ignores an outage tagged exclusively `["copper"]`).

---

## 4. State Storage Schema

All state operations enforce **single-writer concurrency** via PID file locking (`state/.lock`) and **atomic replacement** via temporary files and `os.replace`.

### 4.1. Materialized Active State (`state/alert_history.json`)
Array of `AlertHistoryRow` records representing active and recent incidents.

```json
[
  {
    "raw_id": "9sbjbzxxfpbl",
    "service": "cloudflare",
    "severity": "resolved",
    "title": "Access Configuration Updates Delayed",
    "description": "This incident has been resolved.",
    "regions": ["global"],
    "sub_services": [],
    "link": "https://stspg.io/bywctkqrs5sh",
    "published_at": "2026-03-06T19:30:47Z",
    "last_seen_at": "2026-10-07T04:19:56Z",
    "expires_at": "2026-10-14T04:19:56Z"
  }
]
```
- `last_seen_at`: Timestamp when the provider feed last contained this incident. Bumped on every ingest.
- `expires_at`: Computed as `published_at + OA_TTL_DAYS` (default 7 days).
- **Dashboard Visibility**: Incidents remain visible if `last_seen_at` falls within the active TTL window (`OA_TTL_DAYS`).
- **Auto-Resolution**: If an active incident drops off a provider's feed before explicit resolution, the merge logic auto-resolves it in `alert_history.json` and records a resolution event in `events.ndjson`.

### 4.2. Customer Subscriptions (`state/customers.json`)
Array of customer subscription definitions:

```json
[
  {
    "customer_id": "demo-corp",
    "name": "Demo Corporation",
    "subscriptions": {
      "cloudflare": ["global"],
      "aws": ["ap-southeast-2", "global"],
      "chorus": ["auckland", "wellington"]
    },
    "sub_services": {
      "chorus": ["fibre"]
    }
  }
]
```
- Validated via `lint-customers` against `config.DEFAULT_SERVICES` and `approved-regions.csv`.
- `customer_id` is required; `name` must be unique across all records.
- `subscriptions` maps service name to list of approved region codes.
- `sub_services` maps service name to list of sub-service codes.

### 4.3. Append-Only Event Log (`state/events.ndjson`)
Newline-delimited JSON log of all raw ingest events:
```json
{"ts": "2026-10-07T04:18:36Z", "source": "cloudflare", "event": { ...normalized event fields... }}
```
- Provides immutable audit trail and enables complete state replay (`run.py replay`).

### 4.4. Approved Regions Allowlist (`approved-regions.csv`)
Three-column CSV defining valid service-region pairings:
```csv
Service,Region Code,Display Name
aws,ap-southeast-2,Asia Pacific (Sydney)
cloudflare,global,Global
chorus,auckland,Auckland
```
- Validated by `validate-regions`.
- Dynamic providers (Zscaler, Vector, Chorus, Wiz) can discover and append new approved locations during ingest.

### 4.5. Run Reports (`state/reports/<run-id>.json`)
Execution report emitted at the conclusion of every `run-all` invocation:
```json
{
  "run_id": "20261007T042814Z-16082ba6",
  "schema_version": 1,
  "started_at": "2026-10-07T04:28:14Z",
  "finished_at": "2026-10-07T04:28:25Z",
  "duration_ms": 11000,
  "host": { "platform": "Linux", "python": "3.12.0", "pid": 12958 },
  "providers": [
    { "provider": "cloudflare", "ok": true, "event_count": 5, "raw_bytes": 14200, "duration_ms": 320 }
  ],
  "history": { "history_size": 412, "active_count": 14 },
  "impact_summary": { "customer_count": 85, "outage_count": 412 }
}
```

---

## 5. API Response Contracts

### 5.1. Dashboard Payload (`GET /api/dashboard`)
Authenticated endpoint providing data for dashboard rendering:
```json
{
  "rows": [ /* AlertHistoryRow[] within active TTL window */ ],
  "impact": {
    "by_customer": {
      "Demo Corporation": {
        "customer_id": "demo-corp",
        "total": 2,
        "by_service": { "cloudflare": 1, "aws": 1 }
      }
    },
    "by_outage": {
      "9sbjbzxxfpbl": ["Demo Corporation"]
    }
  },
  "ttl_days": 7,
  "generated_at": "2026-10-07T04:30:00Z",
  "row_counts": { "stored": 412, "visible": 18 },
  "last_run": { /* most recent report metadata */ }
}
```

### 5.2. Health Check (`GET /api/health`)
Public unauthenticated liveness endpoint:
```json
{ "status": "ok", "timestamp": "2026-10-07T04:30:00Z" }
```
