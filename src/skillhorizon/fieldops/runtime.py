"""Atomic public operations with persisted domain state and ordinary skill runs."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import get_origin, get_args

from ..errors import DomainError
from ..util import append_jsonl, load_json, load_jsonl, write_json
from ..experiment.arguments import business_arguments_equal
from ..public_contract import completion, public_progress, register_tools, validate_fields
from .calculations import calculate
from .skills import skill_specs, input_type, SCRIPT_SKILLS, SCRIPT
from .observations import OperationsObservations, receipt
from . import REVISION, PROCURE_INTERFACE_REVISION


class OperationsRuntime(OperationsObservations):
    def __init__(self, root):
        self.root = Path(root)
        self.reload()

    def reload(self):
        self.project = load_json(self.root / "project.json")
        if self.project.get("environment_revision") != REVISION:
            raise ValueError("Rebuild this Operations world; use archived code for historical execution")
        if (self.project["domain"] == "procureops" and
                self.project.get("public_interface_revision") != PROCURE_INTERFACE_REVISION):
            raise ValueError("Rebuild ProcureOps for the joint-acceptance interface; use archived code for old instances")
        self.skills = skill_specs(self.project["domain"], self.project.get("business_program"), self.project.get("business_scenario"))

    def require(self, condition, code, message, details=None):
        if not condition:
            raise DomainError(code, message, details)

    def save(self):
        write_json(self.root / "project.json", self.project)

    def artifact(self, identifier, kind=None):
        value = self.project["artifacts"].get(identifier)
        self.require(value is not None, "UNKNOWN_ARTIFACT", "Unknown public artifact")
        self.require(kind is None or value["kind"] == kind, "ARTIFACT_KIND", "Wrong business artifact type")
        return value

    def emit(self, kind, data, inputs=()):
        identifier = f"artifact-{len(self.project['artifacts']) + 1:05d}"
        value = {"artifact_id": identifier, "kind": kind, "data": deepcopy(data), "inputs": list(inputs)}
        self.project["artifacts"][identifier] = value
        return deepcopy(value)

    def public_progress(self):
        p = self.project
        if p["domain"] == "fieldops":
            pending = {"unrestored_devices": sum(d["status"] != "restored" for d in p["devices"].values()),
                       "open_isolations": sum(i["open"] for i in p["isolations"].values())}
            if "services" in p:
                pending["inactive_services"] = sum(not s["active"] for s in p["services"].values())
        else:
            lines = [line for order in p["orders"].values() for line in order["lines"]]
            pending = {"unfulfilled_requests": sum(sum(line["received"] for line in lines if line["request_id"] == key) != r["quantity"]
                                                   for key, r in p["requests"].items()),
                       "outstanding_order_lines": sum(line["quantity"] > line["received"] + line["canceled"] for line in lines)}
            if "contract" in p:
                pending["unsettled_contract_stages"] = p["contract"]["stages"]-len(p["settlements"])
                cutoff = min(len(p["settlements"])+1, p["contract"]["stages"])
                credited = {key for o in p["orders"].values() for key in o["funding"]["retention_ids"]}
                credited.update(key for v in p["artifacts"].values() if v["kind"] == "retention_collection"
                                for key in v["data"]["retention_ids"])
                pending["uncollected_mature_retention_claims"] = sum(key not in credited and
                    p["artifacts"][key]["data"]["maturity_stage"] <= cutoff for key in p["settlements"].values())
            if "kits" in p:
                pending["unassembled_kits"] = p["contract"]["stages"]-len(p["kits"])
        return public_progress(p.get("active", {}), p["acceptance"], "recorded" if p["acceptance"] else "missing", pending)

    def read_artifact(self, artifact_id):
        return deepcopy(self.artifact(artifact_id))

    def read_skill_file(self, skill_id, path):
        self.require(skill_id in self.skills and path in {"SKILL.md", "scripts/execute.py", "scripts/calculations.py"},
                     "FILE_NOT_EXPOSED", "Only installed skill instructions and optional arithmetic scripts are public")
        source = self.root / "skills" / skill_id / path
        self.require(source.is_file() and not source.is_symlink(), "FILE_NOT_FOUND", "File is unavailable")
        return {"skill_id": skill_id, "path": path, "content": source.read_text()}

    def validate(self, name, args):
        self.require(name in self.skills, "UNKNOWN_SKILL", "Unknown fixed skill")
        fields = self.skills[name][0]
        self.require(isinstance(args, dict), "ARGUMENTS", "Arguments must be an object containing all declared inputs")
        self.require(set(args) == set(fields), "ARGUMENTS",
            "Supply exactly the declared inputs; missing: " + ", ".join(sorted(set(fields) - set(args))) +
            f"; unexpected field count: {len(set(args) - set(fields))}")
        for key, kind in fields.items():
            value = args[key]
            if get_origin(kind) is list:
                item_type = get_args(kind)[0]
                good = isinstance(value, list) and all(isinstance(v, item_type) for v in value)
                if good and item_type is str:
                    good = len(value) == len(set(value))
            else:
                good = type(value) is kind
            self.require(good, "ARGUMENTS", f"{key} must be {input_type(kind)}; null is not allowed")

    def arithmetic(self, operation, data, use_script):
        if not use_script:
            return calculate(operation, data)
        script = self.root / "skills" / operation / "scripts/execute.py"
        support = script.with_name("calculations.py")
        self.require(operation in SCRIPT_SKILLS and script.is_file() and support.is_file(), "NO_SCRIPT", "This skill has no script")
        self.require(not script.is_symlink() and not support.is_symlink() and script.read_text() == SCRIPT and
                     support.read_bytes() == Path(__file__).with_name("calculations.py").read_bytes(),
                     "SCRIPT_CHANGED", "Only the installed fixed arithmetic script can run")
        with tempfile.TemporaryDirectory(prefix="skillhorizon-operation-") as tmp:
            from skillhorizon.experiment.construction_envelope import temporary_environment
            result = subprocess.run([sys.executable, "-I", str(script.resolve())], env=temporary_environment(), cwd=tmp,
                input=json.dumps({"operation": operation, "data": data}), capture_output=True, text=True, timeout=30)
        self.require(result.returncode == 0, "SCRIPT_ERROR", "Arithmetic script rejected its public input")
        return json.loads(result.stdout)

    def execute(self, name, args, use_script=False):
        self.validate(name, args)
        self.require(not use_script or name in SCRIPT_SKILLS, "NO_SCRIPT", "This skill is procedure-only")
        if self.project["domain"] == "fieldops":
            from .fieldops import execute
        else:
            from .procureops import execute
        notices_before = len(self.project.get('notices', []))
        result = execute(self, name, args, use_script)
        immediate = (deepcopy(self.project['notices'][notices_before:])
                     if self.project.get('service_rules', {}).get('allow_restored_rework') else [])
        self.project["revision"] += 1
        result["notices"] = immediate + self._event(name)
        return result

    def _event(self, name):
        event = (next((e for e in self.project["scheduled_changes"] if not e["applied"]), None)
                 if "scheduled_changes" in self.project else self.project["scheduled_change"])
        if not event or event["applied"]:
            return []
        p = self.project
        if event.get("kind") == "local_device_fault":
            if name != "commission-service":
                return []
            from .services import descendants
            target = event["target"]
            device = p["devices"][target]
            source = device["stage"]
            affected = [source, *descendants(p, source)]
            active = [s for s in p["services"].values() if s["active"]]
            if (len(active) < event["minimum_active_services"] or not p["services"][str(source)]["active"] or
                    not any(s["active"] for k,s in p["services"].items() if int(k) in affected and int(k) != source) or
                    not any(s["stage"] not in affected for s in active)):
                return []
            event_time = max(p["artifacts"][s["current_id"]]["data"]["effective_from"] for s in active)
            old_revision = device["revision"]
            device.update(severity=event["severity"], revision=old_revision+1)
            affected_devices = [key for stage in affected for key in p["services"][str(stage)]["device_ids"]]
            previous_active = [s["stage"] for s in active if s["stage"] in affected]
            for stage in affected:
                p["services"][str(stage)]["active"] = False
            for key in affected_devices:
                d = p["devices"][key]
                d["effective_from"] = max(d.get("effective_from", 0), event_time)
                if d["status"] == "restored" or key == target:
                    d["status"] = "needs_revalidation"
            p["physical_history"][target][str(device["revision"])] = deepcopy(device)
            p["acceptance"] = None
            notice = {"type":"local_device_fault", "device_id":target, "previous_revision":old_revision,
                "revision":device["revision"], "severity":device["severity"], "effective_from":event_time,
                "affected_service_stages":affected, "affected_device_ids":affected_devices,
                "previous_active_affected_stages":previous_active,
                "unaffected_active_stages":[s["stage"] for s in active if s["stage"] not in affected],
                "state_changed":True}
        elif event.get("kind") == "supply_pressure":
            progress = event.get("trigger") == "active_service_progress"
            details = {}
            if progress:
                if name != "commission-service":
                    return []
                depths = {}
                for key in sorted(p["services"], key=int):
                    service = p["services"][key]
                    depths[int(key)] = 1 + max((depths[s] for s in service["parent_stages"]), default=0)
                active = [s for s in p["services"].values() if s["active"]]
                active_depth = max((depths[s["stage"]] for s in active), default=0)
                if len(active) < event["minimum_active_services"] or active_depth < event["minimum_active_depth"]:
                    return []
                event_time = max(p["artifacts"][s["current_id"]]["data"]["effective_from"] for s in active)
                details = {"trigger": "active_service_progress", "previous_active_service_count": len(active),
                           "previous_active_max_depth": active_depth,
                           "affected_service_stages": sorted(s["stage"] for s in active)}
            else:
                if name != "restore-group" or sum(a["kind"] == "restoration" for a in p["artifacts"].values()) < event["after_restorations"]:
                    return []
                event_time = next(a["data"]["window_end"] for a in reversed(p["artifacts"].values()) if a["kind"] == "restoration")
            old_revision = p["network"]["revision"]
            p["network"].update(supply_pressure=event["pressure"], revision=old_revision + 1, effective_from=event_time)
            p["network"]["supply_history"][str(old_revision + 1)] = event["pressure"]
            p["network"]["effective_history"][str(old_revision + 1)] = event_time
            for identifier, device in p["devices"].items():
                if device["status"] == "restored":
                    device["status"] = "needs_revalidation"
            for service in p.get("services", {}).values():
                service["active"] = False
            p["acceptance"] = None
            notice = {"type": "supply_pressure_changed", "revision": p["network"]["revision"],
                      "supply_pressure": event["pressure"], "effective_from": event_time, "state_changed": True,
                      **details,
                      "invalidated_artifacts": [k for k, v in p["artifacts"].items() if v["data"].get("supply_revision") == old_revision]}
        elif event.get("kind") == "stage_supply":
            if name != "place-orders":
                return []
            latest = next(reversed(p["orders"].values()))
            if {p["requests"][line["request_id"]]["stage"] for line in latest["lines"]} != {event["stage"]}:
                return []
            supplier = p["suppliers"][event["target"]]
            delivered = sum(line["received"] for order in p["orders"].values() for line in order["lines"] if line["supplier_id"] == event["target"])
            supplier.update(capacity=delivered, revision=supplier["revision"] + 1)
            notice = {"type": "supply_capacity_changed", "supplier_id": event["target"], "capacity": delivered,
                      "stage": event["stage"], "state_changed": True}
        elif p["domain"] == "fieldops" and name == "restore-group":
            device = p["devices"][event["target"]]
            # A pending device fault update does not invalidate an already repaired different group.
            changed = device["status"] == "pending"
            if changed:
                device["severity"] += 2
                device["revision"] += 1
            notice = {"type": "inspection_update", "device_id": event["target"], "revision": device["revision"],
                      "state_changed": changed}
        elif p["domain"] == "procureops" and name == "receive-shipment":
            supplier = p["suppliers"][event["target"]]
            previous_capacity = supplier["capacity"]
            delivered = sum(line["received"] for order in p["orders"].values() for line in order["lines"] if line["supplier_id"] == event["target"])
            supplier["capacity"] = max(delivered, event["capacity"])
            supplier["revision"] += 1
            notice = {"type": "supply_capacity_changed", "supplier_id": event["target"], "capacity": supplier["capacity"],
                      "state_changed": supplier["capacity"] != previous_capacity}
        else:
            return []
        event["applied"] = True
        p["notices"].append(notice)
        return self.public_notices([notice])

    def begin_skill(self, skill_id, arguments):
        self.validate(skill_id, arguments)
        identifier = f"run-{self.project['next_run']}"
        self.project["next_run"] += 1
        self.project["active"][identifier] = {"skill_id": skill_id, "arguments": deepcopy(arguments), "output": None}
        self.project["revision"] += 1
        return {"run_id": identifier, "skill_id": skill_id}

    def active(self, run_id):
        self.require(run_id in self.project["active"], "NO_ACTIVE_SKILL", "Unknown open run")
        return self.project["active"][run_id]

    def execute_step(self, run_id, step_id, arguments):
        run = self.active(run_id)
        from .planning import PLANNING_SKILLS, step
        if run["skill_id"] in PLANNING_SKILLS:
            return step(self, run, step_id, arguments)
        self.require(step_id == "compute" and business_arguments_equal(arguments, run["arguments"]), "RUN_INPUTS",
            "Use step_id=compute and arguments containing the same complete inputs supplied to this run's begin_skill; "
            "do not replace them with an empty object")
        self.require(run["output"] is None, "STEP_COMPLETE", "This operation already committed")
        run["output"] = self.execute(run["skill_id"], arguments)
        return deepcopy(run["output"])

    def run_skill_script(self, run_id):
        run = self.active(run_id)
        self.require(run["output"] is None, "STEP_COMPLETE", "This operation already committed")
        if run["skill_id"] == "plan-purchase":
            from .planning import step
            return step(self, run, "check", {"draft_id": run.get("planning", {}).get("draft_id")}, use_script=True)
        run["output"] = self.execute(run["skill_id"], run["arguments"], use_script=True)
        return deepcopy(run["output"])

    def finish_skill(self, run_id):
        run = self.active(run_id)
        self.require(run["output"] is not None, "SKILL_INCOMPLETE", "Perform the business operation first")
        result = {**receipt(run["output"]), "run_id": run_id, "closed": True}
        del self.project["active"][run_id]
        self.project["revision"] += 1
        return result

    def abandon_skill(self, run_id):
        self.active(run_id)
        del self.project["active"][run_id]
        self.project["revision"] += 1
        return {"run_id": run_id, "abandoned": True, "committed_effects_retained": True}

    def _dispatch(self, name, arguments):
        if name in self.skills:
            return self.execute(name, arguments)
        if name in {"read_project", "read_artifact", "read_skill_file", "begin_skill", "execute_step", "run_skill_script", "finish_skill", "abandon_skill"} | {entry[0] for entry in self.query_tools}:
            return getattr(self, name)(**arguments)
        raise DomainError("UNKNOWN_SKILL", "Unknown public tool")

    def call(self, name, arguments):
        self.reload()
        before = deepcopy(self.project)
        try:
            public = {key: fields for key, _, fields in self.tool_entries("procedural")}
            if name in public:
                arguments = validate_fields(arguments, public[name])
            result = self._dispatch(name, arguments)
            self.save()
            result = {"ok": True, **result}
        except DomainError as exc:
            self.project = before
            result = {"ok": False, "error": exc.to_dict()}
        except (ValueError, TypeError, KeyError, IndexError, ArithmeticError) as exc:
            self.project = before
            result = {"ok": False, "error": {"code": "INVALID_INPUT", "message": str(exc)}}
        log_path = self.root / 'model_skill_actions.jsonl'
        cache_count = self.project.get('service_rules', {}).get('allow_restored_rework', False)
        size = log_path.stat().st_size if log_path.exists() else 0
        if cache_count and getattr(self, '_action_log_size', None) == size:
            count = self._action_log_count
        else:
            count = len(load_jsonl(log_path))
        append_jsonl(log_path, {"skill_id": name, "arguments": deepcopy(arguments), "result": result,
            "node_id": f"action-{count + 1}" if before["revision"] != self.project["revision"] else None})
        if cache_count:
            self._action_log_count, self._action_log_size = count + 1, log_path.stat().st_size
        return result

    def score(self):
        self.reload()
        if self.project["domain"] == "fieldops":
            from .fieldops import evaluate
        else:
            from .procureops import evaluate
        try:
            cases, success = evaluate(self)
            errors = []
        except (DomainError, ValueError, TypeError, KeyError, IndexError, ArithmeticError) as exc:
            cases, success, errors = {}, False, [str(exc)]
        active = self.project.get("active", {})
        acceptance = self.project["acceptance"]
        return {**completion(success, active, acceptance), "cases": cases,
                "completed_cases": sum(v["success"] for v in cases.values()), "total_cases": len(cases),
                "state_errors": errors}

    def snapshot(self):
        return {"project": load_json(self.root / "project.json")}

    def tool_entries(self, execution_mode):
        return operations_tool_entries(self, execution_mode)


def create_operations_server(root, execution_mode="macro", *, runtime_class=OperationsRuntime):
    from mcp.server import MCPServer
    runtime = runtime_class(root)
    server = MCPServer("SkillHorizon Resource and Quantity Project")
    register_tools(server, runtime, runtime.tool_entries(execution_mode))
    return server


def operations_tool_entries(runtime, execution_mode):
    entries = [("read_project", "Inspect goal, rule scalars, collection counts and public progress. Query resource collections for details. handoff_recorded is not verified business success; pending work and open runs are separate.", {}),
               ("read_artifact", "Read a committed public business artifact.", {"artifact_id": str}),
               ("read_skill_file", "Read instructions at path SKILL.md or optional code listed in the public skill file inventory. Paths are case-sensitive and relative to the selected skill.", {"skill_id": str, "path": str})]
    entries += runtime.query_tools
    if execution_mode == "macro":
        entries += [(name, description, fields) for name, (fields, description) in runtime.skills.items()]
    elif execution_mode == "procedural":
        entries += [("begin_skill", "Select a fixed skill and its business inputs.", {"skill_id": str, "arguments": dict}),
                    ("execute_step", "Execute the documented computation with selected inputs.", {"run_id": str, "step_id": str, "arguments": dict}),
                    ("run_skill_script", "Run this skill's optional public arithmetic script.", {"run_id": str}),
                    ("finish_skill", "Close a successfully computed run.", {"run_id": str}),
                    ("abandon_skill", "Close a run without undoing committed business effects.", {"run_id": str})]
    else:
        raise ValueError("Unknown operations execution mode")
    return entries
