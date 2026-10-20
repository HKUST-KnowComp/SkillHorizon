"""One fresh DataOps C0 construction and native reference trial on CPU8."""

import argparse
from pathlib import Path
import time

from integrations.harbor.backend import write_record
from skillhorizon.dataflow.declaration_tasks import build_declaration_dataflow

from .native_reference_length import BAND_REVISION, band_label
from .resource_sample import ResourceSamples


def run(root, *, periods, seed=7):
    if type(seed) is not int or type(periods) is not int or periods not in (8, 12):
        raise ValueError("C0 accepts only frozen 8/12 period profiles")
    if seed != 7:
        raise ValueError("C0 development seed differs")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    report = {"status": "running", "phase": "construct", "seed": seed, "periods": periods,
              "scope": "C0_zero_external_provider_complete_native_reference",
              "band_revision": BAND_REVISION,
              "external_provider_calls": 0}
    write_record(root / "STATUS.json", report)
    resources = ResourceSamples(root, root / "native/harbor-task/environment")
    sampler_started = False
    try:
        resources.start()
        sampler_started = True
        task = build_declaration_dataflow(root / "task", seed=seed, periods=periods,
            regions=3, rows_per_region=6, family="regional_sales", notice_policy="source_only",
            business_program="credit_sales_v2", business_scenario="receivables_rollover")
        report.update(phase="native_reference", construction_task=task["task_id"],
                      construction_public_calls=task["reference_tool_call_count"],
                      construct_seconds=round(time.monotonic() - started, 3))
        write_record(root / "STATUS.json", report)
        native_started = time.monotonic()
        from integrations.harbor.reference_run import run as run_native
        measured = run_native(root / "task", root / "native")
        if measured["status"] != "passed":
            raise ValueError("Complete native public reference did not qualify")
        label = band_label(measured["L_native_ref"])
        report.update(status="passed", phase="complete", L_native_ref=measured["L_native_ref"],
                      L_business_ref=measured["L_business_ref"],
                      native_requests=measured["native_requests"], reference_calls=measured["reference_calls"],
                      elapsed_seconds=round(time.monotonic() - started, 3),
                      native_seconds=round(time.monotonic() - native_started, 3),
                      band_hits=[label] if label is not None else [])
        write_record(root / "STATUS.json", report)
        return report
    except BaseException as error:
        report.update(status="failed", failure_type=type(error).__name__,
                      elapsed_seconds=round(time.monotonic() - started, 3))
        write_record(root / "STATUS.json", report)
        raise
    finally:
        sample = resources.finish()
        if not sampler_started:
            sample["status"] = "unavailable"
            sample["errors"].append({"source": "sampler", "type": "startup_failure"})
        write_record(root / "RESOURCE.json", sample)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--periods", type=int, choices=(8, 12), required=True)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    result = run(args.output, periods=args.periods, seed=args.seed)
    print({k: result[k] for k in ("status", "periods", "L_native_ref",
                                   "L_business_ref", "reference_calls", "native_requests")}, flush=True)


if __name__ == "__main__":
    main()
