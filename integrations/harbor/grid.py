"""Offline migration of the frozen cross-version pilot, never qualification.

Run on CPU8: python -B -m integrations.harbor.grid /workspace/runs/NEW_OUTPUT
Only the cell subprocess imports Harbor or business implementations.
"""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from . import producer

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GRID = ROOT / "artifacts/history_grid_20260916/PILOT_GRID_40.json"
SOURCE_BASE = Path("/workspace/runs")
ENVIRONMENTS = ("DataOps", "FieldOps", "ProcureOps", "C01", "C06", "C09",
                "C11", "C25", "C47", "C50")
BANDS = (64, 128, 256, 512)
CELL_TIMEOUT = 600
SEED = 20260915
SCOPE = "cross_version_pilot_length_availability_union"
LENGTH_DEFINITION = ("Complete uncompressed public reference history; schema once; "
                     "K=1024; inclusive +/-12.5%")
FLAGS = ("export_success", "harbor_task_runtime_load_valid", "public_tools_schema_equal",
         "http_reference_parity", "valid_replay", "business_success")
STAGES = ("source", "export", "task_load", "parity", "replay", "worker")
ROW_FIELDS = ("environment", "band", "interval", "seed", "reference_tokens", "reference_calls",
              "candidate_root", "source_root", "candidate", "batch", "reference_binding",
              "measurement_contract", "source_implementation", "plan_digest",
              "terminal_receipt_digest", "result_digest", "audit_path", "audit_digest")
REFERENCE_FILES = ("REFERENCE_CONTRACT.json", "REFERENCE_ORIGIN.json")
TOKENIZER_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
TOKENIZER_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    # Keep the controller dependency-free; atomic receipts survive worker termination.
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def restricted_source(value):
    if not isinstance(value, str):
        raise ValueError("SOURCE_PATH_RESTRICTED")
    path = Path(value)
    if (not path.is_absolute() or ".." in path.parts or path == SOURCE_BASE
            or not path.is_relative_to(SOURCE_BASE)
            or not path.resolve().is_relative_to(SOURCE_BASE)
            or path.resolve() == SOURCE_BASE):
        raise ValueError("SOURCE_PATH_RESTRICTED")
    return path


def source_task(row):
    relative = "task" if row["environment"].startswith("C") else "construction/task"
    return restricted_source(str(restricted_source(row["candidate_root"]) / relative))


class OfflineReferenceCacheError(RuntimeError):
    """Fail outside the ledger's malformed-data handlers; never fetch tokenizers."""


def load_reference_offline(root):
    cache = os.environ.get("TIKTOKEN_CACHE_DIR")
    if not cache:
        raise OfflineReferenceCacheError("REFERENCE_TOKENIZER_CACHE_MISSING")
    path = Path(cache) / hashlib.sha1(TOKENIZER_URL.encode()).hexdigest()
    if path.is_symlink():
        raise OfflineReferenceCacheError("REFERENCE_TOKENIZER_CACHE_INVALID")
    try:
        data = path.read_bytes()
    except OSError:
        raise OfflineReferenceCacheError("REFERENCE_TOKENIZER_CACHE_MISSING") from None
    if hashlib.sha256(data).hexdigest() != TOKENIZER_SHA256:
        raise OfflineReferenceCacheError("REFERENCE_TOKENIZER_CACHE_INVALID")
    from tiktoken import load as tokenizer_load
    from skillhorizon.experiment.history_measurement import load_reference

    def approved_bytes(blobpath, expected_hash=None):
        if blobpath != TOKENIZER_URL or expected_hash != TOKENIZER_SHA256:
            raise OfflineReferenceCacheError("REFERENCE_TOKENIZER_CACHE_UNAPPROVED")
        return data

    # One cell per process, before HTTP threads: serve verified bytes without the
    # library's cache repair/download path, even if the on-disk cache later changes.
    original = tokenizer_load.read_file_cached
    tokenizer_load.read_file_cached = approved_bytes
    try:
        return load_reference(root)
    finally:
        tokenizer_load.read_file_cached = original


def row_id(row):
    return "{}-{}".format(row["environment"], row["band"])


def validate_grid(document):
    if not isinstance(document, dict):
        raise ValueError("INVALID_GRID")
    if (document.get("scope") != SCOPE or document.get("pilot_seed") != SEED
            or document.get("length_definition") != LENGTH_DEFINITION
            or type(document.get("qualified_cells")) is not int or document["qualified_cells"] != 0
            or document.get("replicated_instances") != 0
            or document.get("expected_replicated_instances") != 120
            or document.get("unified_source_qualification") is not False
            or document.get("model_behavior_evidence") is not False
            or document.get("provider_calls") != 0):
        raise ValueError("PILOT_METADATA_CHANGED")
    rows = document.get("cells")
    if not isinstance(rows, list):
        raise ValueError("INVALID_CELLS")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("INVALID_CELL")
        environment, band = row.get("environment"), row.get("band")
        if environment not in ENVIRONMENTS or type(band) is not int or band not in BANDS:
            raise ValueError("UNEXPECTED_CELL")
        key = (environment, band)
        if key in seen:
            raise ValueError("DUPLICATE_CELL")
        seen.add(key)
        interval = [band * 1024 * 7 // 8, band * 1024 * 9 // 8]
        if (row.get("seed") != SEED or row.get("interval") != interval
                or type(row.get("reference_tokens")) is not int
                or not interval[0] <= row["reference_tokens"] <= interval[1]
                or type(row.get("reference_calls")) is not int or row["reference_calls"] < 1):
            raise ValueError("FROZEN_REFERENCE_CHANGED")
        source_task(row)
        restricted_source(row.get("source_root"))
    if seen != {(environment, band) for environment in ENVIRONMENTS for band in BANDS}:
        raise ValueError("MISSING_CELLS")
    if (document.get("pilot_cells") != 40 or document.get("expected_pilot_cells") != 40
            or document.get("pilot_availability_complete") is not True or document.get("missing") != []):
        raise ValueError("PILOT_COVERAGE_CHANGED")
    return document


def load_manifest(path):
    raw = Path(path).read_bytes()
    document = validate_grid(json.loads(raw))
    return {**{key: value for key, value in document.items() if key != "cells"},
            "source_manifest": str(Path(path).resolve()),
            "source_manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "cells": [{key: row[key] for key in ROW_FIELDS if key in row}
                      for row in document["cells"]]}


def check_destination(output, rows):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError("RESUME_UNSUPPORTED")
    resolved = output.resolve()
    for row in rows:
        for key in ("candidate_root", "source_root"):
            source = restricted_source(row[key]).resolve()
            if resolved.is_relative_to(source) or source.is_relative_to(resolved):
                raise ValueError("OUTPUT_SOURCE_OVERLAP")
    return output


def new_result(row):
    return {"row": row_id(row), "environment": row["environment"], "band": row["band"],
            "seed": row["seed"], "interval": row["interval"],
            "reference_tokens": row["reference_tokens"], "reference_calls": row["reference_calls"],
            "source_task": str(source_task(row)), "source_root": row["source_root"],
            "status": "pending", "phase": "source", "source_available": False,
            **dict.fromkeys(FLAGS, False), "actions_compared": 0,
            "first_difference": None, "errors": dict.fromkeys(STAGES),
            "preserved_reference_files": [], "provider_calls": 0}


def first_difference(result, label, index=None, tool=None):
    if result["first_difference"] is None:
        safe_tool = (tool if isinstance(tool, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tool)
                     else None)
        result["first_difference"] = {"label": label, "index": index, "tool": safe_tool}


def cell_worker(row, output):
    output = check_destination(output, [row])
    output.mkdir(parents=True, exist_ok=False)
    result = new_result(row)
    result["status"] = "running"
    harbor_dir = Path(__file__).resolve().parent
    origins = {"controller_only": True, "phases": {}}

    def audit(phase, required=()):
        try:
            receipt = producer.attest_modules(row["source_root"], harbor_dir, required=required)
        except producer.ModuleOriginError as error:
            origins["phases"][phase] = error.receipt
            raise
        else:
            origins["phases"][phase] = receipt
        finally:
            write_json(output / "MODULE_ORIGINS.json", origins)

    def save(phase):
        result["phase"] = phase
        write_json(output / "CELL_RESULT.json", result)

    save("source")
    source_error = "SOURCE_UNAVAILABLE_OR_INVALID"
    try:
        source_error = "WORKER_IMPORT_ISOLATION_REQUIRED"
        producer.require_isolated_worker(row["source_root"], harbor_dir)
        audit("before_source_helpers")
        write_json(output / "DEPENDENCIES.json", producer.dependency_receipt())
        source_error = "SOURCE_UNAVAILABLE_OR_INVALID"
        source = source_task(row)
        candidate = Path(row["candidate_root"])
        saved = read_json(candidate / "RESULT.json")
        plan = read_json(candidate / "episode/PLAN.json")
        source_error = "TASK_PATH_BINDING_MISMATCH"
        if (saved["task"] != str(source) or plan["task"] != str(source)
                or not source.resolve().is_relative_to(candidate.resolve())
                or source != source.resolve()):
            raise ValueError
        source_error = "SOURCE_UNAVAILABLE_OR_INVALID"
        task_data = read_json(source / "task.json")
        if not (source / "checkpoint").is_dir() or not (source / "BUILD.json").is_file():
            raise ValueError
        source_error = "REFERENCE_HELPER_UNAVAILABLE"
        from integrations.benchmark_suite.reference_policy import reference_actions, bound_reference
        from skillhorizon.experiment.history_measurement import contract
        from skillhorizon.util import sha256_value
        audit("before_reference_actions", required=("integrations.benchmark_suite.reference_policy",
                                                   "skillhorizon.experiment.history_measurement"))
        source_error = "REFERENCE_POLICY_MISMATCH"
        actions = reference_actions(source)
        binding = bound_reference(source)
        source_error = "REFERENCE_BINDING_MISMATCH"
        if (binding != row["reference_binding"] or binding != saved["reference_binding"]
                or binding != read_json(candidate / "episode/REFERENCE_CONTRACT.json")
                or binding.get("track") != "full_online"):
            raise ValueError
        source_error = "REFERENCE_CALL_COUNT_MISMATCH"
        if (not isinstance(actions, list) or not actions
                or len(actions) != row["reference_calls"] or len(actions) != saved["reference_calls"]):
            raise ValueError
        source_error = "REFERENCE_ACTIONS_MISMATCH"
        if sha256_value(actions) != binding["actions_digest"]:
            raise ValueError
        source_error = "REFERENCE_HISTORY_UNAVAILABLE"
        reference = load_reference_offline(candidate / "episode/fixture")
        source_error = "REFERENCE_LENGTH_MISMATCH"
        if (reference["reference_tokens"] != row["reference_tokens"]
                or reference["reference_tokens"] != saved["reference_tokens"]):
            raise ValueError
        source_error = "REFERENCE_MEASUREMENT_MISMATCH"
        measurement = contract()
        if (measurement != row["measurement_contract"]
                or any(reference.get(key) != value for key, value in measurement.items())):
            raise ValueError
        source_error = "REFERENCE_HISTORY_MISMATCH"
        if (reference != saved["reference"] or reference["tool_calls"] != len(actions)
                or reference["status"] != "available" or reference["track"] != "full_online"
                or reference["fixture"] is not True):
            raise ValueError
        audit("after_source_validation")
        result["source_available"] = True
    except producer.ModuleOriginError:
        result["errors"]["source"] = "MODULE_ORIGIN_MISMATCH"
    except OfflineReferenceCacheError as error:
        result["errors"]["source"] = str(error)
    except Exception:
        result["errors"]["source"] = source_error
    else:
        task = output / "harbor-task"
        save("export")
        try:
            from .export import export_task
            manifest = export_task(source, task, max_calls=len(actions) + 50, include_oracle=False,
                                   require_reference_contract=True)
            preserved = [name for name in REFERENCE_FILES if (source / name).is_file()]
            if (manifest["preserved_reference_files"] != preserved
                    or any((source / name).read_bytes() != (task / "reference_metadata" / name).read_bytes()
                           for name in preserved)):
                raise ValueError("REFERENCE_METADATA_CHANGED")
            manifest["pilot_reference"] = {key: row[key] for key in ROW_FIELDS if key in row}
            write_json(task / "migration.json", manifest)
            result["preserved_reference_files"] = preserved
            result["export_success"] = True
            audit("after_export")
        except producer.ModuleOriginError:
            result["errors"]["export"] = "MODULE_ORIGIN_MISMATCH"
        except Exception:
            result["errors"]["export"] = "EXPORT_FAILED"
        else:
            save("task_load")
            try:
                from harbor.models.task.task import Task
                Task(task_dir=task)
                result["harbor_task_runtime_load_valid"] = True
            except Exception:
                result["errors"]["task_load"] = "TASK_LOAD_FAILED"
            else:
                _reference_check(task, output, task_data, actions, manifest, result, save, audit)
    try:
        audit("worker_final")
        write_json(output / "DEPENDENCIES.json", producer.dependency_receipt())
    except producer.ModuleOriginError:
        result["errors"]["worker"] = "MODULE_ORIGIN_MISMATCH"
    result["status"] = "completed" if all(result[key] for key in FLAGS) and not any(result["errors"].values()) else "failed"
    save("done")
    return result


def _reference_check(task, output, task_data, actions, manifest, result, save, audit):
    save("parity")
    gateway = None
    from .private_gateway import PrivateGatewayError
    try:
        from .private_gateway import PrivateGateway
        from .client import Client
        gateway = PrivateGateway(task / "environment/business", output / "state",
                                 max_calls=manifest["max_calls"])
        if gateway.business_source_root != result["source_root"]:
            raise PrivateGatewayError("PRIVATE_SOURCE_ROOT_MISMATCH")
        audit("after_gateway_init")
        client = Client(gateway.url)
        result["public_tools_schema_equal"] = client.tools()["tools"] == task_data["tools"]
        if not result["public_tools_schema_equal"]:
            result["errors"]["parity"] = "PUBLIC_SCHEMA_MISMATCH"
            first_difference(result, "PUBLIC_SCHEMA_MISMATCH")
        else:
            for index, action in enumerate(actions):
                tool = action.get("skill_id") if isinstance(action, dict) else None
                if (not isinstance(action, dict) or not isinstance(tool, str)
                        or not isinstance(action.get("arguments"), dict) or "result" not in action):
                    result["errors"]["parity"] = "CONTINUATION_FIELDS_INVALID"
                    first_difference(result, "CONTINUATION_FIELDS_INVALID", index, tool)
                    break
                try:
                    actual = client.call(tool, action["arguments"])
                except Exception:
                    result["errors"]["parity"] = "HTTP_CALL_FAILED"
                    first_difference(result, "HTTP_CALL_FAILED", index, tool)
                    break  # Uncertain writes must never be resubmitted.
                result["actions_compared"] += 1
                if actual != action["result"]:
                    result["errors"]["parity"] = "RESULT_MISMATCH"
                    first_difference(result, "RESULT_MISMATCH", index, tool)
            result["http_reference_parity"] = (result["actions_compared"] == len(actions)
                                                and result["errors"]["parity"] is None)
        save("replay")
        try:
            report = gateway.seal_and_score()
            result["valid_replay"] = report["business"]["valid"] is True
            result["business_success"] = report["business"]["task_success"] is True
            if not result["valid_replay"]:
                result["errors"]["replay"] = "INDEPENDENT_REPLAY_INVALID"
            elif not result["business_success"]:
                result["errors"]["replay"] = "BUSINESS_UNSUCCESSFUL"
        except PrivateGatewayError as error:
            result["errors"]["replay"] = str(error)
        except Exception:
            result["errors"]["replay"] = "INDEPENDENT_REPLAY_FAILED"
        finally:
            try:
                audit("after_replay")
            except producer.ModuleOriginError:
                result["errors"]["replay"] = "MODULE_ORIGIN_MISMATCH"
    except producer.ModuleOriginError:
        result["errors"]["parity"] = "MODULE_ORIGIN_MISMATCH"
        first_difference(result, "MODULE_ORIGIN_MISMATCH")
    except PrivateGatewayError as error:
        result["errors"]["parity"] = str(error)
        first_difference(result, str(error))
    except Exception:
        result["errors"]["parity"] = "PUBLIC_HTTP_SETUP_FAILED"
        first_difference(result, "PUBLIC_HTTP_SETUP_FAILED")
    finally:
        if gateway is not None:
            try:
                gateway.close()
            except PrivateGatewayError as error:
                result["errors"]["worker"] = str(error)
            except Exception:
                result["errors"]["worker"] = "HTTP_CLEANUP_FAILED"


def summarize(manifest, results, active=(), *, status, selection=None):
    by_id = {row_id(row): row for row in manifest["cells"]}
    ids = [result["row"] for result in results]
    selected = set(by_id) if selection is None else set(selection)
    if not selected or not selected <= set(by_id) or not set(ids) <= selected:
        raise ValueError("INVALID_SELECTION")
    if len(set(ids)) != len(ids) or not set(ids) <= set(by_id):
        raise ValueError("INVALID_RESULT_COVERAGE")
    completed = [result for result in results if result["status"] in {"completed", "failed"}]
    counts = {key: sum(result[key] is True for result in results) for key in FLAGS}
    migrated = sum(result["status"] == "completed" and all(result[key] is True for key in FLAGS)
                   and not any(result["errors"].values()) for result in results)
    return {"scope": SCOPE, "status": status, "expected_cells": 40,
            "selected_cells": [identifier for identifier in by_id if identifier in selected],
            "selection_expected": len(selected), "selection_completed": len(completed),
            "selection_pending": len(selected) - len(completed),
            "selection_complete": migrated == len(selected),
            "overall_coverage": {"numerator": len(completed), "denominator": 40},
            "historical_pilot_cells": manifest["pilot_cells"],
            "historical_pilot_availability_complete": manifest["pilot_availability_complete"],
            "source_available_cells": sum(result["source_available"] is True for result in results),
            "completed_cells": len(completed), "pending_cells": 40 - len(completed),
            "failed_cells": sum(result["status"] == "failed" for result in results),
            "migration_counts": counts, "migrated_cells": migrated,
            "migration_coverage": {"numerator": migrated, "denominator": 40},
            "migration_complete": migrated == 40,
            "qualified_cells": manifest["qualified_cells"],
            "qualified_cells_source": "source_manifest.qualified_cells; never promoted by migration",
            "unified_source_qualification": False, "model_behavior_evidence": False,
            "replicated_instances": manifest["replicated_instances"],
            "expected_replicated_instances": manifest["expected_replicated_instances"],
            "seeds": [manifest["pilot_seed"]], "length_definition": manifest["length_definition"],
            "source_manifest": manifest["source_manifest"],
            "source_manifest_sha256": manifest["source_manifest_sha256"],
            "provider_calls": 0, "active_rows": list(active),
            "error_counts": {stage: dict(Counter(result["errors"][stage] for result in results
                                                if result["errors"][stage])) for stage in STAGES},
            "cells": list(results)}


def stop_group(process):
    # Kill the group even when its leader exited, so descendants cannot survive it.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def read_cell_result(row, directory):
    expected = new_result(row)
    saved = read_json(directory / "CELL_RESULT.json")
    identity = ("row", "environment", "band", "seed", "interval", "reference_tokens",
                "reference_calls", "source_task", "source_root", "provider_calls")
    if (not isinstance(saved, dict) or set(saved) != set(expected)
            or any(saved[key] != expected[key] for key in identity)
            or any(type(saved[key]) is not bool for key in (*FLAGS, "source_available"))
            or saved["status"] not in {"running", "completed", "failed"}
            or not isinstance(saved["errors"], dict) or set(saved["errors"]) != set(STAGES)
            or any(value is not None and not isinstance(value, str) for value in saved["errors"].values())):
        raise ValueError("INVALID_WORKER_RECEIPT")
    return saved


def worker_result(row, directory, label=None):
    result = new_result(row)
    try:
        result = read_cell_result(row, directory)
    except (OSError, ValueError, KeyError, TypeError):
        label = label or "WORKER_RECEIPT_MISSING_OR_INVALID"
    if label or result["status"] not in {"completed", "failed"}:
        result["status"] = "failed"
        result["errors"]["worker"] = label or "WORKER_INCOMPLETE"
    directory.mkdir(exist_ok=True)
    write_json(directory / "CELL_RESULT.json", result)
    return result


def run_grid(grid, output, *, workers=2, only_cell=None):
    if type(workers) is not int or not 1 <= workers <= 2:
        raise ValueError("WORKER_LIMIT")
    manifest = load_manifest(grid)
    selected = [row for row in manifest["cells"] if only_cell is None or row_id(row) == only_cell]
    if not selected:
        raise ValueError("UNKNOWN_CELL")
    output = check_destination(output, manifest["cells"]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    snapshot = output / "GRID_MANIFEST.json"
    write_json(snapshot, manifest)
    overlay = output / "adapter_overlay"
    overlay_receipt = producer.create_adapter_overlay(overlay)
    write_json(output / "CONTROLLER_ORIGINS.json", {
        "controller_only": True, "controller_module": str(Path(__file__).resolve()),
        "adapter": overlay_receipt, "dependencies": producer.dependency_receipt(),
        "note": "Controller origins are not worker or historical dependency attestation."})
    dependencies = producer.dependency_paths()
    rows, active, results = iter(selected), {}, {}

    def progress(status):
        for identifier, (_, row, _) in active.items():
            try:
                current = read_cell_result(row, output / identifier)
                # Completion belongs to the controller only after process cleanup.
                current["status"] = "running"
                results[identifier] = current
            except (OSError, ValueError, KeyError, TypeError):
                pass
        ordered = [results[row_id(row)] for row in manifest["cells"] if row_id(row) in results]
        report = summarize(manifest, ordered, active, status=status,
                           selection=[row_id(row) for row in selected])
        write_json(output / "GRID_PROGRESS.json", report)
        return report

    progress("running")
    exhausted = False
    next_progress = time.monotonic()
    try:
        while active or not exhausted:
            while len(active) < workers and not exhausted:
                row = next(rows, None)
                if row is None:
                    exhausted = True
                    break
                identifier = row_id(row)
                command = [sys.executable, "-B", "-S", str(overlay / "integrations/harbor/producer.py"),
                           "--source", row["source_root"], "--overlay", str(overlay)]
                for path in dependencies:
                    command.extend(["--dependency-path", path])
                command += ["--", str(output),
                           "--grid", str(snapshot), "--cell-worker", identifier]
                try:
                    env = producer.worker_environment(row["source_root"], overlay)
                    started = time.monotonic()
                    process = subprocess.Popen(command, cwd=overlay, env=env, start_new_session=True,
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL)
                except producer.ProducerError as error:
                    result = new_result(row)
                    result.update(status="failed", phase="source")
                    result["errors"]["source"] = str(error)
                    directory = output / identifier
                    directory.mkdir(exist_ok=False)
                    write_json(directory / "CELL_RESULT.json", result)
                    results[identifier] = result
                except OSError:
                    results[identifier] = worker_result(row, output / identifier, "WORKER_START_FAILED")
                else:
                    active[identifier] = (process, row, started)
            changed = False
            for identifier, (process, row, started) in list(active.items()):
                returncode = process.poll()
                timed_out = time.monotonic() - started >= CELL_TIMEOUT
                if returncode is not None or timed_out:
                    stop_group(process)
                    label = "CELL_TIMEOUT" if timed_out else ("WORKER_EXIT_FAILED" if returncode else None)
                    results[identifier] = worker_result(row, output / identifier, label)
                    del active[identifier]
                    changed = True
            if changed or time.monotonic() >= next_progress:
                progress("running")
                next_progress = time.monotonic() + 5
            if active:
                time.sleep(0.1)
    except BaseException:
        for identifier, (process, row, _) in list(active.items()):
            stop_group(process)
            results[identifier] = worker_result(row, output / identifier, "GRID_INTERRUPTED")
            del active[identifier]
        write_json(output / "GRID_RESULT.json", progress("interrupted"))
        raise
    report = progress("completed")
    write_json(output / "GRID_RESULT.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--grid", type=Path, default=DEFAULT_GRID)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--cell-worker", help=argparse.SUPPRESS)
    parser.add_argument("--only-cell", help="Run one original cell; overall denominator remains 40")
    options = parser.parse_args(argv)
    if options.cell_worker is not None:
        if options.only_cell is not None:
            raise ValueError("INVALID_SELECTION")
        document = validate_grid(read_json(options.grid))
        matches = [row for row in document["cells"] if row_id(row) == options.cell_worker]
        if len(matches) != 1:
            raise ValueError("UNKNOWN_CELL")
        cell_worker(matches[0], options.output / options.cell_worker)
        return 0

    def interrupted(*_):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        report = run_grid(options.grid, options.output, workers=options.workers, only_cell=options.only_cell)
    finally:
        signal.signal(signal.SIGTERM, previous)
    print(json.dumps({key: value for key, value in report.items() if key != "cells"}))
    return 0 if report["selection_complete"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Exception, KeyboardInterrupt):
        # Upstream exception strings can contain private action/result payloads.
        print('{"error": "GRID_FAILED", "provider_calls": 0}', file=sys.stderr)
        sys.exit(1)
