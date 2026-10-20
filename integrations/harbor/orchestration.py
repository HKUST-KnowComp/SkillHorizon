"""Shared guarded Harbor job orchestration, without credentials or agent loops."""

import json
import os
from pathlib import Path
import sys
import threading
import time

from .backend import write_record
from .claim_measurement import measure_trial
from .provider_guard import make_server
from .result_summary import summarize_trial
from .smoke_control import classify, recover_containers, run_process, termination_signals


def run_jobs(output, jobs, *, proxy, build_config, declaration,
             cleanup_mode="verify", deadline=None, origin_spec=None):
    """Run already-qualified jobs under one existing provider budget.

    Each job has label, task, max_turns and agent_timeout. build_config(job,
    api_base) supplies the native Harbor configuration. Validation, credentials
    and the STARTED marker belong to the caller; this function owns proxy
    serving, shutdown and RUN_RECEIPT.json.
    """
    output = Path(output)
    jobs = tuple(dict(job) for job in jobs)
    if cleanup_mode not in {"verify", "stop_copy"}:
        raise ValueError("Unknown cleanup mode")
    if cleanup_mode == "stop_copy" and (deadline is None or len(jobs) != 1):
        raise ValueError("Stop/copy qualification requires one job and an absolute deadline")
    outcomes, cleanup_errors = [], []
    server, thread, task, label = None, None, None, None
    started, failed = False, False

    def capture():
        destination = output / "recovery" / label
        if cleanup_mode == "verify":
            return recover_containers(task, destination)
        from .stop_copy import require_capture
        from .smoke_control import _docker
        if destination.exists():
            record = json.loads((destination / "RECOVERY.json").read_text())
            return require_capture(destination, task,
                deadline=min(deadline - 30, record["deadline"]), docker=_docker)
        return recover_containers(task, destination, mode="stop_copy",
            deadline=min(deadline - 90, time.monotonic() + 90))

    def cleanup(step, operation):
        try:
            operation()
        except BaseException as error:
            cleanup_errors.append({"step": step, "type": type(error).__name__})

    with termination_signals():
        try:
            server = make_server(proxy)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            started = True
            api_base = "http://127.0.0.1:" + str(server.server_port) + "/v1"
            environment = {"PATH": str(Path(sys.executable).parent) + ":/usr/local/bin:/usr/bin:/bin",
                "HOME": os.environ["HOME"], "PYTHONPATH": os.environ["PYTHONPATH"],
                "PYTHONDONTWRITEBYTECODE": "1", "OPENAI_API_KEY": "harbor-proxy-dummy",
                "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
            if "TIKTOKEN_CACHE_DIR" in os.environ:
                environment["TIKTOKEN_CACHE_DIR"] = os.environ["TIKTOKEN_CACHE_DIR"]
            for job in jobs:
                if proxy.snapshot()["locked"]:
                    break
                label, task = job["label"], Path(job["task"])
                if (task / "solution").exists():
                    raise ValueError("Oracle assets forbidden in provider acceptance")
                config = build_config(job, api_base)
                path = output / (label + "-effective.json")
                process_options = {}
                if cleanup_mode == "stop_copy":
                    from .stop_copy import remaining
                    remaining(deadline, 1)
                    process_options.update(cleanup_mode=cleanup_mode,
                        deadline=min(time.monotonic() + job["agent_timeout"] + 1800, deadline - 300),
                        cleanup_deadline=deadline - 30)
                if origin_spec is not None:
                    from .native_origins import freeze, spawn_recorder
                    manifest_path = output / (label + "-origins.json")
                    config["agents"][0]["kwargs"]["origin_manifest"] = str(manifest_path)
                    config["verifier"]["kwargs"]["origin_manifest"] = str(manifest_path)
                write_record(path, config)
                if origin_spec is not None:
                    freeze(manifest_path, path, task, origin_spec)
                    process_options["spawn_record"] = spawn_recorder(manifest_path)
                with (output / (label + ".log")).open("w") as log:
                    code = run_process([str(Path(sys.executable).parent / "harbor"), "run", "-c", str(path)],
                        environment, log, task, output / "recovery" / label, proxy,
                        timeout=job["agent_timeout"] + 1800, **process_options)
                result = classify(output / "jobs" / label)
                measurement = None
                if code == 0 and result.get("valid_execution") and result.get("trial_result"):
                    if cleanup_mode == "stop_copy":
                        measurement = measure_trial(task, result["trial_result"],
                            timeout=115, deadline=min(time.monotonic() + 115, deadline - 185))
                    else:
                        measurement = measure_trial(task, result["trial_result"])
                observations = (summarize_trial(result["trial_result"], max_turns=job["max_turns"])
                                if result.get("trial_result") else None)
                if measurement is not None and observations is not None and measurement["status"] != "valid":
                    observations["claims"] = {"status": measurement["status"]}
                outcomes.append({"domain": label, "cli_return_code": code, "trial": result,
                                 "observations": observations})
                write_record(output / "PROGRESS.json", {"jobs": outcomes, "provider": proxy.snapshot()})
                print(json.dumps({"domain": label, "result": result,
                    "actual_provider_attempts": proxy.snapshot()["attempts"]}), flush=True)
                if code != 0 or not result["valid_execution"]:
                    proxy.stop_new_requests()
                    capture()
                    break
        except BaseException as error:
            failed = True
            cleanup("stop_admission", proxy.stop_new_requests)
            if task is not None and (cleanup_mode == "stop_copy" or not (output / "recovery" / label).exists()):
                cleanup("recover_containers", capture)
            cleanup("publish_exception", lambda: write_record(output / "CONTROLLER_EXCEPTION.json", {"type": type(error).__name__}))
            raise
        finally:
            cleanup("stop_admission", proxy.stop_new_requests)
            if cleanup_mode == "stop_copy" and task is not None and not failed:
                cleanup("stop_copy_capture", capture)
            if server is not None:
                if started:
                    cleanup("server_shutdown", server.shutdown)
                cleanup("server_close", server.server_close)
            if started:
                cleanup("server_join", lambda: thread.join(10))
            provider_state = {"unavailable": True}
            try:
                provider_state = proxy.snapshot()
            except BaseException as error:
                cleanup_errors.append({"step": "provider_snapshot", "type": type(error).__name__})
            receipt = {"declaration": declaration, "jobs": outcomes, "provider": provider_state,
                "all_trials_valid": not failed and not cleanup_errors and len(outcomes) == len(jobs) and all(
                    x["cli_return_code"] == 0 and x["trial"]["valid_execution"] for x in outcomes)}
            if cleanup_errors:
                receipt["cleanup_errors"] = cleanup_errors
            cleanup("publish_receipt", lambda: write_record(output / "RUN_RECEIPT.json", receipt))
            if cleanup_errors and not failed:
                raise RuntimeError("Harbor orchestration cleanup failed; inspect RUN_RECEIPT.json")
    return receipt
