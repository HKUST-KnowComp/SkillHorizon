"""Opt-in resource layout for the reviewed zero-provider construction run."""

import os
from pathlib import Path
import sys

from skillhorizon.util import load_json, sha256_value

SOURCE = Path(__file__).resolve().parents[3]
MARKER = "SKILLHORIZON_CONSTRUCTION_APPROVAL"
DOMAIN = "SKILLHORIZON_CONSTRUCTION_DOMAIN"
DIGEST = "SKILLHORIZON_CONSTRUCTION_PLAN_DIGEST"


def envelope():
    marker = os.environ.get(MARKER)
    if marker is None:
        return None
    calibration = os.environ.get("SKILLHORIZON_CONSTRUCTION_CANDIDATE") is not None
    approval_path = SOURCE.parent / ("APPROVED_GRID.json" if calibration else "APPROVED_B0.json")
    if marker != str(approval_path) or approval_path.is_symlink():
        raise ValueError("Construction approval path differs")
    approval = load_json(approval_path)
    plan = approval["plan"]
    domain = os.environ.get(DOMAIN)
    if (approval.get("approved") is not True or sha256_value(plan) != approval.get("plan_digest")
            or approval["plan_digest"] != os.environ.get(DIGEST) or plan["source"] != str(SOURCE)
            or plan["suite"] != str(SOURCE.parent) or plan["root"] != str(SOURCE.parent / "probes")
            or domain not in plan["profiles"]):
        raise ValueError("Construction approval, candidate or source differs")
    root = Path(plan["root"])
    if calibration:
        from integrations.benchmark_suite import grid_journal as journal
        candidate = os.environ[journal.CANDIDATE]
        if plan["revision"] != journal.REVISION or plan["control"] != str(SOURCE.parent)+"-control":
            raise ValueError("Calibration revision or control anchor differs")
        start = journal.authorized_start(plan, os.environ[journal.PERMIT], candidate,
            int(os.environ[journal.START]), os.environ[journal.START_DIGEST], full=False)
        if start["environment"] != domain or start["reserved_bytes"] != plan["limits"]["candidate_bytes"]:
            raise ValueError("Calibration domain or reservation differs")
    else:
        candidate = domain
        start = load_json(root / f"{domain}.START.json")
        expected = [*plan["worker_command"], str(root / domain), "--environment", domain, "--plan", str(root / "PLAN.json")]
        if (start.get("environment") != domain or start.get("revision") != plan["revision"]
                or start.get("argv") != expected or start.get("reserved_bytes") != plan["limits"]["candidate_bytes"]):
            raise ValueError("Construction launch is not committed")
    temporary = root / candidate / "tmp"
    if temporary != temporary.resolve() or not temporary.is_dir() or not (temporary / "home").is_dir():
        raise ValueError("Construction temporary layout differs")
    if (str(Path(sys.executable).resolve()) != plan["runtime"]["executable"]
            or Path(sys.prefix).resolve() != Path(plan["runtime"]["prefix"]).resolve()):
        raise ValueError("Construction process runtime differs")
    for name in ("TMP", "TEMP", "TMPDIR"):
        if os.environ.get(name) != str(temporary):
            raise ValueError("Construction process temporary directory differs")
    return approval, domain, temporary


def temporary_environment(base=None):
    result = dict(base or {})
    bound = envelope()
    if bound is None:
        return result
    approval, domain, temporary = bound
    result.update({"TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary),
                   "HOME": str(temporary / "home"), "PYTHONDONTWRITEBYTECODE": "1",
                   MARKER: os.environ[MARKER], DOMAIN: domain,
                   DIGEST: approval["plan_digest"]})
    if approval["plan"]["revision"] == "history_grid_calibration_v1":
        from integrations.benchmark_suite import grid_journal as journal
        result.update({key: os.environ[key] for key in (journal.PERMIT, journal.CANDIDATE, journal.START, journal.START_DIGEST)})
    return result


def validate_nested_python(command):
    bound = envelope()
    if bound is None:
        return
    expected = bound[0]["plan"]["mcp_python"]
    command = Path(command)
    if (str(command) != expected["invocation"] or not command.is_file()
            or str(command.resolve()) != expected["executable"]
            or command.parent.parent.resolve() != Path(expected["prefix"]).resolve()):
        raise ValueError("Construction nested interpreter route differs")
