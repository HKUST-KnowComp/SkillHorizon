"""Prepare, run and replay an isolated native SDK calibration."""

from skillhorizon.host_runtime import require_supported_python, check_python_executable

require_supported_python()

import argparse
import asyncio
from dataclasses import replace
from importlib.metadata import version
import json
from pathlib import Path
import subprocess

from native import EXECUTION_PROTOCOL, NativeStop, run_native, checked_mcp_connection
from witness import Witness, BatchWitness
from protocol import evaluation_track, qualification_passed, episode_outcome, termination_details
from prefix_resume import PrefixResume, validate_parent, BudgetResume, validate_budget_parent
from policy_prefix import policy_config
from skillhorizon.experiment.config import RunConfig
from skillhorizon.experiment.transport import FixtureTransport, HttpTransport, ProviderFailure, DelegatingTransport
from skillhorizon.experiment.route_admission import ADMISSION_POLICY, probe_config, run_admission
from skillhorizon.experiment.provider_contract import recorded_summary_output_metadata
from skillhorizon.experiment.mcp_tools import REFERENCE_SCHEMA_POLICY, validate_mcp_reference_actions
from skillhorizon.experiment.execution_evidence import dispatch_semantics, reconcile_execution
from skillhorizon.util import canonical_json, load_json, load_jsonl, write_json

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv/bin/python"


def command(*args):
    from skillhorizon.experiment.construction_envelope import validate_nested_python
    validate_nested_python(PYTHON)
    check_python_executable(PYTHON)
    result = subprocess.run([str(PYTHON), "-m", "skillhorizon", *map(str, args)], cwd=ROOT,
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def export_for_plan(plan, output):
    from skillhorizon.experiment.construction_envelope import validate_nested_python
    validate_nested_python(PYTHON)
    check_python_executable(PYTHON)
    workspace = plan.get("workspace")
    contract = plan.get("delivery_contract")
    execution = plan.get("python_execution")
    from skillhorizon.experiment.business_binding import normalize_business_binding
    transport = plan.get("business_transport", "inline")
    run_config = RunConfig.from_record(plan["config"]) if execution is not None else None
    binding = normalize_business_binding(ptc=plan.get("ptc"), transport=transport,
        observation_mode=plan.get("skill_documentation", "full") if transport == "managed_worker" else "full",
        workspace=workspace, execution=execution,
        max_actions=(run_config.max_tool_calls or run_config.max_turns) if run_config is not None else None)
    options = ()
    if transport != "inline":
        options += ("--business-transport", transport, "--observation-mode", binding["business_observation_mode"])
    if execution is not None and workspace is None:
        raise ValueError("Python execution requires workspace configuration")
    if contract is not None and workspace is None:
        raise ValueError("Delivery contract requires workspace configuration")
    if workspace is None:
        return command("export-agent", plan["task"], output, *options)
    from skillhorizon.experiment.workspace import normalize_workspace_config
    workspace = normalize_workspace_config(workspace)
    if plan.get("skill_documentation", "full") != "full":
        raise ValueError("Workspace requires full documentation")
    config_file = output.parent / "WORKSPACE_CONFIG.json"
    if config_file.exists() and load_json(config_file) != workspace:
        raise ValueError("Frozen workspace configuration differs")
    if not config_file.exists():
        write_json(config_file, workspace)
    options += ("--delivery-contract", contract) if contract is not None else ()
    if binding["ptc"] is not None:
        ptc_file = output.parent / "PTC_CONFIG.json"
        if ptc_file.exists() and load_json(ptc_file) != binding["ptc"]:
            raise ValueError("Frozen PTC configuration differs")
        if not ptc_file.exists():
            write_json(ptc_file, binding["ptc"])
        options += ("--ptc-config", ptc_file)
    if execution is not None:
        from skillhorizon.experiment.python_execution import normalize_python_execution
        execution = normalize_python_execution(execution, workspace)
        execution_file = output.parent / "PYTHON_EXECUTION_CONFIG.json"
        if execution_file.exists() and load_json(execution_file) != execution:
            raise ValueError("Frozen Python execution configuration differs")
        if not execution_file.exists():
            write_json(execution_file, execution)
        config = RunConfig.from_record(plan["config"])
        options += ("--python-execution-config", execution_file, "--max-actions", config.max_tool_calls or config.max_turns)
    return command("export-agent", plan["task"], output, "--workspace-config", config_file, *options)


def check_plan_workspace(plan, bundle):
    from skillhorizon.experiment.workspace_bundle import workspace_config
    if (bundle / "manifest.json").exists():
        actual = workspace_config(bundle)
        from skillhorizon.experiment.business_binding import business_binding, normalize_business_binding
        transport = plan.get("business_transport", "inline")
        config = RunConfig.from_record(plan["config"])
        expected_binding = normalize_business_binding(ptc=plan.get("ptc"), transport=transport,
            observation_mode=plan.get("skill_documentation", "full") if transport == "managed_worker" else "full",
            workspace=plan.get("workspace"), execution=plan.get("python_execution"),
            max_actions=config.max_tool_calls or config.max_turns)
        if business_binding(bundle) != expected_binding:
            raise ValueError("Business transport or PTC declaration omitted or changed in PLAN")
        from skillhorizon.experiment.execution_bundle import execution_config, preflight_execution
        if canonical_json(execution_config(bundle)) != canonical_json(plan.get("python_execution")):
            raise ValueError("Python execution declaration omitted or changed in PLAN")
        if plan.get("python_execution") is not None:
            config = RunConfig.from_record(plan["config"])
            preflight_execution(bundle, config.max_tool_calls or config.max_turns, check_backend=False)
        if actual != plan.get("workspace"):
            raise ValueError("Workspace declaration omitted or changed in PLAN")
        if load_json(bundle / "manifest.json").get("delivery_contract") != plan.get("delivery_contract"):
            raise ValueError("Delivery declaration omitted or changed in PLAN")
        return actual
    if (plan.get("workspace") is not None or plan.get("python_execution") is not None
            or plan.get("ptc") is not None or plan.get("business_transport", "inline") != "inline"):
        raise ValueError("Workspace qualification bundle is missing")
    return None


def same_bundle(left, right):
    from skillhorizon.experiment.workspace_bundle import workspace_config, tool_kinds
    from skillhorizon.experiment.workspace import workspace_snapshot
    a, b = workspace_config(left), workspace_config(right)
    from skillhorizon.experiment.business_binding import business_binding
    if business_binding(left) != business_binding(right):
        raise ValueError("Business transport or PTC changed since qualification")
    from skillhorizon.experiment.execution_bundle import execution_config
    if (execution_config(left) != execution_config(right)
            or load_json(left / "manifest.json").get("execution_max_actions") != load_json(right / "manifest.json").get("execution_max_actions")):
        raise ValueError("Python execution configuration changed since qualification")
    if a != b or tool_kinds(left) != tool_kinds(right):
        raise ValueError("Workspace configuration changed since qualification")
    if a is not None and workspace_snapshot(left / "workspace_initial", a) != workspace_snapshot(right / "workspace_initial", b):
        raise ValueError("Workspace initial tree changed since qualification")
    for relative in ("public/history.json", "public/task.json", "private_task.json"):
        if load_json(left / relative) != load_json(right / relative):
            raise ValueError("Task changed since native qualification")
    files = [{str(p.relative_to(root / "checkpoint")): p for p in (root / "checkpoint").rglob("*") if p.is_file()}
             for root in (left, right)]
    if files[0].keys() != files[1].keys() or any(files[0][k].read_bytes() != files[1][k].read_bytes() for k in files[0]):
            raise ValueError("Checkpoint changed since native qualification")


def resume_check_config(plan, parent, config):
    parent_plan = load_json(parent / "PLAN.json")
    old = RunConfig.from_record(parent_plan["config"])
    if plan.get("resume_policy") == "fresh_world_recorded_prefix_budget_extension_v1":
        amendment = plan.get("token_budget_amendment")
        expected = {"old_limit": old.max_total_tokens, "new_limit": config.max_total_tokens,
                    "explicit_user_authorization": True}
        if amendment != expected or plan.get("provider_policy_prefix") != parent_plan.get("provider_policy_prefix"):
            raise ValueError("Budget amendment requires an explicit exact historical boundary")
        validate_budget_parent(parent, config)
        return config
    omitted = {"provider_policy", "provider_instruction_template"}
    if ({k: v for k, v in old.to_dict().items() if k not in omitted}
            != {k: v for k, v in config.to_dict().items() if k not in omitted}):
        raise ValueError("Continuation changed original actor configuration or budgets")
    prefix = plan.get("provider_policy_prefix")
    if old.provider_policy == config.provider_policy and old.provider_instruction_template == config.provider_instruction_template:
        if prefix != parent_plan.get("provider_policy_prefix"):
            raise ValueError("Inherited historical policy prefix changed")
        return config
    expected = {"requests": load_json(parent / "api/SUMMARY.json")["requests"],
                "policy": old.provider_policy, "instruction_template": old.provider_instruction_template}
    if (parent_plan.get("provider_policy_prefix") is not None or prefix != expected
            or not plan.get("provider_policy_amendment", {}).get("explicit_user_authorization")):
        raise ValueError("Policy amendment requires an explicit exact historical boundary")
    return policy_config(config, prefix, 1)


class Replay(FixtureTransport):
    replaying = True

    def __init__(self, root):
        self.root = root
        self.events = iter(load_jsonl(root / "attempts.jsonl"))
        self.request_matches = 0

    def send(self, payload):
        event = next(self.events)
        directory = self.root / "turns" / f'{event["turn"]:04d}'
        if load_json(directory / "request.json") != payload:
            raise ValueError("Native replay request changed")
        self.request_matches += 1
        if event["status"] == "provider_error":
            error = load_json(directory / "error.json")
            raise ProviderFailure(error["code"], error["message"], error["details"])
        metadata = recorded_summary_output_metadata(event)
        return load_json(directory / "response.json"), self.description if metadata is None else metadata


class AdmissionBlockedReplay(Replay):
    """Reproduce a recorded zero-execution admission stop before any SDK request."""

    def __init__(self, root, original):
        start = load_json(root / "START_RESULT.json")
        admission = load_json(root / "provider_admission/RESULT.json")
        counts = ("requests", "actor_turns", "compactions", "reported_tokens", "executed_tool_calls",
                  "attempted_tool_calls", "rejected_tool_calls", "protocol_rejected_calls")
        if (original.get("stop_reason") != "provider_admission_blocked"
                or any(type(original.get(k)) is not int or original[k] != 0 for k in counts)
                or original.get("pending_tool_call_ids") != []
                or original.get("evidence_source") != "pending_admission"
                or start.get("status") != "provider_admission_blocked"
                or start.get("model_execution_started") is not False
                or type(start.get("native_provider_requests")) is not int
                or start["native_provider_requests"] != 0
                or admission.get("passed") is not False or start.get("admission") != admission):
            raise ValueError("Admission-blocked replay requires consistent zero-execution evidence")
        api = root / "api"
        logs = ("attempts", "tools", "dispatch_events", "tool_rejections", "protocol_rejections",
                "model_action_failures", "compactions", "retry_renewals")
        if (any(load_jsonl(api / (name + ".jsonl")) for name in logs)
                or ((api / "turns").exists() and any((api / "turns").iterdir()))):
            raise ValueError("Admission-blocked replay contains native execution evidence")
        super().__init__(api)

    def prepare(self):
        raise NativeStop("provider_admission_blocked")

    def send(self, payload):
        raise ValueError("Admission-blocked replay must stop before native request dispatch")


class AdmittedContinuation(DelegatingTransport):
    """Delay network admission until native startup or validated prefix release."""

    def __init__(self, admit):
        self.admit = admit
        self.live = None

    @property
    def description(self):
        return self.live.description if self.live is not None else {"kind": "pending_admission"}

    def prepare(self):
        if self.live is None:
            self.live = self.admit()

    def send(self, payload):
        self.prepare()
        return self.live.send(payload)


def validate_reconstructed_budgets(parent, reconstructed, config):
    """Compare summaries with counts re-derived by the Runner from raw responses."""
    report = load_json(parent / "api/SUMMARY.json")
    events = load_jsonl(reconstructed / "attempts.jsonl")
    counts = {
        "requests": len(events),
        "actor_turns": sum(e["role"] == "actor" and e["accepted"] is True for e in events),
        "reported_tokens": sum(e["reported_tokens"] or 0 for e in events),
        "compactions": len(load_jsonl(reconstructed / "compactions.jsonl")),
        "attempted_tool_calls": sum(e["event"] == "dispatch" for e in load_jsonl(reconstructed / "dispatch_events.jsonl")),
        "executed_tool_calls": len(load_jsonl(reconstructed / "tools.jsonl")),
        "rejected_tool_calls": len(load_jsonl(reconstructed / "tool_rejections.jsonl")),
        "protocol_rejected_calls": len(load_jsonl(reconstructed / "protocol_rejections.jsonl")),
        "max_tool_calls": config.max_tool_calls or config.max_turns,
    }
    if "infrastructure_failures" in report:
        from native import INFRASTRUCTURE_STATUSES
        counts["infrastructure_failures"] = sum(e["status"] in INFRASTRUCTURE_STATUSES for e in events)
    if any(type(report.get(key)) is not int or report[key] != value for key, value in counts.items()):
        raise ValueError("Parent budget summary differs from reconstructed raw evidence")
    from skillhorizon.experiment.business_binding import business_binding
    if business_binding(parent / "api_bundle")["ptc"] is not None:
        from skillhorizon.experiment.execution_evidence import read_business_evidence
        original_business = read_business_evidence(parent / "api_bundle", parent / "api")
        reconstructed_business = read_business_evidence(reconstructed.parent / "api_bundle", reconstructed)
        charge_keys = ("charged_tool_calls", "nested_requests_admitted", "nested_dispatch_intents",
                       "nested_business_dispatched", "nested_dispatch_unknown", "nested_rejections",
                       "bridge_exhaustion_events", "bridge_bytes")
        if (not original_business["accounting_known"] or not reconstructed_business["accounting_known"]
                or any(original_business[key] != reconstructed_business[key] for key in charge_keys)
                or any(report.get(key) != original_business[key] for key in charge_keys)):
            raise ValueError("Parent nested tool budget differs from reconstructed evidence")
    from skillhorizon.experiment.execution_bundle import execution_config, execution_accounting
    if execution_config(parent / "api_bundle") is not None:
        accounting = execution_accounting(parent / "api_bundle")
        original_usage = accounting["execution_usage"]
        reconstructed_usage = execution_accounting(reconstructed.parent / "api_bundle")["execution_usage"]
        if (canonical_json(report.get("execution_usage")) != canonical_json(original_usage)
                or original_usage != reconstructed_usage
                or canonical_json(report.get("execution_runtime")) != canonical_json(accounting["execution_runtime"])):
            raise ValueError("Parent execution budget differs from reconstructed effects")
    old_events = load_jsonl(parent / "api/attempts.jsonl")
    keys = ("turn", "role", "accepted", "status", "reported_tokens", "usage")
    if ([{k: e.get(k) for k in keys} for e in old_events]
            != [{k: e.get(k) for k in keys} for e in events]):
        raise ValueError("Parent attempt accounting differs from reconstructed raw evidence")


def replay_history_validation(original, replayed):
    """Compare independently recounted public lengths, not cached SDK summaries."""
    from skillhorizon.experiment.history_measurement import contract, read_measurement
    result = {"status": "unavailable", "metrics_match": None,
              "original_status": None, "replay_status": None,
              "original_reasons": [], "replay_reasons": []}
    reports = []
    try:
        for label, path in (("original", original), ("replay", replayed)):
            report = read_measurement(path)
            result[label + "_status"] = report["status"]
            result[label + "_reasons"] = report.get("reasons", [])
            reports.append(report)
        if any(r["status"] != "available" for r in reports):
            return result
        metrics = ("message_tokens", "schema_once_tokens", "cumulative_raw_tokens", "actor_suffix_tokens",
                   "generated_summary_tokens", "applied_summary_tokens")
        for report in reports:
            if (any(report.get(k) != v for k, v in contract().items())
                    or report.get("track") not in {"full_online", "reference_prefix_continuation"}
                    or type(report.get("fixture")) is not bool
                    or any(type(report.get(k)) is not int or report[k] < 0 for k in metrics)
                    or not isinstance(report.get("components"), dict)
                    or any(type(v) is not int or v < 0 for v in report["components"].values())):
                result["reason"] = "incomplete_or_incompatible_measurement"
                return result
        if any(reports[0][k] != reports[1][k] for k in ("track", "fixture")):
            result["reason"] = "incomparable_history_origin"
            return result
        matches = all(reports[0][k] == reports[1][k] for k in (*metrics, "components"))
        return {**result, "status": "valid" if matches else "mismatch", "metrics_match": matches}
    except Exception as exc:
        return {**result, "reason": "measurement_error", "exception_type": type(exc).__name__}


def completion_projection(diagnostics):
    """Export only scalar completion observations; never model inputs or run IDs."""
    booleans = ("business_success", "handoff_ready", "all_runs_closed", "open_run_counts_conserved",
                "final_with_handoff_pending", "optional_housekeeping_pending")
    counts = ("open_runs", "terminal_open_runs", "nonterminal_open_runs")
    result = {"status": "unknown", **dict.fromkeys((*booleans, *counts))}
    if not isinstance(diagnostics, dict):
        return result
    if diagnostics.get("revision") == "internal_mcp_native_process_v1":
        return {**result, "status": "unsupported"}
    process = diagnostics.get("business_process")
    state = process.get("completion_state") if isinstance(process, dict) else None
    if not isinstance(state, dict):
        return result
    if (any(state.get(k) is not None and type(state[k]) is not bool for k in booleans)
            or any(state.get(k) is not None and (type(state[k]) is not int or state[k] < 0) for k in counts)):
        return {**result, "reason": "invalid_completion_types"}
    result.update({key: state.get(key) for key in (*booleans, *counts)})
    if all(result[k] is not None for k in (*booleans, *counts)):
        result["status"] = "available"
    return result


def replay(plan, root):
    from skillhorizon.experiment.workspace_bundle import verify_workspace
    original = load_json(root / "api/SUMMARY.json")
    from skillhorizon.experiment.execution_bundle import execution_config, execution_accounting
    if execution_config(root / "api_bundle") is not None:
        accounting = execution_accounting(root / "api_bundle")
        if any(canonical_json(original.get(key)) != canonical_json(value) for key, value in accounting.items()):
            raise ValueError("Execution summary differs from trusted action accounting")
    association = reconcile_execution(root / "api_bundle", root / "api", original)
    files = verify_workspace(root / "api_bundle", root / "api")
    if not association["replay_eligible"] or not files.get("replay_eligible", files["valid"]):
        evidence = {"provider_calls": 0, "passed": False, "status": "not_run",
                    "reason": "Original dispatch effects or actor observations are incomplete", "execution_evidence": association}
        write_json(root / "REPLAY_AUDIT.json", evidence)
        return evidence
    transport = (AdmissionBlockedReplay(root, original)
                 if original.get("stop_reason") == "provider_admission_blocked" and original.get("requests") == 0
                 else Replay(root / "api"))
    export_for_plan(plan, root / "replay_bundle")
    same_bundle(root / "api_bundle", root / "replay_bundle")
    from skillhorizon.experiment.execution_bundle import prepare_execution_replay
    prepare_execution_replay(root / "replay_bundle", root / "api_bundle", "full")
    result = asyncio.run(run_native(root / "replay_bundle", root / "replay",
                                    RunConfig.from_record(plan["config"]), transport,
                                    skill_documentation=plan.get("skill_documentation", "full"),
                                    retry_window_renewals=plan.get("retry_window_renewals", []),
                                    provider_policy_prefix=plan.get("provider_policy_prefix"),
                                    frozen_condition=plan.get("frozen_condition")))
    effects = command("score-environment", root / "replay_bundle")
    replay_files = verify_workspace(root / "replay_bundle", root / "replay")
    same = all(result[k] == original[k] for k in ("stop_reason", "actor_turns", "requests", "compactions",
                                                 "reported_tokens", "executed_tool_calls", "attempted_tool_calls",
                                                 "max_tool_calls", "pending_tool_call_ids", "rejected_tool_calls", "protocol_rejected_calls"))
    if "infrastructure_failures" in original:
        same = same and result["infrastructure_failures"] == original["infrastructure_failures"]
    evidence = {"provider_calls": 0, "all_requests_match": transport.request_matches == original["requests"],
                "original_effects_match": effects == load_json(root / "ENVIRONMENT_RESULT.json"),
                "tool_dispatches_match": load_jsonl(root / "api/tools.jsonl") == load_jsonl(root / "replay/tools.jsonl"),
                "dispatch_events_match": dispatch_semantics(load_jsonl(root / "api/dispatch_events.jsonl")) == dispatch_semantics(load_jsonl(root / "replay/dispatch_events.jsonl")),
                "workspace_effects_match": files == replay_files,
                "tool_rejections_match": load_jsonl(root / "api/tool_rejections.jsonl") == load_jsonl(root / "replay/tool_rejections.jsonl"),
                "protocol_rejections_match": load_jsonl(root / "api/protocol_rejections.jsonl") == load_jsonl(root / "replay/protocol_rejections.jsonl"),
                "model_action_failures_match": load_jsonl(root / "api/model_action_failures.jsonl") == load_jsonl(root / "replay/model_action_failures.jsonl"),
                "compactions_match": load_jsonl(root / "api/compactions.jsonl") == load_jsonl(root / "replay/compactions.jsonl"),
                "retry_renewals_match": load_jsonl(root / "api/retry_renewals.jsonl") == load_jsonl(root / "replay/retry_renewals.jsonl"),
                "summary_matches": same, "effects": effects}
    evidence["passed"] = all(evidence[k] for k in ("all_requests_match", "original_effects_match", "tool_dispatches_match", "dispatch_events_match", "tool_rejections_match", "protocol_rejections_match", "model_action_failures_match", "compactions_match", "retry_renewals_match", "summary_matches")) and effects["valid"]
    evidence["passed"] = evidence["passed"] and evidence["workspace_effects_match"]
    evidence["history_measurement"] = replay_history_validation(root / "api", root / "replay")
    write_json(root / "REPLAY_AUDIT.json", evidence)
    return evidence


def finalize_episode(plan, root, report, metadata):
    """Independent postprocessing failures do not erase a completed actor run."""
    result = {**metadata, "native_sdk": report,
              "environment": {"valid": False, "task_success": None},
              "completion_state": completion_projection(None),
              "history_validation": {"status": "unavailable", "metrics_match": None,
                                     "reason": "replay_not_available"},
              "postprocessing": {}, "trace_replay_passed": False,
              "evaluation_track": plan.get("evaluation_track", "long_history_continuation"),
              "formal_result": False}

    def persist():
        write_json(root / "RESULT.json", result)

    def stage(name, action, *, world=False):
        result["postprocessing"][name] = {"status": "running"}
        persist()
        try:
            if world:
                from skillhorizon.experiment.managed_session import require_world_quiescent
                require_world_quiescent(root / "api_bundle")
            value = action()
        except Exception as exc:
            # Subprocess exceptions can contain private paths or raw outputs.
            result["postprocessing"][name] = {"status": "error", "exception_type": type(exc).__name__}
            persist()
            return None
        result["postprocessing"][name] = {"status": "completed"}
        persist()
        return value

    def score():
        raw = command("score-environment", root / "api_bundle")
        if not isinstance(raw, dict) or type(raw.get("valid")) is not bool or type(raw.get("task_success")) is not bool:
            raise ValueError("Malformed independent score")
        return raw

    raw = stage("effect_verification", score, world=True)
    if raw is not None:
        write_json(root / "ENVIRONMENT_RESULT.json", raw)
    association = stage("execution_reconciliation", lambda: reconcile_execution(root / "api_bundle", root / "api", report), world=True)
    result["execution_evidence"] = association
    known = (isinstance(raw, dict) and raw.get("valid") is True
             and type(raw.get("task_success")) is bool and association is not None
             and association["effect_association_valid"])
    if raw is not None:
        result["environment"] = {**raw, "valid": bool(known), "task_success": raw.get("task_success") if known else None}
    if not known:
        result["environment"]["unknown_reason"] = "Independent business verification or dispatch association is unavailable or invalid"
    from skillhorizon.experiment.workspace_bundle import verify_workspace
    files = stage("workspace_verification", lambda: verify_workspace(root / "api_bundle", root / "api"), world=True)
    result["workspace"] = files
    from skillhorizon.experiment.delivery import collect_delivery_snapshot, score_bundle_delivery, overall_result
    contract = plan.get("delivery_contract")
    snapshot = None
    if contract is not None:
        if files is not None and files.get("replay_eligible", files["valid"]):
            snapshot = stage("delivery_collection", lambda: collect_delivery_snapshot(root / "api_bundle", plan["workspace"]), world=True)
        else:
            snapshot = {"valid": False, "tree": None, "errors": ["EXECUTION_FILE_STATE_UNVERIFIED"]}
            result["postprocessing"]["delivery_collection"] = {"status": "not_run", "reason": "File state is unverified"}
        write_json(root / "DELIVERY_SNAPSHOT.json", snapshot)
    delivery = stage("delivery_scoring", lambda: score_bundle_delivery(root / "api_bundle", contract, snapshot, known), world=contract is not None)
    result["delivery"] = delivery or {"applicable": contract is not None, "valid": False,
                                        "task_success": None, "errors": ["DELIVERY_SCORING_UNAVAILABLE"]}
    result["overall"] = overall_result(result["environment"], result["delivery"])
    observed = known and association["replay_eligible"]
    if observed:
        def diagnose():
            command("diagnose-native", root / "api_bundle", root / "api", root / "PROCESS_DIAGNOSTICS.json")
            return completion_projection(load_json(root / "PROCESS_DIAGNOSTICS.json"))
        completion = stage("diagnostics", diagnose, world=True)
        if completion is not None:
            result["completion_state"] = completion
        if files is not None and files.get("replay_eligible", files["valid"]):
            audit = stage("replay", lambda: replay(plan, root), world=True)
        else:
            audit = None
            result["postprocessing"]["replay"] = {"status": "not_run", "reason": "Workspace evidence is not replayable"}
        result["trace_replay_passed"] = bool(audit and audit.get("passed") is True)
        if audit and "history_measurement" in audit:
            result["history_validation"] = audit["history_measurement"]
    else:
        for name in ("diagnostics", "replay"):
            result["postprocessing"][name] = {"status": "not_run", "reason": "Unverified effects or missing actor observations"}
        write_json(root / "PROCESS_DIAGNOSTICS.json", {"status": "not_run",
                   "reason": "Unverified effects or missing actor observations", "full_process_diagnostics_run": False})
        write_json(root / "REPLAY_AUDIT.json", {"passed": False, "status": "not_run", "provider_calls": 0,
                   "reason": "Do not re-execute a dispatch with unknown effects or invent a missing observation"})
    outcome = stage("outcome", lambda: episode_outcome(report, result["environment"], load_jsonl(root / "api/attempts.jsonl")))
    result["outcome"] = outcome
    result["termination"] = stage("termination", lambda: termination_details(
        report, RunConfig.from_record(plan["config"]), load_jsonl(root / "api/attempts.jsonl")))
    result["postprocessing_complete"] = all(s["status"] == "completed" for s in result["postprocessing"].values())
    persist()
    return result


def main():
    check_python_executable(PYTHON)
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "run", "replay", "resume", "probe"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--task", type=Path)
    parser.add_argument("--parent", type=Path)
    parser.add_argument("--config", type=Path, help="Explicit frozen RunConfig JSON for a new episode")
    parser.add_argument("--frozen-condition", type=Path, help="Prospective history-scale condition; attach during prepare")
    parser.add_argument("--require-compaction", action="store_true", help="Only for compaction-specific qualification")
    parser.add_argument("--inspection-witness", action="store_true", help="Qualification only: inspect each newly produced artifact's remaining public rows")
    parser.add_argument("--reference-batch-size", type=int, choices=range(1, 9), default=1,
                        help="Zero-provider qualification only: execute the recorded reference in ordered batches")
    parser.add_argument("--skill-documentation", choices=("full", "catalog_only"), default="full")
    parser.add_argument("--workspace-config", type=Path)
    parser.add_argument("--python-execution-config", type=Path)
    parser.add_argument("--ptc-config", type=Path)
    parser.add_argument("--business-transport", choices=("inline", "managed_worker"))
    parser.add_argument("--delivery-contract", choices=("procureops_handoff_v1",))
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--renew-exhausted-retries", action="store_true",
                        help="Explicitly authorize one fresh retry window at the exhausted parent request; original global budgets remain")
    parser.add_argument("--max-total-tokens", type=int,
                        help="Explicit cumulative token extension for a clean cost-budget resume only")
    args = parser.parse_args()
    if args.frozen_condition and (args.mode != "prepare" or args.config is None):
        raise ValueError("Attach a frozen condition only during prepare with its explicit configuration")
    if args.workspace_config and (args.mode != "prepare" or args.skill_documentation != "full"):
        raise ValueError("Workspace configuration is only accepted for full-documentation prepare")
    if args.python_execution_config and (args.mode != "prepare" or not args.workspace_config or args.skill_documentation != "full"):
        raise ValueError("Python execution is only accepted for full-documentation prepare with workspace")
    if args.business_transport is not None and args.mode != "prepare":
        raise ValueError("Business transport is frozen at prepare")
    if args.ptc_config and (args.mode != "prepare" or not args.python_execution_config
                           or args.business_transport != "managed_worker" or args.skill_documentation != "full"):
        raise ValueError("PTC requires full prepare, managed_worker and Python execution")
    if args.delivery_contract and (args.mode != "prepare" or not args.workspace_config):
        raise ValueError("Delivery contract is only accepted for prepare with workspace")
    if args.max_total_tokens is not None and (args.mode != "resume" or args.renew_exhausted_retries):
        raise ValueError("Token extension requires a budget-only resume")
    if args.renew_exhausted_retries and args.mode != "resume":
        raise ValueError("Retry renewal requires an explicit resume parent")
    if args.inspection_witness and args.mode != "prepare":
        raise ValueError("Inspection witness is a zero-provider prepare option only")
    if args.reference_batch_size != 1 and (args.mode != "prepare" or args.inspection_witness):
        raise ValueError("Reference batching is a separate zero-provider prepare option")
    root = args.output.resolve()
    if args.mode == "probe":
        if args.parent is None or args.config or args.task:
            raise ValueError("Probe requires one existing episode --parent; settings cannot be overridden")
        plan = load_json(args.parent / "PLAN.json")
        if plan.get("frozen_condition") is not None:
            raise ValueError("Frozen actor conditions require bound run admission; independent probe is disabled")
        if plan.get("workspace") is not None:
            raise ValueError("Workspace probe is not qualified")
        check_plan_workspace(plan, args.parent / "fixture_bundle")
        if plan["provider"] != "https://api.openlux.ai/v1":
            raise ValueError("Provider outside the frozen synthetic scope")
        from skillhorizon.experiment.provider_probe import run_probe, probe_payloads
        from skillhorizon.experiment.context import public_task
        config = RunConfig.from_record(plan["config"])
        public = public_task(load_json(Path(plan["task"]) / "task.json"))
        probe_payloads(config, public["tools"])
        if root.exists():
            raise ValueError("Probe output must be fresh")
        from run_compaction_calibration import credential
        result = run_probe(root, config, HttpTransport(plan["provider"], credential(args.env_file), config), public["tools"])
        print({k: result[k] for k in ("passed", "requests", "known_tokens", "unknown_usage_requests", "tool_calls_executed")})
        return
    if args.mode == "prepare":
        if args.task is None:
            raise ValueError("Explicit synthetic calibration task required")
        root.mkdir(parents=True, exist_ok=False)
        task_root = args.task.resolve()
        frozen_condition = load_json(args.frozen_condition) if args.frozen_condition else None
        config = RunConfig.from_record({"parallel_tool_calls": True, **load_json(args.config)}) if args.config else RunConfig(max_turns=24, max_provider_requests=32, max_total_tokens=2000000,
                           max_compactions=4, retry_unrequested_linkage=True,
                           rate_limit_retries=3, transport_retries=3, parallel_tool_calls=True)
        if config.max_tool_calls is None:
            config = replace(config, max_tool_calls=config.max_turns)
        if args.reference_batch_size > 1 and not config.parallel_tool_calls:
            raise ValueError("Reference batches require the actor's ordered-batch protocol")
        from skillhorizon.experiment.workspace import normalize_workspace_config
        workspace = normalize_workspace_config(load_json(args.workspace_config)) if args.workspace_config else None
        from skillhorizon.experiment.python_execution import normalize_python_execution
        execution = normalize_python_execution(load_json(args.python_execution_config), workspace) if args.python_execution_config else None
        ptc = load_json(args.ptc_config) if args.ptc_config else None
        business_transport = args.business_transport or "inline"
        export_for_plan({"task": str(task_root), "workspace": workspace,
                         "python_execution": execution, "config": config.to_dict(),
                         "ptc": ptc, "business_transport": business_transport,
                         "delivery_contract": args.delivery_contract,
                         "skill_documentation": args.skill_documentation}, root / "fixture_bundle")
        from integrations.benchmark_suite.reference_policy import reference_actions
        actions = reference_actions(task_root)
        reference_binding = None
        if (task_root / "REFERENCE_CONTRACT.json").is_file() and load_json(task_root / "task.json").get("checkpoint_action_count") == 0:
            from integrations.benchmark_suite.reference_policy import bound_reference
            reference_binding = bound_reference(task_root)
        schema_check = validate_mcp_reference_actions(
            load_json(root / "fixture_bundle/public/task.json")["tools"],
            actions)
        write_json(root / "REFERENCE_SCHEMA_CHECK.json", schema_check)
        witness_type = Witness
        if args.inspection_witness:
            if load_json(task_root / "task.json").get("agent_observation_policy") != "inspect_new_artifact_rows_v1":
                raise ValueError("Inspection qualification requires an explicitly labeled task condition")
            from inspection_witness import InspectionWitness
            witness_type = InspectionWitness
        transport = (BatchWitness([actions[i:i + args.reference_batch_size] for i in range(0, len(actions), args.reference_batch_size)])
                     if args.reference_batch_size > 1 else witness_type(actions))
        result = asyncio.run(run_native(root / "fixture_bundle", root / "fixture", config,
                                        transport,
                                        skill_documentation=args.skill_documentation, frozen_condition=frozen_condition))
        effects = command("score-environment", root / "fixture_bundle")
        passed = qualification_passed(result, effects, require_compaction=args.require_compaction)
        from integrations.benchmark_suite.reference_policy import attest_replay
        attestation = (attest_replay(actions, load_jsonl(root / "fixture/tools.jsonl"), result["history_measurement"])
                       if not args.inspection_witness else {"passed": None, "scope": "inspection_policy_not_fixed_reference"})
        write_json(root / "REFERENCE_REPLAY.json", attestation)
        passed = passed and (args.inspection_witness or attestation["passed"])
        policy_path = task_root / "public_qualification/REFERENCE_POLICY.json"
        if policy_path.is_file():
            write_json(root / "REFERENCE_POLICY.json", load_json(policy_path))
        if reference_binding is not None:
            if bound_reference(task_root) != reference_binding:
                raise ValueError("Reference binding changed during native qualification")
            write_json(root / "REFERENCE_CONTRACT.json", reference_binding)
        task = load_json(root / "fixture_bundle/private_task.json")
        plan = {"purpose": "One native SDK synthetic development episode, not formal performance evaluation",
                "protocol": "native_episode_v5", "execution_protocol": EXECUTION_PROTOCOL,
                "local_tool_concurrency": 1, "evaluation_track": evaluation_track(task),
                "reference_schema_policy": REFERENCE_SCHEMA_POLICY,
                "require_compaction": args.require_compaction,
                "qualification_witness": "inspection" if args.inspection_witness else "reference",
                "qualification_evidence": "scripted_execution_fixture",
                "reference_batch_size": args.reference_batch_size,
                "skill_documentation": args.skill_documentation,
                "workspace": workspace,
                "python_execution": execution,
                "ptc": ptc, "business_transport": business_transport,
                "task": str(task_root), "config": config.to_dict(), "provider": "https://api.openlux.ai/v1",
                "framework": "openai-agents", "framework_version": version("openai-agents"),
                "model_adapter": "ControlledModel with original Responses transport and the frozen RunConfig context policy",
                "provider_admission": ADMISSION_POLICY,
                "max_new_provider_requests": config.max_provider_requests + 1, "token_stop_threshold": config.max_total_tokens,
                "cost_note": "Native token stop is post-response, not a currency cap. One separate, bounded admission request is included in max_new_provider_requests and reported outside native usage; missing usage remains unknown.",
                "sends": "Public synthetic history, tool schemas, synthetic MCP results and summaries only",
                "excludes": "Hidden evaluator, reference continuation, local credentials, user filesystem and SDK tracing",
                "native_stop_semantics": "SDK decides to stop on final model output; no evaluator completion feedback"}
        if args.delivery_contract is not None:
            plan["delivery_contract"] = args.delivery_contract
        if frozen_condition is not None:
            plan["frozen_condition"] = frozen_condition
        if task.get("format") == "skillhorizon.internal_mcp_task.v1":
            plan.update(environment_id=task["config"]["environment_id"], environment_format=task["environment_format"],
                        instance_identity=task.get("instance_identity"), scenario=task["config"].get("scenario"),
                        skill_protocol=task["skill_protocol"], evidence_pool="internal_dependency_native_v1")
        write_json(root / "PLAN.json", plan)
        write_json(root / "QUALIFICATION.json", {"passed": passed, "provider_calls": 0,
                   "evidence_kind": "scripted_execution_fixture", "public_policy_qualified": False,
                   "native_sdk": result, "effects": effects})
        print({"qualified": passed, "fixture": result})
        return
    if args.mode == "resume":
        if args.parent is None or root.exists():
            raise ValueError("Resume requires an explicit parent and a fresh output")
        parent = args.parent.resolve()
        plan = load_json(parent / "PLAN.json")
        check_plan_workspace(plan, parent / "api_bundle")
        if (plan.get("execution_protocol") != EXECUTION_PROTOCOL
                or plan.get("provider_admission") != ADMISSION_POLICY
                or plan.get("reference_schema_policy") != REFERENCE_SCHEMA_POLICY):
            raise ValueError("Historical plans are audit-only; new live work requires a fresh admission-aware qualification with public reference-schema validation")
        if args.config or args.task or args.skill_documentation != "full":
            raise ValueError("Resume inherits configuration and documentation; overrides are not accepted")
        config = RunConfig.from_record(plan["config"])
        amendment = {}
        if args.max_total_tokens is not None:
            old_limit = config.max_total_tokens
            config = replace(config, max_total_tokens=args.max_total_tokens)
            events, _ = validate_budget_parent(parent, config)
            amendment = {"config": config.to_dict(), "token_stop_threshold": config.max_total_tokens,
                         "token_budget_amendment": {"old_limit": old_limit,
                             "new_limit": config.max_total_tokens, "explicit_user_authorization": True}}
        else:
            events, _ = validate_parent(parent, config, renew_exhausted_retries=args.renew_exhausted_retries)
        if root.is_relative_to(parent) or parent.is_relative_to(root):
            raise ValueError("Resume must not overlap parent evidence")
        claim = parent.with_name(parent.name + ".resume_claim.json")
        if claim.exists():
            raise ValueError("Parent already has a claimed successor")
        root.mkdir(parents=True)
        write_json(root / "PLAN.json", {**plan, "resume_parent": str(parent),
                                       "max_new_provider_requests": config.max_provider_requests - len(events) + 1,
                                       "renew_exhausted_retries": args.renew_exhausted_retries,
                                       "retry_window_renewals": [*plan.get("retry_window_renewals", []),
                                           *([events[-1]["turn"]] if args.renew_exhausted_retries else [])],
                                       "resume_policy": ("fresh_world_recorded_prefix_budget_extension_v1" if amendment
                                           else "fresh_world_recorded_prefix_transport_retry_v1"), **amendment})
        write_json(root / "QUALIFICATION.json", load_json(parent / "QUALIFICATION.json"))
    plan = load_json(root / "PLAN.json")
    if plan.get("workspace") is not None and plan.get("skill_documentation", "full") != "full":
        raise ValueError("Workspace configuration or recovery is not qualified")
    if not load_json(root / "QUALIFICATION.json")["passed"] or version("openai-agents") != plan["framework_version"]:
        raise ValueError("Qualification or framework version mismatch")
    if args.mode == "replay":
        print({k:v for k,v in replay(plan, root).items() if k != "effects"})
        return
    if (plan.get("execution_protocol") != EXECUTION_PROTOCOL
            or plan.get("provider_admission") != ADMISSION_POLICY
            or plan.get("reference_schema_policy") != REFERENCE_SCHEMA_POLICY):
        raise ValueError("Historical plans are audit-only; new live work requires a fresh admission-aware qualification with public reference-schema validation")
    if (root / "api").exists() or (root / "api_bundle").exists():
        raise ValueError("No implicit repeat of a started API calibration")
    if plan["provider"] != "https://api.openlux.ai/v1":
        raise ValueError("Provider outside the frozen synthetic scope")
    check_plan_workspace(plan, Path(plan["resume_parent"]) / "api_bundle" if "resume_parent" in plan else root / "fixture_bundle")
    export_for_plan(plan, root / "api_bundle")
    parent = Path(plan["resume_parent"]) if "resume_parent" in plan else None
    same_bundle(parent / "api_bundle" if parent else root / "fixture_bundle", root / "api_bundle")
    config = RunConfig.from_record(plan["config"])
    if plan.get("frozen_condition") is not None:
        if plan.get("provider_policy_prefix") is not None or plan.get("retry_window_renewals") or plan.get("renew_exhausted_retries"):
            raise ValueError("Frozen actor condition rejects unbound execution overrides")
        from integrations.benchmark_suite.history_condition import launch_binding
        launch_binding(plan["frozen_condition"], root / "api_bundle", config,
                       skill_documentation=plan.get("skill_documentation", "full"))
        from integrations.benchmark_suite.history_condition import frozen_provider_module
        module = frozen_provider_module(plan["frozen_condition"])
        credential, ProgressTransport = module.credential, module.ProgressTransport
    else:
        from run_compaction_calibration import credential, ProgressTransport
    resume_config = resume_check_config(plan, parent, config) if parent else config
    from skillhorizon.experiment.execution_bundle import preflight_execution, prepare_execution_replay, finish_execution_replay
    checked_mcp_connection(root / "api_bundle")
    if parent:
        prepare_execution_replay(root / "api_bundle", parent / "api_bundle", "prefix")
    preflight_execution(root / "api_bundle", config.max_tool_calls or config.max_turns, check_backend=not bool(parent))
    admission = {"passed": False, "status": "not_run", "provider_calls": 0}

    def admit():
        nonlocal admission
        checked_mcp_connection(root / "api_bundle")
        preflight_execution(root / "api_bundle", config.max_tool_calls or config.max_turns)
        key = credential(args.env_file)
        admission = run_admission(load_json(root / "api_bundle/public/task.json")["tools"], config,
                                  HttpTransport(plan["provider"], key, probe_config(config)), root / "provider_admission")
        if not admission["passed"]:
            write_json(root / "START_RESULT.json", {"status": "provider_admission_blocked", "model_execution_started": False,
                       "native_provider_requests": 0, "admission": admission, "business_success": None})
            raise NativeStop("provider_admission_blocked")
        live_transport = ProgressTransport(plan["provider"], key, config)
        if parent:
            claim = parent.with_name(parent.name + ".resume_claim.json")
            with claim.open("x") as handle:
                json.dump({"successor": str(root)}, handle)
        return live_transport

    if parent or plan.get("business_transport", "inline") == "managed_worker":
        live = AdmittedContinuation(admit)
    else:
        try:
            live = admit()
        except NativeStop:
            print({"status": "provider_admission_blocked", "provider_calls": admission["provider_calls"],
                   "native_provider_requests": 0})
            return
    transport = live
    if parent:
        def validate_prefix():
            from skillhorizon.experiment.workspace_bundle import verify_workspace, workspace_config, workspace_action_records
            from skillhorizon.experiment.workspace import workspace_snapshot
            original_report = load_json(parent / "api/SUMMARY.json")
            finish_execution_replay(root / "api_bundle")
            validate_reconstructed_budgets(parent, root / "api", config)
            association = reconcile_execution(parent / "api_bundle", parent / "api", original_report)
            if not association["replay_eligible"]:
                raise ValueError("Parent effects or observations are not reconstructable")
            for bundle, run in ((parent / "api_bundle", parent / "api"), (root / "api_bundle", root / "api")):
                files = verify_workspace(bundle, run)
                if not files.get("replay_eligible", files["valid"]):
                    raise ValueError("Workspace effects or observations are not reconstructable")
            workspace = workspace_config(parent / "api_bundle")
            if workspace is not None:
                if (workspace_snapshot(parent / "api_bundle/agent_workspace", workspace)
                        != workspace_snapshot(root / "api_bundle/agent_workspace", workspace)
                        or workspace_action_records(parent / "api_bundle")
                        != workspace_action_records(root / "api_bundle")):
                    raise ValueError("Reconstructed workspace differs from interrupted parent")
            for name in ("tools.jsonl", "tool_rejections.jsonl", "protocol_rejections.jsonl", "model_action_failures.jsonl", "compactions.jsonl"):
                if load_jsonl(root / "api" / name) != load_jsonl(parent / "api" / name):
                    raise ValueError("Reconstructed tool batch history differs from the interrupted parent")
            if dispatch_semantics(load_jsonl(root / "api/dispatch_events.jsonl")) != dispatch_semantics(load_jsonl(parent / "api/dispatch_events.jsonl")):
                raise ValueError("Reconstructed dispatch evidence differs from the interrupted parent")
            effects = command("score-environment", root / "api_bundle")
            original_effects = command("score-environment", parent / "api_bundle")
            if (original_effects.get("valid") is not True
                    or original_effects != load_json(parent / "ENVIRONMENT_RESULT.json")
                    or effects != original_effects):
                raise ValueError("Fresh replayed world differs from the interrupted parent")
        if plan.get("resume_policy") == "fresh_world_recorded_prefix_budget_extension_v1":
            transport = BudgetResume(parent, resume_config, live, validate_prefix)
        else:
            transport = PrefixResume(parent, resume_config, live, validate_prefix,
                                     renew_exhausted_retries=plan.get("renew_exhausted_retries", False))
    report = asyncio.run(run_native(root / "api_bundle", root / "api", config, transport,
                                    skill_documentation=plan.get("skill_documentation", "full"),
                                    retry_window_renewals=plan.get("retry_window_renewals", []),
                                    provider_policy_prefix=plan.get("provider_policy_prefix"),
                                    frozen_condition=plan.get("frozen_condition")))
    if parent and not transport.boundary_checked:
        write_json(root / "START_RESULT.json", {"status": "prefix_reconstruction_blocked",
                   "model_execution_started": False, "native_provider_requests": 0,
                   "admission": admission, "business_success": None})
    result = finalize_episode(plan, root, report, {"provider_admission": admission,
                                      "model_execution_started": bool(transport.new_requests) if parent else True,
                                      "wire_requests_this_invocation": admission["provider_calls"] + (transport.new_requests if parent else report["requests"]),
                                      "resumption": ({"parent": str(parent), "new_provider_requests": transport.new_requests,
                                          "renew_exhausted_retries": plan.get("renew_exhausted_retries", False),
                                          "prefix_requests": len(transport.events), "prefix_requests_reconstructed": transport.index,
                                          "prefix_world_verified_before_live": transport.boundary_checked,
                                          "new_reported_tokens": (report["reported_tokens"] - transport.parent_report["reported_tokens"]
                                              if transport.boundary_checked else None),
                                          "note": "Cumulative parent/successor counts overlap; do not sum them. Unknown parent usage remains unknown."} if parent else None),
                                      })
    effects = result["environment"]
    print({"native_sdk": report, "effects_valid": effects["valid"], "task_success": effects["task_success"],
           "trace_replay_passed": result["trace_replay_passed"]})


if __name__ == "__main__":
    main()
