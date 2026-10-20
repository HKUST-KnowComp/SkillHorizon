"""Zero-provider real-MCP transport, trace replay and length diagnostics."""

import asyncio
import json
from pathlib import Path
import shutil
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import tiktoken

from .io import read_json, trace_event, write_json


ROOT = Path(__file__).resolve().parents[2]


def qualify_direct(task, runtime_type, witness, evaluator, *, options=None, progress=None, phase_failure=None):
    """Record even failed construction traces; replay successful public actions."""
    progress = progress or (lambda stage: None)
    def advance(stage, original=None):
        try:
            progress(stage)
        except Exception as diagnostic_error:
            if original is None:
                raise
            original.add_note("Progress diagnostics failed: " + type(diagnostic_error).__name__)

    advance("direct_setup")
    env, events = runtime_type(task, task / "runtime"), []
    from .framework import metadata_tools
    from .reference_binding import inputs, freeze, callable_policy
    policy = callable_policy(witness, options or {})
    tools = metadata_tools(env)
    before = inputs(task, tools, policy)
    failure = None
    async def call(name, **arguments):
        result = env.call(name, arguments)
        events.append(trace_event(name, arguments, result))
        if result["status"] != "success":
            raise AssertionError(events[-1])
        return result["data"]
    try:
        advance("direct_reference")
        asyncio.run(witness(call, **(options or {})))
        advance("direct_evaluation")
        result = evaluator(task, task / "runtime")
        write_json(task / "EVALUATION.json", result)
        if not result["accepted"]:
            raise AssertionError(result)
        advance("direct_replay")
        shutil.copytree(task / "checkpoint", task / "replay")
        replay = runtime_type(task, task / "replay")
        for event in events:
            if replay.call(event["name"], event["arguments"]) != event["output"]:
                raise AssertionError("Direct scenario replay output mismatch")
        if env.world != replay.world:
            raise AssertionError("Direct scenario replay state mismatch")
        advance("direct_freeze")
        freeze(task, before, tools, policy, events)
        write_json(task / "DIRECT_VERIFICATION.json", {"provider_calls": 0, "accepted": True,
            "replay_verified": True, "calls": len(events), "model_behavior_evidence": False})
        return env, events, result
    except BaseException as error:
        failure = error
        if phase_failure is not None:
            try:
                phase_failure(error)
            except Exception as diagnostic_error:
                error.add_note("Timing diagnostics failed: " + type(diagnostic_error).__name__)
        if isinstance(error, Exception):
            advance("direct_failure_record", original=error)
            write_json(task / "DIRECT_VERIFICATION.json", {"provider_calls": 0, "accepted": False,
                "failure": type(error).__name__, "detail": str(error), "calls": len(events), "model_behavior_evidence": False})
        raise
    finally:
        diagnostic = None
        try:
            advance("direct_trace_write", original=failure)
        except Exception as error:
            diagnostic = error
        write_json(task / "trace.json", events)
        if diagnostic is not None:
            raise diagnostic


async def execute(module, witness, task, runtime, *, replay=None, **options):
    params = StdioServerParameters(command=sys.executable, args=["-m", module, str(task), str(runtime)], cwd=str(ROOT))
    events = []
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            names = sorted(t.name for t in (await session.list_tools()).tools)
            if "read_task" not in names or any("evaluat" in n for n in names):
                raise AssertionError("Actor tool contract mismatch")
            with (runtime / "observed_trace.jsonl").open("a", encoding="utf-8") as log:
                async def call(name, **arguments):
                    response = await session.call_tool(name, arguments)
                    output = response.structured_content
                    event = trace_event(name, arguments, output)
                    events.append(event)
                    log.write(json.dumps(event, allow_nan=False) + "\n")
                    log.flush()
                    if output is None or response.is_error or output["status"] != "success":
                        raise AssertionError(event)
                    return output["data"]

                if replay is None:
                    await witness(call, **options)
                else:
                    for expected in replay:
                        await call(expected["name"], **expected["arguments"])
                        if events[-1] != expected:
                            raise AssertionError({"event": len(events), "actual": events[-1], "expected": expected})
    return events, names


def messages(events):
    result = []
    for index, event in enumerate(events):
        identifier = f"internal-mcp-call-{index + 1}"
        result.append({"role": "assistant", "tool_calls": [{"id": identifier, "type": "function", "function": {
            "name": event["name"], "arguments": json.dumps(event["arguments"])}}]})
        result.append({"role": "tool", "tool_call_id": identifier, "content": json.dumps(event["output"])})
    return result


def export_report(task, events, names, evaluation, state_key, *, replay_verified, options):
    raw = messages(events)
    write_json(task / "trace.json", events)
    write_json(task / "raw_messages.json", raw)
    encoding = tiktoken.get_encoding("o200k_base")
    content = sum(len(encoding.encode(m["content"] if m["role"] == "tool" else m["tool_calls"][0]["function"]["arguments"], disallowed_special=())) for m in raw)
    report = {"evaluation": evaluation, "provider_calls": 0, "provider_input_tokens": None, "provider_length_admitted": False,
        "mcp_calls": len(events), "exposed_tools": names, "observed_skill_runs": len(read_json(task / "runtime/world.json")[state_key]["skill_runs"]),
        "local_message_content_tokens": content, "local_serialized_message_tokens": len(encoding.encode(json.dumps(raw), disallowed_special=())),
        "exact_response_and_state_replay": replay_verified, "options": options, "minimal_required_horizon_proven": False,
        "evidence_kind": "selected_zero_model_trajectory_not_model_behavior"}
    write_json(task / "REPORT.json", report)
    return report


def qualify(module, witness, evaluator, task, state_key, *, options=None, expected_success=True):
    options = options or {}
    events, names = asyncio.run(execute(module, witness, task, task / "runtime", **options))
    evaluation = evaluator(task, task / "runtime")
    write_json(task / "EVALUATION.json", evaluation)
    if evaluation["accepted"] != expected_success:
        raise AssertionError(evaluation)
    shutil.copytree(task / "checkpoint", task / "replay")
    asyncio.run(execute(module, witness, task, task / "replay", replay=events))
    if read_json(task / "runtime/world.json") != read_json(task / "replay/world.json"):
        raise AssertionError("Replay state mismatch")
    return export_report(task, events, names, evaluation, state_key, replay_verified=True, options=options), events


def continue_checkpoint(module, witness, evaluator, task, output, state_key, prefix):
    for directory in ("public", "private", "checkpoint"):
        shutil.copytree(task / directory, output / directory)
    for filename in ("controller.json", "background.json"):
        shutil.copy2(task / filename, output / filename)
    shutil.copytree(task / "runtime", output / "runtime")
    shutil.copytree(task / "runtime", output / "resume_checkpoint")
    write_json(output / "history_prefix.json", messages(prefix))
    write_json(output / "mcp.json", {"mcpServers": {"internal-domain": {"command": sys.executable,
        "args": ["-m", module, str(output), str(output / "runtime")], "cwd": str(ROOT)}}})
    suffix, names = asyncio.run(execute(module, witness, output, output / "runtime"))
    write_json(output / "suffix_trace.json", suffix)
    evaluation = evaluator(output, output / "runtime")
    if not evaluation["accepted"]:
        raise AssertionError(evaluation)
    shutil.copytree(output / "resume_checkpoint", output / "replay")
    asyncio.run(execute(module, witness, output, output / "replay", replay=suffix))
    if read_json(output / "runtime/world.json") != read_json(output / "replay/world.json"):
        raise AssertionError("Resumed replay state mismatch")
    return export_report(output, prefix + suffix, names, evaluation, state_key, replay_verified=True,
        options={"prefix_calls": len(prefix), "suffix_calls": len(suffix)})
