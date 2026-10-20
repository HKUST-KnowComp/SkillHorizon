"""Bounded native transport check for the public reference helper, not a task result."""

import argparse
import json
from pathlib import Path
import time

from integrations.benchmark_suite.native_reference_length import business_length
from .backend import write_record
from .config import job_config
from .export import export_task
from .orchestration import run_jobs
from .reference_fixture import ReferenceActor
from .reference_run import LoopbackReferenceProxy, append_events, trial_result


class OnePublicRead:
    def __init__(self, invoke):
        self.invoke = invoke

    async def run(self):
        result = await self.invoke("read_project", {})
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ValueError("Public read did not return a valid response")
        return {"status": "policy_complete", "terminal_result": result}


def probe_valid(receipt, actor, trial, events, business):
    public = actor.policy_result
    return (receipt["all_trials_valid"] is True and trial.get("business_success") is False
            and actor.native_complete and actor.failure is None and actor.meter.invalid is False
            and actor.meter.pending is False and actor.ordinal == 1
            and isinstance(public, dict) and public.get("status") == "policy_complete"
            and isinstance(public.get("terminal_result"), dict)
            and business is not None and business["top_level_calls"] == 1
            and len(events) == 2 and events[0].get("phase") == "dispatch"
            and events[0].get("name") == "read_project" and events[0].get("arguments") == {}
            and events[1].get("phase") == "result"
            and public["terminal_result"] == events[1].get("result"))


def page_count(actor):
    total = 0
    for event in actor.meter.events:
        if event["origin"] != "reference_response":
            continue
        response = json.loads(event["message"]["content"])
        total += sum(" page " in command["keystrokes"] for command in response["commands"])
    return total


def run(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    export_task(source, output / "harbor-task", max_calls=10, include_oracle=False,
                require_reference_contract=True)
    actor = ReferenceActor(OnePublicRead,
        on_events=lambda events: append_events(output / "NATIVE_VISIBLE.jsonl", events))
    proxy = LoopbackReferenceProxy(actor, max_responses=30)
    try:
        def config(job, api_base):
            value = job_config(job["task"], output / "jobs" / "DataOps",
                agent="terminus-2", model="openai/gpt-5.6-sol", api_base=api_base,
                max_turns=30, total_window=1000000, compact_at=900000,
                max_output=8192, observed=True, agent_timeout=540)
            value["agents"][0]["kwargs"]["enable_summarize"] = False
            return value

        receipt = run_jobs(output, [{"label": "DataOps", "task": output / "harbor-task",
            "max_turns": 30, "agent_timeout": 540}], proxy=proxy, build_config=config,
            declaration={"purpose": "native public terminal transport probe", "external_provider_calls": 0},
            cleanup_mode="stop_copy", deadline=time.monotonic() + 900)
        trial = trial_result(receipt)
        gateway = Path(trial["trial_result"]).parent / "business_private/gateway.jsonl"
        events = [json.loads(line) for line in gateway.read_text().splitlines() if line] if gateway.is_file() else []
        business = business_length(events) if events else None
        pages = page_count(actor)
        passed = probe_valid(receipt, actor, trial, events, business) and pages > 0
        result = {"status": "passed" if passed else "failed", "native_requests": actor.requests,
                  "public_calls": actor.ordinal, "native_complete": actor.native_complete,
                  "terminal_events": len(actor.meter.events), "business_dispatches": business["top_level_calls"] if business else None,
                  "business_success": trial.get("business_success"), "page_commands": pages,
                  "payload_match": actor.policy_result.get("terminal_result") == events[1].get("result")
                      if isinstance(actor.policy_result, dict) and len(events) == 2 else False,
                  "external_provider_calls": 0,
                  "fixture_failure": actor.failure}
        write_record(output / "TERMINAL_PROBE.json", result)
        return result
    except BaseException as error:
        write_record(output / "TERMINAL_PROBE.json", {"status": "failed", "type": type(error).__name__,
            "native_requests": actor.requests, "public_calls": actor.ordinal,
            "external_provider_calls": 0})
        raise
    finally:
        proxy.stop_new_requests()
        actor.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = run(args.source, args.output)
    print(result, flush=True)
    if result["status"] != "passed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
