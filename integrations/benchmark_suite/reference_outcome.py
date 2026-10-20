"""Read-only outcome validation, run with the original cohort on PYTHONPATH."""

import argparse
import importlib
import json
from pathlib import Path

from skillhorizon.util import load_json, load_jsonl, sha256_file, sha256_value


def json_outcome(value):
    def unique_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Outcome has colliding JSON object keys")
            result[key] = item
        return result
    return json.loads(json.dumps(value, allow_nan=False), object_pairs_hook=unique_pairs)


def semantic_files(root):
    result = {}
    for path in sorted(Path(root).rglob("*")):
        relative = path.relative_to(root)
        if (not path.is_file() or "environment" in relative.parts or "__pycache__" in relative.parts
                or path.name in {"model_skill_actions.jsonl", "observed_trace.jsonl"}):
            continue
        result[str(relative)] = sha256_value(load_json(path)) if path.suffix == ".json" else sha256_file(path)
    return result


def validate(candidate):
    candidate = Path(candidate)
    episode = candidate / "episode"
    task_root = Path(load_json(episode / "PLAN.json")["task"])
    task = load_json(task_root / "task.json")
    runtime = episode / "fixture_bundle/runtime"
    effects = load_json(episode / "QUALIFICATION.json")["effects"]
    records = load_jsonl(episode / "fixture/tools.jsonl")
    actual = load_jsonl(runtime / "model_skill_actions.jsonl")
    if [(r["skill_id"], r["arguments"], r["result"]) for r in actual] != [(r["name"], r["arguments"], r["output"]) for r in records]:
        raise ValueError("Qualified runtime action log differs from SDK effects")
    if task["format"] == "skillhorizon.dataflow.v2":
        from skillhorizon.dataflow.runtime import DataflowRuntime
        from skillhorizon.dataflow.verification import verify_release_content
        reference = task_root / "public_qualification/runtime"
        outcome = json_outcome(DataflowRuntime(runtime).score())
        content = verify_release_content(runtime)
        if not content["passed"] or content != effects["independent_content_verification"]:
            raise ValueError("Independent DataOps content validation differs")
    elif task["format"] == "skillhorizon.operations.v1":
        from skillhorizon.fieldops.runtime import OperationsRuntime
        reference = task_root / "reference"
        outcome = json_outcome(OperationsRuntime(runtime).score())
    else:
        from skillhorizon.experiment.internal_mcp import domain_entry
        entry = domain_entry(task["config"]["environment_id"], task["environment_format"])
        evaluator = importlib.import_module(entry.get("evaluate_module", entry["module"] + ".evaluate")).evaluate
        reference = candidate / "construction/task/runtime"
        outcome = json_outcome(evaluator(episode / "fixture_bundle/checkpoint/environment", runtime))
        if outcome != effects["domain_evaluation"] or outcome.get("accepted") is not True:
            raise ValueError("Independent C-domain outcome differs from qualified runtime")
    if task["format"] in {"skillhorizon.dataflow.v2", "skillhorizon.operations.v1"}:
        if outcome != effects["state"] or outcome.get("success") is not True:
            raise ValueError("Independent Ops outcome differs from qualified runtime")
    final_files = semantic_files(runtime)
    if final_files != semantic_files(reference):
        raise ValueError("Qualified final artifacts differ from the original public reference")
    return {"passed": True, "outcome": outcome, "final_semantic_files": final_files,
            "runtime": str(runtime), "reference_runtime": str(reference),
            "scope": "Fresh outcome/content validation and original exact-replay evidence verification; no new replay",
            "excluded": ["separately checked action logs", "unchanged embedded environment copy", "bytecode caches"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.candidate)))
