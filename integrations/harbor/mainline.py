"""Fresh, bounded operational acceptance on the guarded native Harbor path."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .backend import write_record
from .config import job_config
from .export import export_task
from .orchestration import run_jobs
from .smoke import MODEL


DOMAINS = ("DataOps", "FieldOps")
MAX_TURNS = 32
AGENT_TIMEOUT = 7200


def policy():
    from .provider_reliable import ProviderPolicy
    return ProviderPolicy(max_responses=96, max_infra_failures=288, max_input_tokens=2000000,
                          max_output_tokens=262144, per_output=8192)


def effective_config(task, jobs, api_base):
    return job_config(task, jobs, agent="terminus-2", model="openai/" + MODEL,
        api_base=api_base, max_turns=MAX_TURNS, max_output=policy().per_output,
        observed=True, agent_timeout=AGENT_TIMEOUT)


def declaration():
    return {"version": 2, "model": MODEL, "reasoning_effort": "medium", "domains": list(DOMAINS),
        "provider_policy": asdict(policy()), "max_turns_per_job": MAX_TURNS,
        "agent_timeout_sec": AGENT_TIMEOUT, "compact_at": 200000,
        "purpose": "operational migration acceptance", "crash_resume": False}


def prepare(deployment, output):
    output.mkdir(parents=True, exist_ok=False)
    tasks = []
    for domain in DOMAINS:
        source = deployment / "qualification" / domain / "source"
        reference = json.loads((source / "continuation.json").read_text())
        task = output / "tasks" / domain
        export_task(source, task, max_calls=len(reference) + 50, include_oracle=False)
        from harbor.models.task.task import Task
        from harbor.models.job.config import JobConfig
        Task(task_dir=task)
        config = effective_config(task, output / "jobs" / domain, "http://127.0.0.1:1/v1")
        JobConfig.model_validate(config)
        write_record(output / (domain + "-prepared.json"), config)
        tasks.append({"domain": domain, "reference_calls": len(reference)})
    write_record(output / "PREPARED.json", {"declaration": declaration(), "tasks": tasks, "provider_calls": 0})


def require_qualification(path):
    value = json.loads(path.read_text())
    if not (value.get("status") == "passed" and value.get("real_provider_calls") == 0
            and value.get("native_observer_payload_parity") is True
            and value.get("transient_retry_recovered") is True
            and value.get("task_budget_excludes_infra") is True
            and value.get("real_terminus_compact_calls") == 3):
        raise ValueError("Native observer/retry/compact qualification required")


def run(output):
    from .provider_guard import PRODUCTION_URL
    from .provider_reliable import ReliableBudgetProxy
    prepared = json.loads((output / "PREPARED.json").read_text())
    if prepared["declaration"] != json.loads(json.dumps(declaration())):
        raise ValueError("Prepared policy no longer matches this execution entry")
    if [t["domain"] for t in prepared["tasks"]] != list(DOMAINS):
        raise ValueError("Prepared task selection changed")
    with (output / "STARTED.json").open("x") as handle:
        json.dump({"mode": "single_run_no_resume", "declaration": declaration()}, handle)
    key = sys.stdin.read(8192).strip()
    if not key or any(c in key for c in "\n\r") or key.startswith("sk-proj-"):
        raise ValueError("OpenLux credential channel invalid")
    proxy = ReliableBudgetProxy(output / "provider", model=MODEL,
        upstream_url=PRODUCTION_URL, api_key=key, policy=policy())
    del key
    jobs = [{"label": domain, "task": output / "tasks" / domain,
             "max_turns": MAX_TURNS, "agent_timeout": AGENT_TIMEOUT} for domain in DOMAINS]

    def build_config(job, api_base):
        return effective_config(job["task"], output / "jobs" / job["label"], api_base)

    return run_jobs(output, jobs, proxy=proxy, build_config=build_config, declaration=declaration())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "run"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--qualification-receipt", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        if args.deployment is None:
            parser.error("--deployment required")
        prepare(args.deployment.resolve(), args.output.resolve())
    else:
        if args.qualification_receipt is None:
            parser.error("--qualification-receipt required")
        require_qualification(args.qualification_receipt)
        run(args.output.resolve())


if __name__ == "__main__":
    main()
