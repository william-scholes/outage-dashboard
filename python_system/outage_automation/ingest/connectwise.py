"""ConnectWise ingester.

ConnectWise's status page is hosted on Status.io. Endpoint:
    https://status.connectwise.com/api/v2/incidents.json

The response is the Status.io wrapped shape (see `status_io.py`).
"""
from __future__ import annotations

from .status_io import StatusIoAdapter


SERVICE = "connectwise"
DEFAULT_URL = "https://api.status.io/1.0/status/619cf82551fec9053d612f09"
URL_ENV_VAR = "OA_CONNECTWISE_URL"
# ConnectWise status pages prepend a fixed container id; use the
# generic Status.io incident URL template.
DEFAULT_LINK_TEMPLATE = (
    "https://status.connectwise.com/pages/incident/619cf82551fec9053d612f09/{id}"
)


class ConnectWiseAdapter(StatusIoAdapter):
    name = SERVICE
    default_url = DEFAULT_URL
    url_env_var = URL_ENV_VAR

    def parse(self, payload):  # type: ignore[override]
        from .status_io import parse_status_io_incidents
        return parse_status_io_incidents(
            payload,
            self.name,
            default_link_template=DEFAULT_LINK_TEMPLATE,
        )
