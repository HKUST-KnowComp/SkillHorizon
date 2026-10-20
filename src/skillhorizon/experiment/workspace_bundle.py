"""Shared workspace composition; business state and file state stay separate."""

from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from ..util import canonical_json, load_json, load_jsonl
from .workspace import (WorkspaceRuntime, initialize_workspace, normalize_workspace_config,
                        workspace_snapshot, workspace_tools)


def workspace_config(bundle):
    bundle = Path(bundle).resolve()
    manifest = load_json(bundle / "manifest.json")
    public = load_json(bundle / "public/task.json")
    config = manifest.get("workspace")
    from .execution_bundle import execution_config
    from .python_execution import python_tools
    execution = execution_config(bundle)
    from .business_binding import business_binding
    from .ptc_config import ptc_tools
    ptc = business_binding(bundle)["ptc"]
    if public.get("workspace") != config:
        raise ValueError("Public workspace configuration differs from controller declaration")
    task = load_json(bundle / "private_task.json")
    from .delivery import normalize_delivery_contract, public_delivery_instruction
    delivery = normalize_delivery_contract(manifest.get("delivery_contract"), task, config)
    if public.get("delivery_contract") != delivery:
        raise ValueError("Public delivery contract differs from controller declaration")
    if delivery is not None:
        instruction = {"role": "user", "content": public_delivery_instruction(delivery)}
        if (instruction not in public["instructions"]
                or instruction not in load_json(bundle / "public/history.json")):
            raise ValueError("Delivery requirement is missing from public instructions")
    business = {tool["name"]: "business" for tool in task["tools"]}
    if config is not None:
        config = normalize_workspace_config(config)
        extra = workspace_tools()
        if any(tool["name"] in business for tool in extra):
            raise ValueError("Workspace tool name collides with business tool")
        expected = [*task["tools"], *extra]
        kinds = {**business, **{tool["name"]: "workspace" for tool in extra}}
        if execution is not None:
            if any(tool["name"] in kinds for tool in python_tools()):
                raise ValueError("Execution tool name collides with another capability")
            expected.extend(python_tools())
            kinds.update({tool["name"]: "execution" for tool in python_tools()})
        if ptc is not None:
            if any(tool["name"] in kinds for tool in ptc_tools()):
                raise ValueError("Programmatic tool name collides with another capability")
            expected.extend(ptc_tools())
            kinds.update({tool["name"]: "execution" for tool in ptc_tools()})
        if public["tools"] != expected or manifest.get("tool_kinds") != kinds:
            raise ValueError("Workspace schemas or trusted tool categories changed")
    elif public["tools"] != task["tools"]:
        raise ValueError("Business schemas differ from the declared public catalog")
    return config


def tool_kinds(bundle):
    config = workspace_config(bundle)
    task = load_json(Path(bundle) / "private_task.json")
    result = {t["name"]: "business" for t in task["tools"]}
    if config is not None:
        result.update({t["name"]: "workspace" for t in workspace_tools()})
    from .execution_bundle import execution_config
    if execution_config(bundle) is not None:
        result["python_execute"] = "execution"
    from .business_binding import business_binding
    if business_binding(bundle)["ptc"] is not None:
        result["ptc_execute"] = "execution"
    return result


def start_workspace(bundle, *, business=None):
    bundle = Path(bundle).resolve()
    from .execution_bundle import ensure_execution_known
    ensure_execution_known(bundle)
    config = workspace_config(bundle)
    if config is None:
        return None
    root = bundle / "agent_workspace"
    if root.exists() or (bundle / "workspace_actions.jsonl").exists():
        raise ValueError("Workspace execution must start in a fresh episode")
    workspace_snapshot(bundle / "workspace_initial", config)
    shutil.copytree(bundle / "workspace_initial", root)
    from .execution_bundle import execution_config, ProductionExecution
    execution = execution_config(bundle)
    if execution is not None:
        return ProductionExecution(bundle, root, config, execution, business=business)
    return GuardedFileWorkspace(root, bundle / "workspace_actions.jsonl", config)


class GuardedFileWorkspace(WorkspaceRuntime):
    def call(self, name, arguments):
        from .execution_bundle import ensure_execution_known
        ensure_execution_known(self.journal.parent)
        return super().call(name, arguments)


def workspace_action_records(bundle):
    from .execution_bundle import execution_config, execution_records, ensure_execution_known
    ensure_execution_known(bundle)
    return (execution_records(bundle) if execution_config(bundle) is not None
            else load_jsonl(Path(bundle) / "workspace_actions.jsonl"))


def verify_workspace(bundle, run=None):
    from .managed_session import require_world_quiescent
    require_world_quiescent(bundle)
    bundle = Path(bundle).resolve()
    config = workspace_config(bundle)
    if config is None:
        return {"enabled": False, "valid": True, "operations": 0}
    from .execution_bundle import execution_config, execution_records, ensure_execution_known, logical_execution_usage
    from .execution_workspace import ExecutionWorkspace
    ensure_execution_known(bundle)
    execution = execution_config(bundle)
    from .business_binding import business_binding
    ptc = business_binding(bundle)["ptc"]
    actions = execution_records(bundle) if execution is not None else load_jsonl(bundle / "workspace_actions.jsonl")
    observations_match = True
    if run is not None:
        _, records = split_tools(bundle, load_jsonl(Path(run) / "tools.jsonl"))
        observations_match = canonical_json([{k: r[k] for k in ("name", "arguments", "output")} for r in records]) == canonical_json(
            [{k: r[k] for k in ("name", "arguments", "output")} for r in actions])
    with TemporaryDirectory(prefix="workspace-effects-") as temporary:
        directory = Path(temporary).resolve()
        root = directory / "workspace"
        shutil.copytree(bundle / "workspace_initial", root)
        runtime = (ExecutionWorkspace(root, directory / "actions.jsonl", config, execution,
            max_actions=load_json(bundle / "manifest.json")["execution_max_actions"], records=actions, replay_mode="full", ptc=ptc)
            if execution is not None else WorkspaceRuntime(root, directory / "actions.jsonl", config))
        for action in actions:
            if runtime.call(action["name"], action["arguments"]) != action["output"]:
                raise ValueError("Workspace operation differs from independent replay")
        if execution is not None:
            runtime.finish_replay()
        valid = workspace_snapshot(root, config) == workspace_snapshot(bundle / "agent_workspace", config)
    result = {"enabled": True, "valid": valid, "observations_match": observations_match,
            "replay_eligible": valid and observations_match, "operations": len(actions),
            "scope": "File effects only, not business completion or deliverable correctness"}
    if execution is not None:
        from .execution_bundle import execution_accounting
        execution_accounting(bundle)
        result["execution_usage"] = logical_execution_usage(actions, ptc)
        result["verification_physical_executions"] = runtime.usage["physical_executions"]
    return result


def split_tools(bundle, records):
    kinds = tool_kinds(bundle)
    business, workspace = [], []
    for row in records:
        kind = kinds[row["name"]]
        if row.get("tool_kind", kind) != kind:
            raise ValueError("Recorded tool category differs from trusted declaration")
        (business if kind == "business" else workspace).append(row)
    return business, workspace


def workspace_usage(records):
    records = [r for r in records if r["name"] not in {"python_execute", "ptc_execute"}]
    succeeded = [r for r in records if r["output"].get("ok") is True]
    return {"calls": len(records), "public_rejections": len(records) - len(succeeded),
            "returned_text_bytes": sum(len(r["output"]["data"]["content"].encode("utf-8"))
                                       for r in succeeded if r["name"] == "workspace_read"),
            "written_content_bytes": sum(r["output"]["data"]["bytes"] for r in succeeded
                                         if r["name"] in {"workspace_write", "workspace_edit"}),
            "scope": "Logical returned text and replacement content, not total OS I/O or token usage"}


class WorkspaceComposition:
    """Only the internal MCP metadata adapter needs this composition wrapper."""

    def __init__(self, business, workspace):
        self.business, self.workspace = business, workspace
        from .execution_bundle import ProductionExecution
        from .python_execution import python_tools
        extra = [*workspace_tools(), *(python_tools() if isinstance(workspace, ProductionExecution) else [])]
        if isinstance(workspace, ProductionExecution):
            from .business_binding import business_binding
            from .ptc_config import ptc_tools
            if business_binding(workspace.bundle)["ptc"] is not None:
                extra.extend(ptc_tools())
        self.workspace_names = {tool["name"] for tool in extra}
        if self.workspace_names & business.metadata.keys():
            raise ValueError("Duplicate MCP tool name")
        self.metadata = {**business.metadata, **{
            tool["name"]: {"description": tool["description"], "input_schema": tool["parameters"]}
            for tool in extra}}

    def call(self, name, arguments):
        from .execution_bundle import ProductionExecution, ensure_execution_known
        bundle = self.workspace.bundle if isinstance(self.workspace, ProductionExecution) else self.workspace.journal.parent
        ensure_execution_known(bundle)
        return (self.workspace if name in self.workspace_names else self.business).call(name, arguments)
