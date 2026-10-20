"""Explicit construction coverage; no provider or credential access."""

import argparse
from copy import deepcopy
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from .characterize import SCOPES
from .io import read_json, write_json
from .qualification import qualify_direct
from .qualify_topologies import DOMAINS, ROOT
from .structure import business_nodes, graph_metrics, trace_profile

COMPOSITIONS = {
    "C09": "specialist_disruption", "C25": "expiring_amendments",
    "C06": "overlap_reactivation", "C47": "constrained_renewal",
    "C50": "constrained_rework", "C01": "restricted_funding_revision",
    "C11": "ambiguous_withdrawal",
}
SMALL = {
    "C09": {"cohorts": 2, "legs": 4},
    "C25": {"width": 3, "depth": 4},
    "C06": {"campaigns": 2, "waves": 3},
    "C47": {"units": 2, "periods": 3, "resources_per_unit": 2},
    "C50": {"levels": 3, "terminal_quantity": 1},
    "C01": {"cases": 3, "phases": 3},
    "C11": {"sections": 2, "rounds": 3, "studies_per_section": 1,
            "alias_copies": 1, "distractors_per_section": 1},
}


class CoverageMissingError(AssertionError):
    """The reference did not exercise a required business mechanism."""


class NegativeControlFailure(AssertionError):
    """An evaluator accepted an intentionally invalid copied business state."""


def construction_plan():
    catalog = read_json(Path(__file__).with_name("scenario_catalog.json"))
    rows = []
    for domain in catalog["environments"]:
        identifier = domain["environment_id"]
        scenarios = [(s, (7,), "component") for s in domain["families"]]
        scenarios.append((COMPOSITIONS[identifier], (7, 19), "composition"))
        for scenario, seeds, kind in scenarios:
            for seed in seeds:
                pattern = "staggered" if identifier == "C25" and scenario in (
                    "independent_amendments", "expiring_amendments") else "distributed"
                rows.append({"id": f"{identifier}_{scenario}_{pattern}_{seed}", "environment": identifier,
                    "kind": kind, "parameters": {**SMALL[identifier], "scenario": scenario},
                    "pattern": pattern, "seed": seed, "background": True, "alternative": seed == 19})
    for identifier in ("C25", "C06", "C50", "C01"):
        for pattern in ("alternating_fork_join", "protected_branch"):
            if identifier == "C50" and pattern == "protected_branch":
                continue
            parameters = {**SMALL[identifier], "scenario": DOMAINS[identifier]["parameters"]["scenario"]}
            if identifier == "C06":
                parameters["campaigns"] = 3
            rows.append({"id": f"{identifier}_{pattern}_7", "environment": identifier,
                "kind": "structural_control", "parameters": parameters,
                "pattern": pattern, "seed": 7, "background": True, "alternative": False})
    return {"scope": "construction_development", "provider_calls": 0,
        "independent_component_families": 14, "composition_scenarios": 7,
        "unsupported": ["C09/C47/C11 generic layered topologies", "C50 protected_branch with two lanes"],
        "rows": rows}


def inventory(task, public, world, events):
    skills = task / "public/skills"
    scripts = list(skills.glob("*/scripts/*.py"))
    names = {s["name"] for s in public["skills"]}
    opened = {e["arguments"].get("skill_id") for e in events if e["name"] == "read_skill_file"}
    table_rows = [r for rows in world["tables"].values() for r in rows]
    archive = read_json(task / "background.json") if (task / "background.json").exists() else {}
    archived_rows = sum(len(rows) for rows in archive.values() if isinstance(rows, list))
    background_rows = [r for r in table_rows if r.get("task_id") != public["task_id"]]
    background_rows += [r for rows in archive.values() if isinstance(rows, list) for r in rows if isinstance(r, dict)]
    def ids(rows):
        return {v for r in rows for k, v in r.items() if k.endswith("_id") and isinstance(v, str)}
    active_ids = ids([r for r in table_rows if r.get("task_id") == public["task_id"]])
    background_ids = ids(background_rows) - active_ids
    observed = set()
    def visit(value):
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            observed.add(value)
    for event in events:
        visit(event["output"])
    return {"skills": len(names), "python_files": len(scripts),
        "skills_with_python": len({p.parent.parent for p in scripts}),
        "reference_opened_skills": sorted(names & opened),
        "background_table_rows": sum(r.get("task_id") != public["task_id"] for r in table_rows),
        "archived_background_rows": archived_rows,
        "active_table_rows": sum(r.get("task_id") == public["task_id"] for r in table_rows),
        "observed_background_identity_values": len(observed & background_ids),
        "observed_active_identity_values": len(observed & active_ids),
        "observed_distractor_fraction": None,
        "note": "Background includes source rows without task_id. Observed identity counts use exact structured string values, not identifiers embedded in prose. They are not semantic distractor ratios. Script files include helpers; reference opens are not model selection."}


def composition_checks(identifier, task, world, evaluation, evaluate, *, output=None):
    """Post-state negative controls, always in a separate copied workspace."""
    report_root = task if output is None else Path(output)
    if output is not None:
        report_root.mkdir(parents=True, exist_ok=False)
    def require(condition, message):
        if not condition:
            raise CoverageMissingError(message)
    mutations = []
    if identifier == "C09":
        metrics = evaluation["freight_metrics"]
        require(metrics["operations_events"] == 1 and metrics["handling_restricted_movements"] > 0,
                "Carrier change and specialist custody must both occur")
        require(metrics["cancelled_manifests"] > 0, "Carrier change did not require actual custody repair")
        bad = deepcopy(world)
        bad["transfer_network"]["service_events"] = []
        mutations.append(("omitted_carrier_change", bad))
    elif identifier == "C25":
        require(len(world["planning"]["applied_updates"]) == 2, "Both document amendments must occur")
        blocks = {b["block_id"]: b for b in world["tables"]["page_block"]}
        expiry_constraints = 0
        for page in world["tables"]["page"]:
            requirements = json.loads(blocks[page["requirements_block_id"]]["content"])
            publications = [p for p in world["planning"]["publications"].values() if p["page_id"] == page["page_id"]]
            current = max(publications, key=lambda p: p["page_version"])
            for source in current["contract"]["capacity"]["portfolio"]:
                expiry_constraints += sum(source["expires_stage"] < c["activation_stage"]
                    for c in requirements["capacity_policy"]["consumers"])
        if not expiry_constraints:
            raise CoverageMissingError("No actual current quota is constrained by consumer-stage expiry")
        for page in world["tables"]["page"][2::3]:
            if sum(p["page_id"] == page["page_id"] for p in world["planning"]["publications"].values()) != 1:
                raise AssertionError("Independent track was unnecessarily republished")
        bad = deepcopy(world)
        bad["planning"]["applied_updates"].pop()
        mutations.append(("missing_amendment", bad))
    elif identifier == "C06":
        metrics = evaluation["exposure_metrics"]["conversion_feedback"]
        require(metrics["deduplicated_overlap_contributions"] > 0, "No observed conversion overlap")
        require(metrics["reactivated_cohort_contributions"] > 0, "No observed cooldown reactivation")
        bad = deepcopy(world)
        publication = next(p for p in bad["social"]["publications"].values() if p["reach"]["conversion_snapshot_ids"])
        publication["reach"]["conversion_snapshot_ids"].pop()
        mutations.append(("missing_conversion_origin", bad))
    elif identifier == "C47":
        require(len(world["tables"]["commitment"]) >= 3, "Staged obligations were not exercised")
        require(evaluation["workload_metrics"]["codec_accounting"]["published_outputs"]["encoding_counts"].get("dense", 0) > 0,
                "No actual narrow-link encoding")
        bad = deepcopy(world)
        binding = next(b for b in bad["finops"]["workload"]["bindings"].values() if b["decompression_capacity"] > 0)
        binding["decompression_capacity"] //= 2
        mutations.append(("undercharged_decode", bad))
        bad = deepcopy(world)
        bad["tables"]["commitment"][0]["hourly_commitment_cents"] = 0
        mutations.append(("missing_reserved_capacity", bad))
    elif identifier == "C50":
        require(evaluation["rework_attempts"] > 0, "No actual manufacturing rework")
        if not any(r["accepted_unit_ids"] and r["quarantined_unit_ids"] for r in world["production"]["dispositions"].values()):
            raise CoverageMissingError("Partial rework did not preserve any accepted sibling")
        bad = deepcopy(world)
        bad["production"]["first_pass_deviations"] = {}
        mutations.append(("erased_quality_disturbance", bad))
        public = read_json(task / "public/task.json")
        bad = deepcopy(world)
        target = next(r for r in bad["tables"]["run"] if r["cell_id"] == public["equipment_calendar"][0]["cell_id"])
        event = next(e for e in bad["production"]["events"] if e["type"] == "run.scheduled" and e["entity_id"] == target["run_id"])
        event["data"]["scheduled_start"] = public["origin"]
        mutations.append(("maintenance_collision", bad))
    elif identifier == "C01":
        state = world["mail"]
        amended = read_json(task / "public/task.json")["policy"]["declared_amendment_node_id"]
        rows = list(state["acknowledgements"].values())
        for receipt in rows:
            node = state["nodes"][receipt["node_id"]]
            brief = state["documents"][node["brief_document_id"]]["versions"][str(receipt["brief_revision"])]["content"]
            policy = brief["funding_policy"]
            ready = receipt["funding"]["operational_ready_day"]
            expected = ([{"available_day": 0, "amount_cents": policy["upfront_grant_cents"]}]
                if policy["upfront_grant_cents"] > 0 else [])
            expected += [{"available_day": ready + part["ready_offset_days"], "amount_cents": part["amount_cents"]}
                for part in policy["grant_installments"] if part["amount_cents"] > 0]
            if receipt["funding"]["local_tranches"] != sorted(expected, key=lambda r: r["available_day"]):
                raise AssertionError("Actual installments differ from the current public brief")
        bad = deepcopy(world)
        row = next(r for r in bad["mail"]["acknowledgements"].values() if r["node_id"] == amended)
        require(row["brief_revision"] == 2, "Revised funding brief was not consumed")
        row["brief_revision"] = 1
        mutations.append(("stale_funding_revision", bad))
    elif identifier == "C11":
        state = world["library"]
        require(state["aliases"], "No actual alias reconciliation")
        reviews = [r for r in state["artifacts"].values() if r["kind"] == "review"]
        reasons = {e["reason"] for s in reviews[-1]["sections"] for e in s["synthesis"]["exclusions"]}
        require({"retracted_study", "incompatible_metric_or_population"} <= reasons,
                "Both incompatible and withdrawn evidence must be excluded")
        bad = deepcopy(world)
        final = next(r for r in reversed(list(bad["library"]["artifacts"].values())) if r["kind"] == "review")
        keys = final["cross_section_synthesis"]["unique_study_source_keys"]
        keys.append(keys[0])
        mutations.append(("duplicate_pooled_source", bad))
    findings = []
    for label, changed in mutations:
        target = report_root / "negative_controls" / label
        shutil.copytree(task / "runtime", target)
        write_json(target / "world.json", changed)
        result = evaluate(task, target)
        findings.append({"mutation": label, "rejected": not result["accepted"]})
    result = {"provider_calls": 0, "both_components_observed": True,
        "negative_controls": findings, "passed": bool(findings) and all(r["rejected"] for r in findings)}
    write_json(report_root / "COMPOSITION_CHECKS.json", result)
    if not result["passed"]:
        raise NegativeControlFailure("A wrong composed business state was accepted")
    return result


def qualify_row(row, output):
    identifier = row["environment"]
    spec = deepcopy(DOMAINS[identifier])
    module = "integrations." + spec["module"]
    builder = importlib.import_module(module + "." + spec["builder"]).build
    runtime = getattr(importlib.import_module(module + "." + spec["runtime"]), spec["class"])
    witness = importlib.import_module(module + "." + spec["witness"]).run
    evaluate = importlib.import_module(module + "." + spec.get("evaluator", "evaluate")).evaluate
    task = output / row["id"]
    if task.exists():
        raise ValueError("Fresh construction row directory required")
    public = builder(ROOT.parent / "Infinite_context_compact-main/envs" / spec["source"], task,
        seed=row["seed"], background=row["background"], dependency_pattern=row["pattern"],
        runtime_identity="task_local_v1", **row["parameters"])
    initial = read_json(task / "checkpoint/world.json")
    nodes = business_nodes(identifier, public, initial)
    options = {**spec.get("witness_options", {}), "alternative": row["alternative"]}
    env, events, evaluation = qualify_direct(task, runtime, witness, evaluate, options=options)
    checks = composition_checks(identifier, task, env.world, evaluation, evaluate) if row["kind"] == "composition" else None
    return {**row, "bundle": str(task), "direct_accepted": evaluation["accepted"],
        "native_qualified": False, "model_tested": False, "provider_calls": 0,
        "graph": graph_metrics(nodes), "graph_scope": SCOPES[identifier],
        "mechanisms": public.get("scenario", {}).get("mechanisms", []),
        "composition_of": public.get("scenario", {}).get("composition_of", []),
        "inventory": inventory(task, public, initial, events), "reference": trace_profile(events),
        "evaluation": evaluation, "composition_checks": checks, "cell_qualified": True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--kind", choices=("component", "composition", "structural_control"))
    parser.add_argument("--environments", nargs="+", choices=DOMAINS)
    parser.add_argument("--worker-row", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_row:
        plan = read_json(args.output / "PLAN.json")
        row = next(r for r in plan["rows"] if r["id"] == args.worker_row)
        try:
            result = qualify_row(row, args.output)
        except Exception as error:
            direct_path = args.output / row["id"] / "DIRECT_VERIFICATION.json"
            direct = read_json(direct_path) if direct_path.exists() else {}
            result = {**row, "direct_accepted": direct.get("accepted", False), "cell_qualified": False, "provider_calls": 0,
                "failure": type(error).__name__, "detail": str(error)}
        write_json(args.output / (row["id"] + ".RESULT.json"), result)
        return
    if args.output.exists():
        raise ValueError("Fresh coverage directory required")
    plan = construction_plan()
    plan["rows"] = [r for r in plan["rows"] if (not args.kind or r["kind"] == args.kind)
        and (not args.environments or r["environment"] in args.environments)]
    write_json(args.output / "PLAN.json", plan)
    rows = []
    for row in plan["rows"]:
        subprocess.run([sys.executable, "-m", "integrations.internal_mcp.richness", str(args.output.resolve()),
            "--worker-row", row["id"]], cwd=ROOT, check=True)
        result = read_json(args.output / (row["id"] + ".RESULT.json"))
        rows.append(result)
        write_json(args.output / "COVERAGE.json", {"scope": plan["scope"], "provider_calls": 0,
            "all_direct_accepted": all(r["direct_accepted"] for r in rows),
            "all_cells_qualified": all(r["cell_qualified"] for r in rows),
            "complete": len(rows) == len(plan["rows"]), "rows": rows})
        print(row["id"], result["cell_qualified"], result.get("failure", ""), flush=True)


if __name__ == "__main__":
    main()
