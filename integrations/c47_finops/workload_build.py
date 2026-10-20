"""Cross-unit workload batches with future delivery reservations and real cloud costs."""

import argparse
import random
from pathlib import Path
from skillhorizon.construction_provenance import observe_call

from integrations.internal_mcp.io import read_json, write_json
from .build import build as base_build
from integrations.internal_mcp.topology import parent_positions, disclose_pattern


FORMAT = "skillhorizon.c47.workload_network.v1"


def build(source, output, *, seed=47, units=3, periods=4, resources_per_unit=2, background=True, dependency_pattern="staggered"):
    if dependency_pattern not in ("staggered", "distributed"):
        raise ValueError("Workload topology requires its domain-specific delivery construction")
    parent_positions(0, 0, units, seed, dependency_pattern)
    output = Path(output)
    public = observe_call(base_build, source, output, seed=seed, units=units, periods=periods, resources_per_unit=resources_per_unit, background=background, workload_network=True)
    world = read_json(output / "checkpoint/world.json")
    compute = [r for r in world["finops"]["resources"].values() if r["service_category"] == "compute"]
    jobs = []
    for stage, period in enumerate(public["periods"]):
        for index, resource in enumerate(compute):
            jobs.append({"job_id": f"job-{stage}-{index}", "period_id": period["period_id"], "stage": stage + 1,
                "resource_id": resource["resource_id"], "local_records": 180 + 20 * ((index + seed) % 4), "consumers": []})
    by_key = {(j["stage"], j["resource_id"]): j for j in jobs}
    for job in jobs:
        index = next(i for i, r in enumerate(compute) if r["resource_id"] == job["resource_id"])
        lane, member = divmod(index, resources_per_unit)
        ages = list(range(1, min(3, periods - job["stage"]) + 1))
        rng = random.Random(f"workload-distributed-v1:{seed}:{job['job_id']}")
        if dependency_pattern == "distributed" and ages:
            later = list(range(2, periods - job["stage"] + 1))
            ages = [1, *sorted(rng.sample(later, min(2, len(later))))]
        for age in ages:
            target_lane = (lane + 1 + (age - 1) % (units - 1)) % units
            target_index = target_lane * resources_per_unit + (member + age) % resources_per_unit
            if dependency_pattern == "distributed":
                target_lane = rng.choice([i for i in range(units) if i != lane])
                target_index = target_lane * resources_per_unit + rng.randrange(resources_per_unit)
            consumer = by_key[(job["stage"] + age, compute[target_index]["resource_id"])]
            job["consumers"].append({"job_id": consumer["job_id"], "delivery_stage": consumer["stage"], "weight": age})
    public["format"] = world["format"] = FORMAT
    disclose_pattern(public, dependency_pattern, seed)
    public["workload_policy"] = {"jobs": jobs, "records_per_capacity": 100,
        "modes": {"raw": {"capacity_overhead": 0, "output_divisor": 1}, "compact": {"capacity_overhead": 1, "output_divisor": 2}},
        "rule": "Input records equal local records plus the exact reserved share from every producer naming this job. Capacity is ceil(input/100) plus the mode overhead. Output records are ceil(input/output_divisor). Split output among all future consumers using floor(output*weight/sum_weights), retaining the remainder. Compaction is synthetic lossless storage compaction; it costs processing capacity now and reduces future record-volume workload. Bind actual metered producer handoffs, not merely a previous monthly total."}
    public["price_catalog"].extend([{"size_id": "xlarge", "capacity": 12, "hourly_cents": 17},
        {"size_id": "xxlarge", "capacity": 24, "hourly_cents": 32}, {"size_id": "scale32", "capacity": 32, "hourly_cents": 40}])
    # Raw forwarding bounds record volume. Add the possible compaction overhead before pricing;
    # this fixed source-derived envelope covers both modes without assuming their observed choices.
    upper_allocations, price_bound = {}, {}
    for job in jobs:
        total = job["local_records"] + sum(rows.get(job["job_id"], 0) for rows in upper_allocations.values())
        required = -(-total // 100) + 1
        price_bound[(job["resource_id"], job["period_id"])] = min(p["hourly_cents"] for p in public["price_catalog"] if p["size_id"] != "storage" and p["capacity"] >= required)
        denominator = sum(c["weight"] for c in job["consumers"])
        upper_allocations[job["job_id"]] = {c["job_id"]: total * c["weight"] // denominator for c in job["consumers"]}
    envelope = sum(u["billable_hours"] * price_bound.get((u["resource_id"], u["period_id"]), 3) for u in world["finops"]["usage"].values())
    public["authorization_ceiling_cents"] = envelope + envelope // 8
    public["workload_policy"]["budget_construction"] = "Source-derived raw-volume upper bound plus one compaction capacity unit, priced at the cheapest sufficient declared machine, plus 12.5 percent headroom. Fixed before execution."
    shares, residue = divmod(public["authorization_ceiling_cents"], units)
    for index, budget in enumerate(world["tables"]["budget"]):
        budget["amount_usd"] = (shares + (residue if index == 0 else 0)) / 100
    public["goal"] += " Process the cross-unit workload network, preserve nonadjacent future reservations, and bind resulting demand to capacity assessments and metered resource bills."
    public["skills"].append({"name": "reconcile-workload-reservations", "files": ["SKILL.md"]})
    path = output / "public/skills/reconcile-workload-reservations/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Reconcile cross-unit workload reservations\n\nRead declared jobs, all actual producer handoffs and their reserved consumer shares, including older months. "
        "Join only this job's incoming records, choose raw or compact processing, derive required machine capacity and bind those inputs before assessment. "
        "After actual metering, publish the produced record volume and all future consumer reservations, preserving the unallocated remainder. "
        "A period close is financial evidence, not a replacement for producer job identity or outstanding data batches. Rebinding a planning input invalidates its old capacity assessments; closed metered output is immutable.\n", encoding="utf-8")
    world["finops"]["workload"] = {"next_id": 1, "bindings": {}, "current_bindings": {}, "handoffs": {}}
    for folder in ("checkpoint", "runtime"):
        write_json(output / folder / "world.json", world)
    write_json(output / "public/task.json", public)
    spec = read_json(output / "private/initial_obligations.json")
    spec.update(public=public, initial_tables=world["tables"])
    write_json(output / "private/initial_obligations.json", spec)
    config = read_json(output / "mcp.json")
    config["mcpServers"]["c47-finops"]["args"][1] = "integrations.c47_finops.workload_server"
    write_json(output / "mcp.json", config)
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=47)
    parser.add_argument("--units", type=int, default=3)
    parser.add_argument("--periods", type=int, default=4)
    parser.add_argument("--resources-per-unit", type=int, default=2)
    print(build(**vars(parser.parse_args()))["task_id"])
