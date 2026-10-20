"""Controller-only public witness qualification; never imported by its policy."""

import asyncio
from pathlib import Path
import shutil
import time

from ..experiment.environment_score import evaluate_environment
from ..experiment.project_runtime import project_runtime, project_server
from ..util import load_jsonl, write_json
from .public_witness import PublicCreditWitness
from .verification import verify_release_content


class PublicQualificationError(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__("DataOps public witness qualification failed; see QUALIFICATION.json")


def verify_public_credit(initial, runtime_root, *, task=None, replay_mode="full"):
    """Post-policy content/state gates with an optional second exact replay.

    This descriptor is controller-local: it describes the zero-action initial,
    not a new actor task and not the old construction reference or its prefix.
    """
    if task is None:
        task = {"format": "skillhorizon.dataflow.v2", "config": {},
                "checkpoint_action_count": 0, "messages": []}
    if task.get("checkpoint_action_count") != 0:
        raise ValueError("Public qualification describes the zero-action initial")
    if replay_mode not in {"full", "none"}:
        raise ValueError("Unknown construction replay mode")
    started = time.monotonic()
    content = verify_release_content(runtime_root)
    content_seconds = time.monotonic() - started
    started = time.monotonic()
    business = project_runtime(task, runtime_root).score()
    business_seconds = time.monotonic() - started
    started = time.monotonic()
    replay = (evaluate_environment(task, initial, runtime_root) if replay_mode == "full" else
              {"status": "not_run", "valid": None, "task_success": None,
               "reason": "construction_content_and_state_validation"})
    replay_seconds = time.monotonic() - started if replay_mode == "full" else None
    return {"accepted": (content.get("checked") is True and content.get("passed") is True
                         and business.get("success") is True and business.get("termination_ready") is True
                         and (replay_mode == "none" or (replay.get("valid") is True
                              and replay.get("task_success") is True))),
            "validation_level": "content_state_and_replay" if replay_mode == "full" else "content_and_state",
            "independent_content": content, "business": business, "exact_replay": replay,
            "timings": {"content_seconds": content_seconds, "business_seconds": business_seconds,
                        "replay_seconds": replay_seconds}}


def qualify_public_credit(initial, output, *, max_calls=5000, max_rounds=8, page_size=200, tools=None, task=None, replay_mode="full",
                          reference_policy="public_credit_v1"):
    """Run a fresh public-only policy; raise on ANY failed qualification gate.

    output must be a fresh controller-owned directory. It retains its own initial,
    runtime, action log and report even on failure. No reference directory is read.
    """
    initial, output = Path(initial).resolve(), Path(output).resolve()
    if replay_mode not in {"full", "none"}:
        raise ValueError("Unknown construction replay mode")
    if output.exists() or output.is_relative_to(initial) or initial.is_relative_to(output):
        raise ValueError("Qualification output must be fresh and disjoint from initial")
    output.mkdir(parents=True)
    report = {"accepted": False, "scope": "programmed_public_credit_witness_not_model_behavior",
              "provider_calls": 0, "reference_calls": None, "reference_reused": False, "replay_mode": replay_mode,
              "initial": str(initial), "public_calls": 0, "policy_status": "not_started",
              "bounds": dict(max_calls=max_calls, max_rounds=max_rounds, page_size=page_size)}
    policy = None
    from ..experiment.reference_trace import dataops_policy, freeze_trace
    descriptor = task if task is not None else {
        "format": "skillhorizon.dataflow.v2", "config": {}, "execution_mode": "procedural",
        "checkpoint_action_count": 0, "messages": []}
    protocol = descriptor.get("skill_protocol", "procedural")
    policy_options = dict(max_calls=max_calls, max_rounds=max_rounds, page_size=page_size, skill_protocol=protocol)
    policy_before = dataops_policy(**policy_options, reference_policy=reference_policy)
    report["skill_protocol"] = protocol
    report["reference_policy"] = reference_policy
    try:
        if descriptor.get("checkpoint_action_count") != 0:
            raise ValueError("Qualification requires a zero-action task")
        original = project_runtime(descriptor, initial).snapshot()
        world = original["project"]
        if (world.get("business_program") != "credit_sales_v2" or world.get("artifacts")
                or world.get("active") or world.get("publication") is not None
                or load_jsonl(initial / "model_skill_actions.jsonl")):
            raise ValueError("Qualification requires the unexecuted credit initial, not a checkpoint or reference")
        if any(path.is_symlink() for path in initial.rglob("*")):
            raise ValueError("Initial must contain independent regular files, not symlinks")
        checkpoint, runtime_root = output / "initial", output / "runtime"
        shutil.copytree(initial, checkpoint)
        shutil.copytree(checkpoint, runtime_root)
        runtime = project_runtime(descriptor, runtime_root)
        if runtime.snapshot() != original:
            raise ValueError("Independent initial copy differs")

        async def call(name, arguments):
            # Forward only the actual public response. Never query an evaluator.
            return runtime.call(name, arguments)

        if reference_policy == "public_state_cache_v1":
            from .cached_witness import CachedPublicCreditWitness
            policy = CachedPublicCreditWitness(call, **policy_options)
        else:
            policy = PublicCreditWitness(call, **policy_options)
        report["policy_status"] = "running"
        started = time.monotonic()
        result = asyncio.run(policy.run())
        report["reference_seconds"] = time.monotonic() - started
        report["policy_status"] = result["status"]
        report["policy_result"] = result
        verification_options = {} if task is None else {"task": task}
        if replay_mode != "full":
            verification_options["replay_mode"] = replay_mode
        report["verification"] = verify_public_credit(checkpoint, runtime_root, **verification_options)
        report["initial_unchanged"] = project_runtime(descriptor, initial).snapshot() == original
        report["accepted"] = report["verification"]["accepted"] and report["initial_unchanged"]
    except Exception as exc:
        report["failure"] = {"code": getattr(exc, "code", type(exc).__name__), "detail": str(exc)}
        report["policy_status"] = "failed" if report["policy_status"] != "policy_complete" else "policy_complete"
    finally:
        if policy is not None:
            report["public_calls"] = policy.call_count
            report["rounds"] = policy.rounds
        actions = load_jsonl(output / "runtime/model_skill_actions.jsonl")
        report["recorded_public_calls"] = len(actions)
        report["public_skill_starts"] = sum(a["skill_id"] == "begin_skill" for a in actions)
        report["replay_tool_calls"] = (report.get("verification", {}).get("exact_replay", {}).get("executed_tool_calls", 0)
                                       if replay_mode == "full" else None)
        report["accepted"] = report["accepted"] and len(actions) == report["public_calls"]
        write_json(output / "QUALIFICATION.json", report)
    if not report["accepted"]:
        raise PublicQualificationError(report)
    if dataops_policy(**policy_options, reference_policy=reference_policy) != policy_before:
        raise ValueError("Public reference policy changed during execution")
    if tools is None:
        from mcp import Client
        from ..experiment.mcp_tools import provider_tools_from_mcp_listing

        async def exported_tools():
            async with Client(project_server(descriptor, initial)) as client:
                return provider_tools_from_mcp_listing(await client.list_tools())

        tools = asyncio.run(exported_tools())
    freeze_trace(output / "REFERENCE_POLICY.json", initial=initial, tools=tools,
                 trace=output / "runtime/model_skill_actions.jsonl", policy=policy_before)
    return report
