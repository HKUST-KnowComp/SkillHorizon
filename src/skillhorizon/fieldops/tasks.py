"""Four business families and construction-only public reference policies."""
import asyncio
from copy import deepcopy
from pathlib import Path
from skillhorizon.construction_provenance import construction_entry, observe_call
import random
import shutil

from ..experiment.mcp_tools import provider_tools_from_mcp_listing
from ..experiment.tasks import action_messages, user_message
from ..util import canonical_json, load_jsonl, write_json
from . import FORMAT, REVISION, PROCURE_INTERFACE_REVISION
from .runtime import OperationsRuntime, create_operations_server
from .skills import install_skills, skill_specs, SCRIPT_SKILLS
from .planning import PLANNING_SKILLS

FAMILIES = {"joint_recovery": "fieldops", "staged_maintenance": "fieldops",
            "joint_procurement": "procureops", "split_fulfillment": "procureops"}


def _validate_options(family, seed, groups, revision, composition, resource_profile,
                      business_program, business_scenario, device_counts, service_scale=None):
    from .field_layout import SERVICE_LIMITS
    if service_scale is not None and (service_scale not in SERVICE_LIMITS or
            (business_program != "pressure_branches_v3" or business_scenario not in
             {"mesh_convergence", "mesh_supply_shocks"})):
        raise ValueError("Extended services require a named FieldOps mesh scenario")
    if business_program is not None and type(business_program) is not str:
        raise ValueError("business_program must be a supported name or None")
    maximum = 16 if business_program in {"pressure_branches_v3", "assembly_network_v3"} else 12
    if service_scale:
        maximum = SERVICE_LIMITS[service_scale][1]
    if business_program == "assembly_network_v3" and business_scenario == "quality_reuse":
        maximum = 64
    if type(family) is not str or family not in FAMILIES or type(groups) is not int or not 2 <= groups <= maximum or type(seed) is not int or type(revision) is not bool:
        raise ValueError(f"Use a supported family, integer seed, 2..{maximum} groups and boolean revision")
    if type(composition) is not str or composition not in {"standard", "coupled"}:
        raise ValueError("Unknown business composition")
    if type(resource_profile) is not str or resource_profile not in {"relaxed", "constrained"}:
        raise ValueError("Unknown resource profile")
    from .scenarios import validate
    validate(business_scenario, FAMILIES[family], business_program, revision, groups, service_scale)
    if business_program is not None:
        from .horizon import PROGRAMS
        if business_program not in PROGRAMS or PROGRAMS[business_program] != FAMILIES[family]:
            raise ValueError("Use a domain-matching horizon program")
        minimum = 8 if business_program in {"pressure_branches_v3", "assembly_network_v3"} else 6
        if service_scale:
            minimum = SERVICE_LIMITS[service_scale][0]
        if groups < minimum:
            raise ValueError(f"Horizon program requires at least {minimum} groups")
        required_family = "staged_maintenance" if FAMILIES[family] == "fieldops" else "split_fulfillment"
        if family != required_family or composition != "standard" or resource_profile != "relaxed":
            raise ValueError("Horizon programs require their domain family and standard relaxed construction")
    from .field_layout import normalize_counts
    return normalize_counts(FAMILIES[family], business_program, groups, device_counts, service_scale)


def initialize_operations(root, *, family="joint_recovery", seed=20260905, groups=2, revision=False, composition="standard", resource_profile="relaxed", business_program=None, business_scenario=None, device_counts=None, skill_protocol="procedural", service_scale=None, instance_variant=None):
    if skill_protocol not in {"procedural", "skill_declaration_v1"}:
        raise ValueError("Unknown Operations construction skill protocol")
    if skill_protocol == "skill_declaration_v1" and (family != "staged_maintenance" or business_program != "pressure_branches_v3"):
        raise ValueError("Declaration construction requires FieldOps pressure_branches_v3")
    counts = _validate_options(family, seed, groups, revision, composition, resource_profile,
                               business_program, business_scenario, device_counts, service_scale)
    from .field_layout import stage_layout, REVISION as LAYOUT_REVISION
    from .scenarios import configure as configure_scenario
    root = Path(root)
    root.mkdir(parents=True)
    rng, domain = random.Random(seed), FAMILIES[family]
    p = {"project_id": f"{family}-{seed}-G{groups}-{composition}-{resource_profile}", "environment_revision": REVISION, "domain": domain, "family": family, "composition": composition,
         "resource_profile": resource_profile, "business_scenario": business_scenario,
         "artifacts": {}, "active": {}, "next_run": 1, "revision": 0, "acceptance": None, "notices": [],
         "scheduled_change": None}
    if service_scale:
        p["service_scale"] = service_scale
    if domain == "fieldops":
        layout = stage_layout(counts if counts is not None else [2] * groups)
        if counts is not None:
            p["device_counts"] = counts
            p["device_layout"] = {"revision": LAYOUT_REVISION, "stages": layout}
        p.update(goal="Restore every device to the public load-test limits using safe isolation closure and qualified crews. "
                      "Respect recorded maintenance windows and upstream measured pressure for staged work. "
                      "Use compatible parts or legitimate recalibration, close all isolations, and accept the complete system. "
                      "Public inspection updates may arrive after a restoration milestone. "
                      "Each repair consumes its device's duration in maintenance hours and each valid test one hour, "
                      "including failed measurements and rework. Per-device work must fit the isolation window; "
                      "devices within one boundary may be serviced in parallel. Reads and planning do not advance work time.",
                 devices={}, windows={}, crews={"crew-A": {"qualified": True}, "crew-B": {"qualified": True}},
                 parts={"spare-A": {"compatible_type": "pump", "quantity": 2 * groups, "initial_quantity": 2 * groups},
                        "spare-B": {"compatible_type": "pump", "quantity": groups, "initial_quantity": groups}},
                 isolations={}, minimum_upstream_pressure=85)
        for group in range(groups):
            p["windows"][f"W{group + 1}"] = {"start": group * 8, "end": group * 8 + 6}
            # Preserve the original RNG stream even when one role is absent.
            severities = [rng.randrange(5, 14) for _ in range(2)]
            row = layout[str(group + 1)]
            for key, role in zip(row["device_ids"], row["severity_roles"]):
                predecessors = list(layout[str(group)]["device_ids"]) if family == "staged_maintenance" and group else []
                if composition == "coupled" and family == "staged_maintenance" and group == groups - 1:
                    predecessors = [key for stage in range(1, group + 1) for key in layout[str(stage)]["device_ids"]]
                p["devices"][key] = {"type": "pump", "boundaries": [f"feed-{group + 1}"], "duration": 2,
                    "earliest_start": group * 8 if family == "staged_maintenance" else 0,
                    "predecessors": predecessors, "severity": severities[1 if role == "high" else 0], "revision": 1,
                    "repair_methods": {"replace": 12, "recalibrate": 9}, "minimum_test_load": 50,
                    "maximum_residual": 5, "rated_pressure": 100, "minimum_pressure": 90, "status": "pending"}
        if revision:
            p["scheduled_change"] = {"target": layout[str(groups)]["device_ids"][-1], "applied": False}
        if composition == "coupled" and family == "joint_recovery":
            p["crews"].pop("crew-B")
        if resource_profile == "constrained":
            for index, device in enumerate(p["devices"].values()):
                device["severity"] = rng.randrange(6, 9) if index % 2 == 0 else 13
            for part, count in zip(p["parts"].values(), ((groups + 1) // 2, groups // 2)):
                part.update(quantity=count, initial_quantity=count)
    else:
        requests = {f"REQ{i + 1:02d}": {"quantity": 4 + rng.randrange(3), "delivery_deadline": 4 * groups + 8} for i in range(groups)}
        total = sum(value["quantity"] for value in requests.values())
        p.update(goal="Deliver exactly each request's quantity while respecting total and allocated budgets, current supplier "
                      "capacity, request delivery deadlines and delivery-slot limits. Slots are scheduled resource reservations, not the API-turn clock. "
                      "Alternative suppliers, split purchase plans and partial receipts are allowed. "
                      "Cancel unfulfillable undelivered quantities and reuse their refunded budget if supply changes. "
                      "Accept a complete nonduplicated set of receipts with no outstanding open commitments.",
                 requests=requests, total_budget_cents=total * 130, budget_id=None, orders={},
                 suppliers={"SUP-A": {"unit_price_cents": 100, "capacity": total, "revision": 1,
                                      "per_slot_capacity": 3 if family == "split_fulfillment" else total, "earliest_slot": 1},
                            "SUP-B": {"unit_price_cents": 125, "capacity": total, "revision": 1,
                                      "per_slot_capacity": 4 if family == "split_fulfillment" else total, "earliest_slot": 1}})
        if revision:
            p["scheduled_change"] = {"target": "SUP-A", "capacity": max(1, total // 3), "applied": False}
        if composition == "coupled":
            p["suppliers"]["SUP-B"]["unit_price_cents"] = 110
            p["suppliers"]["SUP-C"] = {"unit_price_cents": 125, "capacity": total, "revision": 1,
                                      "per_slot_capacity": 3 if family == "split_fulfillment" else total, "earliest_slot": 2}
            for i, request in enumerate(p["requests"].values()):
                request["eligible_suppliers"] = ["SUP-A" if i % 2 == 0 else "SUP-B", "SUP-C"]
        if resource_profile == "constrained":
            for i, request in enumerate(p["requests"].values()):
                request.update(quantity=4 + rng.randrange(2), delivery_deadline=max(4, 2 * groups + 2 if family == "split_fulfillment" else groups + 2),
                               eligible_suppliers=["SUP-A" if i % 2 == 0 else "SUP-B", "SUP-C"])
            a_demand = sum(v["quantity"] for i, v in enumerate(p["requests"].values()) if i % 2 == 0)
            b_demand = sum(v["quantity"] for i, v in enumerate(p["requests"].values()) if i % 2 == 1)
            p["suppliers"] = {
                "SUP-A": {"unit_price_cents": 100, "capacity": a_demand, "revision": 1, "per_slot_capacity": 2, "earliest_slot": 1},
                "SUP-B": {"unit_price_cents": 110, "capacity": b_demand - 2 * (groups // 2), "revision": 1,
                          "per_slot_capacity": 1 if family == "split_fulfillment" else 2, "earliest_slot": 1},
                "SUP-C": {"unit_price_cents": 125, "capacity": 2 * groups, "revision": 1,
                          "per_slot_capacity": 2, "earliest_slot": 2}}
            p["total_budget_cents"] = sum((v["quantity"] - 2) * (100 if i % 2 == 0 else 110) + 250
                                           for i, v in enumerate(p["requests"].values()))
            if revision:
                p["scheduled_change"] = {"target": "SUP-A", "capacity": a_demand - 2 * ((groups + 1) // 2), "applied": False}
    from .horizon import configure
    configure(p, groups, rng, business_program, revision)
    configure_scenario(p, groups, rng, business_scenario)
    if service_scale:
        p["project_id"] += "-" + service_scale
    from .field_variants import configure as configure_variant
    configure_variant(p, seed, instance_variant)
    if domain == "procureops":
        p["public_interface_revision"] = PROCURE_INTERFACE_REVISION
    if skill_protocol == "skill_declaration_v1":
        p["skill_protocol"] = skill_protocol
        p["project_id"] += "-declaration-v1"
        del p["active"], p["next_run"]
    write_json(root / "project.json", p)
    install_skills(root / "skills", domain, business_program, business_scenario, skill_protocol=skill_protocol)
    if p.get('service_rules', {}).get('allow_restored_rework'):
        for name in ('plan-isolation', 'isolate-devices', 'repair-device'):
            path = root / 'skills' / name / 'SKILL.md'
            with path.open('a') as handle:
                handle.write('\nCorrective maintenance: restored boundaries can be isolated again using a new '
                    'feasible window after all completed work in the affected service and its descendants. '
                    'Affected open isolations must first be closed. Re-isolation suspends the service and '
                    'downstream certifications, retaining current physical repairs and unrelated services. '
                    'Use repair-device to improve an inadequate method, or retest-device to reuse a current repair.\n')
    return p


def execute_reference(runtime, *, execution_mode="macro", alternate=False, use_scripts=True,
                      repair_policy="feasible", supplier_policy="feasible", skill_protocol=None, max_calls=None,
                      reference_policy=None):
    if reference_policy not in (None, "public_state_cache_v1", "public_demand_cache_v2", "public_replacement_cache_v1"):
        raise ValueError("Unknown reference policy")
    if reference_policy and (runtime.project.get("domain") != "fieldops" or
                             runtime.project.get("business_program") != "pressure_branches_v3"):
        raise ValueError("Public state cache requires FieldOps pressure_branches_v3")
    if skill_protocol not in (None, "skill_declaration_v1"):
        raise ValueError("Unknown reference skill protocol")
    if skill_protocol == "skill_declaration_v1" and (
            runtime.project.get("skill_protocol") != skill_protocol or
            runtime.project.get("domain") != "fieldops" or
            runtime.project.get("business_program") != "pressure_branches_v3" or
            execution_mode != "procedural" or type(max_calls) is not int or max_calls < 1):
        raise ValueError("Declaration reference requires a matching FieldOps world and finite call limit")
    if skill_protocol is None and (runtime.project.get("skill_protocol") == "skill_declaration_v1" or max_calls is not None):
        raise ValueError("Declaration reference protocol must be explicit")
    if runtime.project.get("business_program"):
        if repair_policy != "feasible" or supplier_policy != "feasible":
            raise ValueError("Legacy diagnostic policies do not apply to horizon programs")
        from .horizon_witness import HorizonWitness
        if reference_policy == "public_state_cache_v1":
            from .cached_witness import CachedFieldWitness
            HorizonWitness = CachedFieldWitness
        elif reference_policy == "public_demand_cache_v2":
            from .demand_witness import DemandFieldWitness
            HorizonWitness = DemandFieldWitness
        if reference_policy == "public_replacement_cache_v1":
            from .replacement_witness import ReplacementWitness
            HorizonWitness = ReplacementWitness
        async def invoke(name, arguments):
            return runtime.call(name, arguments)
        policy = HorizonWitness(invoke, mode=execution_mode, scripts=use_scripts, alternate=alternate,
                                skill_protocol=skill_protocol, max_calls=max_calls)
        asyncio.run(policy.run())
        if not runtime.score()["success"]:
            raise ValueError("Horizon reference failed independent evaluation")
        return policy.stages
    if repair_policy not in {"feasible", "replace_only"} or supplier_policy not in {"feasible", "backup_only"}:
        raise ValueError("Unknown construction diagnostic policy")
    stages, read = [], set()
    def call(name, args):
        output = runtime.call(name, args)
        if not output["ok"]:
            raise ValueError(f"Reference operation rejected: {name}: {output}")
        return output
    def run(name, args):
        start = len(load_jsonl(runtime.root / "model_skill_actions.jsonl"))
        if execution_mode == "macro":
            output = call(name, args)
        elif execution_mode == "procedural":
            if name not in read:
                call("read_skill_file", {"skill_id": name, "path": "SKILL.md"})
                read.add(name)
            opened = call("begin_skill", {"skill_id": name, "arguments": args})
            if name in PLANNING_SKILLS:
                draft = call("execute_step", {"run_id": opened["run_id"], "step_id": "draft", "arguments": args})
                checked = (call("run_skill_script", {"run_id": opened["run_id"]}) if use_scripts and name in SCRIPT_SKILLS else
                           call("execute_step", {"run_id": opened["run_id"], "step_id": "check", "arguments": {"draft_id": draft["artifact_id"]}}))
                call("execute_step", {"run_id": opened["run_id"], "step_id": "submit", "arguments": {
                    "draft_id": draft["artifact_id"], "check_id": checked["artifact_id"]}})
            elif use_scripts and name in SCRIPT_SKILLS:
                call("run_skill_script", {"run_id": opened["run_id"]})
            else:
                call("execute_step", {"run_id": opened["run_id"], "step_id": "compute", "arguments": args})
            output = call("finish_skill", {"run_id": opened["run_id"]})
        else:
            raise ValueError("Unknown execution mode")
        stages.append({"skill_id": name, "arguments": deepcopy(args), "artifact_id": output["artifact_id"],
                       "action_start": start, "action_end": len(load_jsonl(runtime.root / "model_skill_actions.jsonl"))})
        return output["artifact_id"]
    if runtime.project["domain"] == "fieldops":
        boundaries = sorted({b for d in runtime.project["devices"].values() for b in d["boundaries"]}, key=lambda v: int(v.split("-")[1]))
        if alternate and runtime.project["family"] == "joint_recovery":
            boundaries.reverse()
        restorations = []
        for slot, boundary in enumerate(boundaries, 1):
            devices = [key for key, d in runtime.project["devices"].items() if boundary in d["boundaries"]]
            predecessors = {v for key in devices for v in runtime.project["devices"][key]["predecessors"]}
            upstream = [value for value in restorations if predecessors & set(runtime.artifact(value)["data"]["device_ids"])]
            plan = run("plan-isolation", {"device_ids": devices, "boundary_ids": [boundary], "window_id": f"W{slot}",
                       "crew_id": "crew-B" if alternate and "crew-B" in runtime.project["crews"] else "crew-A", "upstream_restoration_ids": upstream})
            isolation = run("isolate-devices", {"plan_id": plan})
            tests = []
            for device in reversed(devices) if alternate else devices:
                condition = runtime.project["devices"][device]
                methods = ("recalibrate", "replace") if alternate or runtime.project["resource_profile"] == "constrained" else ("replace", "recalibrate")
                if repair_policy == "replace_only":
                    methods = ("replace",)
                choices = [(method, load) for load in ((75, 50) if alternate else (50, 75)) for method in methods
                           if max(0, condition["severity"] - condition["repair_methods"][method]) + load // 25 <= condition["maximum_residual"]]
                if not choices:
                    raise ValueError("Public repair options cannot meet the load-test goal")
                method, load = choices[0]
                parts = [key for key, value in runtime.project["parts"].items() if value["quantity"] > 0 and value["compatible_type"] == condition["type"]]
                repair = run("repair-device", {"device_id": device, "isolation_id": isolation,
                    "method": method, "part_id": "" if method == "recalibrate" else ((parts[-1] if alternate else parts[0]) if parts else "spare-A")})
                tests.append(run("test-device", {"device_id": device, "repair_id": repair, "test_load": load}))
            restorations.append(run("restore-group", {"isolation_id": isolation, "test_ids": tests}))
            call("read_project", {})
        run("accept-system", {"restoration_ids": restorations})
    else:
        from .reference_planning import purchase_lines
        from .procureops import _committed, delivery_schedule
        p = runtime.project
        budgets = {key: (value["quantity"] * 130 if p["resource_profile"] == "relaxed" else
                   (value["quantity"] - 2) * min(p["suppliers"][s]["unit_price_cents"] for s in value["eligible_suppliers"]) + 250)
                   for key, value in p["requests"].items()}
        budget = run("allocate-budget", {"allocations": [{"request_id": key, "budget_cents": amount} for key, amount in budgets.items()]})
        receipts = []
        for _ in range(1000):
            p = runtime.project
            _, _, used = _committed(p)
            overcommitted = {s for s, quantity in used.items() if quantity > p["suppliers"][s]["capacity"]}
            pending = [(order_id, line) for order_id, order in p["orders"].items() for line in order["lines"]
                       if line["quantity"] > line["received"] + line["canceled"]]
            if overcommitted:
                for order_id in sorted({key for key, _ in pending}):
                    canceled = [{"request_id": row["request_id"], "supplier_id": row["supplier_id"],
                                 "quantity": row["quantity"] - row["received"] - row["canceled"]}
                                for key, row in pending if key == order_id and row["supplier_id"] in overcommitted]
                    if canceled:
                        run("cancel-remainder", {"order_id": order_id, "quantities": canceled})
                continue
            lines = purchase_lines(p, budgets, alternate)
            if supplier_policy == "backup_only" and not p["orders"]:
                backup = max(p["suppliers"], key=lambda key: p["suppliers"][key]["unit_price_cents"])
                lines = [{"request_id": key, "supplier_id": backup, "quantity": value["quantity"]} for key, value in p["requests"].items()]
            if lines:
                plan = run("plan-purchase", {"budget_id": budget, "lines": lines})
                run("place-orders", {"plan_id": plan})
                continue
            if not pending:
                break
            schedule = delivery_schedule(p, [{"order_id": key, "request_id": row["request_id"], "supplier_id": row["supplier_id"],
                        "quantity": row["quantity"] - row["received"] - row["canceled"]} for key, row in pending])
            if not schedule:
                raise ValueError("No public deadline-feasible fulfillment schedule")
            first = schedule[-1] if alternate else schedule[0]
            shipment = [v for v in schedule if v["delivery_slot"] == first["delivery_slot"] and v["order_id"] == first["order_id"]] if alternate else [first]
            receipts.append(run("receive-shipment", {"order_id": first["order_id"], "delivery_slot": first["delivery_slot"],
                "quantities": [{k: v[k] for k in ("request_id", "supplier_id", "quantity")} for v in shipment]}))
            call("read_project", {})
        else:
            raise ValueError("Reference fulfillment did not converge")
        run("accept-deliveries", {"receipt_ids": receipts})
    if not runtime.score()["success"]:
        raise ValueError("Reference failed independent business evaluation")
    return stages


def structure_metrics(runtime, stages):
    from ..experiment.structure_metrics import business_data_structure
    actions = load_jsonl(runtime.root / "model_skill_actions.jsonl")
    order = {a["result"]["artifact_id"]: i for i, s in enumerate(stages) for a in actions[s["action_start"]:s["action_end"]]
             if a["result"].get("artifact_id")}
    depths, gaps, joins = {}, [], 0
    for identifier, artifact in runtime.project["artifacts"].items():
        parents = artifact["inputs"]
        depths[identifier] = 1 + max((depths[v] for v in parents), default=0)
        gaps.extend(order[identifier] - order[v] for v in parents)
        joins += int(len(parents) > 1)
    return {"measurement": "reference_execution_not_minimal_plan", "reference_skill_stages": len(stages),
            "business_data_graph": business_data_structure(runtime.project, stages, actions),
            "reference_data_depth": max(depths.values(), default=0), "content_join_count": joins,
            "max_data_reuse_gap_skills": max(gaps, default=0), "event_count": len(runtime.project["notices"]),
            "effective_event_count": sum(n["state_changed"] for n in runtime.project["notices"]),
            "resource_constraints": ["crew", "isolation_boundary", "window", "spare_stock"] if runtime.project["domain"] == "fieldops" else
                                    ["request_budget", "project_budget", "supplier_capacity", "delivery_slot_capacity", "delivery_deadline"],
            "intermediate_plan_artifacts_included_in_data_depth": True,
            "physical_state_constraints_included_in_data_depth": False}


async def _tools(root, mode):
    from mcp import Client
    async with Client(create_operations_server(root, mode)) as client:
        return provider_tools_from_mcp_listing(await client.list_tools())


@construction_entry
def build_operations(output, *, family="joint_recovery", seed=20260905, groups=2, revision=False,
                     execution_mode="macro", suffix_skills=None, composition="standard", resource_profile="relaxed", business_program=None, business_scenario=None, device_counts=None):
    root = Path(output).resolve()
    if root.exists():
        raise FileExistsError(root)
    if suffix_skills is not None and (type(suffix_skills) is not int or suffix_skills < 1):
        raise ValueError("suffix_skills must be positive")
    counts = _validate_options(family, seed, groups, revision, composition, resource_profile,
                               business_program, business_scenario, device_counts)
    root.mkdir(parents=True)
    config = dict(family=family, seed=seed, groups=groups, revision=revision, execution_mode=execution_mode, suffix_skills=suffix_skills, composition=composition, resource_profile=resource_profile, business_program=business_program, business_scenario=business_scenario, device_counts=deepcopy(device_counts))
    write_json(root / "BUILD.json", {"status": "building", "config": config})
    observe_call(initialize_operations, root / "initial", family=family, seed=seed, groups=groups, revision=revision, composition=composition, resource_profile=resource_profile, business_program=business_program, business_scenario=business_scenario, device_counts=counts)
    shutil.copytree(root / "initial", root / "reference")
    runtime = OperationsRuntime(root / "reference")
    from ..experiment.reference_trace import callable_policy, freeze_task
    policy_before = callable_policy(execute_reference, {"execution_mode": execution_mode})
    stages = execute_reference(runtime, execution_mode=execution_mode)
    remaining = len(stages) if suffix_skills is None else suffix_skills
    if remaining > len(stages):
        raise ValueError("suffix exceeds reference business stages")
    count = len(stages) - remaining
    prefix = stages[count - 1]["action_end"] if count else 0
    actions = load_jsonl(root / "reference/model_skill_actions.jsonl")
    shutil.copytree(root / "initial", root / "checkpoint")
    checkpoint = OperationsRuntime(root / "checkpoint")
    for action in actions[:prefix]:
        if checkpoint.call(action["skill_id"], action["arguments"]) != action["result"]:
            raise ValueError("Nondeterministic operation prefix")
    brief = {"project": OperationsRuntime(root / "initial").read_project(), "skill_catalog": [
        {"skill_id": name, "description": spec[1], "script_available": name in SCRIPT_SKILLS,
         "entrypoint": "SKILL.md", "files": ["SKILL.md"] + (["scripts/execute.py", "scripts/calculations.py"] if name in SCRIPT_SKILLS else [])}
        for name, spec in runtime.skills.items()]}
    messages = [user_message("ACTIVE PROJECT. " + canonical_json(brief)), *action_messages(actions[:prefix], "operations-history"),
                user_message(f"Continue active project {runtime.project['project_id']} to its business goal using the available tools.")]
    task = {"format": FORMAT, "version": 1, "environment_revision": REVISION, "task_id": runtime.project["project_id"], "config": config,
        "messages": messages, "tools": asyncio.run(_tools(root / "checkpoint", execution_mode)),
        "checkpoint_action_count": prefix, "remaining_skill_count": remaining, "reference_skill_count": len(stages),
        "reference_tool_call_count": len(actions) - prefix, "skill_ids": list(runtime.skills), "execution_mode": execution_mode,
        "history_source": f"executed_{execution_mode}_expert_prefix", "static_dag_exposed": False,
        "observation_protocol": "chronological_ops_summary_query_v2",
        "skill_discovery_protocol": "public_entrypoint_and_file_inventory_v1",
        "structure": structure_metrics(runtime, stages)}
    from ..benchmark_spec import BENCHMARK_REVISION
    task["benchmark_revision"] = BENCHMARK_REVISION
    if counts is not None:
        from .field_layout import REVISION as LAYOUT_REVISION
        task["device_layout_revision"] = LAYOUT_REVISION
    if runtime.project["domain"] == "procureops":
        task["observation_protocol"] = PROCURE_INTERFACE_REVISION
        task["acceptance_coverage"] = {"service": "accept-deliveries", "scope": "joint_public_contract",
                                       "business_goal_relation": "goal_equivalent_on_valid_states",
                                       "ends_episode": False}
    write_json(root / "task.json", task)
    write_json(root / "continuation.json", actions[prefix:])
    if callable_policy(execute_reference, {"execution_mode": execution_mode}) != policy_before:
        raise ValueError("Operations reference producer changed during execution")
    if business_program and execution_mode == "procedural":
        freeze_task(root, task, actions[prefix:], policy_before)
    write_json(root / "construction.json", {"stages": stages, "structure": task["structure"]})
    write_json(root / "BUILD.json", {"status": "complete", "config": config, "expert_success": True, "external_calls": 0})
    return task
