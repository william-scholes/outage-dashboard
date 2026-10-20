"""Cloudflare ingester.

Source: https://www.cloudflarestatus.com/api/v2/incidents.json
This is the same Statuspage.io v2 endpoint used by the existing JS parser.
The ingester does not require any authentication.
"""
from __future__ import annotations

from .statuspage_v2 import StatuspageV2Adapter


SERVICE = "cloudflare"
DEFAULT_URL = "https://www.cloudflarestatus.com/api/v2/incidents.json"
URL_ENV_VAR = "OA_CLOUDFLARE_URL"


class CloudflareAdapter(StatuspageV2Adapter):
    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR
