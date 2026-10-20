"""Private post-score routing and supervision; never determines business reward."""

from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys

from .backend import write_record
from .declarations import PROTOCOL, strict_json


SCHEMA = "harbor_claim_measurement_v1"
DIRECTORY = "claim_measurement"
IDENTITY = ("run_id", "skill_id", "event_index", "last_resolved_business_dispatch")
VERDICTS = ("supported", "contradicted", "unknown")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def consumer_for(task):
    if (task.get("skill_protocol") != PROTOCOL
            or type(task.get("checkpoint_action_count")) is not int
            or task["checkpoint_action_count"] != 0):
        return None
    config = task.get("config", {})
    if task.get("format") == "skillhorizon.internal_mcp_task.v1":
        return {("C25", "skillhorizon.c25.portfolio_network.v2"): "integrations.docops.claims",
                ("C06", "skillhorizon.c06.reach_network.v2"): "integrations.socialops.claims",
                ("C09", "skillhorizon.c09.freight_network.v2"): "integrations.logisticsops.claims",
                ("C11", "skillhorizon.c11.calibration_network.v2"): "integrations.researchops.claims",
                ("C50", "skillhorizon.c50.coupled_tolerance.v2"): "integrations.factoryops.claims"}.get(
                    (config.get("environment_id"), task.get("environment_format")))
    if task.get("execution_mode") != "procedural":
        return None
    if (task.get("format") == "skillhorizon.dataflow.v2"
            and config.get("business_program") == "credit_sales_v2"
            and config.get("workspace_profile") is None):
        return "skillhorizon.dataflow.claims"
    if (task.get("format") == "skillhorizon.operations.v1"
            and config.get("business_program") == "pressure_branches_v3"
            and config.get("family") == "staged_maintenance"):
        return "skillhorizon.fieldops.claims"
    return None


def read(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Private input symlink")
    value = strict_json(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Private record is not an object")
    return value


def input_hashes(task_root, state_root):
    paths = {"task": Path(task_root) / "task.json",
             "evaluation": Path(state_root) / "evaluation.json",
             "declarations": Path(state_root) / "declaration_claims.json"}
    for phase in ("SERVE", "VERIFY"):
        path = Path(state_root) / ("CODE_ORIGINS_" + phase + ".json")
        if path.exists() or path.is_symlink():
            paths[phase.lower() + "_origin"] = path
    result = {}
    for key, path in paths.items():
        if path.is_symlink():
            raise ValueError("Private input symlink")
        result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def require_business_source(state_root, manifest):
    for phase in ("SERVE", "VERIFY"):
        receipt = read(Path(state_root) / ("CODE_ORIGINS_" + phase + ".json"))
        if (receipt.get("status") != "passed" or receipt.get("phase") != phase.lower()
                or receipt.get("code_manifest_sha256") != manifest):
            raise ValueError("Claims source differs from sealed business source")


def sealed_inputs(task_root, state_root):
    task = read(Path(task_root) / "task.json")
    final = read(Path(state_root) / "evaluation.json")
    document = read(Path(state_root) / "declaration_claims.json")
    gateway = final.get("gateway", {})
    if (gateway.get("sealed") is not True or gateway.get("execution_unknown") is not False
            or "inflight" not in gateway or gateway["inflight"] is not None
            or final.get("business", {}).get("valid") is not True
            or type(final["business"].get("task_success")) is not bool
            or final.get("declarations") != document or document.get("valid") is not True
            or document.get("schema") != PROTOCOL or document.get("claims_file_valid") is False):
        raise ValueError("Claims require consistent sealed evidence")
    return task, document


def validate_measurement(value, document):
    if not isinstance(value, dict) or type(value.get("valid")) is not bool:
        raise ValueError("Malformed claim measurement")
    if not value["valid"]:
        return
    claims, rows = document["claims"], value["claims"]
    count = document["business_dispatches"]
    if type(count) is not int or count < 0 or not isinstance(claims, list) or not isinstance(rows, list):
        raise ValueError("Invalid claims coverage")
    for row in claims + rows:
        if (not isinstance(row, dict) or not isinstance(row.get("run_id"), str)
                or not isinstance(row.get("skill_id"), str)
                or type(row.get("event_index")) is not int or row["event_index"] < 1
                or type(row.get("last_resolved_business_dispatch")) is not int
                or not 0 <= row["last_resolved_business_dispatch"] <= count):
            raise ValueError("Invalid claim identity")
    if (len({c["run_id"] for c in claims}) != len(claims)
            or Counter(tuple(c[k] for k in IDENTITY) for c in claims)
            != Counter(tuple(c[k] for k in IDENTITY) for c in rows)):
        raise ValueError("Claim identity mismatch")
    for key, expected in (("completed_count", len(claims)), ("replay_dispatches", count),
                          ("visited_cuts", count + 1), ("additional_replay_passes", 1)):
        if type(value.get(key)) is not int or value[key] != expected:
            raise ValueError("Claim replay coverage mismatch")
    totals = Counter(row.get("goal_verdict") for row in rows)
    if set(totals) - set(VERDICTS) or value.get("process_adherence") != "unobserved":
        raise ValueError("Invalid verdict or process inference")
    reported = value.get("counts")
    if (not isinstance(reported, dict) or set(reported) != set(VERDICTS)
            or any(type(reported[k]) is not int or reported[k] != totals[k] for k in VERDICTS)):
        raise ValueError("Claim counts mismatch")
    fraction = value.get("assessable_fraction")
    if not claims:
        if fraction is not None:
            raise ValueError("Empty assessability must be null")
    elif (type(fraction) not in {int, float} or not math.isfinite(fraction)
          or abs(fraction - (totals["supported"] + totals["contradicted"]) / len(claims)) > 1e-12):
        raise ValueError("Assessability mismatch")


def run_worker(invocation_path, *, timeout, deadline=None):
    from .stop_copy import remaining
    invocation = read(invocation_path)
    code = Path(invocation["code_root"])
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TIKTOKEN_CACHE_DIR") if key in os.environ}
    env.update(PYTHONPATH=os.pathsep.join((str(code / "src"), str(code))), PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, "-B", "-m", "integrations.harbor.claims_worker", str(invocation_path)]
    with Path(invocation_path).with_name("worker.log").open("xb") as log:
        remaining(deadline, timeout)
        process = subprocess.Popen(command, cwd=code, env=env, stdin=subprocess.PIPE,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        failure = None
        try:
            if process.wait(timeout=remaining(deadline, timeout)) != 0:
                raise ValueError("Claims worker failed")
        except BaseException as error:
            failure = error
        # Attempt both cleanup operations without masking an active cancellation.
        cleanup_errors = []
        try:
            process.stdin.close()
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            process.wait(timeout=5)
        except BaseException as error:
            cleanup_errors.append(error)
        errors = ([failure] if failure is not None else []) + cleanup_errors
        for error in errors:
            if not isinstance(error, Exception):
                raise error
        if errors:
            raise errors[0]


def measure_claims(code_root, task_root, state_root, *, require_code_origins=True, timeout=120, deadline=None):
    code, task_root, state_root = map(lambda p: Path(p).resolve(), (code_root, task_root, state_root))
    output = state_root / DIRECTORY
    # Exclusive reservation survives interruption; never silently rerun a measurement.
    output.mkdir(mode=0o700, exist_ok=False)
    envelope = {"schema": SCHEMA, "status": "invalid", "consumer": None}
    try:
        task = read(task_root / "task.json")
        module = consumer_for(task)
        if module is None:
            envelope["status"] = "not_applicable" if task.get("skill_protocol") != PROTOCOL else "unsupported"
        else:
            task, document = sealed_inputs(task_root, state_root)
            invocation = {"code_root": str(code), "task_root": str(task_root), "state_root": str(state_root),
                          "consumer": module, "inputs": input_hashes(task_root, state_root),
                          "require_code_origins": require_code_origins,
                          "manifest": hashlib.sha256((code / "CODE_ORIGINS.json").read_bytes()).hexdigest()
                          if require_code_origins else None}
            if require_code_origins:
                require_business_source(state_root, invocation["manifest"])
            envelope.update(consumer=module, invocation_digest=digest(invocation))
            write_record(output / "invocation.json", invocation)
            options = {"deadline": deadline} if deadline is not None else {}
            run_worker(output / "invocation.json", timeout=timeout, **options)
            if input_hashes(task_root, state_root) != invocation["inputs"]:
                raise ValueError("Claims inputs changed")
            value = read(output / "consumer.json")
            receipt = read(output / "receipt.json")
            if (receipt.get("invocation_digest") != digest(invocation)
                    or receipt.get("measurement_digest") != digest(value)
                    or receipt.get("consumer") != module or receipt.get("manifest") != invocation["manifest"]):
                raise ValueError("Claims receipt mismatch")
            if require_code_origins:
                if (hashlib.sha256((code / "CODE_ORIGINS.json").read_bytes()).hexdigest() != invocation["manifest"]
                        or any(receipt.get(phase, {}).get("status") != "passed"
                               or receipt[phase].get("code_manifest_sha256") != invocation["manifest"]
                               for phase in ("before", "after"))):
                    raise ValueError("Claims source changed")
            validate_measurement(value, document)
            envelope.update(status="valid" if value["valid"] else "invalid", measurement=value)
    except Exception as error:
        envelope.update(status="invalid", failure_type=type(error).__name__)
    write_record(output / "result.json", envelope)
    return envelope


def measure_trial(export_root, result_path, *, timeout=120, deadline=None):
    """Run after native Harbor has returned and its business result is fixed."""
    private = Path(export_root) / "environment/business"
    state = Path(result_path).parent / "business_private"
    try:
        options = {"deadline": deadline} if deadline is not None else {}
        return measure_claims(private / "code", private / "task", state, timeout=timeout, **options)
    except Exception as error:
        # A failed/reused postprocessing attempt cannot turn a valid trial into failure.
        return {"schema": SCHEMA, "status": "invalid", "failure_type": type(error).__name__}


def summary(state_root):
    output = Path(state_root) / DIRECTORY
    if not output.exists():
        return {"status": "missing"}
    try:
        envelope = read(output / "result.json")
        if envelope.get("schema") != SCHEMA or envelope.get("status") not in {
                "valid", "invalid", "unsupported", "not_applicable"}:
            raise ValueError("Invalid measurement envelope")
        status = envelope["status"]
        if status != "valid":
            return {"status": status}
        invocation = read(output / "invocation.json")
        if (envelope.get("invocation_digest") != digest(invocation)
                or Path(invocation["state_root"]).resolve() != Path(state_root).resolve()
                or envelope.get("consumer") != invocation["consumer"]
                or input_hashes(invocation["task_root"], state_root) != invocation["inputs"]):
            raise ValueError("Stale measurement")
        _, document = sealed_inputs(invocation["task_root"], state_root)
        value = envelope["measurement"]
        receipt = read(output / "receipt.json")
        if (read(output / "consumer.json") != value
                or receipt.get("invocation_digest") != digest(invocation)
                or receipt.get("measurement_digest") != digest(value)
                or receipt.get("consumer") != invocation["consumer"]
                or receipt.get("manifest") != invocation["manifest"]):
            raise ValueError("Measurement receipt mismatch")
        if invocation["require_code_origins"] and any(
                receipt.get(phase, {}).get("status") != "passed"
                or receipt[phase].get("code_manifest_sha256") != invocation["manifest"]
                for phase in ("before", "after")):
            raise ValueError("Measurement attestation mismatch")
        if invocation["require_code_origins"]:
            require_business_source(state_root, invocation["manifest"])
        validate_measurement(value, document)
        if value["valid"] is not True:
            raise ValueError("Invalid consumer")
        return {"status": "valid", "completed_count": value["completed_count"],
                "counts": value["counts"], "assessable_fraction": value["assessable_fraction"],
                "replay_dispatches": value["replay_dispatches"]}
    except Exception:
        return {"status": "invalid"}
