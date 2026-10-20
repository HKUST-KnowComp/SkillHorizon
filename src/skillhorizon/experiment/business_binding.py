"""Shared declaration checks, with no worker or execution authority."""

from pathlib import Path

from ..util import canonical_json, load_json
from .ptc_config import normalize_business_transport, normalize_ptc


def normalize_business_binding(*, ptc=None, transport="inline", observation_mode="full",
                               workspace=None, execution=None, max_actions=None):
    transport = normalize_business_transport(transport)
    if observation_mode not in ("full", "catalog_only"):
        raise ValueError("Unsupported business observation mode")
    ptc = normalize_ptc(ptc, workspace=workspace, execution=execution,
                        observation_mode=observation_mode, business_transport=transport,
                        max_actions=max_actions)
    return {"business_transport": transport, "ptc": ptc,
            "business_observation_mode": observation_mode}


def business_binding(bundle):
    """Bind both declarations; metadata alone never enables managed execution."""
    bundle = Path(bundle)
    manifest = load_json(bundle / "manifest.json")
    public = load_json(bundle / "public/task.json")
    defaults = {"business_transport": "inline", "ptc": None,
                "business_observation_mode": "full"}
    for key, default in defaults.items():
        if canonical_json(public.get(key, default)) != canonical_json(manifest.get(key, default)):
            raise ValueError("Public business declaration differs: " + key)
    from .execution_bundle import execution_config
    return normalize_business_binding(
        ptc=manifest.get("ptc"), transport=manifest.get("business_transport", "inline"),
        observation_mode=manifest.get("business_observation_mode", "full"),
        workspace=manifest.get("workspace"), execution=execution_config(bundle),
        max_actions=manifest.get("execution_max_actions"))
