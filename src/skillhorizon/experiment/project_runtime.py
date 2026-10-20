"""Select the business runtime without changing either task's semantics."""

from ..dataflow import FORMAT as DATAFLOW_FORMAT
from ..fieldops import FORMAT as OPERATIONS_FORMAT

PROJECT_FORMATS = {DATAFLOW_FORMAT, OPERATIONS_FORMAT}


def _runtime_class(task):
    protocol = task.get("skill_protocol", task.get("execution_mode", "procedural"))
    if protocol not in {"macro", "procedural", "skill_declaration_v1"}:
        raise ValueError("Unknown project skill protocol")
    if protocol == "skill_declaration_v1":
        if task["format"] == OPERATIONS_FORMAT:
            if (task["config"].get("business_program") != "pressure_branches_v3"
                    or task["config"].get("family") != "staged_maintenance"
                    or task.get("execution_mode") != "procedural"):
                raise ValueError("Declaration FieldOps requires the pressure_branches_v3 step surface")
            from ..fieldops.declaration_runtime import DeclarationOperationsRuntime
            return DeclarationOperationsRuntime
        if (task["format"] != DATAFLOW_FORMAT or task["config"].get("business_program") != "credit_sales_v2"
                or task["config"].get("workspace_profile") is not None
                or task.get("execution_mode") != "procedural"):
            raise ValueError("Declaration DataOps requires the credit_sales_v2 step surface")
        from ..dataflow.declaration_runtime import DeclarationDataflowRuntime
        return DeclarationDataflowRuntime
    if task["format"] == OPERATIONS_FORMAT:
        from ..fieldops.runtime import OperationsRuntime
        return OperationsRuntime
    if task["format"] == DATAFLOW_FORMAT:
        if task["config"].get("workspace_profile") == "release_maintenance_v2":
            from ..dataflow.maintenance import MaintenanceRuntime
            return MaintenanceRuntime
        from ..dataflow.runtime import DataflowRuntime
        return DataflowRuntime
    raise ValueError("Unsupported content workflow format")


def project_runtime(task, root):
    runtime = _runtime_class(task)(root)
    declared = task.get("skill_protocol") == "skill_declaration_v1"
    world_protocol = runtime.project.get("skill_protocol")
    if declared:
        operations = task["format"] == OPERATIONS_FORMAT
        program = "pressure_branches_v3" if operations else "credit_sales_v2"
        if (world_protocol != "skill_declaration_v1"
                or runtime.project.get("business_program") != program
                or runtime.project.get("workspace_profile") is not None
                or "active" in runtime.project or "next_run" in runtime.project):
            raise ValueError("Declaration task and business world protocol differ")
        if operations and (runtime.project.get("domain") != "fieldops"
                           or runtime.project.get("family") != task["config"]["family"]):
            raise ValueError("Declaration FieldOps task and world domain/family differ")
    elif world_protocol not in (None, "macro", "procedural"):
        raise ValueError("Legacy task cannot dispatch a declaration or unknown business world")
    return runtime


def project_server(task, root):
    if task["format"] == OPERATIONS_FORMAT:
        from ..fieldops.runtime import create_operations_server
        return create_operations_server(root, task["execution_mode"],
                                        runtime_class=lambda path: project_runtime(task, path))
    if task["format"] == DATAFLOW_FORMAT:
        from ..dataflow.runtime import create_dataflow_server
        return create_dataflow_server(root, task["execution_mode"],
                                      runtime_class=lambda path: project_runtime(task, path))
    raise ValueError("Unsupported content workflow format")
