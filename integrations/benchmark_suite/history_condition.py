"""Budget-explicit actor conditions and identity-checked offline reporting."""

import argparse
from pathlib import Path

from skillhorizon.experiment.config import RunConfig
from skillhorizon.experiment.history_measurement import read_measurement, strict_rows, tokens
from skillhorizon.util import load_json, sha256_file, sha256_tree, sha256_value, write_json
from .history_release import ACTOR_POLICY, validate_instance, source_identity

ROOT = Path(__file__).resolve().parents[2]
BUDGETS = ("max_turns", "max_tool_calls", "max_provider_requests", "max_total_tokens")


def actor_source_identity(root):
    return {**source_identity(root), "provider_script": sha256_file(Path(root) / "scripts/run_compaction_calibration.py")}


def frozen_provider_module(condition):
    import importlib.util
    import sys
    validate_condition(condition)
    name = "run_compaction_calibration"
    expected = ROOT / "scripts" / (name + ".py")
    cached = sys.modules.get(name)
    if cached is not None and Path(getattr(cached, "__file__", "")).resolve() != expected.resolve():
        raise ValueError("Cached provider module differs from frozen source")
    selected = importlib.util.find_spec(name)
    if selected is None or not selected.origin or Path(selected.origin).resolve() != expected.resolve():
        raise ValueError("Provider module resolution differs from frozen source")
    spec = importlib.util.spec_from_file_location("_skillhorizon_frozen_provider", expected)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolved_config(record):
    if any(type(record.get(key)) is not int or record[key] < 1 for key in BUDGETS):
        raise ValueError("All four positive actor budgets must be explicit before condition binding")
    config = RunConfig.from_record(record).to_dict()
    if any(config[key] != value for key, value in ACTOR_POLICY.items()):
        raise ValueError("Actor configuration differs from the fixed history-scale policy")
    return config


def bind(instance, config):
    validate_instance(instance)
    condition = {"revision": "history_actor_condition_v1", "instance": instance,
                 "config": resolved_config(config), "actor_source_identity": actor_source_identity(ROOT),
                 "mcp_python": {"path": str(ROOT / ".venv/bin/python"),
                                "resolved_binary": str((ROOT / ".venv/bin/python").resolve()),
                                "digest": sha256_file(ROOT / ".venv/bin/python")},
                 "interface": {"skill_documentation": "full", "workspace": None,
                               "python_execution": None, "ptc": None, "business_transport": "inline"},
                 "provider_calls": 0, "execution_authorized_by_binding": False}
    condition["condition_id"] = sha256_value(condition)
    return condition


def validate_condition(condition):
    if condition["condition_id"] != sha256_value({k: v for k, v in condition.items() if k != "condition_id"}):
        raise ValueError("Frozen condition changed")
    if resolved_config(condition["config"]) != condition["config"] or actor_source_identity(ROOT) != condition["actor_source_identity"]:
        raise ValueError("Frozen actor configuration or implementation changed")
    validate_instance(condition["instance"])
    return condition


def launch_binding(condition, bundle, config, *, skill_documentation="full"):
    validate_condition(condition)
    bundle = Path(bundle)
    instance = condition["instance"]
    task = load_json(bundle / "private_task.json")
    if (config.to_dict() != condition["config"] or skill_documentation != "full"
            or sha256_value(task) != instance["task_digest"]
            or sha256_tree(bundle / "checkpoint", excluded_directory_names={"__pycache__"}) != instance["checkpoint_digest"]):
        raise ValueError("Actor launch differs from frozen condition or task")
    public = load_json(bundle / "public/task.json")
    if (public["tools"] != task["tools"] or load_json(bundle / "public/history.json") != task["messages"]
            or public.get("business_transport") != "inline"
            or any(public.get(k) is not None for k in ("workspace", "python_execution", "ptc", "delivery_contract"))):
        raise ValueError("Actor interface differs from the frozen MCP-only condition")
    validate_mcp_route(condition, bundle, task)
    return {"condition_id": condition["condition_id"], "actor_source_identity": condition["actor_source_identity"]}


def validate_mcp_route(condition, bundle, task):
    from skillhorizon.experiment.internal_mcp import FORMAT
    servers = load_json(bundle / "mcp.json")["mcpServers"]
    if set(servers) != {"skillhorizon"}:
        raise ValueError("Unexpected MCP server route")
    route = servers["skillhorizon"]
    python = Path(route["command"])
    expected = condition["mcp_python"]
    if (not python.is_absolute() or str(python) != expected["path"]
            or str(python.resolve()) != expected["resolved_binary"]
            or sha256_file(python) != expected["digest"] or set(route) - {"command", "args", "cwd"}):
        raise ValueError("MCP executable or environment override differs from frozen route")
    internal = task.get("format") == FORMAT
    args = (["-m", "integrations.internal_mcp.framework", "serve", str(bundle), str(bundle / "runtime")]
            if internal else ["-m", "skillhorizon.experiment.mcp_server", str(bundle), str(bundle / "runtime"), "--bundle"])
    if route["args"] != args or route.get("cwd") != (str(ROOT) if internal else None):
        raise ValueError("MCP module, working directory or bundle target differs from frozen route")


def length_report(instance, history, rows):
    if history.get("status") != "available":
        peak = None
    else:
        schema = tokens(rows[0]["tools"])
        events = {r["seq"]: r["message"] for r in rows if r["kind"] in {"admission", "summary"}}
        peak = max((schema + sum(tokens(events[i]) for i in r["event_ids"] if events[i] is not None)
                    for r in rows if r["kind"] == "exposure" and r["role"] == "actor"), default=None)
    return {"reference_tokens": instance["reference_tokens"], "task_band": instance["band"],
            "agent_raw_tokens": history.get("cumulative_raw_tokens"),
            "agent_history_status": history["status"], "peak_public_actor_exposure_tokens": peak,
            "peak_scope": "Locally serialized public exposure; not provider wire token accounting",
            "applied_summary_tokens": history.get("applied_summary_tokens"),
            "generated_summary_tokens": history.get("generated_summary_tokens")}


def validated_history(run):
    path = Path(run) / "PUBLIC_HISTORY.jsonl"
    seen = set()
    while path not in seen:
        seen.add(path)
        rows = strict_rows(path)
        source = rows[-1].get("provenance", {}) if rows else {}
        if source.get("mode") in {None, "root"} or not source.get("path"):
            break
        path = Path(source["path"])
    history = read_measurement(run)
    if history["status"] != "available":
        raise ValueError("Present-invalid public history: " + ", ".join(history.get("reasons", [])))
    return history, strict_rows(Path(run) / "PUBLIC_HISTORY.jsonl")


def report(condition, run, output):
    if output.exists():
        raise ValueError("Report output must be fresh")
    run = Path(run)
    if condition["condition_id"] != sha256_value({k: v for k, v in condition.items() if k != "condition_id"}):
        raise ValueError("Frozen condition changed")
    base = {"revision": "history_actor_report_v1", "condition_id": condition["condition_id"],
            "instance_id": condition["instance"]["instance_id"], "config": condition["config"]}
    def unavailable(error):
        result = {**base, "availability": {"status": "unavailable", "reason": "missing_required_evidence", "file": str(error)},
            "lengths": length_report(condition["instance"], {"status": "unavailable", "cumulative_raw_tokens": None}, []),
            "business": None, "infrastructure": None, "execution": None, "skill_observability": None}
        write_json(output, result)
        return result
    try:
        metadata = load_json(run / "run.json")
        bundle = Path(metadata["bundle"])
        binding = launch_binding(condition, bundle, RunConfig.from_record(metadata["config"]),
                                 skill_documentation=metadata["skill_documentation"])
    except FileNotFoundError as error:
        return unavailable(error)
    if metadata.get("frozen_condition") != binding or metadata.get("local_tool_concurrency") != 1:
        raise ValueError("Run has no matching prospective frozen-condition binding")
    if metadata["transport"].get("kind") != "external":
        raise ValueError("Report requires an external actor run, not a replay or scripted fixture")
    try:
        history, rows = validated_history(run)
        if history.get("fixture") is True:
            raise ValueError("A scripted reference fixture is not an actor result")
        summary = load_json(run / "SUMMARY.json")
        from .history_join import verify
        verify(run, bundle, rows)
        from skillhorizon.experiment.native_diagnostics import diagnose_native
        diagnostics_path = output.with_suffix(".diagnostics.json")
        diagnose_native(bundle, run, diagnostics_path)
    except FileNotFoundError as error:
        return unavailable(error)
    diagnostics = load_json(diagnostics_path)
    common = diagnostics["benchmark_measurements"]
    result = {**base, "availability": {"status": "available"},
        "lengths": length_report(condition["instance"], history, rows),
        "compactions": summary["compactions"], "skill_observability": common["skill_observability"],
        "selection": common["selection"], "execution": common["execution"], "lifecycle": common["lifecycle"],
        "business": common["business"], "infrastructure": common["infrastructure"],
        "evidence_kind": metadata["transport"], "diagnostics": str(diagnostics_path),
        "behavior_note": "Accepted interface calls are not automatically correct skill choices; no LITM conclusion is inferred"}
    write_json(output, result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    create = sub.add_parser("bind")
    create.add_argument("release", type=Path)
    create.add_argument("instance_id")
    create.add_argument("config", type=Path)
    create.add_argument("output", type=Path)
    show = sub.add_parser("report")
    show.add_argument("condition", type=Path)
    show.add_argument("run", type=Path)
    show.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.mode == "bind":
        if args.output.exists():
            raise ValueError("Condition output must be fresh")
        instance = next(r for r in load_json(args.release)["instances"] if r["instance_id"] == args.instance_id)
        write_json(args.output, bind(instance, load_json(args.config)))
    else:
        report(load_json(args.condition), args.run, args.output)
