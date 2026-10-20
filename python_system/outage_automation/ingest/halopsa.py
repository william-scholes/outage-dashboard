"""HaloPSA ingester (Status.io).

Source: https://status.halopsa.com (redirects to https://status.haloservicesolutions.com)
Under the hood, Halo Service Solutions uses Status.io with page ID 63ef45da7ee94905308a1a4a.
API Endpoint: https://api.status.io/1.0/status/63ef45da7ee94905308a1a4a

Region & Component Inference:
  - Defaults to `global` (SaaS application).
  - Normalizes Status.io containers (e.g. Australia, Europe, North America) to regional codes.
  - Sub-services mapped from affected components (Core Application, Integration Syncs, Email Processing).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .status_io import StatusIoAdapter, parse_status_io_incidents

SERVICE = "halopsa"
DEFAULT_URL = "https://api.status.io/1.0/status/63ef45da7ee94905308a1a4a"
URL_ENV_VAR = "OA_HALOPSA_URL"
DEFAULT_LINK_TEMPLATE = (
    "https://status.haloservicesolutions.com/pages/incident/63ef45da7ee94905308a1a4a/{id}"
)


class HaloPsaAdapter(StatusIoAdapter):
    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def parse(self, payload: Any):
        return parse_status_io_incidents(
            payload,
            self.name,
            default_link_template=DEFAULT_LINK_TEMPLATE,
        )
