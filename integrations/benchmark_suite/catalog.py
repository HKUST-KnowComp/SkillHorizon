"""One current release view; historical evidence remains in its original scope."""

import argparse
from copy import deepcopy
import importlib
import inspect
from pathlib import Path

from integrations.internal_mcp.io import read_json, write_json
from integrations.internal_mcp.richness import COMPOSITIONS, SMALL
from integrations.internal_mcp.qualify_topologies import DOMAINS
from skillhorizon.dataflow.scenarios import SCENARIOS as DATA_SCENARIOS
from skillhorizon.dataflow.skills import skill_specs as data_skills
from skillhorizon.fieldops.scenarios import SCENARIOS as OPS_SCENARIOS
from skillhorizon.fieldops.skills import skill_specs as ops_skills
from skillhorizon.experiment.benchmark_measurements import REVISION as MEASUREMENT_REVISION


ROOT = Path(__file__).resolve().parents[2]
OPS = {
    "DataOps": {"domain": "dataops", "program": "credit_sales_v2", "scenario": "credit_rollover", "axis": "periods"},
    "FieldOps": {"domain": "fieldops", "program": "pressure_branches_v3", "scenario": "mesh_supply_shocks", "axis": "groups"},
    "ProcureOps": {"domain": "procureops", "program": "assembly_network_v3", "scenario": "quality_reuse", "axis": "groups"},
}
AXES = {"C09": "legs", "C25": "depth", "C06": "waves", "C47": "periods",
        "C50": "levels", "C01": "phases", "C11": "rounds"}
DELEGATES = {"C09": "network_build", "C25": "capacity_build", "C06": "exposure_build",
             "C47": "workload_build", "C50": "assembly_build", "C01": "funding_build",
             "C11": "synthesis_build"}


def resolve_construction(identifier, size, *, seed=20260910, environment_format=None):
    """Resolve expected invocation parameters, not evidence of an executed call."""
    offset = ("small", "medium", "long").index(size)
    if identifier in OPS:
        spec = OPS[identifier]
        module = "skillhorizon.dataflow.tasks" if identifier == "DataOps" else "skillhorizon.fieldops.tasks"
        name = "build_dataflow" if identifier == "DataOps" else "build_operations"
        axis, amount = spec["axis"], 8 + 2 * offset
        overrides = {axis: amount, "seed": seed, "business_program": spec["program"],
            "business_scenario": spec["scenario"], "execution_mode": "procedural", "suffix_skills": None,
            "family": "regional_sales" if identifier == "DataOps" else
                "staged_maintenance" if identifier == "FieldOps" else "split_fulfillment"}
        if identifier == "DataOps":
            overrides.update(regions=3, rows_per_region=6, notice_policy="source_only")
        source = None
        consumers = [module + "." + ("initialize_dataflow" if identifier == "DataOps" else "initialize_operations")]
    else:
        from .programs import binding
        spec = binding(identifier, environment_format)
        module, name = "integrations." + spec["module"] + "." + spec["builder"], "build"
        axis = AXES[identifier]
        amount = SMALL[identifier][axis] + offset
        overrides = {**SMALL[identifier], axis: amount, "seed": seed,
            "scenario": COMPOSITIONS[identifier], "background": True,
            "dependency_pattern": "staggered" if identifier == "C25" else "distributed",
            "runtime_identity": "task_local_v1"}
        if "profile_parameters" in spec:
            overrides = {**spec["profile_parameters"], "seed": seed,
                         axis: spec["profile_parameters"][axis] + offset}
            amount = overrides[axis]
        source = str(ROOT.parent / "Infinite_context_compact-main/envs" / spec["source"])
        package = "integrations." + spec["module"]
        consumers = [package + "." + DELEGATES[identifier] + ".build", package + ".build.build"]
        consumers = spec.get("consumers", consumers)
    builder_module = importlib.import_module(module)
    if environment_format is not None and environment_format != builder_module.FORMAT:
        raise ValueError("Resolved builder format differs from the requested program")
    builder = getattr(builder_module, name)
    bound = inspect.signature(builder).bind(**overrides, output=None, **({"source": source} if source else {}))
    bound.apply_defaults()
    parameters = {key: deepcopy(value) for key, value in bound.arguments.items() if key not in {"source", "output"}}
    return {"environment_id": identifier, "size": size, "axis": axis, "axis_value": amount,
        "builder": module + "." + name, "source": source, "parameters": parameters, "consumers": consumers,
        "environment_format": builder_module.FORMAT,
        "skill_protocol": "procedural" if identifier in OPS else "observational_v1"}


def effective_parameter(profile, name):
    """Known public normalization contract, separate from requested defaults.

    C25's nullable amendment selects a scenario-dependent boolean before its
    capacity builder is called. The original requested None stays in parameters.
    """
    value = profile["parameters"][name]
    if name == "device_counts":
        from skillhorizon.fieldops.field_layout import normalize_counts
        return normalize_counts(profile["environment_id"].lower(), profile["parameters"]["business_program"],
                                profile["parameters"]["groups"], value)
    if profile["environment_id"] == "C25" and name == "amendment" and value is None:
        from .programs import CAPACITY
        if profile["environment_format"] == CAPACITY:
            raise ValueError("Capacity amendment must be boolean")
        return profile["parameters"]["scenario"] != "expiring_reservations"
    return value


def resolve_profile(identifier, *, seed, overrides, environment_format=None):
    """Explicit construction request; no claim about measured history size."""
    if identifier not in {*OPS, *DOMAINS} or type(seed) is not int or not isinstance(overrides, dict):
        raise ValueError("Explicit environment, integer seed and parameter object required")
    base = resolve_construction(identifier, "small", seed=seed, environment_format=environment_format)
    forbidden = {"seed", "source", "output", "runtime_identity", "execution_mode", "suffix_skills"}
    if set(overrides) - set(base["parameters"]) or set(overrides) & forbidden:
        raise ValueError("Unknown, seed, path or identity overrides are forbidden")
    parameters = {**base["parameters"], **deepcopy(overrides)}
    module, name = base["builder"].rsplit(".", 1)
    builder = getattr(importlib.import_module(module), name)
    binding = inspect.signature(builder).bind(output=None, **parameters,
        **({"source": base["source"]} if base["source"] else {}))
    binding.apply_defaults()
    result = {**base, "size": "explicit", "axis_value": parameters[base["axis"]],
        "parameters": parameters, "revision": "explicit_grid_profile_v1", "overrides": deepcopy(overrides)}
    result["expected_effective"] = {k: effective_parameter(result, k) for k in parameters}
    return result


def validate_profile(profile, *, identifier, seed):
    expected = resolve_profile(identifier, seed=seed, overrides=profile["overrides"],
                               environment_format=profile["environment_format"])
    if profile != expected:
        raise ValueError("Explicit profile, domain, seed or resolved defaults differ")
    return expected


def catalog():
    rows = []
    for identifier, spec in OPS.items():
        domain = spec["domain"]
        skills = data_skills(spec["program"]) if domain == "dataops" else ops_skills(domain, spec["program"], spec["scenario"])
        rows.append({"environment_id": identifier, "family": "ops", "skill_protocol": "procedural",
            "business_program": spec["program"], "candidate_skills": len(skills),
            "scenarios": list(DATA_SCENARIOS if domain == "dataops" else OPS_SCENARIOS[domain]),
            "scale_axis": spec["axis"], "sizes": {"small": 8, "medium": 10, "long": 12},
            "construction_scenario": spec["scenario"]})
    source = read_json(ROOT / "integrations/internal_mcp/catalog.json")
    families = {r["environment_id"]: r["families"] for r in read_json(ROOT / "integrations/internal_mcp/scenario_catalog.json")["environments"]}
    for spec in source["dependency_profiles"]:
        identifier = spec["environment_id"]
        start = SMALL[identifier][AXES[identifier]]
        rows.append({"environment_id": identifier, "family": "internal_mcp", "skill_protocol": "observational_v1",
            "environment_format": spec["task_format"], "domain": spec["domain"],
            "candidate_skills": spec["skill_count"], "scripted_skills": spec["script_skill_count"],
            "component_families": list(families[identifier]), "composition": COMPOSITIONS[identifier],
            "scale_axis": AXES[identifier], "sizes": dict(zip(("small", "medium", "long"), range(start, start + 3))),
            "backend_source": DOMAINS[identifier]["source"],
            "profile_catalog": "integrations/internal_mcp/catalog.json"})
    for row in rows:
        row["construction_parameters"] = resolve_construction(row["environment_id"], "small")["parameters"]
    return {"revision": "ten_domain_release_view_v1", "scope": "construction_development_not_heldout",
        "measurement_contract": MEASUREMENT_REVISION, "environments": rows,
        "counting": {"environments": len(rows), "domain_local_candidate_skills": sum(r["candidate_skills"] for r in rows),
            "formal_task_count": None, "note": "Seeds, scales, checkpoints and compositions are not independent semantic families."},
        "measurement_axes": ["business_obligations", "executed_artifact_dependencies", "actor_trajectory", "observed_context", "infrastructure"],
        "evidence_rules": {"construction": "Public reference and negative-control execution, not model performance",
            "native_qualification": "Actual SDK/MCP with scripted model fixture, not behavior",
            "behavior": "Accepted provider actions and independent terminal evaluation",
            "scope": "Every result retains model, harness, protocol, seed, checkpoint and version; never pool by environment ID alone"}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Release view output must be fresh")
    write_json(args.output, catalog())
