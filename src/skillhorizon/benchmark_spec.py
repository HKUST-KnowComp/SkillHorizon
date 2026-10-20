"""Small, explicit composition split and observable error definitions."""
from .dataflow import FORMAT as DATAFLOW_FORMAT
from .fieldops import FORMAT as OPERATIONS_FORMAT

SPEC_ID = "content_workflows_20260905"
BENCHMARK_REVISION = "content_workflows_20260907_public_contract_v2"
DEVELOPMENT_SEEDS = range(20260905, 20261000)
HELDOUT_SEEDS = range(20270101, 20270200)


def world_group(task):
    config = task["config"]
    # Deliberately group even near-duplicate scale/layout variants of the same source seed.
    domain = "dataops" if task["format"] == DATAFLOW_FORMAT else "operations"
    return f"{domain}:{config['family']}:{config['seed']}"


def assigned_split(task):
    config = task["config"]
    seed = config["seed"]
    if config.get("workspace_profile") == "release_maintenance_v2":
        # This new family has construction checks, not a frozen behavioral heldout split.
        return "construction_qualification"
    if task["format"] == DATAFLOW_FORMAT:
        dev_shape = 2 <= config["regions"] <= 3 and 1 <= config["periods"] <= 2
        held_shape = 4 <= config["regions"] <= 6 and 3 <= config["periods"] <= 6
    elif task["format"] == OPERATIONS_FORMAT:
        dev_shape = 2 <= config["groups"] <= 3 and config.get("composition", "standard") == "standard"
        held_shape = 4 <= config["groups"] <= 6 and config.get("composition") == "coupled"
    else:
        raise ValueError("This spec covers only the new content workflow formats")
    if seed in DEVELOPMENT_SEEDS:
        return "construction_development" if dev_shape else "construction_qualification"
    if seed in HELDOUT_SEEDS and held_shape:
        return "heldout_composition"
    return "out_of_spec"


def validate_manifest(rows):
    groups = {}
    for row in rows:
        expected = assigned_split(row["task"])
        if row["split"] != expected or expected == "out_of_spec":
            raise ValueError("Manifest label does not match the frozen source/composition rule")
        group = world_group(row["task"])
        broad = "heldout" if expected == "heldout_composition" else "development"
        if group in groups and groups[group] != broad:
            raise ValueError("Related worlds/checkpoints cross development and heldout")
        groups[group] = broad
    return {"configuration_count": len(rows), "source_world_groups": len(groups),
            "heldout_world_groups": sum(value == "heldout" for value in groups.values())}


ERROR_LABELS = {
    "UNKNOWN_ARTIFACT": ["object_or_version"], "UNKNOWN_SOURCE": ["object_or_version"],
    "UNKNOWN_OBJECT": ["object_or_version"], "STALE_ARTIFACT": ["object_or_version", "dependency"],
    "OBJECT_MISMATCH": ["object_or_version"], "WORK_PACKAGE_MISMATCH": ["object_or_version", "dependency"],
    "STALE_ASSESSMENT": ["object_or_version", "dependency"], "STALE_PLAN": ["object_or_version", "dependency"],
    "POLICY_SCOPE": ["object_or_version", "business_content"], "POLICY_LIMIT": ["business_content"],
    "SUPERSEDED": ["object_or_version"], "SUPERSEDES": ["object_or_version", "dependency"],
    "RESOLVED": ["business_content"],
    "PERIOD_DEPENDENCY": ["dependency"], "UPSTREAM_DEPENDENCY": ["dependency"],
    "ISOLATION_REQUIRED": ["dependency"], "ACTIVE_SKILL": ["internal_step"],
    "NO_ACTIVE_SKILL": ["internal_step"], "STEP_ORDER": ["internal_step"], "STEP_COMPLETE": ["internal_step"],
    "RUN_INPUTS": ["internal_step", "argument_validation"], "SKILL_INCOMPLETE": ["internal_step"],
    "RESOURCE_CONFLICT": ["resource"], "PART_STOCK": ["resource"], "BUDGET_EXCEEDED": ["resource"],
    "SUPPLY_CAPACITY": ["resource"], "DELIVERY_CAPACITY": ["resource"], "MAINTENANCE_WINDOW": ["resource", "dependency"],
    "CONTENT_MISMATCH": ["business_content"], "UPSTREAM_CONTENT": ["business_content", "dependency"],
    "TEST_NOT_PASSING": ["business_content"], "TEST_LOAD": ["business_content", "argument_validation"],
    "PLAN_INFEASIBLE": ["resource", "internal_step"], "DELIVERY_DEADLINE": ["resource"],
    "MAINTENANCE_TIME": ["resource", "recovery_cost"],
    "CONTRACT_STAGE": ["dependency"], "SETTLEMENT_CHAIN": ["dependency", "object_or_version"],
    "RETENTION": ["dependency", "business_content"], "FUNDING_BINDING": ["dependency", "business_content"],
    "CREDIT_BINDING": ["dependency", "object_or_version"],
    "CREDIT_LEDGER_COVERAGE": ["dependency", "business_content"], "CREDIT_LIMIT": ["business_content", "resource"],
    "LIQUIDITY": ["resource", "business_content"], "STAGE_RECEIPTS": ["dependency", "business_content"],
    "OPEN_COMMITMENTS": ["business_content"],
    "SERVICE_STAGE": ["dependency"], "SERVICE_DEPENDENCY": ["dependency", "object_or_version"],
    "SERVICE_RESTORATIONS": ["dependency", "object_or_version"], "SERVICE_FLOW": ["business_content"],
    "FLOW_CAPACITY": ["resource"], "SERVICE_PRESSURE": ["business_content"], "SERVICE_UNCHANGED": ["business_content"],
    "MATERIAL_INPUT": ["argument_validation"], "MATERIAL_STOCK": ["resource", "business_content"],
    "MATERIAL_GRADE": ["business_content", "dependency"], "MATERIAL_ORIGIN": ["dependency", "object_or_version"],
    "CONDITIONING_CAPACITY": ["resource"], "ALREADY_ASSEMBLED": ["business_content"], "CONTRACT_KIT": ["dependency"],
    "QUANTITY": ["business_content", "argument_validation"], "DELIVERY_COVERAGE": ["business_content"],
    "SYSTEM_COVERAGE": ["business_content"], "AUDIT_COVERAGE": ["business_content", "dependency"],
    "INVALID_INPUT": ["argument_validation", "cause_undetermined"], "ARGUMENTS": ["argument_validation"],
    "UNKNOWN_SKILL": ["unavailable_skill"], "NO_SCRIPT": ["internal_step"],
    "ARGUMENT_SCHEMA": ["argument_validation"], "INVALID_ARGUMENTS": ["argument_validation"],
    "MULTIPLE_CALLS": ["interaction_policy"], "DUPLICATE_CALL_ID": ["interaction_policy"],
}


def observable_diagnostics(events, outcome):
    rows = []
    for event in events:
        for result in event.get("results", []):
            output = result.get("output") or {}
            code = output.get("error", {}).get("code")
            if code:
                rows.append({"turn": event["turn"], "code": code,
                             "labels": ERROR_LABELS.get(code, ["cause_undetermined"])})
    categories = {
        "success": "task_completed", "no_skill_call": "premature_stop_observed",
        "stagnation": "repeated_nonprogress", "turn_budget": "budget_censored",
        "cost_budget": "cost_censored", "request_budget": "infrastructure_or_protocol",
        "infrastructure_budget": "infrastructure_or_protocol", "incomplete_output": "budget_censored",
        "provider_error": "infrastructure_or_protocol", "invalid_response": "infrastructure_or_protocol",
        "invalid_run": "infrastructure_or_protocol", "context_limit": "infrastructure_or_protocol",
        "local_error": "infrastructure_or_protocol", "provider_contract_error": "configuration_conflict",
        "provider_measurement_error": "measurement_conflict", "output_limit": "measurement_conflict",
        "state_verification_failed": "state_verification_failed", "interrupted": "interrupted",
        "incomplete_response": "infrastructure_or_protocol",
    }
    stopping = categories.get(outcome, "unclassified")
    return {"errors": rows, "first_error_turn": rows[0]["turn"] if rows else None, "stopping_category": stopping,
            "skill_selection_error_rate": None, "selection_note": "No expert-next-action matching; successful alternative actions and reads are legal.",
            "litm_attribution": "not_established", "causal_owner": "undetermined_without_trace_review"}


def artifact_reuse_measurements(project, actions, prefix_count, messages=None):
    """Measure executed artifact edges, not gold dependencies or inferred attention."""
    import json
    from statistics import median
    artifacts = project["artifacts"]
    producers, latest_exposure, latest_full = {}, {}, {}
    original_gaps, exposure_gaps, full_gaps = [], [], []
    exposures, reuse_edges = [], []
    bindings, active_runs = [], {}
    reads = {"project_state_reads": 0, "public_content_reads": 0, "public_object_queries": 0}
    consumer_count = 0

    def observe(value, boundary, source):
        if not isinstance(value, dict):
            return
        identifier = value.get("artifact_id") or value.get("table_id")
        if identifier in artifacts and ("rows" in value or "data" in value):
            if "rows" in value and not value["rows"] and value.get("row_count", 0) > 0:
                return
            partial = bool(value.get("rows_truncated")) or value.get("next_offset") is not None
            if "rows" in value:
                partial |= value.get("offset", 0) > 0 or len(value["rows"]) < value.get("row_count", len(value["rows"]))
            latest_exposure[identifier] = boundary
            if not partial:
                latest_full[identifier] = boundary
            exposures.append({"artifact_id": identifier, "tool_call_boundary": boundary,
                              "coverage": "partial" if partial else "full", "source": source})
        # Only known public observation containers, never arbitrary identifier strings.
        if "project" in value:
            observe(value["project"], boundary, source)
        for key in ("active_skill_runs", "active"):
            for run in value.get(key, {}).values():
                observe(run.get("output"), boundary, source + "." + key)

    snapshots = {}
    boundary = 0
    for message in messages or []:
        boundary += int(message.get("type") == "function_call_output")
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, str):
            continue
        for prefix in ("ACTIVE PROJECT. ", "PROJECT STATE SNAPSHOT. "):
            if content.startswith(prefix):
                snapshots.setdefault(boundary, []).append(json.loads(content[len(prefix):]))
    for snapshot in snapshots.get(0, []):
        observe(snapshot, 0, "initial_snapshot")
    for index, action in enumerate(actions):
        output = action["result"]
        name, arguments = action["skill_id"], action.get("arguments", {})
        selected = arguments.get("skill_id") if name == "begin_skill" else active_runs.get(arguments.get("run_id"))
        phase = {"begin_skill": "selection", "revise_skill": "revision", "execute_step": "manual_step"}.get(name, "macro")
        if name not in {"run_skill_script", "finish_skill", "abandon_skill", "read_project", "read_table", "read_artifact", "read_skill_file", "read_object", "search_objects", "object_history", "read_source_version"}:
            selected = selected or name
            chosen = arguments.get("arguments", {}) if name in {"begin_skill", "revise_skill", "execute_step"} else arguments
            def identifiers(value, path=""):
                if isinstance(value, dict):
                    for key, item in value.items():
                        yield from identifiers(item, f"{path}.{key}" if path else key)
                elif isinstance(value, list):
                    for number, item in enumerate(value):
                        yield from identifiers(item, f"{path}[{number}]")
                elif isinstance(value, str) and value in artifacts:
                    yield path, value
            if index >= prefix_count:
                for argument, identifier in identifiers(chosen):
                    bindings.append({"skill_id": selected, "phase": phase, "argument": argument,
                        "artifact_id": identifier, "tool_call_boundary": index + 1,
                        "operation_succeeded": output.get("ok") is True,
                        "producer_age": index + 1 - producers[identifier] if identifier in producers else None,
                        "content_exposure_age": index + 1 - latest_exposure[identifier] if identifier in latest_exposure else None,
                        "full_content_exposure_age": index + 1 - latest_full[identifier] if identifier in latest_full else None})
        if output.get("ok") and name == "begin_skill":
            active_runs[output["run_id"]] = arguments["skill_id"]
        elif output.get("ok") and name in {"finish_skill", "abandon_skill"}:
            active_runs.pop(arguments["run_id"], None)
        if not output.get("ok"):
            for snapshot in snapshots.get(index + 1, []):
                observe(snapshot, index + 1, "handoff_snapshot")
            continue
        if index >= prefix_count:
            reads["project_state_reads"] += int(action["skill_id"] == "read_project")
            reads["public_content_reads"] += int(action["skill_id"] in {"read_artifact", "read_table"})
            reads["public_object_queries"] += int(action["skill_id"] in {"read_object", "search_objects", "object_history", "read_source_version"})
        identifier = output.get("artifact_id")
        if identifier in artifacts and identifier not in producers and action["skill_id"] not in {"read_table", "read_artifact", "read_project"}:
            parents = artifacts[identifier]["inputs"]
            parents = [v if isinstance(v, str) else v["id"] for v in parents
                       if isinstance(v, str) or v["type"] == "artifact"]
            if index >= prefix_count:
                consumer_count += 1
                for parent in parents:
                    edge = {"parent": parent, "consumer": identifier, "tool_call_boundary": index + 1}
                    for label, known, gaps in (("producer_age", producers, original_gaps),
                                               ("content_exposure_age", latest_exposure, exposure_gaps),
                                               ("full_content_exposure_age", latest_full, full_gaps)):
                        age = index + 1 - known[parent] if parent in known else None
                        edge[label] = age
                        if age is not None:
                            gaps.append(age)
                    reuse_edges.append(edge)
            producers[identifier] = index + 1
        observe(output, index + 1, action["skill_id"])
        for snapshot in snapshots.get(index + 1, []):
            observe(snapshot, index + 1, "handoff_snapshot")
    def summarize(values):
        return {"count": len(values), "minimum": min(values) if values else None,
                "median": median(values) if values else None, "maximum": max(values) if values else None}
    return {"scope": "executed_suffix_artifact_edges_including_prefix_producers", "unit": "executed_runtime_calls_including_prefix",
            "produced_artifacts": consumer_count, "producer_age": summarize(original_gaps),
            "latest_output_exposure_age": summarize(exposure_gaps),
            "latest_full_output_exposure_age": summarize(full_gaps),
            "edges_without_observed_content": sum(e["content_exposure_age"] is None for e in reuse_edges),
            "edges_without_full_observed_content": sum(e["full_content_exposure_age"] is None for e in reuse_edges),
            "exposures": exposures, "reuse_edges": reuse_edges, **reads,
            "explicit_input_bindings": bindings,
            "binding_note": "Explicit model-facing argument selections are timed separately from artifact creation. run_skill_script does not itself select its stored input IDs. Failed known-artifact bindings are retained. Neither binding nor a dataflow edge proves attention or memory retrieval.",
            "notes": "A page may expose only part of an artifact. These are output ages, not proof of complete evidence retrieval or attention; current project state may provide an alternative solution."}
