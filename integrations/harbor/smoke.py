"""Two reviewed operational trajectories through one controller budget proxy."""

import argparse
import json
import os
from pathlib import Path
import sys
import threading

from .backend import write_record
from .config import job_config
from .export import export_task
from .smoke_control import classify, recover_containers, run_process, termination_signals


MODEL = "gpt-5.6-sol"


def prepare(deployment, output):
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    tasks = []
    for domain in ("DataOps", "FieldOps"):
        source = deployment / "qualification" / domain / "source"
        task = output / "tasks" / domain
        reference = json.loads((source / "continuation.json").read_text())
        export_task(source, task, max_calls=len(reference) + 50)
        config = job_config(task, output / "jobs" / domain, agent="terminus-2",
                            model="openai/" + MODEL, api_base="http://127.0.0.1:1/v1")
        from harbor.models.task.task import Task
        from harbor.models.job.config import JobConfig
        Task(task_dir=task)
        JobConfig.model_validate(config)
        path = output / (domain + ".json")
        write_record(path, config)
        tasks.append({"domain": domain, "task": str(task), "config": str(path)})
    write_record(output / "PREPARED.json", {"model": MODEL, "tasks": tasks,
        "max_turns_per_job": 12, "max_provider_attempts_shared": 32,
        "max_estimated_input_shared": 300000, "max_output_shared": 65536,
        "max_output_per_request": 4096, "provider_calls": 0})


def require_compatibility(path):
    receipt = json.loads(path.read_text())
    state = receipt["provider"]
    protocol = receipt["protocol"]
    if not (receipt.get("passed") is True and receipt.get("usage_settled") is True
            and state["attempts"] == 1 and len(state["records"]) == 1
            and state["pending"] is None and state["records"][0]["outcome"] == "settled"
            and state["records"][0]["usage"] is not None
            and state["policy"]["model"] == MODEL
            and state["policy"]["testing_only"] is False
            and all(protocol.get(k) is True for k in (
                "harbor_chat_accepted", "terminus_parser_accepted", "no_commands", "completion_flag"))):
        raise ValueError("Compatibility receipt did not pass")


def run(output):
    from .provider_guard import BudgetProxy, PRODUCTION_URL, make_server
    prepared = json.loads((output / "PREPARED.json").read_text())
    if prepared["model"] != MODEL or [t["domain"] for t in prepared["tasks"]] != ["DataOps", "FieldOps"]:
        raise ValueError("Unexpected prepared condition")
    # An exclusive start marker prevents the same smoke from gaining two new jobs.
    with (output / "STARTED.json").open("x") as handle:
        json.dump({"mode": "single_run_no_resume", "model": MODEL}, handle)
    key = sys.stdin.read(8192).strip()
    if not key or "\n" in key or "\r" in key:
        raise ValueError("Credential channel invalid")
    proxy = BudgetProxy(output / "provider", model=MODEL, upstream_url=PRODUCTION_URL, api_key=key, timeout=120)
    del key
    server = make_server(proxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = {"PATH": str(Path(sys.executable).parent) + ":/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ["HOME"], "PYTHONPATH": os.environ["PYTHONPATH"],
        "PYTHONDONTWRITEBYTECODE": "1", "OPENAI_API_KEY": "harbor-proxy-dummy",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    if "TIKTOKEN_CACHE_DIR" in os.environ:
        environment["TIKTOKEN_CACHE_DIR"] = os.environ["TIKTOKEN_CACHE_DIR"]
    outcomes = []
    task, entry = None, None
    try:
        for entry in prepared["tasks"]:
            if proxy.snapshot()["locked"]:
                break
            task = Path(entry["task"])
            if task.parent != output / "tasks" or (task / "solution").exists():
                raise ValueError("Unqualified task or oracle leakage")
            # Rebuild canonical config instead of trusting editable client route fields.
            config = job_config(task, output / "jobs" / entry["domain"], agent="terminus-2",
                model="openai/" + MODEL, api_base="http://127.0.0.1:" + str(server.server_port) + "/v1")
            path = output / (entry["domain"] + "-effective.json")
            write_record(path, config)
            with (output / (entry["domain"] + ".log")).open("w") as log:
                code = run_process([str(Path(sys.executable).parent / "harbor"), "run", "-c", str(path)],
                    environment, log, task, output / "recovery" / entry["domain"], proxy)
            result = classify(output / "jobs" / entry["domain"])
            outcomes.append({"domain": entry["domain"], "cli_return_code": code,
                "trial": result})
            write_record(output / "PROGRESS.json", {"jobs": outcomes, "provider": proxy.snapshot()})
            print(json.dumps({"domain": entry["domain"], "cli_return_code": code,
                              "actual_provider_attempts": proxy.snapshot()["attempts"]}), flush=True)
            if code != 0 or not result["valid_execution"]:
                proxy.stop_new_requests()
                recover_containers(task, output / "recovery" / entry["domain"])
                break
    except BaseException as error:
        proxy.stop_new_requests()
        if task is not None and not (output / "recovery" / entry["domain"]).exists():
            recover_containers(task, output / "recovery" / entry["domain"])
        write_record(output / "CONTROLLER_EXCEPTION.json", {"type": type(error).__name__, "valid_for_scoring": False})
        raise
    finally:
        try:
            proxy.stop_new_requests()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(10)
            write_record(output / "RUN_RECEIPT.json", {"jobs": outcomes, "provider": proxy.snapshot(),
                         "scope": "operational smoke, not formal long-horizon performance"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "run"])
    parser.add_argument("output", type=Path)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--compatibility-receipt", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.mode == "prepare":
        if args.deployment is None:
            parser.error("--deployment required for prepare")
        prepare(args.deployment.resolve(), output)
    else:
        if args.compatibility_receipt is None:
            parser.error("--compatibility-receipt required for successor smoke")
        require_compatibility(args.compatibility_receipt)
        with termination_signals():
            run(output)


if __name__ == "__main__":
    main()
