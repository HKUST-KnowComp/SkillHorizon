"""Native public-policy reference trial, using only a local synthetic provider.

The actor starts with the exported public task. Instance data reaches its policy
through actual terminal output from Client calls. This is construction evidence,
never model behavior or provider token usage.
"""

import argparse
import json
from pathlib import Path
import threading
import time

from integrations.benchmark_suite.native_reference_length import business_length
from skillhorizon.dataflow.public_witness import PublicCreditWitness

from .backend import write_record
from .config import job_config
from .export import export_task
from .orchestration import run_jobs
from .reference_fixture import ReferenceActor


MAX_TURNS = 15000
MAX_SECONDS = 10800


class LoopbackReferenceProxy:
    """Small synthetic transport; it has no remote URL, key or paid provider."""

    def __init__(self, actor, max_responses):
        self.actor, self.max_responses = actor, max_responses
        self.attempts = 0
        self.locked = False
        self._lock = threading.Lock()

    def forward(self, payload):
        with self._lock:
            if self.locked or self.attempts >= self.max_responses:
                self.locked = True
                raise ValueError("Reference response budget exhausted")
            if (not isinstance(payload, dict) or not isinstance(payload.get("messages"), list)
                    or payload.get("model") != "gpt-5.6-sol"):
                raise ValueError("Unexpected synthetic reference request")
            response = self.actor.respond(payload)
            self.attempts += 1
            return json.dumps({"id": "skillhorizon-reference-fixture", "object": "chat.completion",
                "created": 1, "model": "gpt-5.6-sol",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": json.dumps(response)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}},
                allow_nan=False).encode()

    def stop_new_requests(self):
        self.locked = True

    def snapshot(self):
        return {"locked": self.locked, "attempts": self.attempts,
                "external_provider_calls": 0, "usage": "synthetic fixture values, not model billing"}


def policy(invoke):
    return PublicCreditWitness(invoke, skill_protocol="skill_declaration_v1",
                               max_calls=5000, max_rounds=8, page_size=200)


def configuration(task, output, api_base, *, max_turns):
    config = job_config(task, Path(output) / "jobs" / "DataOps", agent="terminus-2",
        model="openai/gpt-5.6-sol", api_base=api_base, max_turns=max_turns,
        total_window=1000000, compact_at=900000, max_output=8192,
        observed=True, agent_timeout=MAX_SECONDS)
    config["agents"][0]["kwargs"]["enable_summarize"] = False
    return config


def trial_result(receipt):
    if len(receipt["jobs"]) != 1:
        raise ValueError("Expected one native reference trial")
    return receipt["jobs"][0]["trial"]


def run(source, output, *, max_turns=MAX_TURNS):
    source, output = Path(source).resolve(), Path(output).resolve()
    if type(max_turns) is not int or not 1 <= max_turns <= MAX_TURNS:
        raise ValueError("Unsupported reference turn budget")
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True, exist_ok=False)
    from skillhorizon.experiment.tasks import load_task
    task = load_task(source)
    if (task["format"] != "skillhorizon.dataflow.v2"
            or task.get("skill_protocol") != "skill_declaration_v1"
            or task.get("checkpoint_action_count") != 0
            or task["config"].get("business_scenario") != "receivables_rollover"):
        raise ValueError("Reference runner currently supports only full-online declaration DataOps receivables")
    from integrations.benchmark_suite.reference_policy import reference_actions
    actions = reference_actions(source)
    export = export_task(source, output / "harbor-task", max_calls=len(actions) + 200,
                         include_oracle=False, require_reference_contract=True)
    write_record(output / "PREPARED.json", {"source": str(source), "export": export,
        "max_turns": max_turns, "external_provider_calls": 0,
        "scope": "public deterministic reference under native Terminus-2"})
    actor = proxy = None
    try:
        actor = ReferenceActor(policy, timeout=300,
            on_events=lambda events: append_events(output / "NATIVE_VISIBLE.jsonl", events))
        proxy = LoopbackReferenceProxy(actor, max_responses=max_turns)
        receipt = run_jobs(output, [{"label": "DataOps", "task": output / "harbor-task",
            "max_turns": max_turns, "agent_timeout": MAX_SECONDS}], proxy=proxy,
            build_config=lambda job, url: configuration(job["task"], output, url, max_turns=max_turns),
            declaration={"purpose": "native public reference length", "external_provider_calls": 0},
            deadline=time.monotonic() + MAX_SECONDS + 1200)
        trial = trial_result(receipt)
        business_success = (receipt["all_trials_valid"] is True
                            and trial.get("valid_execution") is True
                            and trial.get("business_success") is True)
        measured = actor.meter.finish(native_complete=actor.native_complete,
                                      business_success=business_success)
        gateway = Path(trial["trial_result"]).parent / "business_private/gateway.jsonl"
        events = [json.loads(line) for line in gateway.read_text().splitlines() if line] if gateway.is_file() else []
        business = business_length(events) if business_success else None
        complete = (business_success and actor.failure is None and actor.policy_result is not None
                    and actor.policy_result.get("status") == "policy_complete"
                    and measured["status"] == "available" and business is not None
                    and business["top_level_calls"] == actor.ordinal)
        result = {"status": "passed" if complete else "failed",
            "L_native_ref": measured["L_native_ref"] if complete else None,
            "L_business_ref": business["L_business_ref"] if complete else None,
            "native_measurement": measured, "business_measurement": business,
            "reference_calls": actor.ordinal, "native_requests": actor.requests,
            "business_success": business_success, "native_complete": actor.native_complete,
            "fixture_failure": actor.failure, "external_provider_calls": 0,
            "trial_result": trial.get("trial_result")}
        write_record(output / "REFERENCE_LENGTH.json", result)
        return result
    except BaseException as error:
        write_record(output / "REFERENCE_LENGTH.json", {"status": "failed",
            "failure_type": type(error).__name__, "reference_calls": actor.ordinal if actor else 0,
            "native_requests": actor.requests if actor else 0,
            "fixture_failure": actor.failure if actor else None,
            "external_provider_calls": 0})
        raise
    finally:
        if proxy is not None:
            proxy.stop_new_requests()
        if actor is not None:
            actor.close()


def append_events(path, events):
    with Path(path).open("a", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS)
    args = parser.parse_args()
    result = run(args.source, args.output, max_turns=args.max_turns)
    print(json.dumps({k: result[k] for k in ("status", "reference_calls", "native_requests",
                                              "L_native_ref", "L_business_ref", "external_provider_calls")}))


if __name__ == "__main__":
    main()
