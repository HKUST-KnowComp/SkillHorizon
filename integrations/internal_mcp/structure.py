"""Controller-only business structure; never infer dependencies from trace order."""

from collections import Counter
import json


def graph_metrics(nodes):
    """Nodes name explicit business prerequisites, not a prescribed tool policy."""
    by_id = {row["id"]: row for row in nodes}
    if len(by_id) != len(nodes):
        raise ValueError("Duplicate business node")
    parents = {key: set(row["parents"]) for key, row in by_id.items()}
    for key, values in parents.items():
        if len(values) != len(by_id[key]["parents"]) or not values <= by_id.keys():
            raise ValueError("Duplicate or unknown business prerequisite")
    depth, pending = {}, set(by_id)
    while pending:
        ready = sorted(key for key in pending if parents[key] <= depth.keys())
        if not ready:
            raise ValueError("Cyclic business prerequisites")
        for key in ready:
            depth[key] = 1 + max((depth[p] for p in parents[key]), default=0)
        pending.difference_update(ready)
    edges = [(parent, child) for child, values in parents.items() for parent in values]
    neighbors = {key: set(values) for key, values in parents.items()}
    for parent, child in edges:
        neighbors[parent].add(child)
    unseen, components = set(by_id), 0
    while unseen:
        pending_component = [unseen.pop()]
        components += 1
        while pending_component:
            newly_seen = neighbors[pending_component.pop()] & unseen
            unseen.difference_update(newly_seen)
            pending_component.extend(newly_seen)
    return {"nodes": len(nodes), "edges": len(edges), "longest_path_nodes": max(depth.values(), default=0),
        "weak_components": components, "root_nodes": sum(not p for p in parents.values()),
        "leaf_nodes": len(by_id.keys() - {p for p, _ in edges}),
        "join_nodes": sum(len(p) > 1 for p in parents.values()),
        "max_fan_in": max(map(len, parents.values()), default=0),
        "max_fan_out": max(Counter(p for p, _ in edges).values(), default=0),
        "cross_group_edges": sum(by_id[p]["group"] is not None and by_id[c]["group"] is not None
                                 and by_id[p]["group"] != by_id[c]["group"] for p, c in edges),
        "nonadjacent_stage_edges": sum(by_id[c]["stage"] - by_id[p]["stage"] > 1 for p, c in edges),
        "depth_layer_counts": dict(sorted(Counter(depth.values()).items())),
        "minimum_tool_horizon": None,
        "note": "Explicit business data/order obligations only. Layer size is not maximum antichain width; resource conflicts and policy-dependent edges are excluded."}


def business_nodes(identifier, public, world):
    nodes = []

    def add(key, parents, group, stage, kind):
        nodes.append({"id": key, "parents": list(parents), "group": group, "stage": stage, "kind": kind})

    if identifier == "C09":
        bookings = {}
        for request in public["requests"]:
            bookings[request["passenger_id"]] = request["bookings"]
            for leg, key in enumerate(request["bookings"], 1):
                add(key, request["bookings"][leg - 2:leg - 1] if leg > 1 else [], request["cohort_id"], leg, "passenger_leg")
        for group in public["transfer_network"]["groups"]:
            leg = group["leg"]
            add(group["group_id"], [bookings[p][leg - 2] for p in group["passenger_ids"]],
                None, leg, "cross_cohort_transfer")
        # Baggage constraints are separate obligations, not fabricated booking edges.
        for cargo in public["transfer_network"]["baggage_obligations"]:
            cohort = next(r["cohort_id"] for r in public["requests"] if r["passenger_id"] == cargo["passenger_id"])
            add(cargo["obligation_id"], [cargo["origin_booking_id"]], cohort,
                cargo["delivery_leg"], "through_baggage_obligation")
    elif identifier == "C25":
        blocks = {b["block_id"]: b for b in world["tables"]["page_block"]}
        for page in world["tables"]["page"]:
            required = json.loads(blocks[page["requirements_block_id"]]["content"])
            policy = required["capacity_policy"]
            add(page["page_id"], required["parent_page_ids"], page["project_id"], policy["stage"], "published_contract")
    elif identifier == "C06":
        for brief in world["social"]["briefs"].values():
            add(brief["brief_id"], brief["parent_brief_ids"], brief["campaign_id"], brief["wave"], "campaign_learning")
    elif identifier == "C47":
        jobs = public["workload_policy"]["jobs"]
        parents = {job["job_id"]: [] for job in jobs}
        resources = world["finops"]["resources"]
        for job in jobs:
            for consumer in job["consumers"]:
                parents[consumer["job_id"]].append(job["job_id"])
        for job in jobs:
            resource = resources[job["resource_id"]]
            add(job["job_id"], parents[job["job_id"]], resource["business_unit_id"], job["stage"], "reserved_workload")
    elif identifier == "C50":
        for order in public["orders"]:
            add(order["order_id"], order["parent_order_ids"], order["lane"], order["level"], "material_order")
    elif identifier == "C01":
        for node in public["nodes"]:
            add(node["node_id"], node["parent_node_ids"], node["case_id"], node["phase"], "funded_dispatch")
    elif identifier == "C11":
        for batch in world["library"]["batches"]:
            for key, work in batch["works"].items():
                version = work["versions"]["1"]
                value = json.loads(next(p["text"] for p in version["passages"] if p["passage_id"] == "results"))
                if value["role"] == "calibration":
                    parents = [p["source_key"] for p in value.get("parent_calibrations", [])]
                else:
                    parents = [value["calibration_doi"]] if value.get("calibration_doi") else []
                add(key, parents, None,
                    work["acquisition_round"], value["role"])
    else:
        raise ValueError("Unknown structural domain")
    graph_metrics(nodes)
    return nodes


def instance_identity(identifier, public):
    # Human-readable structured identity, not another artifact hash protocol.
    return {"environment_id": identifier, "environment_format": public["format"],
            "task_id": public["task_id"], "scenario": public.get("scenario"),
            "dependency_construction": public.get("dependency_construction"),
            "runtime_identity": public.get("runtime_identity")}


SKILL_TOOLS = {"begin_skill", "select_skill_run", "finish_skill", "read_skill_run", "read_skill_file"}


def trace_profile(events):
    counts = Counter(event["name"] for event in events)
    runs, active, switches = {}, None, 0
    successful_scripts, attributed, unowned, errors = 0, 0, 0, 0
    for event in events:
        name, args, result = event["name"], event["arguments"], event["output"]
        success = result.get("status") == "success"
        errors += not success
        if name not in SKILL_TOOLS | {"read_task", "run_skill_script"}:
            attributed += active is not None
            unowned += active is None
        if not success:
            continue
        if name == "begin_skill":
            active = result["data"]["run_id"]
            runs[active] = {"skill_id": args["skill_id"], "claimed_complete": False}
        elif name == "select_skill_run":
            switches += active != args["run_id"]
            active = args["run_id"]
        elif name == "finish_skill":
            if args["run_id"] in runs:
                runs[args["run_id"]]["claimed_complete"] = True
            if active == args["run_id"]:
                active = None
        elif name == "run_skill_script":
            successful_scripts += 1
    return {"reference_calls": len(events), "tool_counts": dict(sorted(counts.items())),
        "skill_bookkeeping_and_read_calls": sum(counts[n] for n in SKILL_TOOLS),
        "script_calls": counts["run_skill_script"], "successful_script_calls": successful_scripts,
        "primitive_business_calls": attributed + unowned, "attributed_primitive_calls": attributed,
        "unattributed_primitive_calls": unowned, "observed_runs": len(runs),
        "claimed_complete_runs": sum(r["claimed_complete"] for r in runs.values()),
        "distinct_selected_skills": len({r["skill_id"] for r in runs.values()}),
        "existing_run_switches": switches, "tool_errors": errors,
        "minimum_horizon_or_skill_correctness": "not_established"}
