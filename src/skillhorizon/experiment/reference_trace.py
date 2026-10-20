"""Bind a newly executed public reference to its producer and initial world."""

import inspect
import importlib
from pathlib import Path

from skillhorizon.util import load_json, sha256_file, sha256_tree, sha256_value, write_json

REVISION = "public_reference_trace_v1"


def instruction_dependency(entrypoint):
    affected = {"integrations.logisticsops.network_witness.run",
                "integrations.logisticsops.freight_witness.run",
                "integrations.c01_correspondence.witness.run",
                "integrations.c47_finops.witness.run"}
    if entrypoint not in affected:
        return {}
    from integrations.internal_mcp import reference_reads
    expected = Path(__file__).resolve().parents[3] / "integrations/internal_mcp/reference_reads.py"
    actual = Path(inspect.getfile(reference_reads.ImmutableSkillDocuments)).resolve()
    if actual != expected or not actual.is_file():
        raise ValueError("Instruction reader resolved outside the reference source")
    return {"instruction_reader": {"revision": "immutable_skill_documents_v1",
            "module": "integrations.internal_mcp.reference_reads", "source_digest": sha256_file(actual)}}


def callable_policy(function, options):
    signature = inspect.signature(function)
    bound = signature.bind(None, **options)
    bound.apply_defaults()
    settings = dict(bound.arguments)
    settings.pop(next(iter(signature.parameters)))
    for name, parameter in signature.parameters.items():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            settings.update(settings.pop(name, {}))
    effective = dict(settings)
    delegates = {"integrations.logisticsops.freight_witness": "run_network",
                 "integrations.socialops.reach_witness": "campaign_run"}
    if function.__module__ in delegates and function.__name__ == "run":
        delegate = getattr(importlib.import_module(function.__module__), delegates[function.__module__])
        effective = callable_policy(delegate, settings)["settings"]
        if function.__module__ == "integrations.logisticsops.freight_witness":
            effective["freight"] = "integrations.logisticsops.freight_witness.FreightPolicy"
    package = Path(inspect.getfile(function)).parent
    entrypoint = function.__module__ + "." + function.__qualname__
    return {"entrypoint": entrypoint,
            "settings": settings, "effective_settings": effective,
            **instruction_dependency(entrypoint),
            "source_files": {p.name: sha256_file(p) for p in sorted(package.glob("*.py"))}}


def current_callable_policy(saved):
    module, name = saved["entrypoint"].rsplit(".", 1)
    return callable_policy(getattr(importlib.import_module(module), name), saved["settings"])


def task_binding(root, task, actions, policy, *, lineage=None):
    root = Path(root)
    return {"revision": "bound_reference_task_v1", "policy": policy, "lineage": lineage,
            "task_digest": sha256_value(task), "actions_digest": sha256_value(actions),
            "checkpoint_digest": sha256_tree(root / "checkpoint", excluded_directory_names={"__pycache__"}),
            "track": "full_online" if task.get("checkpoint_action_count") == 0 else "prefix_continuation"}


def freeze_task(root, task, actions, policy, *, lineage=None):
    result = task_binding(root, task, actions, policy, lineage=lineage)
    write_json(Path(root) / "REFERENCE_CONTRACT.json", result)
    if lineage is not None:
        write_json(Path(root) / "REFERENCE_ORIGIN.json", lineage)
    return result


def validate_task(root, task, actions, policy, *, lineage=None):
    saved = load_json(Path(root) / "REFERENCE_CONTRACT.json")
    expected = task_binding(root, task, actions, policy, lineage=lineage)
    if saved != expected:
        raise ValueError("Task reference binding differs; regenerate the reference")
    if saved["track"] != "full_online":
        raise ValueError("Main reference bands require a full-online task")
    return saved


def dataops_policy(*, max_calls=5000, max_rounds=8, page_size=200, skill_protocol="procedural",
                   reference_policy=None):
    from skillhorizon.dataflow.public_witness import PublicCreditWitness
    if skill_protocol not in {"procedural", "skill_declaration_v1"}:
        raise ValueError("Unknown DataOps public reference protocol")
    if reference_policy is None:
        reference_policy = "public_state_cache_v1" if skill_protocol == "skill_declaration_v1" else "public_credit_v1"
    if reference_policy not in {"public_credit_v1", "public_state_cache_v1"}:
        raise ValueError("Unknown DataOps reference policy")
    policy = {"entrypoint": "skillhorizon.dataflow.public_witness.PublicCreditWitness",
            "source_digest": sha256_file(Path(inspect.getfile(PublicCreditWitness))),
            "settings": {"max_calls": max_calls, "max_rounds": max_rounds, "page_size": page_size,
                         **({"skill_protocol": skill_protocol} if skill_protocol != "procedural" else {}),
                         "scripts": "installed optional scripts; immutable text read once",
                         "ordering": "public project scope and deterministic source-defined tie breaking",
                         "source_reads": "metadata plus decision-relevant tables",
                         "freshness": "public current flags and source-change restart"}}
    if reference_policy == "public_state_cache_v1":
        from skillhorizon.dataflow.cached_witness import CachedPublicCreditWitness
        if skill_protocol != "skill_declaration_v1":
            raise ValueError("Cached reference requires run-free explicit steps")
        policy.update(entrypoint="skillhorizon.dataflow.cached_witness.CachedPublicCreditWitness",
                      base_source_digest=policy["source_digest"],
                      source_digest=sha256_file(Path(inspect.getfile(CachedPublicCreditWitness))))
        policy["settings"].update(reference_policy=reference_policy,
            source_reads="observed metadata cached; changed sources refreshed; complete public rows reused",
            freshness="public notices invalidate knowledge; public current-artifact summaries before reuse")
    return policy


def freeze_trace(path, *, initial, tools, trace, policy):
    record = {"revision": REVISION, "policy": policy, "initial_digest": sha256_tree(initial),
              "schema_digest": sha256_value(tools), "trace_digest": sha256_file(trace),
              "scope": "programmed public policy; not a shortest solution or model behavior"}
    write_json(path, record)
    return record


def validate_trace(path, *, initial, tools, trace, policy):
    saved = load_json(path)
    expected = {"revision": REVISION, "policy": policy, "initial_digest": sha256_tree(initial),
                "schema_digest": sha256_value(tools), "trace_digest": sha256_file(trace),
                "scope": "programmed public policy; not a shortest solution or model behavior"}
    if saved != expected:
        raise ValueError("Public reference trace binding differs; regenerate under the frozen policy")
    return saved
