"""Provider registry: maps provider name -> adapter instance."""
from __future__ import annotations

from typing import Dict, List, Type

from .aws import AwsAdapter
from .azure import AzureAdapter
from .base import IngestResult, ProviderAdapter
from .cloudflare import CloudflareAdapter
from .connectwise import ConnectWiseAdapter
from .m365 import M365Adapter
from .zscaler import ZscalerAdapter
from .chorus import ChorusAdapter
from .googleworkspace import GoogleWorkspaceAdapter
from .vector import VectorAdapter
from .atlassian import AtlassianAdapter
from .wiz import WizAdapter
from .xero import XeroAdapter
from .halopsa import HaloPsaAdapter
from .ninjaone import NinjaOneAdapter
from .duo import DuoAdapter
from .kaseya import KaseyaAdapter
from .enable import EnableAdapter
from .mimecast import MimecastAdapter
from .lightwire import LightwireAdapter
from .myob import MyobAdapter
from .rewst import RewstAdapter
from .crowdstrike import CrowdStrikeAdapter
from .kordia import KordiaAdapter
from .pia import PiaAdapter
from .nbn import NbnAdapter
from .avepoint import AvePointAdapter
from .wasabi import WasabiAdapter
from .tenable import TenableAdapter
from .eset import EsetAdapter
from .googleai import GoogleAiAdapter


PROVIDERS: Dict[str, Type[ProviderAdapter]] = {
    "aws": AwsAdapter,
    "azure": AzureAdapter,
    "cloudflare": CloudflareAdapter,
    "connectwise": ConnectWiseAdapter,
    "m365": M365Adapter,
    "zscaler": ZscalerAdapter,
    "chorus": ChorusAdapter,
    "googleworkspace": GoogleWorkspaceAdapter,
    "vector": VectorAdapter,
    "atlassian": AtlassianAdapter,
    "wiz": WizAdapter,
    "xero": XeroAdapter,
    "halopsa": HaloPsaAdapter,
    "ninjaone": NinjaOneAdapter,
    "duo": DuoAdapter,
    "kaseya": KaseyaAdapter,
    "enable": EnableAdapter,
    "mimecast": MimecastAdapter,
    "lightwire": LightwireAdapter,
    "myob": MyobAdapter,
    "rewst": RewstAdapter,
    "crowdstrike": CrowdStrikeAdapter,
    "kordia": KordiaAdapter,
    "pia": PiaAdapter,
    "nbn": NbnAdapter,
    "avepoint": AvePointAdapter,
    "wasabi": WasabiAdapter,
    "tenable": TenableAdapter,
    "eset": EsetAdapter,
    "googleai": GoogleAiAdapter,
}

_ALIASES: Dict[str, str] = {
    "google-workspace": "googleworkspace",
    "google": "googleworkspace",
    "halo": "halopsa",
    "ninja": "ninjaone",
    "cisco-duo": "duo",
    "datto": "kaseya",
    "itglue": "kaseya",
    "it-glue": "kaseya",
    "enable-networks": "enable",
    "aidesk": "pia",
    "nbnco": "nbn",
    "antigravity": "googleai",
    "google-ai": "googleai",
    "gemini": "googleai",
    "aistudio": "googleai",
}


def list_providers() -> List[str]:
    return sorted(PROVIDERS.keys())


def get_adapter(name: str, **kwargs) -> ProviderAdapter:
    normalized = _ALIASES.get(name.lower(), name.lower())
    if normalized not in PROVIDERS:
        raise KeyError(
            f"unknown provider '{name}' (available: {', '.join(list_providers())})"
        )
    return PROVIDERS[normalized](**kwargs)


def run_provider(name: str, state_dir=None, **kwargs) -> IngestResult:
    adapter = get_adapter(name, **kwargs)
    return adapter.fetch(state_dir=state_dir)
