"""Fresh zero-provider Harbor trials, with explicit positive and negative controls."""

import argparse
import json
import os
from pathlib import Path
import sys

from .audit import audit
from .backend import write_record
from .config import job_config
from .export import export_task
from .smoke_control import recover_containers, run_process, termination_signals


class _NoProvider:
    """The shared process controller needs a stop hook; oracle has no provider."""

    def stop_new_requests(self):
        pass


def _cleanup(task, recovery):
    # run_process already recovers on exceptions. Consume its evidence rather
    # than running a second seal/collection against the same containers.
    result = {"status": "failed", "quiescence_confirmed": False}
    try:
        if recovery.exists():
            record = json.loads((recovery / "RECOVERY.json").read_text())
        else:
            record = recover_containers(task, recovery)
        result.update(receipt=str(recovery / "RECOVERY.json"),
                      quiescence_confirmed=record.get("quiescence_confirmed") is True)
        shutdown = recovery / "CONTROLLER_SHUTDOWN.json"
        errors = json.loads(shutdown.read_text())["errors"] if shutdown.exists() else []
        result["controller_errors"] = errors
        failed_steps = [step for step in record["steps"]
                        if "error" in step or step.get("return_code", 0) != 0]
        result["failed_steps"] = failed_steps
        if "error_type" in record:
            result["error_type"] = record["error_type"]
        if result["quiescence_confirmed"] and not errors and not failed_steps and "error_type" not in record:
            result["status"] = "passed"
    except BaseException as error:
        result["error_type"] = type(error).__name__
    return result


def run(source, output, mode):
    if mode not in {"oracle", "noop", "fault"}:
        raise ValueError("Unsupported lifecycle control")
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    task = output / "harbor-task"
    receipt_path = output / "RUN_RECEIPT.json"
    receipt = {"mode": mode, "provider_calls": 0, "cli_return_code": None,
               "trial_results": [], "status": "preparing", "stage": "prepare"}
    write_record(receipt_path, receipt)
    launched = False
    with termination_signals():
        try:
            from integrations.benchmark_suite.reference_policy import reference_actions
            actions = reference_actions(source)
            export_task(source, task, max_calls=len(actions) + 50, include_oracle=True,
                        oracle_actions=actions, require_reference_contract=True)
            if mode == "noop":
                write_record(task / "solution/actions.json", [])
            config = job_config(task, output / "jobs")
            if mode == "fault":
                config["verifier"]["import_path"] = "integrations.harbor.controls:InjectedFailureVerifier"
            from harbor.models.task.task import Task
            from harbor.models.job.config import JobConfig
            Task(task_dir=task)
            JobConfig.model_validate(config)
            write_record(output / "job.json", config)
            # Preserve the frozen oldsrc:oldroot:overlay import ordering exactly.
            environment = {key: os.environ[key] for key in (
                "PATH", "HOME", "PYTHONPATH", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                "XDG_RUNTIME_DIR", "TMPDIR", "TIKTOKEN_CACHE_DIR") if key in os.environ}
            environment.update(PYTHONDONTWRITEBYTECODE="1", LITELLM_LOCAL_MODEL_COST_MAP="True")
            receipt.update(status="running", stage="harbor_cli")
            write_record(receipt_path, receipt)
            with (output / "cli.log").open("w") as log:
                launched = True
                receipt["cli_return_code"] = run_process(
                    [str(Path(sys.executable).parent / "harbor"), "run", "-c", str(output / "job.json")],
                    environment, log, task, output / "recovery", _NoProvider(), timeout=2400)
            receipt["trial_results"] = [str(p) for p in sorted((output / "jobs").glob("*/*/result.json"))]
            if receipt["cli_return_code"] != 0:
                raise RuntimeError("Harbor CLI exited nonzero; inspect cli.log")
            receipt.update(status="awaiting_trial_and_container_audit", stage="audit")
            write_record(receipt_path, receipt)
            audit(output)
            receipt.update(status="passed", stage="complete")
        except BaseException as error:
            receipt.update(status="failed", error_type=type(error).__name__)
            if error.__context__ is not None:
                receipt["cause_type"] = type(error.__context__).__name__
            if launched:
                receipt["cleanup"] = _cleanup(task, output / "recovery")
            raise
        finally:
            receipt["trial_results"] = [str(p) for p in sorted((output / "jobs").glob("*/*/result.json"))]
            write_record(receipt_path, receipt)
    print(json.dumps({"receipt": str(receipt_path), "trial_result": receipt["trial_results"][0]}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--mode", choices=["oracle", "noop", "fault"], default="oracle")
    args = parser.parse_args()
    run(args.source, args.output, args.mode)


if __name__ == "__main__":
    main()
