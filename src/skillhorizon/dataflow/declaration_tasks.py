"""Fresh, zero-prefix DataOps tasks using run-free public business steps."""

import asyncio
from pathlib import Path
import shutil

from ..benchmark_spec import BENCHMARK_REVISION
from ..construction_provenance import construction_entry, observe_call
from ..experiment.mcp_tools import provider_tools_from_mcp_listing
from ..experiment.project_runtime import project_runtime, project_server
from ..experiment.reference_trace import dataops_policy, freeze_task
from ..experiment.tasks import user_message
from ..util import canonical_json, load_json, load_jsonl, write_json
from . import FORMAT
from .public_qualification import qualify_public_credit
from .support import environment_support
from .tasks import initialize_dataflow


@construction_entry
def build_declaration_dataflow(output, *, seed=20260905, regions=3, periods=2, rows_per_region=12,
                               revision=False, family="regional_sales", event_kind=None,
                               event_trigger="release_review_ready", event_region=1, event_period=1,
                               notice_policy="managed", event_program=None,
                               business_program="credit_sales_v2", business_scenario=None, replay_mode="full",
                               credit_event_customer=None, credit_event_trigger_period=None,
                               reference_policy="public_state_cache_v1"):
    if business_program != "credit_sales_v2":
        raise ValueError("Declaration construction currently supports credit_sales_v2 only")
    if reference_policy not in {"public_credit_v1", "public_state_cache_v1"}:
        raise ValueError("Unknown DataOps reference policy")
    root = Path(output).resolve()
    if root.exists():
        raise FileExistsError(root)
    config = dict(seed=seed, regions=regions, periods=periods, rows_per_region=rows_per_region,
                  revision=revision, family=family, event_kind=event_kind, event_trigger=event_trigger,
                  event_region=event_region, event_period=event_period, notice_policy=notice_policy,
                  event_program=event_program, business_program=business_program, business_scenario=business_scenario,
                  credit_event_customer=credit_event_customer, credit_event_trigger_period=credit_event_trigger_period)
    protocol = "skill_declaration_v1"
    root.mkdir(parents=True)
    write_json(root / "BUILD.json", {"status": "building", "config": config, "external_calls": 0})
    try:
        observe_call(initialize_dataflow, root / "initial", **config, skill_protocol=protocol)
        shutil.copytree(root / "initial", root / "checkpoint")
        task = {"format": FORMAT, "version": 2, "benchmark_revision": BENCHMARK_REVISION,
                "config": config, "skill_protocol": protocol, "execution_mode": "procedural",
                "checkpoint_action_count": 0, "history_source": "unexecuted_business_initial",
                "static_dag_exposed": False, "messages": [],
                "observation_protocol": "chronological_summary_query_v2",
                "feedback_protocol": "current_at_observation_v1",
                "skill_discovery_protocol": "public_entrypoint_and_file_inventory_v1",
                "environment_support": environment_support(notice_policy)}
        if reference_policy != "public_credit_v1":
            task["reference_policy"] = reference_policy
        initial = project_runtime(task, root / "checkpoint")
        task["task_id"] = initial.project["project_id"]
        task["skill_ids"] = list(initial.skills)
        brief = {"project": initial.read_project(), "skill_catalog": [
            {"skill_id": name, "description": skill["description"], "script_available": skill["script"],
             "entrypoint": "SKILL.md", "files": ["SKILL.md"] + (
                 ["scripts/execute.py", "scripts/table_ops.py"] if skill["script"] else [])}
            for name, skill in initial.skills.items()],
            "interaction": "Select reusable skills independently. Read public tables and skill files as needed. "
                           "Execute business steps with explicit inputs; declarations do not bind or authorize them."}
        task["messages"] = [user_message("ACTIVE PROJECT. " + canonical_json(brief)),
                            user_message(f"Complete project {task['task_id']} using the available tools.")]

        async def tools():
            from mcp import Client
            async with Client(project_server(task, root / "checkpoint")) as client:
                return provider_tools_from_mcp_listing(await client.list_tools())

        task["tools"] = asyncio.run(tools())
        report = qualify_public_credit(root / "initial", root / "public_qualification", task=task,
                                       tools=task["tools"], replay_mode=replay_mode,
                                       reference_policy=reference_policy)
        if report.get("accepted") is not True:
            raise ValueError("Public declaration reference did not pass qualification")
        actions = load_jsonl(root / "public_qualification/runtime/model_skill_actions.jsonl")
        reference = project_runtime(task, root / "public_qualification/runtime")
        # Public business dispatches are not gateway skill declarations or a minimal plan.
        task.update(reference_tool_call_count=len(actions), reference_skill_count=None, remaining_skill_count=None)
        task["structure"] = {
            "measurement": "public_business_reference_not_minimal_plan_or_declared_skill_runs",
            "reference_business_dispatches": len(actions),
            "committed_artifact_count": len(reference.project["artifacts"]),
            "intermediate_artifact_count": sum(bool(a.get("intermediate")) for a in reference.project["artifacts"].values()),
            "regional_period_work_packages": len(reference.project["work_packages"]),
            "arbitrary_checkpoint_qualification": "not_run", "declaration_claims_qualification": "not_run"}
        write_json(root / "task.json", task)
        write_json(root / "continuation.json", actions)
        write_json(root / "construction.json", {"structure": task["structure"], "external_calls": 0})
        freeze_task(root, task, actions, dataops_policy(skill_protocol=protocol, reference_policy=reference_policy),
                    lineage=load_json(root / "public_qualification/REFERENCE_POLICY.json"))
        write_json(root / "BUILD.json", {"status": "complete", "config": config, "external_calls": 0,
                   "public_qualification": "public_qualification/QUALIFICATION.json", "expert_success": True,
                   "scope": "zero_prefix_business_reference_only_not_claims_or_harbor_qualification"})
        return task
    except Exception as error:
        write_json(root / "BUILD.json", {"status": "failed", "config": config, "external_calls": 0,
                   "failure_type": type(error).__name__})
        raise
