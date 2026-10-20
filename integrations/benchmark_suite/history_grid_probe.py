"""B1b successor diagnostics with frozen B0 parameter comparison, not grid cells."""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys

from skillhorizon.util import canonical_json, load_json, write_json, sha256_value
from skillhorizon.experiment.config import RunConfig
from skillhorizon.experiment.history_measurement import load_reference, contract
from .catalog import resolve_profile
from .build import build_profile
from .history_bands import ROOT, bounded_worker, disk_bytes, implementation_identity, require_remote
from .history_coverage import backends, band_for
from .history_grid import PILOT_SEED, ENVIRONMENTS
from .reference_policy import bound_reference, reference_actions
from .reference_lag import from_episode

REVISION = "history_grid_b1b_reference_probe_v1"
OVERRIDES = {
    "DataOps": {"regions": 2, "periods": 3, "rows_per_region": 3, "business_scenario": "credit_horizon"},
    "FieldOps": {"groups": 8}, "ProcureOps": {"groups": 8},
    "C09": {"cohorts": 2, "legs": 3, "custody_span": 3},
    "C25": {"scenario": "standard", "width": 2, "depth": 2, "amendment": True},
    "C06": {"campaigns": 2, "waves": 2},
    "C47": {"scenario": "standard", "units": 2, "periods": 2, "resources_per_unit": 2},
    "C50": {"levels": 2, "terminal_quantity": 1},
    "C01": {"scenario": "standard", "cases": 2, "phases": 2},
    "C11": {"scenario": "standard", "sections": 2, "rounds": 2, "studies_per_section": 1,
            "alias_copies": 0, "distractors_per_section": 0},
}
for _domain in ENVIRONMENTS:
    if _domain.startswith("C"):
        OVERRIDES[_domain]["background"] = False
LIMITS = {"candidate_seconds": 3600, "candidate_bytes": 8*1024**3,
          "total_bytes": 160*1024**3, "rss_bytes": 16*1024**3, "log_bytes": 256*1024**2}


def fixed_profiles():
    baseline = load_json(ROOT / "protocols/history_grid_b0_parameters.json")
    if set(baseline["profiles"]) != set(ENVIRONMENTS):
        raise ValueError("Frozen predecessor domains differ")
    profiles = {d: resolve_profile(d, seed=PILOT_SEED, overrides=OVERRIDES[d]) for d in ENVIRONMENTS}
    for domain, profile in profiles.items():
        expected = baseline["profiles"][domain]
        parameters = dict(expected["parameters"])
        if domain.startswith("C"):
            if parameters["background"] is not True:
                raise ValueError("Predecessor did not retain background")
            parameters["background"] = False
        actual = {k: profile[k] for k in expected}
        if actual != {**expected, "parameters": parameters}:
            raise ValueError("Unapproved change from full predecessor profile: " + domain)
    return profiles, baseline


def runtime_identity():
    packages = {name: importlib.metadata.version(dist) for name, dist in
                (("agents", "openai-agents"), ("mcp", "mcp"), ("tiktoken", "tiktoken"))}
    locations = {name: str(Path(importlib.util.find_spec(name).origin).resolve()) for name in packages}
    if any(not Path(p).is_relative_to(Path(sys.prefix).resolve()) for p in locations.values()):
        raise ValueError("Runtime modules are outside the approved interpreter environment")
    return {"invocation": sys.executable, "executable": str(Path(sys.executable).resolve()),
            "prefix": sys.prefix, "version": sys.version, "packages": packages, "locations": locations}


def verify_cache(cache):
    cache = Path(cache)
    expected = {"o200k_base": "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"}
    for name, digest in expected.items():
        url = f"https://openaipublic.blob.core.windows.net/encodings/{name}.tiktoken"
        path = cache / hashlib.sha1(url.encode()).hexdigest()
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Approved tokenizer cache missing or invalid; downloads are not allowed")


def controlled_env(cache, temporary):
    verify_cache(cache)
    # Do not inspect or inherit arbitrary parent variables or credentials.
    return {"PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
            "PYTHONPATH": ":".join(str(p) for p in (ROOT / "src", ROOT, ROOT / "integrations/agents_sdk", ROOT / "scripts")),
            "TIKTOKEN_CACHE_DIR": str(cache), "TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary),
            "HOME": str(temporary / "home"), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "OPENAI_AGENTS_DISABLE_TRACING": "true"}


def nested_identity(suite, cache):
    alias = ROOT / ".venv/bin/python"
    outer = runtime_identity()
    if (not alias.is_file() or alias.resolve() != Path(sys.executable).resolve()
            or (ROOT / ".venv").resolve() != Path(sys.prefix).resolve()):
        raise ValueError("Control/MCP alias does not resolve to the approved runtime")
    command = [str(alias), "-c", "import json; from integrations.benchmark_suite.history_grid_probe import runtime_identity; print(json.dumps(runtime_identity()))"]
    result = subprocess.run(command, cwd=ROOT, env=controlled_env(cache, suite / "preflight"),
                            capture_output=True, text=True, check=True, timeout=30)
    import json
    nested = json.loads(result.stdout)
    if (nested["invocation"] != str(alias) or Path(nested["prefix"]).resolve() != Path(outer["prefix"]).resolve()
            or any(nested[k] != outer[k] for k in ("executable", "version", "packages", "locations"))):
        raise ValueError("Actual control/MCP interpreter identity differs")
    return nested


def exclusive_json(path, record):
    """Durable once-only launch/terminal receipts, not mutable progress snapshots."""
    path = Path(path)
    with path.open("x") as stream:
        stream.write(canonical_json(record) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def make_plan(root, *, suite, cache):
    suite, root, cache = Path(suite), Path(root), Path(cache)
    if (suite != suite.resolve() or ROOT != suite / "source" or root != suite / "probes"
            or cache != cache.resolve() or not cache.is_dir()):
        raise ValueError("Source, suite, output or cache is outside its explicit layout")
    verify_cache(cache)
    profiles, baseline = fixed_profiles()
    return {"revision": REVISION, "root": str(root), "seed": PILOT_SEED,
            "profiles": profiles, "backends": backends(profiles), "implementation": implementation_identity(),
            "predecessor_parameters": baseline,
            "measurement": contract(), "limits": LIMITS, "attempts": 10, "provider_calls": 0,
            "suite": str(suite), "source": str(ROOT), "cache": str(cache), "runtime": runtime_identity(),
            "mcp_python": nested_identity(suite, cache),
            "control_export_command": [str(ROOT / ".venv/bin/python"), "-m", "skillhorizon"],
            "worker_command": [sys.executable, "-m", "integrations.benchmark_suite.history_grid_probe", "worker"],
            "control_command": [sys.executable, str(ROOT / "integrations/agents_sdk/control.py"), "prepare"],
            "scope": "b1b_small_profile_successor_diagnostic_not_grid_qualification",
            "family_scope": "Some minimum families differ from legacy composites; no cross-band causal claim",
            "budget_scope": "Additional reviewed B1b ten diagnostics; no calibration or replication attempts",
            "parallel_candidates": 1, "reviewer": "gpt-6-astra/high Boole",
            "c09_revision": "custody_span=3 retains first-hub through-baggage; span1 proposal never executed"}


def worker(root, domain, plan_path):
    require_remote(root)
    plan = load_json(plan_path)
    suite = Path(plan["root"])
    start = load_json(suite / f"{domain}.START.json")
    if (plan != make_plan(suite, suite=Path(plan["suite"]), cache=Path(plan["cache"]))
            or domain not in ENVIRONMENTS or root != suite / domain
            or start["controller_pid"] != os.getppid() or start["environment"] != domain
            or start["argv"] != [*plan["worker_command"], str(root), "--environment", domain, "--plan", str(plan_path)]):
        raise ValueError("Probe is outside its frozen supervised plan")
    return measure_candidate(root, domain, plan["profiles"][domain], plan,
        validate=lambda: plan == make_plan(suite, suite=Path(plan["suite"]), cache=Path(plan["cache"])))


def measure_candidate(root, domain, profile, plan, *, validate):
    """Shared real construction/import/SDK path; controller owns admission."""
    from .pipeline_timing import PhaseRecorder
    notes = (["Ops construction includes its builder's internal reference execution"]
             if not domain.startswith("C") else [])
    with PhaseRecorder(root / "PIPELINE_TIMINGS.json", progress=lambda name: phase(root, name),
                       notes=notes) as timing:
        return _measure_candidate(root, domain, profile, plan, validate=validate, timing=timing)


def _measure_candidate(root, domain, profile, plan, *, validate, timing):
    timing.stage("plan_validation")
    if not validate():
        raise ValueError("Candidate plan changed before construction")
    construction = build_profile(domain, root / "construction", seed=plan["seed"], profile=profile,
                                 progress=timing.stage, phase_failure=timing.failed)
    task = Path(construction["task"])
    if domain.startswith("C"):
        timing.stage("backend_binding")
        if construction["reference_policy"]["backend"] != plan["backends"][domain]:
            raise ValueError("Constructed backend differs")
        timing.stage("import")
        command = [sys.executable, "-m", "integrations.internal_mcp.framework", "import", domain,
                   str(root / "task"), "--bundle", str(task), "--trace", str(task / "trace.json")]
        exclusive_json(root / "IMPORT_ARGV.json", command)
        subprocess.run(command, check=True)
        task = root / "task"
    timing.stage("reference_binding")
    binding = bound_reference(task)
    actions = reference_actions(task)
    turns = math.ceil(len(actions)/8)
    config = RunConfig(context_window=524288, max_output_tokens=32768, parallel_tool_calls=True,
                      max_turns=turns+2, max_provider_requests=turns+66, max_tool_calls=len(actions)+1,
                      max_total_tokens=100000000)
    write_json(root / "config.json", config.to_dict())
    timing.stage("sdk_prepare_replay")
    command = [*plan["control_command"], str(root / "episode"), "--task", str(task), "--config", str(root / "config.json"),
               "--reference-batch-size", "8"]
    exclusive_json(root / "CONTROL_ARGV.json", command)
    subprocess.run(command, check=True)
    timing.stage("qualification_binding")
    qualification = load_json(root / "episode/QUALIFICATION.json")
    if (not qualification["passed"] or binding != load_json(root / "episode/REFERENCE_CONTRACT.json")
            or binding != bound_reference(task)
            or not validate()):
        raise ValueError("Probe reference or implementation changed")
    timing.stage("history_measurement")
    reference = load_reference(root / "episode/fixture")
    timing.stage("public_lag")
    try:
        lag = {"status": "available", "measurement": from_episode(root / "episode/fixture",
                    [node["id"] for node in construction["executed_dependencies"]["nodes"]])}
    except (ValueError, KeyError, OSError) as error:
        lag = {"status": "unavailable", "reason": type(error).__name__}
        timing.unavailable()
    timing.stage("result_write")
    result = {"environment": domain, "diagnostic_passed": True, "grid_qualified": False,
              "provider_calls": 0, "task": str(task), "seed": plan["seed"], "profile": profile,
              "reference_tokens": reference["reference_tokens"], "observed_band": band_for(reference["reference_tokens"]),
              "reference": reference, "reference_binding": binding, "reference_calls": len(actions),
              "business_obligations": construction["business_obligations"],
              "executed_dependencies": construction["executed_dependencies"],
              "observed_inventory": construction.get("observed_inventory"),
              "public_reference_uses": lag,
              "mechanism_qualification": {"status": "unverified", "reason": "Not inferred from diagnostic business success"},
              "qualification": str(root / "episode/QUALIFICATION.json")}
    exclusive_json(root / "RESULT.json", result)
    return result


def phase(root, name):
    write_json(root / "PHASE.json", {"phase": name})


def run(root, approval_path):
    require_remote(root)
    approval = load_json(approval_path)
    plan = approval["plan"]
    if (Path(approval_path).resolve().is_relative_to(root) or approval.get("approved") is not True
            or approval.get("plan_digest") != sha256_value(plan) or str(root) != plan["root"]
            or plan != make_plan(root, suite=Path(plan["suite"]), cache=Path(plan["cache"]))):
        raise ValueError("Independent approval does not match this unique run")
    root.mkdir()  # Every existing root, including an empty one, is rejected.
    exclusive_json(root / "PLAN.json", plan)
    rows = []
    for domain in ENVIRONMENTS:
        used = disk_bytes(Path(plan["suite"]))
        available = shutil.disk_usage(root).free
        if (used + LIMITS["candidate_bytes"] > LIMITS["total_bytes"]
                or available < LIMITS["candidate_bytes"]*2):
            raise ValueError("No disk headroom for a full candidate reservation")
        target = root / domain
        command = [*plan["worker_command"], str(target), "--environment", domain, "--plan", str(root / "PLAN.json")]
        exclusive_json(root / f"{domain}.START.json", {"environment": domain, "argv": command,
            "controller_pid": os.getpid(), "revision": REVISION, "reserved_bytes": LIMITS["candidate_bytes"]})
        (target / "tmp").mkdir(parents=True)
        (target / "tmp/home").mkdir()
        env = controlled_env(Path(plan["cache"]), target / "tmp")
        from skillhorizon.experiment.construction_envelope import MARKER, DOMAIN, DIGEST
        env.update({MARKER: str(Path(approval_path).resolve()), DOMAIN: domain, DIGEST: approval["plan_digest"]})
        resources = bounded_worker(command, target, Path(plan["suite"]),
            root / f"{domain}.log", LIMITS, env=env)
        result_path = target / "RESULT.json"
        passed = resources["returncode"] == 0 and resources["failure"] is None and result_path.is_file()
        result = load_json(result_path) if passed else {"environment": domain, "diagnostic_passed": False,
            "grid_qualified": False, "provider_calls": 0, "mechanism_qualification": {
                "status": "unverified", "reason": "Diagnostic failed before separate mechanism qualification"}}
        failure = None if passed else {
            "category": resources["failure"] or ("worker_failed" if resources["returncode"] != 0 else "missing_result"),
            "phase": load_json(target / "PHASE.json").get("phase") if (target / "PHASE.json").exists() else "worker_start",
            "detail": load_json(target / "FAILURE.json") if (target / "FAILURE.json").exists() else None,
            "model_failure": False}
        row = {**result, "resources": resources, "failure": failure}
        exclusive_json(root / f"{domain}.TERMINAL.json", row)
        rows.append(row)
        write_json(root / "PROBES.json", {"revision": REVISION, "rows": rows, "provider_calls": 0,
            "diagnostics_terminal": len(rows), "diagnostics_passed": sum(r["diagnostic_passed"] for r in rows),
            "mechanisms_verified": sum(r["mechanism_qualification"]["status"] == "verified" for r in rows),
            "qualified_grid_slots": 0, "complete": False})
        print(domain, result["diagnostic_passed"], result.get("reference_tokens"), resources["failure"], flush=True)
        if (not resources["quiescent"] or resources.get("monitor_error_class") is not None
                or resources["failure"] in {"total_storage_limit", "resource_monitor_failed"}):
            raise ValueError("Probe cleanup or global resource limit requires audit")
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "worker"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--environment", choices=ENVIRONMENTS)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--approval", type=Path)
    args = parser.parse_args()
    if args.mode == "worker":
        try:
            worker(args.root.resolve(), args.environment, args.plan)
        except Exception as error:
            exclusive_json(args.root / "FAILURE.json", {"error_class": type(error).__name__, "model_failure": False})
            raise SystemExit(1)
    else:
        run(args.root.resolve(), args.approval)
