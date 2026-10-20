"""Fresh zero-prefix FieldOps construction with explicit public business inputs."""

import asyncio
from copy import deepcopy
from pathlib import Path
import shutil

from ..benchmark_spec import BENCHMARK_REVISION
from ..construction_provenance import construction_entry, observe_call
from ..experiment.mcp_tools import provider_tools_from_mcp_listing, validate_mcp_reference_actions
from ..experiment.project_runtime import project_runtime, project_server
from ..experiment.reference_trace import callable_policy, freeze_task
from ..experiment.tasks import user_message
from ..util import canonical_json, load_json, load_jsonl, write_json
from . import FORMAT, REVISION
from .skills import SCRIPT_SKILLS
from .tasks import _validate_options, execute_reference, initialize_operations, structure_metrics


@construction_entry
def build_declaration_operations(output, *, family="staged_maintenance", seed=7, groups=8,
                                 revision=False, composition="standard", resource_profile="relaxed",
                                 business_program="pressure_branches_v3", business_scenario=None,
                                 device_counts=None, max_calls=6000, service_scale=None,
                                 instance_variant=None, reference_policy="public_demand_cache_v2"):
    if family != "staged_maintenance" or business_program != "pressure_branches_v3":
        raise ValueError("Declaration construction requires FieldOps pressure_branches_v3")
    if type(max_calls) is not int or max_calls < 1:
        raise ValueError("Reference requires a finite positive call limit")
    counts = _validate_options(family, seed, groups, revision, composition, resource_profile,
                               business_program, business_scenario, device_counts, service_scale)
    root = Path(output).resolve()
    if root.exists():
        raise FileExistsError(root)
    config = dict(family=family, seed=seed, groups=groups, revision=revision, composition=composition,
                  resource_profile=resource_profile, business_program=business_program,
                  business_scenario=business_scenario, device_counts=deepcopy(device_counts))
    if service_scale is not None:
        config["service_scale"] = service_scale
    if instance_variant is not None:
        config["instance_variant"] = instance_variant
    protocol = "skill_declaration_v1"
    options = dict(execution_mode="procedural", skill_protocol=protocol, max_calls=max_calls)
    if reference_policy is not None:
        options["reference_policy"] = reference_policy
    root.mkdir(parents=True)
    write_json(root / "BUILD.json", dict(status="building", config=config, external_calls=0,
                                        reference_options=options))
    phase = "initialize"
    qualification = dict(accepted=False, provider_calls=0,
                         scope="zero_prefix_business_reference_only_not_claims_or_harbor_qualification")
    try:
        observe_call(initialize_operations, root / "initial", **{**config, "device_counts": counts},
                     skill_protocol=protocol)
        shutil.copytree(root / "initial", root / "checkpoint")
        shutil.copytree(root / "initial", root / "reference")
        task = _task_description(root, config, counts)
        producer = callable_policy(execute_reference, options)
        reference = project_runtime(task, root / "reference")
        phase = "public_reference"
        stages = execute_reference(reference, **options)
        actions = load_jsonl(root / "reference/model_skill_actions.jsonl")
        phase = "schema_validation"
        schema_check = validate_mcp_reference_actions(task["tools"], actions)
        qualification["schema"] = schema_check
        write_json(root / "QUALIFICATION.json", qualification)
        return _complete_reference(root, task, reference, stages, actions, producer,
                                   qualification, config, options)
    except Exception as error:
        failure = dict(phase=phase, code=getattr(error, "code", "CONSTRUCTION_FAILED"),
                       type=type(error).__name__, detail=str(error))
        qualification.update(accepted=False, failure=failure)
        write_json(root / "QUALIFICATION.json", qualification)
        write_json(root / "BUILD.json", dict(status="failed", config=config, reference_options=options,
                   external_calls=0, failure_type=type(error).__name__, failure=failure,
                   recorded_public_calls=len(load_jsonl(root / "reference/model_skill_actions.jsonl"))))
        raise


def _task_description(root, config, counts):
    """Describe the saved initial world without executing any business action."""
    protocol = "skill_declaration_v1"
    task = dict(format=FORMAT, version=2, benchmark_revision=BENCHMARK_REVISION,
                environment_revision=REVISION, config=config, execution_mode="procedural",
                skill_protocol=protocol, checkpoint_action_count=0,
                history_source="unexecuted_business_initial", static_dag_exposed=False,
                observation_protocol="chronological_ops_summary_query_v2",
                skill_discovery_protocol="public_entrypoint_and_file_inventory_v1")
    initial = project_runtime(task, root / "checkpoint")
    task.update(task_id=initial.project["project_id"], skill_ids=list(initial.skills))
    if counts is not None:
        from .field_layout import REVISION as LAYOUT_REVISION
        task["device_layout_revision"] = initial.project.get("device_layout", {}).get("revision", LAYOUT_REVISION)
    brief = {"project": initial.read_project(), "skill_catalog": [
        {"skill_id": name, "description": spec[1], "script_available": name in SCRIPT_SKILLS,
         "entrypoint": "SKILL.md", "files": ["SKILL.md"] + (
             ["scripts/execute.py", "scripts/calculations.py"] if name in SCRIPT_SKILLS else [])}
        for name, spec in initial.skills.items()],
        "interaction": "Select reusable skills independently. Read public resources and skill files as needed. "
                       "Execute business steps with explicit inputs; declarations do not authorize them."}
    task["messages"] = [user_message("ACTIVE PROJECT. " + canonical_json(brief)),
                        user_message(f"Complete project {task['task_id']} using the available tools.")]

    async def exported_tools():
        from mcp import Client
        async with Client(project_server(task, root / "checkpoint")) as client:
            return provider_tools_from_mcp_listing(await client.list_tools())

    task["tools"] = asyncio.run(exported_tools())
    return task


def _complete_reference(root, task, reference, stages, actions, producer,
                        qualification, config, options):
    # The reference already executed these actions. Score its saved final state;
    # duplicating the entire execution is not a construction requirement.
    score = reference.score()
    qualification["business_state"] = score
    qualification.setdefault("exact_replay", dict(status="not_run", valid=None,
        reason="Full replay is not part of default construction"))
    if not score["success"] or not score["termination_ready"]:
        raise ValueError("Declaration reference did not complete the business goal")
    task.update(reference_tool_call_count=len(actions), reference_skill_count=None, remaining_skill_count=None)
    task["structure"] = structure_metrics(reference, stages)
    task["structure"].update(measurement="public_business_reference_not_minimal_plan_or_declared_skill_runs",
                             reference_business_dispatches=len(actions),
                             programmed_business_stage_count=len(stages),
                             arbitrary_checkpoint_qualification="not_run", declaration_claims_qualification="not_run")
    task["structure"]["programmed_stage_reuse_gap"] = task["structure"].pop("max_data_reuse_gap_skills")
    task["structure"].pop("reference_skill_stages")
    graph = task["structure"]["business_data_graph"]
    for old, new in (("reference_business_skill_data_depth", "programmed_stage_data_depth"),
                     ("intra_skill_artifact_edges", "within_stage_artifact_edges"),
                     ("cross_skill_artifact_edges", "between_stage_artifact_edges"),
                     ("cross_skill_join_stages", "multi_parent_stages")):
        graph[new] = graph.pop(old)
    graph["scope"] = "programmed_public_business_stages_not_declared_runs_or_minimum_plan; physical_constraints_excluded"
    write_json(root / "task.json", task)
    write_json(root / "continuation.json", actions)
    write_json(root / "construction.json", dict(stages=stages, structure=task["structure"], external_calls=0))
    freeze_task(root, task, actions, producer)
    qualification["accepted"] = True
    write_json(root / "QUALIFICATION.json", qualification)
    write_json(root / "BUILD.json", dict(status="complete", config=config, reference_options=options,
               external_calls=0, expert_success=True))
    return task


def recorded_business_stages(actions):
    """Recover stage annotations from a completed declaration reference, without calls."""
    stages, pending = [], None
    for index, action in enumerate(actions):
        if action["skill_id"] not in {"execute_step", "run_skill_script"}:
            continue
        args = action["arguments"]
        name, step = args["skill_id"], args["step_id"]
        if name == "plan-isolation" and step in {"check", "submit"}:
            if pending is None:
                raise ValueError("Incomplete recorded isolation stage")
            if step == "check":
                continue
            stage, pending = pending, None
        else:
            start = index
            if index and actions[index-1]["skill_id"] == "read_skill_file":
                start -= 1
            stage = dict(skill_id=name, arguments=deepcopy(args["arguments"]), action_start=start,
                         measurement="public_policy_business_stage_not_declaration_run")
            if name == "plan-isolation" and step == "draft":
                if pending is not None:
                    raise ValueError("Overlapping recorded isolation stages")
                pending = stage
                continue
        stage.update(artifact_id=action["result"]["artifact_id"], action_end=index+1)
        stages.append(stage)
    if pending is not None:
        raise ValueError("Unfinished recorded isolation stage")
    return stages


def finish_declaration_reference(output):
    """Publish a complete reference whose redundant replay was stopped by the user.

    Load this helper alongside the original business source for interrupted builds.
    No initialization, reference execution, or replay is performed here.
    """
    root = Path(output).resolve()
    build = load_json(root / "BUILD.json")
    qualification = load_json(root / "QUALIFICATION.json")
    if (build["status"] != "building" or not qualification.get("schema", {}).get("passed")
            or qualification.get("exact_replay", {}).get("status") != "stopped_by_user"):
        raise ValueError("Expected a completed reference with user-stopped replay")
    actions = load_jsonl(root / "reference/model_skill_actions.jsonl")
    if not actions or actions[-1]["skill_id"] != "read_project":
        raise ValueError("Reference did not finish its final public observation")
    config, options = build["config"], build["reference_options"]
    task = _task_description(root, config, config.get("device_counts"))
    qualification["completion_method"] = "saved_final_state_and_recorded_actions_no_reexecution"
    task = _complete_reference(root, task, project_runtime(task, root / "reference"),
        recorded_business_stages(actions), actions, callable_policy(execute_reference, options),
        qualification, config, options)
    completed = load_json(root / "BUILD.json")
    completed["metadata_completion"] = dict(method=qualification["completion_method"],
        reference_reexecuted=False, source_file=str(Path(__file__).resolve()),
        original_construction_provenance="unavailable_interrupted_before_wrapper_return")
    write_json(root / "BUILD.json", completed)
    return task
