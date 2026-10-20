"""Fixed public reference selection and a small, explicit policy manifest."""

from collections import Counter
from pathlib import Path

from skillhorizon.util import load_json, load_jsonl, sha256_file, canonical_json
from skillhorizon.experiment.history_measurement import contract
from skillhorizon.experiment.reference_trace import dataops_policy, validate_trace

ROOT = Path(__file__).resolve().parents[2]

POLICY_REVISION = "public_reference_policy_v1"


def bound_reference(root):
    from skillhorizon.experiment.reference_trace import current_callable_policy, validate_task
    root = Path(root)
    task = load_json(root / "task.json")
    saved = load_json(root / "REFERENCE_CONTRACT.json")
    policy = (dataops_policy(skill_protocol=task.get("skill_protocol", "procedural"),
                            reference_policy=task.get("reference_policy", "public_credit_v1")) if task["format"] == "skillhorizon.dataflow.v2"
              else current_callable_policy(saved["policy"]))
    actions = reference_actions(root)
    lineage = None
    if task["format"] == "skillhorizon.dataflow.v2":
        lineage = validate_trace(root / "public_qualification/REFERENCE_POLICY.json", initial=root / "initial",
            tools=task["tools"], trace=root / "public_qualification/runtime/model_skill_actions.jsonl", policy=policy)
    elif task.get("format") == "skillhorizon.internal_mcp_task.v1":
        from integrations.internal_mcp.reference_binding import imported_lineage
        lineage = imported_lineage(root, task["tools"], actions, policy)
    return validate_task(root, task, actions, policy, lineage=lineage)


def reference_actions(task_root):
    """Select the actual public-policy trace for full-online funded DataOps."""
    root = Path(task_root)
    task = load_json(root / "task.json")
    if (task.get("format") == "skillhorizon.dataflow.v2"
            and task["config"].get("business_program") == "credit_sales_v2"
            and task.get("checkpoint_action_count") == 0):
        report = load_json(root / "public_qualification/QUALIFICATION.json")
        if report.get("accepted") is not True or report.get("provider_calls") != 0:
            raise ValueError("A qualified public reference is required; no privileged-policy fallback")
        validate_trace(root / "public_qualification/REFERENCE_POLICY.json", initial=root / "initial",
                       tools=task["tools"], trace=root / "public_qualification/runtime/model_skill_actions.jsonl",
                       policy=dataops_policy(skill_protocol=task.get("skill_protocol", "procedural"),
                                             reference_policy=task.get("reference_policy", "public_credit_v1")))
        actions = load_jsonl(root / "public_qualification/runtime/model_skill_actions.jsonl")
        if len(actions) != report["public_calls"] or not actions:
            raise ValueError("Public reference actions are incomplete")
        return actions
    return load_jsonl(root / "reference/model_skill_actions.jsonl") if (
        task.get("checkpoint_action_count") == 0 and task.get("format") == "skillhorizon.operations.v1"
    ) else load_json(root / "continuation.json")


def policy_manifest(identifier, environment_format=None):
    ops_formats = {"DataOps": "skillhorizon.dataflow.v2", "FieldOps": "skillhorizon.operations.v1",
                   "ProcureOps": "skillhorizon.operations.v1"}
    if identifier in ops_formats and environment_format not in (None, ops_formats[identifier]):
        raise ValueError("Reference policy belongs to a different business program")
    if identifier == "DataOps":
        actual = dataops_policy()
        entry = actual["entrypoint"]
        files = [ROOT / "src/skillhorizon/dataflow/public_witness.py"]
        settings = actual["settings"]
    elif identifier in {"FieldOps", "ProcureOps"}:
        entry = "skillhorizon.fieldops.horizon_witness.HorizonWitness"
        package = ROOT / "src/skillhorizon/fieldops"
        files = sorted({*package.glob("*witness*.py"), *package.glob("reference*.py")})
        settings = {"mode": "procedural", "scripts": True, "alternate": False,
                    "supplier_preference": "price", "page_size": 200,
                    "cache": "observed artifacts and terminal closed orders; mutable state re-read"}
    else:
        from .programs import binding
        spec = binding(identifier, environment_format)
        entry = f'integrations.{spec["module"]}.{spec["witness"]}.run'
        package = ROOT / "integrations" / spec["module"]
        files = sorted({*package.glob("*witness*.py"), *package.glob("reference*.py")})
        settings = {"domain_options": spec.get("witness_options", {}),
                    "interface": "public async call callback", "cache": "as implemented in frozen policy source"}
    if not files:
        raise ValueError("Reference policy source is missing")
    from skillhorizon.experiment.reference_trace import instruction_dependency
    return {"revision": POLICY_REVISION, "environment": identifier, "entrypoint": entry,
            **instruction_dependency(entry),
            "settings": settings, "measurement": contract(),
            "ordering": "public scope order and deterministic tie-breaking in the bound policy source",
            "source_files": {str(p.relative_to(ROOT)): sha256_file(p) for p in files},
            "claims": {"public_observations": True, "programmed_domain_knowledge": True,
                       "shortest_solution": False, "model_behavior": False}}


def attest_replay(actions, actual, history):
    expected = [(a["skill_id"], a["arguments"], a["result"]) for a in actions]
    observed = [(a["name"], a["arguments"], a["output"]) for a in actual]
    passed = expected == observed and history.get("status") == "available"
    return {"passed": passed, "expected_calls": len(expected), "observed_calls": len(observed),
            "ordered_calls_arguments_results_equal": expected == observed,
            "call_id_mapping": [{"reference_index": i, "sdk_call_id": row["call_id"]} for i, row in enumerate(actual)],
            "history_available": history.get("status") == "available"}


def repeat_read_audit(records):
    """Flag identical read outputs without assuming a freshness check is useless."""
    seen, repeated, docs = set(), [], Counter()
    for index, row in enumerate(records):
        name = row.get("skill_id", row.get("name"))
        if not name.startswith(("read_", "search_", "list_", "get_")):
            continue
        args, result = row["arguments"], row.get("result", row.get("output"))
        key = canonical_json([name, args, result])
        if key in seen:
            repeated.append({"index": index, "name": name, "arguments": args})
        seen.add(key)
        if name == "read_skill_file":
            docs[canonical_json(args)] += 1
    return {"identical_repeat_reads": repeated, "count": len(repeated),
            "repeated_immutable_skill_reads": sum(max(0, count-1) for count in docs.values()),
            "interpretation": "Review candidates; mutable-state freshness checks can legitimately return unchanged data"}
