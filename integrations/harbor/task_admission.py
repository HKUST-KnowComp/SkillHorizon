"""Fresh private-task loading admission, separate from SERVE/VERIFY scoring."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from .business_view import full_inventory
from .private_gateway import private_environment, read_record, reap


SCHEMA = "harbor_private_task_admission_v1"
REQUIRED = ("integrations.harbor.backend", "skillhorizon.experiment.tasks",
            "skillhorizon.experiment.internal_mcp")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identity(business):
    return {"business_root": str(business), "code_root": str(business / "code"),
            "task_root": str(business / "task"),
            "task_sha256": hashlib.sha256((business / "task/task.json").read_bytes()).hexdigest(),
            "code_manifest_sha256": hashlib.sha256((business / "code/CODE_ORIGINS.json").read_bytes()).hexdigest(),
            "full_inventory_digest": digest(full_inventory(business))}


def check_receipt(receipt, expected):
    if (receipt.get("schema") != SCHEMA or receipt.get("valid") is not True
            or receipt.get("identity") != expected or receipt.get("provider_calls") != 0
            or type(receipt.get("provider_calls")) is not int or receipt.get("business_dispatches") != 0
            or type(receipt.get("business_dispatches")) is not int
            or receipt.get("required_modules") != list(REQUIRED)):
        raise ValueError("Private task admission receipt differs")
    origins = receipt.get("origins", {})
    if (origins.get("status") != "passed" or origins.get("phase") != "admission"
            or origins.get("code_manifest_sha256") != expected["code_manifest_sha256"]
            or not set(REQUIRED) <= set(origins.get("modules", {}))):
        raise ValueError("Private task admission origins differ")
    for module in origins["modules"].values():
        if (module.get("owner") not in {"business", "adapter"} or not module.get("sha256")
                or not Path(module.get("path", "")).resolve().is_relative_to(expected["code_root"])):
            raise ValueError("Private task admission origin escaped")


def admit(business, receipt_path, *, timeout=30):
    business, receipt_path = Path(business).resolve(), Path(receipt_path)
    if (receipt_path != receipt_path.resolve() or receipt_path.is_relative_to(business)
            or business.is_relative_to(receipt_path) or receipt_path.exists()):
        raise ValueError("Fresh external admission receipt required")
    expected = identity(business)
    from .provenance import attest
    attest(business / "code", phase="admission", task_root=business / "task", modules={})
    command = [sys.executable, "-B", "-m", "integrations.harbor.task_admission", str(business), str(receipt_path)]
    with receipt_path.with_suffix(".log").open("xb") as log:
        process = subprocess.Popen(command, cwd=business / "code", env=private_environment(business / "code"),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=False)
        primary = None
        try:
            if process.wait(timeout=timeout) != 0:
                raise ValueError("Private task admission worker failed")
        except BaseException as error:
            primary = error
        try:
            reap(process)
        except BaseException as error:
            if primary is None or (isinstance(primary, Exception) and not isinstance(error, Exception)):
                primary = error
            for action in (process.kill, lambda: process.wait(timeout=2)):
                try:
                    action()
                except ProcessLookupError:
                    pass
                except BaseException as cleanup_error:
                    if isinstance(primary, Exception) and not isinstance(cleanup_error, Exception):
                        primary = cleanup_error
        if primary is not None:
            raise primary
    receipt = read_record(receipt_path)
    check_receipt(receipt, expected)
    if identity(business) != expected:
        raise ValueError("Private task changed during admission")
    return receipt


def worker(business, receipt_path):
    business, receipt_path = Path(business).resolve(), Path(receipt_path)
    code, task = business / "code", business / "task"
    if (Path(__file__).resolve() != code / "integrations/harbor/task_admission.py"
            or receipt_path != receipt_path.resolve() or receipt_path.is_relative_to(business)
            or receipt_path.exists()):
        raise ValueError("Private task admission location differs")
    before = identity(business)
    document = read_record(task / "task.json")
    if (document.get("format") != "skillhorizon.internal_mcp_task.v1"
            or document.get("skill_protocol") != "skill_declaration_v1"):
        raise ValueError("Admission route requires internal declarations")
    from .backend import require_full_online
    from .provenance import attest
    require_full_online(task)
    origins = attest(code, phase="admission", task_root=task, required_modules=REQUIRED, entry_file=__file__)
    if identity(business) != before:
        raise ValueError("Private task changed while loading")
    receipt = {"schema": SCHEMA, "valid": True, "identity": before, "origins": origins,
               "required_modules": list(REQUIRED), "provider_calls": 0, "business_dispatches": 0}
    check_receipt(receipt, before)
    with receipt_path.open("x") as stream:
        json.dump(receipt, stream, sort_keys=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("business", type=Path)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    worker(args.business, args.receipt)


if __name__ == "__main__":
    main()
