"""Fixed seven-domain native interface qualification; loopback fixtures only."""

import argparse
from http.server import ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .backend import write_record
from .config import job_config
from .export import export_task
from .native_fixture import ActorHandler, ActorState, fixture
from .native_origins import MODULES, installation, sha, tree_hash, exclusive
from .orchestration import run_jobs
from .private_gateway import PrivateGateway
from .client import Client
from .stop_copy import remaining


DOMAINS = ("C25", "DataOps", "FieldOps", "C06", "C09", "C11", "C50")


def runtime_spec():
    modules = {}
    for name in MODULES:
        found = importlib.util.find_spec(name)
        if found is None or found.origin is None:
            raise ValueError("Native module missing")
        path = Path(found.origin).resolve()
        modules[name] = {"path": str(path), "sha256": sha(path)}
    executable, cli = Path(sys.executable).resolve(), Path(sys.executable).parent / "harbor"
    return {"modules": modules, "installation": installation(), "executable": str(executable),
        "executable_sha256": sha(executable), "cli": str(cli), "cli_sha256": sha(cli),
        "harbor_root": str(Path(importlib.util.find_spec("harbor").origin).resolve().parent),
        "adapter_root": str(Path(__file__).resolve().parent)}


def prepare_cell(domain, original_task, root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    task = root / "harbor-task"
    before = tree_hash(Path(original_task))
    exported = export_task(original_task, task, max_calls=1, include_oracle=False,
                           require_reference_contract=True)
    from harbor.models.task.task import Task
    from harbor.models.job.config import JobConfig
    from .agent import preflight_config
    Task(task_dir=task)
    config = configuration(domain, task, root, "http://127.0.0.1:1/v1")
    config["agents"][0]["kwargs"]["origin_manifest"] = str(root / (domain + "-origins.json"))
    agent_preflight = preflight_config(JobConfig.model_validate(config).agents[0])
    admission = None
    if domain not in {"DataOps", "FieldOps"}:
        from .task_admission import admit
        admission = admit(task / "environment/business", root / "PRIVATE_TASK_ADMISSION.json")
    gateway = PrivateGateway(task / "environment/business", root / "public-schema-state", max_calls=1)
    try:
        tools = Client(gateway.url).tools()
    finally:
        gateway.close()
    control = json.loads((root / "public-schema-state/control.json").read_text())
    if control["attempts"] != 0 or control["dispatched"] != 0 or control["declarations"]["attempts"] != 0:
        raise ValueError("Public schema inspection dispatched a call")
    definition = fixture(domain, task / "environment/public", tools)
    write_record(root / "ACTOR_FIXTURE.json", definition)
    write_record(root / "PUBLIC_TOOLS.json", tools)
    if tree_hash(Path(original_task)) != before:
        raise ValueError("Original task changed during native export")
    runtime = runtime_spec()
    agent_preflight["agent_module_sha256"] = runtime["modules"]["integrations.harbor.agent"]["sha256"]
    prepared = {"domain": domain, "task": str(task), "original_task": str(original_task),
        "original_identity": before, "export_identity": tree_hash(task), "export": exported,
        "private_admission": admission, "fixture_sha256": sha(root / "ACTOR_FIXTURE.json"),
        "public_tools_sha256": sha(root / "PUBLIC_TOOLS.json"), "business_dispatches": 0,
        "external_provider_calls": 0, "runtime": runtime, "agent_preflight": agent_preflight}
    write_record(root / "PREPARED.json", prepared)


def configuration(domain, task, root, api_base):
    return job_config(task, Path(root) / "jobs" / domain, agent="terminus-2",
        model="openai/gpt-5.6-sol", api_base=api_base, max_turns=4,
        total_window=480000, compact_at=200000, max_output=8192, observed=True, agent_timeout=600)


def run_one(root, *, deadline):
    from .native_gate import accept
    from .provider_reliable import ProviderPolicy, ReliableBudgetProxy
    root = Path(root)
    prepared = json.loads((root / "PREPARED.json").read_text())
    domain, task = prepared["domain"], Path(prepared["task"])
    if (prepared["export_identity"] != tree_hash(task)
            or prepared["fixture_sha256"] != sha(root / "ACTOR_FIXTURE.json")
            or prepared["public_tools_sha256"] != sha(root / "PUBLIC_TOOLS.json")):
        raise ValueError("Prepared native input drift")
    definition = json.loads((root / "ACTOR_FIXTURE.json").read_text())
    if definition != fixture(domain, task / "environment/public", json.loads((root / "PUBLIC_TOOLS.json").read_text())):
        raise ValueError("Actor fixture is not derived from public sources")
    exclusive(root / "STARTED.json", {"deadline": deadline, "external_provider_calls": 0})
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), ActorHandler)
    upstream.actor = ActorState(definition)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    server_started = False
    proxy = None
    primary = None
    try:
        thread.start()
        server_started = True
        proxy = ReliableBudgetProxy(root / "provider", model="gpt-5.6-sol",
            upstream_url=f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions",
            api_key="native-fixture-dummy", testing_only=True,
            policy=ProviderPolicy(max_responses=4, max_infra_failures=1, max_input_tokens=200000,
                max_output_tokens=32768, per_output=8192))
        receipt = run_jobs(root, [{"label": domain, "task": task, "max_turns": 4, "agent_timeout": 600}],
            proxy=proxy, build_config=lambda job, url: configuration(domain, task, root, url),
            declaration={"purpose": "known-action native interface control", "external_provider_calls": 0},
            cleanup_mode="stop_copy", deadline=deadline, origin_spec=prepared["runtime"])
        write_record(root / "FIXTURE_REQUESTS.json", {"requests": upstream.actor.requests,
            "failures": upstream.actor.failures, "external_provider_calls": 0})
        result = accept(root, task, receipt, definition, upstream.actor, proxy,
                        deadline=min(time.monotonic() + 60, deadline - 30))
        write_record(root / "QUALIFICATION.json", result)
        return result
    except BaseException as error:
        primary = error
        try:
            write_record(root / "QUALIFICATION.json", {"status": "failed", "failure_type": type(error).__name__,
                "business_result_source": str(root / "RUN_RECEIPT.json"), "external_provider_calls": 0})
        except BaseException:
            pass
        raise
    finally:
        errors = []
        operations = ([proxy.stop_new_requests] if proxy is not None else [])
        if server_started:
            operations.append(upstream.shutdown)
        operations.append(upstream.server_close)
        if server_started:
            operations.append(lambda: thread.join(timeout=5))
        for operation in operations:
            try:
                operation()
            except BaseException as error:
                errors.append(error)
        if errors and primary is None:
            try:
                write_record(root / "QUALIFICATION.json", {"status": "failed", "failure_type": type(errors[0]).__name__,
                    "external_provider_calls": 0})
            finally:
                raise errors[0]


def validate_execution(root, execution, *, remaining_only=False):
    root = Path(root)
    domains = DOMAINS[4:] if remaining_only else DOMAINS
    if remaining_only:
        from .native_remaining import validate_suffix
        validate_suffix(root, execution)
    ready = json.loads((root / "PREPARATION_RESULT.json").read_text())
    if ready.get("status") != "passed" or ready.get("cells") != len(domains) or ready.get("business_dispatches") != 0:
        raise ValueError("Complete native preparation required")
    if [row["domain"] for row in execution["cells"]] != list(domains):
        raise ValueError("Exact fixed native cell sequence required")
    for row in execution["cells"]:
        domain = row["domain"]
        path = root / "probes" / domain
        if row["root"] != str(path) or sha(path / "PREPARED.json") != row["prepared_sha256"]:
            raise ValueError("Execution domain/root/preparation binding differs")
        prepared = json.loads((path / "PREPARED.json").read_text())
        task = path / "harbor-task"
        original = root / "inputs" / domain
        if (prepared["domain"] != domain or prepared["task"] != str(task)
                or prepared["original_task"] != str(original)
                or prepared["export_identity"] != tree_hash(task)
                or prepared["original_identity"] != tree_hash(original)
                or prepared["export"]["max_calls"] != 1
                or prepared["export"]["oracle_in_controller_only_solution"] is not False
                or prepared["fixture_sha256"] != sha(path / "ACTOR_FIXTURE.json")
                or prepared["public_tools_sha256"] != sha(path / "PUBLIC_TOOLS.json")):
            raise ValueError("Prepared domain/export/input binding differs")


def run(root, *, remaining_only=False):
    started = time.monotonic()
    shared_seconds = 9500 if remaining_only else 20000
    deadline = started + shared_seconds
    root = Path(root).resolve()
    results = []
    def interrupt(signum, frame):
        raise TimeoutError("Shared native qualification deadline expired")
    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, remaining(deadline, shared_seconds))
    try:
        execution = json.loads((root / "NATIVE_EXECUTION.json").read_text())
        if remaining_only:
            validate_execution(root, execution, remaining_only=True)
        else:
            validate_execution(root, execution)
        remaining(deadline, 1)
        exclusive(root / "NATIVE_STARTED.json", {"external_provider_calls": 0, "attempts": 1})
        with (root / "mainline.log").open("xb") as log:
            child = subprocess.Popen([sys.executable, "-B", "-m", "integrations.harbor.qualify_mainline", str(root / "mainline")],
                env={k: os.environ[k] for k in ("HOME", "PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "TIKTOKEN_CACHE_DIR")
                     if k in os.environ}, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            primary = None
            try:
                if child.wait(timeout=remaining(deadline, 280)):
                    raise RuntimeError("Native mainline control failed")
            except BaseException as error:
                primary = error
            try:
                from .process_cleanup import terminate_group
                terminate_group(child, deadline=min(deadline, time.monotonic() + 20))
            except BaseException as error:
                if primary is None:
                    primary = error
            if primary is not None:
                raise primary
        from .mainline import require_qualification
        require_qualification(root / "mainline/QUALIFICATION.json")
        for row in execution["cells"]:
            if deadline - time.monotonic() < 2760:
                raise TimeoutError("Insufficient shared time for next complete job")
            path = Path(row["root"])
            if sha(path / "PREPARED.json") != row["prepared_sha256"]:
                raise ValueError("Frozen native admission differs")
            result = run_one(path, deadline=min(deadline - 60, time.monotonic() + 2700))
            if result["status"] != "passed":
                raise ValueError("Native gate failed")
            results.append({"domain": row["domain"], "result": result})
            write_record(root / "NATIVE_PROGRESS.json", {"cells": results})
        write_record(root / "NATIVE_RESULT.json", {"status": "passed", "cells": results,
            "seconds": time.monotonic() - started, "external_provider_calls": 0})
    except BaseException as error:
        try:
            write_record(root / "NATIVE_RESULT.json", {"status": "failed", "cells": results,
                "failure_type": type(error).__name__, "seconds": time.monotonic() - started, "external_provider_calls": 0})
        except BaseException:
            pass
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    prepare = sub.add_parser("prepare-cell")
    prepare.add_argument("domain", choices=DOMAINS)
    prepare.add_argument("task", type=Path)
    prepare.add_argument("root", type=Path)
    execute = sub.add_parser("run")
    execute.add_argument("root", type=Path)
    suffix = sub.add_parser("run-remaining")
    suffix.add_argument("root", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare-cell":
        prepare_cell(args.domain, args.task, args.root)
    else:
        from .smoke_control import termination_signals
        with termination_signals():
            run(args.root, remaining_only=args.mode == "run-remaining")


if __name__ == "__main__":
    main()
