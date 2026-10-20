"""Attest the actual C-domain scoring process without changing its evaluator."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from .backend import write_record
from .provenance import attest


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def score(task_root, state_root):
    from skillhorizon.experiment.tasks import load_task
    from skillhorizon.experiment.internal_mcp import domain_entry
    from integrations.internal_mcp.framework import evaluate
    import importlib
    task_root, state_root = Path(task_root), Path(state_root)
    task = load_task(task_root)
    domain = domain_entry(task["config"]["environment_id"], task["environment_format"])
    evaluator = domain.get("evaluate_module", domain["module"] + ".evaluate")
    importlib.import_module(evaluator)
    required = {load_task.__module__, evaluate.__module__, evaluator}
    options = dict(phase="verify", task_root=task_root, runtime_root=state_root / "runtime",
                   entry_file=__file__, required_modules=required)
    root = Path(__file__).resolve().parents[2]
    attest(root, **options)
    result = evaluate(task, task_root / "checkpoint", state_root / "runtime")
    receipt = attest(root, **options)
    receipt.update(role="internal_domain_evaluator", score_digest=digest(result))
    write_record(state_root / "CODE_ORIGINS_EVALUATOR.json", receipt)
    return result


def evaluate_attested(task_root, state_root):
    root = Path(__file__).resolve().parents[2]
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "TIKTOKEN_CACHE_DIR") if key in os.environ}
    environment.update(PYTHONPATH=f"{root / 'src'}:{root}", PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run([sys.executable, "-m", "integrations.harbor.score_worker",
        str(task_root), str(state_root)], cwd=root, env=environment, capture_output=True,
        text=True, timeout=550)
    if completed.returncode != 0:
        raise ValueError("Attested domain evaluator failed")
    result = json.loads(completed.stdout)
    receipt = json.loads((Path(state_root) / "CODE_ORIGINS_EVALUATOR.json").read_text())
    manifest = (root / "CODE_ORIGINS.json").read_bytes()
    if (receipt.get("status") != "passed" or receipt.get("role") != "internal_domain_evaluator"
            or receipt.get("score_digest") != digest(result)
            or receipt.get("code_manifest_sha256") != hashlib.sha256(manifest).hexdigest()):
        raise ValueError("Domain evaluator source receipt mismatch")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task_root", type=Path)
    parser.add_argument("state_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(score(args.task_root, args.state_root), allow_nan=False))


if __name__ == "__main__":
    main()
