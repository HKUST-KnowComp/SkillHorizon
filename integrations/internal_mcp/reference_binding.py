"""Generation and import lineage for public C-domain reference policies."""

from pathlib import Path

from skillhorizon.experiment.reference_trace import callable_policy, current_callable_policy
from skillhorizon.util import load_json, sha256_file, sha256_tree, sha256_value, write_json
from .io import controller_source


def backend_identity(source):
    source = Path(source)
    return {str(p.relative_to(source)): sha256_file(p)
            for folder in ("mcp_servers", "data") for p in sorted((source / folder).rglob("*"))
            if p.is_file() and p.suffix in {".py", ".json"}}


def inputs(task, tools, policy):
    return {"revision": "c_public_reference_generation_v1", "policy": policy,
            "checkpoint": sha256_tree(task / "checkpoint", excluded_directory_names={"__pycache__"}),
            "public": sha256_tree(task / "public", excluded_directory_names={"__pycache__"}),
            "background": sha256_file(task / "background.json") if (task / "background.json").exists() else None,
            "backend": backend_identity(controller_source(task)), "schema": sha256_value(tools)}


def freeze(task, before, tools, policy, events):
    after = inputs(task, tools, current_callable_policy(policy))
    if before != after:
        raise ValueError("C reference inputs or producer changed during qualification")
    result = {**before, "trace": sha256_value(events)}
    write_json(task / "REFERENCE_GENERATION.json", result)
    return result


def validate(task, tools, events):
    saved = load_json(task / "REFERENCE_GENERATION.json")
    current = {**inputs(task, tools, current_callable_policy(saved["policy"])), "trace": sha256_value(events)}
    if current != saved:
        raise ValueError("C generation binding differs; do not import a stale reference")
    return saved


def imported_lineage(root, tools, actions, policy):
    environment = Path(root) / "checkpoint/environment"
    saved = load_json(Path(root) / "REFERENCE_ORIGIN.json")
    events = [{"name": a["skill_id"], "arguments": a["arguments"], "output": a["result"]} for a in actions]
    actual = {**inputs(environment, tools, policy), "trace": sha256_value(events)}
    if actual != saved:
        raise ValueError("Imported generation lineage differs from retained source evidence")
    return actual
