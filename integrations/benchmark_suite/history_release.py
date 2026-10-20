"""Freeze qualified reference histories without running a model or changing evidence."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from skillhorizon.experiment.history_measurement import contract, load_reference, projection, strict_rows
from skillhorizon.util import load_json, load_jsonl, sha256_file, sha256_tree, sha256_value, write_json
from .history_bands import BANDS, interval
from .history_coverage import DOMAINS, band_for
from .reference_policy import attest_replay, bound_reference, reference_actions

REVISION = "reference_history_release_v2"
ACTOR_POLICY = {"model": "gpt-5.6-sol", "reasoning_effort": "medium", "context_window": 480000,
                "context_policy": "summary", "compact_at": 200000, "max_output_tokens": 12288,
                "parallel_tool_calls": False}


def source_identity(root):
    return {folder: sha256_tree(Path(root) / folder, excluded_directory_names={"__pycache__", ".pytest_cache"})
            for folder in ("src", "integrations")}


def verified_reference(candidate):
    episode = Path(candidate) / "episode"
    qualification = load_json(episode / "QUALIFICATION.json")
    if (qualification.get("passed") is not True or qualification.get("provider_calls") != 0
            or qualification["effects"].get("valid") is not True
            or qualification["effects"].get("task_success") is not True):
        raise ValueError("A completed, independently verified zero-provider reference is required")
    plan = load_json(episode / "PLAN.json")
    if plan["reference_batch_size"] != 8 or plan.get("skill_documentation", "full") != "full":
        raise ValueError("Reference packing/documentation differs from the frozen policy")
    task_root = Path(plan["task"])
    task = load_json(task_root / "task.json")
    bundle = episode / "fixture_bundle"
    if task != load_json(bundle / "private_task.json") or task["checkpoint_action_count"] != 0:
        raise ValueError("Qualified task differs or is not full-online")
    public = load_json(bundle / "public/task.json")
    initial = load_json(bundle / "public/history.json")
    if (public["tools"] != task["tools"] or initial != task["messages"]
            or sha256_tree(bundle / "checkpoint", excluded_directory_names={"__pycache__"}) !=
               sha256_tree(task_root / "checkpoint", excluded_directory_names={"__pycache__"})):
        raise ValueError("Qualified checkpoint/schema/history differs from original task")
    rows = strict_rows(episode / "fixture/PUBLIC_HISTORY.jsonl")
    reference = load_reference(episode / "fixture")
    admissions = [r for r in rows if r["kind"] == "admission"]
    if rows[0]["tools"] != public["tools"] or [r["message"] for r in admissions if r["attempt"] == 0] != [projection(m) for m in initial]:
        raise ValueError("Qualified schema or initial messages differ from the public ledger")
    records = load_jsonl(episode / "fixture/tools.jsonl")
    calls = [r["message"] for r in admissions if (r["message"] or {}).get("type") == "function_call"]
    batches = {}
    for r in admissions:
        if (r["message"] or {}).get("type") == "function_call":
            batches[r["attempt"]] = batches.get(r["attempt"], 0) + 1
    if list(batches.values()) != [min(8, len(calls)-i) for i in range(0, len(calls), 8)]:
        raise ValueError("Recorded reference response packing differs from batch eight")
    returns = [r["message"] for r in admissions if (r["message"] or {}).get("type") == "function_call_output"]
    if ([(m["call_id"], m["name"], json.loads(m["arguments"])) for m in calls] !=
            [(r["call_id"], r["name"], r["arguments"]) for r in records]
            or [(m["call_id"], json.loads(m["output"])) for m in returns] != [(r["call_id"], r["output"]) for r in records]):
        raise ValueError("Public ledger differs from actual SDK tool effects")
    attestation = attest_replay(reference_actions(task_root), records, reference)
    if attestation != load_json(episode / "REFERENCE_REPLAY.json") or not attestation["passed"]:
        raise ValueError("Fixed reference differs from native replay attestation")
    if len(records) != qualification["native_sdk"]["executed_tool_calls"]:
        raise ValueError("Reference execution log is incomplete")
    return task_root, task, reference


def validate_membership(row, saved, task_root, task, plan, cohort):
    if cohort == "stage3":
        seed = task["config"].get("seed")
        if seed != plan["seed"]:
            raise ValueError("Candidate/task seed differs from frozen controller plan")
        if (saved["environment"] != "DataOps" or saved["seed"] != seed or saved["profile"] != plan["profile"]
                or saved["axis"] != "periods" or task["config"]["periods"] != saved["axis_value"]
                or not plan["limits"]["axis_min"] <= saved["axis_value"] <= plan["limits"]["axis_max"]
                or any(task["config"].get(k) != v for k, v in plan["profile"].items())):
            raise ValueError("DataOps candidate is outside the frozen business profile")
    else:
        from .profile import check_profile
        identifier = saved["environment"]
        if identifier not in DOMAINS:
            raise ValueError("Unknown canonical environment")
        profile = plan["profiles"][identifier]
        if (profile["parameters"]["seed"] != plan["seed"] or row["environment"] != identifier
                or check_profile(profile, task_root)["construction"]["status"] != "match"):
            raise ValueError("Canonical candidate differs from its frozen construction profile")


def fresh_outcome(candidate, source):
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TIKTOKEN_CACHE_DIR") if k in os.environ}
    env.update(PYTHONPATH=os.pathsep.join((str(source / "src"), str(source))), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run([sys.executable, str(Path(__file__).with_name("reference_outcome.py")), str(candidate)],
        env=env, cwd=source, capture_output=True, text=True, timeout=120)
    if completed.returncode:
        raise ValueError("Frozen-implementation outcome validation failed: " + completed.stderr[-2000:])
    return json.loads(completed.stdout)


def freeze_row(row, plan, source, cohort):
    source = Path(source)
    resource = row["resource_status"]
    if row.get("accepted") is not True or resource["quiescent"] is not True or resource["failure"] is not None or resource["returncode"] != 0:
        raise ValueError("A failed or uncontained candidate cannot be frozen")
    if source_identity(source) != plan["implementation"]:
        raise ValueError("Original construction source changed")
    candidate = Path(row["directory"])
    saved = load_json(candidate / "CANDIDATE.json")
    if any(row.get(k) != value for k, value in saved.items()):
        raise ValueError("Candidate report differs from its controller result")
    task_root, task, reference = verified_reference(candidate)
    if task_root != Path(saved["task"]) or not task_root.resolve().is_relative_to(candidate.resolve()):
        raise ValueError("Qualified task path differs from the candidate or escapes its directory")
    validate_membership(row, saved, task_root, task, plan, cohort)
    if saved.get("reference_tokens") != reference["reference_tokens"]:
        raise ValueError("Claimed reference length differs from corrected remeasurement")
    if cohort == "stage3":
        if plan["revision"] != "reference_history_bands_v1" or saved["implementation"] != plan["implementation"]:
            raise ValueError("Unsupported Stage 3 construction lineage")
        from skillhorizon.experiment.reference_trace import dataops_policy, validate_trace
        lineage = validate_trace(task_root / "public_qualification/REFERENCE_POLICY.json", initial=task_root / "initial",
            tools=task["tools"], trace=task_root / "public_qualification/runtime/model_skill_actions.jsonl", policy=dataops_policy())
        if sha256_tree(task_root / "checkpoint") != sha256_tree(task_root / "initial"):
            raise ValueError("Stage 3 starting checkpoint differs from its bound initial world")
    elif cohort == "stage4":
        if plan["revision"] != "canonical_history_coverage_v1":
            raise ValueError("Unsupported Stage 4 construction lineage")
        lineage = bound_reference(task_root)
        if load_json(candidate / "episode/REFERENCE_CONTRACT.json") != lineage:
            raise ValueError("Native qualification did not retain the task contract")
        if saved["environment"].startswith("C") and lineage["lineage"]["backend"] != plan["backends"][saved["environment"]]:
            raise ValueError("Reference backend differs from preconstruction plan")
    else:
        raise ValueError("Unknown construction cohort")
    evidence = [candidate / "CANDIDATE.json", candidate / "episode/QUALIFICATION.json",
        candidate / "episode/PLAN.json", candidate / "episode/REFERENCE_REPLAY.json",
        candidate / "episode/fixture/PUBLIC_HISTORY.jsonl", candidate / "episode/fixture/tools.jsonl",
        Path(plan["root"]) / "PLAN.json", Path(plan["root"]) / ("CALIBRATION.json" if cohort == "stage3" else "COVERAGE.json")]
    for relative in ("REFERENCE_CONTRACT.json", "REFERENCE_ORIGIN.json", "public_qualification/REFERENCE_POLICY.json",
                     "public_qualification/QUALIFICATION.json", "public_qualification/runtime/model_skill_actions.jsonl",
                     "reference/model_skill_actions.jsonl", "continuation.json"):
        if (task_root / relative).is_file():
            evidence.append(task_root / relative)
    reference_input = task_root / ("initial" if (task_root / "initial").is_dir() else "checkpoint")
    outcome = fresh_outcome(candidate, source)
    record = {"environment": saved["environment"], "seed": plan["seed"], "construction_cohort": cohort,
        "source_root": str(Path(source).resolve()), "source_identity": plan["implementation"],
        "task_root": str(task_root), "task_id": task["task_id"], "task_digest": sha256_value(task),
        "checkpoint_digest": sha256_tree(task_root / "checkpoint", excluded_directory_names={"__pycache__"}),
        "reference_input_root": str(reference_input),
        "reference_input_digest": sha256_tree(reference_input, excluded_directory_names={"__pycache__"}),
        "task_config": task["config"], "track": "full_online", "reference_tokens": reference["reference_tokens"],
        "reference_K": reference["reference_K"], "band": band_for(reference["reference_tokens"]),
        "reference_calls": reference["tool_calls"], "reference_batch_size": 8, "reference_binding": lineage,
        "fresh_outcome_validation": outcome,
        "evidence": {str(p): sha256_file(p) for p in evidence},
        "remeasurement": {"contract": contract(), "matches_original": True, "original_evidence_modified": False}}
    record["instance_id"] = sha256_value(record)[:20]
    return record


def validate_instance(instance):
    root = Path(instance["task_root"])
    if (sha256_value(load_json(root / "task.json")) != instance["task_digest"]
            or sha256_tree(root / "checkpoint", excluded_directory_names={"__pycache__"}) != instance["checkpoint_digest"]
            or source_identity(instance["source_root"]) != instance["source_identity"]):
        raise ValueError("Frozen task or construction source changed")
    if sha256_tree(Path(instance["reference_input_root"]), excluded_directory_names={"__pycache__"}) != instance["reference_input_digest"]:
        raise ValueError("Frozen reference input changed")
    if "fresh_outcome_validation" in instance:
        from .reference_outcome import semantic_files
        outcome = instance["fresh_outcome_validation"]
        if any(semantic_files(outcome[key]) != outcome["final_semantic_files"] for key in ("runtime", "reference_runtime")):
            raise ValueError("Frozen final business artifacts changed")
    for path, digest in instance["evidence"].items():
        if sha256_file(Path(path)) != digest:
            raise ValueError("Frozen reference evidence changed")
    if sha256_value({k: v for k, v in instance.items() if k != "instance_id"})[:20] != instance["instance_id"]:
        raise ValueError("Frozen instance metadata changed")
    return instance


def freeze(output, dataops, dataops_source, coverage, coverage_source):
    if output.exists():
        raise ValueError("Release output must be fresh")
    data = load_json(dataops / "CALIBRATION.json")
    nine = load_json(coverage / "COVERAGE.json")
    if len(nine["rows"]) != 9 or {r["environment"] for r in nine["rows"]} != set(DOMAINS) or any(r.get("status") == "not_run" for r in nine["rows"]):
        raise ValueError("All nine planned outcomes must be terminal before freezing")
    for root, document, rows in ((dataops, data, data["candidates"]), (coverage, nine, nine["rows"])):
        if load_json(root / "PLAN.json") != document["plan"] or Path(document["plan"]["root"]).resolve() != root.resolve():
            raise ValueError("Controller report plan differs from standalone frozen plan")
        directories = [Path(r["directory"]).resolve() for r in rows]
        if len(set(directories)) != len(directories) or any(p.parent != root.resolve() for p in directories):
            raise ValueError("Candidate directories must be unique and belong to their controller")
        names = ([f'P{r["axis_value"]:02d}' for r in rows] if root == dataops else [r["environment"] for r in rows])
        if [p.name for p in directories] != names or len(set(names)) != len(names):
            raise ValueError("Candidate directory does not match its unique planned axis/domain")
    instances, rejected = [], []
    for cohort, document, rows, source in (("stage3", data, data["candidates"], dataops_source),
                                         ("stage4", nine, nine["rows"], coverage_source)):
        for row in rows:
            if row.get("accepted") is not True:
                rejected.append({"cohort": cohort, "environment": row.get("environment", "DataOps"),
                                 "directory": row["directory"], "resource_status": row["resource_status"]})
                continue
            instances.append(freeze_row(row, document["plan"], source, cohort))
    cells = [{"environment": d, "band": b, "instances": [i["instance_id"] for i in instances if i["environment"] == d and i["band"] == b]}
             for d in ("DataOps", *DOMAINS) for b in BANDS]
    report = {"revision": REVISION, "measurement": contract(), "bands": {str(b): interval(b) for b in BANDS},
              "scope": "construction_development", "instances": instances, "coverage": cells, "rejected": rejected,
              "provider_calls": 0, "actor_policy_target": ACTOR_POLICY,
              "actor_conditions": [], "behavior_results": [],
              "construction_cohorts": {"stage3": str(dataops_source), "stage4": str(coverage_source)}}
    write_json(output, report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--dataops", type=Path, required=True)
    parser.add_argument("--dataops-source", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    parser.add_argument("--coverage-source", type=Path, required=True)
    args = parser.parse_args()
    result = freeze(**vars(args))
    print({"instances": len(result["instances"]), "occupied_cells": sum(bool(c["instances"]) for c in result["coverage"]), "provider_calls": 0})
