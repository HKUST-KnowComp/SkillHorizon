"""Formal report identity for the six handed-off internal declaration tasks."""

import hashlib
from pathlib import Path

from .claim_measurement import digest, read


DOMAINS = {"C09", "C11", "C50"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def ordinary(path):
    path = Path(path)
    require(path.is_absolute() and path == path.resolve() and path.is_file(),
            "Ordinary qualification input required")
    return path


def file_hash(path):
    return hashlib.sha256(ordinary(path).read_bytes()).hexdigest()


def equal(left, right):
    return digest(left) == digest(right)


def integer(value, expected):
    return type(value) is int and value == expected


def formal_binding(row, calls):
    domain = row["label"].split("-", 1)[0]
    require(domain in DOMAINS, "Unsupported formal binding domain")
    original, copied = Path(row["original_task"]), Path(row["task"])
    task, original_task = read(copied / "task.json"), read(original / "task.json")
    require(equal(task, original_task) and task["format"] == "skillhorizon.internal_mcp_task.v1"
            and task["config"]["environment_id"] == domain, "Formal task/domain identity differs")
    status_path = ordinary(row["historical_status"])
    generation_path = ordinary(row["historical_generation"])
    report_path = ordinary(row["historical_business"])
    qualification, bundle = original.parent, original.parent / "bundle"
    require(status_path == qualification / "STATUS.json" and report_path == bundle / "EVALUATION.json"
            and generation_path == bundle / "REFERENCE_GENERATION.json", "Formal report location differs")
    status, report = read(status_path), read(report_path)
    imported = status["framework_import"]
    require(status["status"] == "passed" and status["phase"] == "complete"
            and integer(status["provider_calls"], 0) and integer(status["model_calls"], 0)
            and equal(status["business"], report), "Formal qualification outcome differs")
    require(imported["task_id"] == task["task_id"] and imported["environment_id"] == domain
            and isinstance(task["instance_identity"], dict)
            and equal(imported["instance_identity"], task["instance_identity"])
            and imported["output"] == str(original) and status["framework_task"] == str(original)
            and status["bundle"] == str(bundle) and integer(imported["prefix_calls"], 0)
            and integer(imported["reference_suffix_calls"], calls) and integer(imported["provider_calls"], 0),
            "Formal imported instance identity differs")
    generation = read(generation_path)
    contract, origin = read(copied / "REFERENCE_CONTRACT.json"), read(copied / "REFERENCE_ORIGIN.json")
    require(equal(status["reference_contract"], contract)
            and equal(status["reference_generation"], generation) and equal(origin, generation)
            and equal(contract["lineage"], generation), "Formal reference generation differs")
    if domain == "C50":
        require(report["task_id"] == task["task_id"], "Formal C50 business identity differs")
    require(type(report["accepted"]) is bool, "Formal accepted must be a boolean")
    return report["accepted"]


def supplement_binding(row):
    require(row["label"] == "C09-normal", "Unexpected historical supplement")
    supplement = ordinary(row["historical_supplement"])
    original, candidate = Path(row["original_task"]), Path(row["original_candidate"])
    case = candidate / "qualification/normal/case"
    require(candidate.is_absolute() and candidate == candidate.resolve()
            and original == case / "qualification/task", "Supplement original location differs")
    report = read(supplement)
    require(report["status"] == "passed" and report["operation"] == "readonly_v2_normal_postprocess"
            and report["original_root"] == str(candidate) and report["original_status"] == "failed"
            and report["original_unchanged"] is True
            and report["completion"] == "normal_postprocessed_not_original_run_passed",
            "Supplement does not bind the preserved failed normal")
    for key in ("native_dispatches", "assessments", "additional_replay_passes", "provider_calls", "model_calls"):
        require(integer(report[key], 0), "Supplement performed additional work")
    required = {"STATUS.json", "qualification/STATUS.json", "qualification/task/task.json",
                "qualification/bundle/EVALUATION.json", "claims/declaration_claims.json",
                "claims/CLAIMS.json", "claims/runtime/model_skill_actions.jsonl"}
    files = report["input_files"]
    require(isinstance(files, dict) and required <= set(files), "Incomplete supplement input binding")
    for relative, expected in files.items():
        path = Path(relative)
        require(not path.is_absolute() and ".." not in path.parts and path.as_posix() == relative,
                "Unsafe supplement relative path")
        require(file_hash(case / path) == expected, "Supplement original input changed")
    receipts = report["original_receipts"]
    required_receipts = {str(candidate / "qualification/STATUS.json"),
                         str(candidate / "qualification/normal/PROCESS.json")}
    require(set(receipts) == required_receipts, "Supplement original receipt set differs")
    for path, expected in receipts.items():
        require(file_hash(path) == expected, "Supplement original receipt changed")
    require(read(case / "STATUS.json")["status"] == "failed"
            and read(candidate / "qualification/STATUS.json")["status"] == "failed",
            "Original normal failure was replaced")
    return file_hash(supplement)
