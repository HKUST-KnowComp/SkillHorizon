"""Complete admission gate for one known-action native interface control."""

import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

from .audit import audit
from .claim_measurement import sealed_inputs, validate_measurement, summary
from .native_origins import load, sha, validate as validate_origins
from .stop_copy import remaining


def require(condition, message):
    if not condition:
        raise ValueError(message)


def jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line]


def check_evidence(receipt, definition, state, events, declarations, measured, observations,
                   observer_events, fixture_requests, fixture_failures, cached_payloads):
    jobs = receipt["jobs"]
    require(receipt.get("all_trials_valid") is True and len(jobs) == 1, "Native single trial invalid")
    job, provider = jobs[0], receipt["provider"]
    trial = job["trial"]
    require(job["cli_return_code"] == 0 and trial.get("valid_execution") is True
            and trial.get("business_success") is False and type(trial.get("reward")) in {int, float}
            and trial["reward"] == 0 and type(trial.get("actions")) is int and trial["actions"] == 1,
            "Expected valid one-call incomplete business outcome")
    gateway = state["gateway"]
    require(gateway["sealed"] is True and gateway["execution_unknown"] is False and gateway["inflight"] is None
            and type(gateway["dispatched"]) is int and gateway["dispatched"] == 1
            and gateway["attempts"] == 1 and gateway.get("admission_rejections", {}) == {},
            "Business journal has unexpected activity")
    require(len(events) == 2 and [e["phase"] for e in events] == ["dispatch", "result"], "Business event coverage")
    dispatch = events[0]
    require(dispatch["name"] == definition["action"]["name"] and dispatch["arguments"] == {}
            and dispatch["sequence"] == events[1]["sequence"] == 1 and dispatch["event_index"] == 2
            and dispatch["attribution"].get("skill_run_id") == definition["run_id"], "Business operation differs")
    require(len(declarations) == 2, "Exactly two declarations required")
    for row, name, args, index, cut in ((declarations[0], "begin_skill", definition["begin"], 1, 0),
                                      (declarations[1], "finish_skill", definition["finish"], 3, 1)):
        require(row["name"] == name and row["arguments"] == args and row["event_index"] == index
                and row["last_resolved_business_dispatch"] == cut and row["outcome"] == "accepted",
                "Declaration identity or completion cut differs")
    document = state["declarations"]
    expected = {"run_id": definition["run_id"], "skill_id": definition["skill_id"],
                "event_index": 3, "last_resolved_business_dispatch": 1}
    require(document["valid"] is True and document["claims"] == [expected]
            and document["declaration_attempts"] == 2, "Completed declaration differs")
    require(measured["status"] == "valid", "Claims measurement invalid")
    validate_measurement(measured["measurement"], document)
    require(measured["measurement"]["completed_count"] == 1, "Single completed claim required")
    require(observations.get("observation_valid") is True, "Native observations invalid")
    starts = [e["request_id"] for e in observer_events if e["kind"] == "actor_query_start"]
    ends = [e["request_id"] for e in observer_events if e["kind"] == "actor_query_end"]
    require(len(starts) == len(set(starts)) == 2 and starts == ends, "Native query coverage differs")
    records = provider["records"]
    require(provider["attempts"] == 2 and provider["pending"] is None
            and provider["accounting"]["task"]["responses"] == 2
            and provider["accounting"]["infra"]["failures"] == 0
            and len(records) == 2 and [r["request_id"] for r in records] == starts
            and all(r["status"] == 200 and r["task_accepted"] is True for r in records), "Fixture transport coverage differs")
    require(len(fixture_requests) == 2 and fixture_failures == [], "Fixture request sequence invalid")
    require([cached_payloads[rid] for rid in starts] == fixture_requests, "Fixture payload/request identity differs")
    forbidden = ("REFERENCE_CONTRACT", "REFERENCE_ORIGIN", "CODE_ORIGINS", "declaration_claims.json",
                 "evaluation.json", "/solution/", "controller_origins", "expected_verdict")
    require(not any(word in json.dumps(fixture_requests) for word in forbidden), "Private control data in actor input")
    return {"business_success": False, "business_calls": 1, "declarations": 2,
            "claims": measured["measurement"]["counts"], "fake_provider_responses": 2, "external_provider_calls": 0}


def image_members(tar, expected, *, deadline):
    actual, checked = {}, 0
    private_names = {"evaluation.json", "declaration_claims.json", "claims_worker.py", "score_worker.py",
        "EXPECTED.json", "CLAIMS.json", "CONTROLLER_EXCEPTION.json"}
    for member in tar:
        remaining(deadline, 1)
        name = member.name.removeprefix("./").rstrip("/")
        parts = Path(name).parts
        require(not name.startswith("/") and ".." not in parts, "Unsafe image member")
        require(not any(part in private_names or part.startswith(("REFERENCE_", "CODE_ORIGINS"))
            or part in {"controller_origins", "claim_measurement", "internal_mcp"} for part in parts),
            "Private artifact anywhere in image")
        require(not any(name == p or name.startswith(p + "/") for p in
            ("task", "state", "code", "solution", "controller_origins")), "Private image content")
        if name in expected:
            require(member.isfile(), "Public asset is not a regular file")
            require(name not in actual, "Duplicate public image asset")
            actual[name] = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
        elif name.startswith("skills/") or name.startswith("opt/skillhorizon/"):
            cache = name == "opt/skillhorizon/__pycache__/client.cpython-312.pyc" and member.isfile()
            require(member.isdir() or cache, "Unexpected public image asset")
        checked += 1
    require(actual == expected, "Installed public image assets differ")
    return actual, checked


def image_audit(task, isolation, output, *, deadline):
    main = next(item for item in isolation["containers"] if item["service"] == "main")
    expected = {"opt/skillhorizon/client.py": sha(Path(task) / "environment/public/client.py")}
    for path in (Path(task) / "environment/public/skills").rglob("*"):
        if path.is_file():
            expected["skills/" + path.relative_to(Path(task) / "environment/public/skills").as_posix()] = sha(path)
    archive = Path(output) / "main-container.tar"
    with archive.open("xb") as stream:
        subprocess.run(["docker", "export", main["name"]], stdout=stream, stderr=subprocess.PIPE,
                       check=True, timeout=remaining(deadline, 30))
    with tarfile.open(archive) as tar:
        actual, checked = image_members(tar, expected, deadline=deadline)
    # Actual stopped container metadata supplies merged runtime mounts/networks;
    # retain the Compose source and expanded build configuration as separate evidence.
    environment = Path(task) / "environment"
    result = subprocess.run(["docker", "compose", "-f", str(environment / "docker-compose.yaml"), "config"],
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "CONTEXT_DIR": str(environment)},
        capture_output=True, text=True, check=True, timeout=remaining(deadline, 15))
    (Path(output) / "compose-expanded.yaml").write_text(result.stdout)
    return {"status": "passed", "archive_sha256": sha(archive), "entries_checked": checked,
            "public_assets_matched": len(actual)}


def accept(root, task, receipt, definition, actor, proxy, *, deadline):
    root, task = Path(root), Path(task)
    result = Path(receipt["jobs"][0]["trial"]["trial_result"])
    trial, state = result.parent, result.parent / "business_private"
    sealed_inputs(task / "environment/business/task", state)
    require(summary(state)["status"] == "valid", "Claims receipts invalid")
    cached = {key: json.loads(value[0]) for key, value in proxy._cache.items()}
    outcome = check_evidence(receipt, definition, load(state / "evaluation.json"),
        jsonl(state / "gateway.jsonl"), jsonl(state / "declarations.jsonl"),
        load(state / "claim_measurement/result.json"), receipt["jobs"][0]["observations"],
        jsonl(trial / "controller_observation/events.jsonl"), actor.requests, actor.failures, cached)
    outcome["native_origins"] = validate_origins(root / (definition["domain"] + "-origins.json"), trial)
    isolation = audit(root, receipt={"mode": "interface", "cli_return_code": 0,
        "trial_results": [str(result)]}, task=task, deadline=deadline)
    outcome["isolation"] = {"status": isolation["status"], "containers_stopped": True}
    outcome["image"] = image_audit(task, isolation, root, deadline=deadline)
    outcome["status"] = "passed"
    return outcome
