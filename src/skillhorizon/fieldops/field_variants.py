"""Explicit successor construction; never applied to released checkpoints."""
import math
import random

VARIANT = "fieldops_role_progress_v1"


def configure(project, seed, variant):
    if variant is None:
        return
    if variant.startswith(("fieldops_replacement_v1_", "fieldops_replacement_v2_")):
        from .replacement_profiles import configure as replacement
        return replacement(project, seed, variant)
    if variant != VARIANT or project.get("business_scenario") not in {
            "mesh_convergence", "mesh_supply_shocks"} or project.get("domain") != "fieldops":
        raise ValueError("Role/progress variant requires a FieldOps mesh scenario")
    p = project
    rng = random.Random(f"{variant}:{seed}")
    keys = list(p["devices"])
    new_keys = list(keys)
    rng.shuffle(new_keys)
    mapping = dict(zip(keys, new_keys))
    severities = [p["devices"][key]["severity"] for key in keys]
    rng.shuffle(severities)
    devices = {}
    for key, severity in zip(keys, severities):
        device = p["devices"][key]
        device["severity"] = severity
        device["predecessors"] = sorted(mapping[k] for k in device["predecessors"])
        devices[mapping[key]] = device
    p["devices"] = dict(sorted(devices.items()))
    p["device_layout"]["revision"] = "field_device_roles_permuted_v2"
    for stage, service in p["services"].items():
        service["device_ids"] = sorted(mapping[k] for k in service["device_ids"])
        layout = p["device_layout"]["stages"][stage]
        layout["device_ids"] = list(service["device_ids"])
        layout["severity_roles"] = ["low" if p["devices"][k]["severity"] <= 9 else "high"
                                    for k in service["device_ids"]]
    p["instance_variant"] = variant
    p["project_id"] += "-" + variant
    # Use the same available windows for paired normal/recovery instances.
    groups = len(p["services"])
    for i in range(groups * 2):
        p["windows"][f"TW{i+1}"] = {"start": groups*24+i*8, "end": groups*24+i*8+6}
    if p["business_scenario"] == "mesh_supply_shocks":
        depths = {}
        for key in sorted(p["services"], key=int):
            service = p["services"][key]
            depths[int(key)] = 1 + max((depths[s] for s in service["parent_stages"]), default=0)
        depth = max(depths.values())
        p["scheduled_changes"] = [
            {"kind": "supply_pressure", "trigger": "active_service_progress",
             "minimum_active_services": math.ceil(groups * fraction),
             "minimum_active_depth": max(2, math.ceil(depth * fraction)),
             "pressure": pressure, "applied": False}
            for fraction, pressure in ((0.5, 116), (0.8, 112))]
        p["goal"] = p["goal"].replace(
            "More than one supply correction may occur at restoration milestones.",
            "Supply corrections may occur after multi-layer services have been commissioned; "
            "recovery must rebuild the affected current certification chains.")
    p["goal"] += " Device identifiers and list positions do not encode severity or repair choice; inspect public device properties."
