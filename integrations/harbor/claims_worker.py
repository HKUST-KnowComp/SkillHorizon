"""Fresh private process for a complete domain-owned claims consumer."""

import argparse
import importlib
import os
from pathlib import Path
import signal
import stat
import threading

from .backend import write_record
from .claim_measurement import (consumer_for, digest, input_hashes, read, require_business_source,
                                sealed_inputs, validate_measurement)
from .provenance import attest


def guard_parent_lifetime():
    group = os.getpgrp()
    if group != os.getpid() or not stat.S_ISFIFO(os.fstat(0).st_mode):
        raise ValueError("Claims worker requires an isolated group and parent pipe")

    def watch():
        try:
            # The parent keeps its sole write end open but sends no data.
            os.read(0, 1)
        finally:
            # EOF, unexpected data and read errors all close this owned group.
            try:
                os.killpg(group, signal.SIGKILL)
            except OSError:
                os._exit(70)

    threading.Thread(target=watch, name="claims-parent-lifeline", daemon=True).start()


def execute(invocation_path):
    invocation_path = Path(invocation_path)
    invocation = read(invocation_path)
    code = Path(__file__).resolve().parents[2]
    task, state = Path(invocation["task_root"]), Path(invocation["state_root"])
    if (str(code) != invocation["code_root"] or invocation_path.resolve().parent != state / "claim_measurement"
            or input_hashes(task, state) != invocation["inputs"]):
        raise ValueError("Invocation identity mismatch")
    if invocation["require_code_origins"]:
        require_business_source(state, invocation["manifest"])
    from skillhorizon.experiment.tasks import load_task
    loaded_task = load_task(task)
    if consumer_for(loaded_task) != invocation["consumer"]:
        raise ValueError("Consumer differs from validated task")
    module = importlib.import_module(invocation["consumer"])
    options = dict(phase="verify", task_root=task, runtime_root=state / "runtime", entry_file=__file__,
                   required_modules={load_task.__module__, module.__name__})
    before = attest(code, **options) if invocation["require_code_origins"] else None
    if before is not None and before["code_manifest_sha256"] != invocation["manifest"]:
        raise ValueError("Source differs from frozen invocation")
    _, document = sealed_inputs(task, state)
    value = module.evaluate_claims(task, state)
    after = attest(code, **options) if invocation["require_code_origins"] else None
    if ((after is not None and after["code_manifest_sha256"] != invocation["manifest"])
            or input_hashes(task, state) != invocation["inputs"]):
        raise ValueError("Source or input changed during claims replay")
    validate_measurement(value, document)
    output = invocation_path.parent
    write_record(output / "consumer.json", value)
    write_record(output / "receipt.json", {"invocation_digest": digest(invocation),
        "measurement_digest": digest(value), "consumer": module.__name__, "manifest": invocation["manifest"],
        "before": before, "after": after})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("invocation", type=Path)
    args = parser.parse_args()
    guard_parent_lifetime()
    execute(args.invocation)
