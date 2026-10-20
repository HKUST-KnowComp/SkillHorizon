"""Current content-workflow scoring; historical DAG formats are not migrated."""

from pathlib import Path

from ..benchmark_spec import BENCHMARK_REVISION
from ..util import canonical_json, load_json
from .execution import RUN_VERSION
from .project_runtime import PROJECT_FORMATS


def score_run(run_root):
    root = Path(run_root)
    run = load_json(root / "run.json")
    task = load_json(root / "task.json")
    if run.get("version") != RUN_VERSION:
        raise ValueError("Obsolete run format: retain the original result and use its archived scorer; no automatic migration")
    if task.get("format") not in PROJECT_FORMATS or task.get("benchmark_revision") != BENCHMARK_REVISION:
        raise ValueError("Obsolete benchmark task: retain its original evidence and rebuild a current content workflow")
    if not isinstance(run.get("run_id"), str) or not run["run_id"]:
        raise ValueError("Run has no attempt identity")
    from .semantic_scoring import score_project_run
    return score_project_run(root)


def summarize_runs(roots: list[Path]) -> dict:
    roots = [Path(root).resolve() for root in roots]
    if len(roots) != len(set(roots)):
        raise ValueError("The same run path cannot be counted more than once")
    records = [load_json(root / "run.json") for root in roots]
    identities = [record["run_id"] for record in records]
    if len(identities) != len(set(identities)):
        raise ValueError("Copies of the same attempt cannot be counted as independent runs")
    claimed = set()
    for record in records:
        lineage = {record["run_id"]}
        if record.get("resume"):
            lineage.add(record["resume"]["parent_run_id"])
        if lineage & claimed:
            raise ValueError("Overlapping parent/resumed trajectories cannot count as independent runs")
        claimed.update(lineage)
    reports = [score_run(root) for root in roots]
    excluded_outcomes = {
        "interrupted",
        "provider_error",
        "provider_contract_error",
        "provider_measurement_error",
        "invalid_response",
        "local_error",
        "context_limit",
        "output_limit",
        "incomplete_output",
        "incomplete_response",
        "turn_budget",
        "cost_budget",
        "tool_budget",
        "request_budget",
        "infrastructure_budget",
    }
    observed = [
        r
        for r in reports
        if r["valid"]
        and r["evidence_source"] == "external"
        and r["outcome"] not in excluded_outcomes
        and not r["provider_checks"].get("continued_configuration_warning_responses", 0)
    ]
    bounded_outcomes = {"success", "state_verification_failed", "no_skill_call", "stagnation",
                        "turn_budget", "incomplete_output"}
    bounded = [r for r in reports if r["valid"] and r["evidence_source"] == "external"
               and r["outcome"] in bounded_outcomes
               and not r["provider_checks"].get("continued_configuration_warning_responses", 0)]
    outcomes: dict[str, int] = {}
    external_outcomes: dict[str, int] = {}
    for row in reports:
        outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
        if row["evidence_source"] == "external":
            external_outcomes[row["outcome"]] = external_outcomes.get(row["outcome"], 0) + 1
    conditions = {}
    for root, report, record in zip(roots, reports, records):
        task = load_json(root / "task.json")
        condition = {
            "task_format": task["format"], "benchmark_revision": task.get("benchmark_revision"),
            "scope": report.get("benchmark_scope"), "source": report["evidence_source"],
            "task_configuration": {k: v for k, v in task["config"].items() if k != "seed"},
            "run_configuration": record["config"], "provider_route": record["transport"].get("base_url"),
            "history_policy": task["history_source"],
            "nominal_observation_tokens": task.get("observation_intervention", {}).get("nominal_local_tokens"),
            "track": report.get("evaluation_track", "unverified"),
        }
        key = canonical_json(condition)
        group = conditions.setdefault(key, {"condition": condition, "reports": [], "worlds": set()})
        group["reports"].append(report)
        group["worlds"].add(report.get("source_world_group", task["task_id"]))
    strata = []
    for group in conditions.values():
        assigned = [r for r in group["reports"] if r["evidence_source"] == "external"]
        endpoints = [r for r in assigned if r in observed]
        limited = [r for r in assigned if r in bounded]
        strata.append({"condition": group["condition"], "attempt_count": len(group["reports"]),
            "source_world_count": len(group["worlds"]), "external_assigned_count": len(assigned),
            "budget_bounded_attempt_count": len(limited),
            "cost_censored_attempt_count": sum(r["outcome"] == "cost_budget" for r in assigned),
            "infrastructure_censored_attempt_count": sum(r["outcome"] in {"request_budget", "infrastructure_budget", "provider_error", "provider_contract_error", "provider_measurement_error", "invalid_response", "local_error"} for r in assigned),
            "task_success_count_within_run_limits": sum(r["task_success"] for r in limited),
            "business_success_with_open_runs_count": sum(r["task_success"] and not r["lifecycle_complete"] for r in limited),
            "business_success_pending_terminal_close_count": sum(r["task_success"] and not r["termination_ready"] for r in limited),
            "task_success_rate_within_run_limits": sum(r["task_success"] for r in limited) / len(limited) if limited else None,
            "interpretable_endpoint_count": len(endpoints),
            "observed_success_count": sum(r["task_success"] for r in endpoints),
            "success_rate_among_interpretable_endpoints": sum(r["task_success"] for r in endpoints) / len(endpoints) if endpoints else None,
            "outcomes": {outcome: sum(r["outcome"] == outcome for r in group["reports"])
                         for outcome in sorted({r["outcome"] for r in group["reports"]})}})
    one_external_condition = sum(s["external_assigned_count"] > 0 for s in strata) == 1
    return {
        "run_count": len(reports),
        "fixture_run_count": sum(r["evidence_source"] == "fixture" for r in reports),
        "invalid_run_count": sum(not r["valid"] for r in reports),
        "external_interpretable_endpoint_count": len(observed),
        "external_budget_bounded_attempt_count": len(bounded),
        "external_observed_serial_warning_count": sum(r["valid"] and r["evidence_source"] == "external" and r["provider_checks"].get("evidence_class") == "observed_serial_with_configuration_warning" for r in reports),
        "external_fixed_instruction_template_count": sum(r["valid"] and r["evidence_source"] == "external" and r["provider_checks"].get("evidence_class") == "fixed_instruction_template_with_unverified_injection" for r in reports),
        "external_allowlisted_instruction_template_count": sum(r["valid"] and r["evidence_source"] == "external" and r["provider_checks"].get("evidence_class") == "allowlisted_instruction_template_with_unverified_injection" for r in reports),
        "external_openlux_declared_image_serial_count": sum(r["valid"] and r["evidence_source"] == "external" and r["provider_checks"].get("evidence_class") == "openlux_declared_image_observed_serial" for r in reports),
        "external_assigned_count": sum(external_outcomes.values()),
        "external_outcomes": external_outcomes,
        "external_task_success_count_within_run_limits": sum(r["task_success"] for r in bounded),
        "external_business_success_with_open_runs_count": sum(r["task_success"] and not r["lifecycle_complete"] for r in bounded),
        "external_task_success_rate_within_run_limits": sum(r["task_success"] for r in bounded) / len(bounded)
        if bounded and one_external_condition else None,
        "external_success_rate_among_interpretable_endpoints": sum(r["task_success"] for r in observed)
        / len(observed)
        if observed and one_external_condition
        else None,
        "condition_strata": strata,
        "aggregation_note": "No pooled success rate across heterogeneous conditions. The interaction-bounded rate includes usable-turn exhaustion and max-output truncation, but excludes cost, total-request and infrastructure-cap censoring. Rejected response spend remains recorded. Report all assigned outcomes and censoring counts alongside this conditional rate; it is not an unconditional success estimate. Business success, terminal-run closure and all-run cleanup are separate. Repeated seeds are not independent source worlds.",
        "outcomes_all_sources": outcomes,
        "litm_inference": "not_established_by_aggregate_success_rates",
        "runs": reports,
    }
