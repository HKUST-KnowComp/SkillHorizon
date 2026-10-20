"""Read only this migration trial's artifacts and Docker metadata."""

import argparse
import json
from pathlib import Path
import subprocess

from .backend import write_record


def docker(*args, deadline=None):
    from .stop_copy import remaining
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True,
                          timeout=remaining(deadline, 30)).stdout


def audit(root, *, receipt=None, task=None, deadline=None):
    root = Path(root).resolve()
    if receipt is None:
        receipt = json.loads((root / "RUN_RECEIPT.json").read_text())
    task = root / "harbor-task" if task is None else Path(task).resolve()
    if not task.is_relative_to(root):
        raise ValueError("Task outside this qualification root")
    if len(receipt["trial_results"]) != 1:
        raise ValueError("Exactly one trial required")
    result_path = Path(receipt["trial_results"][0])
    if not result_path.is_relative_to(root / "jobs"):
        raise ValueError("Trial path outside this job")
    trial = result_path.parent
    result = json.loads(result_path.read_text())
    report = json.loads((trial / "business_private/evaluation.json").read_text())
    if not result.get("finished_at"):
        raise ValueError("Trial did not finish")
    mode = receipt["mode"]
    if mode not in {"oracle", "noop", "fault", "interface"}:
        raise ValueError("Unknown audit mode")
    incomplete = mode in {"noop", "interface"}
    reward = (result.get("verifier_result") or {}).get("rewards", {}).get("reward")
    exception = result.get("exception_info")
    expected_verifier = "integrations.harbor.verifier:SkillHorizonVerifier"
    if mode == "fault":
        expected_verifier = "integrations.harbor.controls:InjectedFailureVerifier"
        if reward is not None or "INJECTED_ZERO_PROVIDER_VERIFIER_FAILURE_AFTER_EVIDENCE_COLLECTION" not in json.dumps(exception):
            raise ValueError("Fault control did not fail for the intended reason")
    elif exception is not None or type(reward) not in {int, float} or reward != (0.0 if incomplete else 1.0):
        raise ValueError("Unexpected trial error or business reward")
    if result["config"]["verifier"]["import_path"] != expected_verifier:
        raise ValueError("Wrong verifier selected")
    if (report["business"]["valid"] is not True
            or report["business"]["task_success"] is not (not incomplete)):
        raise ValueError("Business replay/outcome mismatch")
    gateway = report["gateway"]
    if not gateway["sealed"] or gateway["execution_unknown"] or gateway["inflight"]:
        raise ValueError("Unsealed or unknown business state")
    if (type(gateway["dispatched"]) is not int
            or (gateway["dispatched"] == 0) != (mode == "noop")):
        raise ValueError("Unexpected business action count")
    if mode == "interface" and (receipt["cli_return_code"] != 0 or gateway["dispatched"] != 1):
        raise ValueError("Interface control requires exactly one business dispatch")
    exit_code = trial / "agent/exit-code.txt"
    if exit_code.exists() and exit_code.read_text().strip() != "0":
        raise ValueError("Oracle did not execute successfully")
    options = {"deadline": deadline} if deadline is not None else {}
    ids = docker("ps", "-aq", "--filter", "label=com.docker.compose.project.working_dir=" + str(task / "environment"), **options).split()
    metadata = []
    fmt = ('{"name":{{json .Name}},"state":{{json .State.Status}},"mounts":{{json .Mounts}},'
           '"networks":{{json .NetworkSettings.Networks}},"privileged":{{json .HostConfig.Privileged}},'
           '"cap_drop":{{json .HostConfig.CapDrop}},"service":{{json (index .Config.Labels "com.docker.compose.service")}}}')
    for identity in ids:
        metadata.append(json.loads(docker("inspect", "--format", fmt, identity, **options)))
    services = {item["service"]: item for item in metadata}
    if len(services) != len(metadata):
        raise ValueError("Duplicate service containers in this export")
    if not {"main", "business"} <= services.keys():
        raise ValueError("Expected stopped retained containers are absent")
    for item in services.values():
        if item["state"] != "exited" or item["privileged"] or "ALL" not in (item["cap_drop"] or []):
            raise ValueError("Container state or permissions differ")
    allowed_mounts = {str(trial / "agent"), str(trial / "verifier"), str(trial / "artifacts/logs/artifacts")}
    for mount in services["main"]["mounts"]:
        if mount["Type"] != "bind" or mount["Source"] not in allowed_mounts:
            raise ValueError("Unexpected main mount, including private paths or ancestors")
    for item in services.values():
        if not item["networks"]:
            raise ValueError("Missing isolated task network")
        for network in item["networks"]:
            if json.loads(docker("network", "inspect", "--format", "{{json .Internal}}", network, **options)) is not True:
                raise ValueError("Task container has external network")
    summary = {"mode": mode, "provider_calls": 0, "trial": str(trial), "cli_return_code": receipt["cli_return_code"],
               "reward": reward, "business_success": report["business"]["task_success"],
               "valid_replay": True, "actions": gateway["dispatched"], "sealed": True,
               "expected_exception": bool(exception), "retained_stopped_containers": True,
               "oracle_exit_code_file_present": exit_code.exists(),
               "main_mount_allowlist_passed": True, "internal_network_only": True,
               "containers": metadata, "status": "passed"}
    write_record(root / "LIFECYCLE_AUDIT.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.root)))


if __name__ == "__main__":
    main()
