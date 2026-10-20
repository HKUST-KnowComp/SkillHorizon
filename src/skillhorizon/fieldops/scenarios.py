"""Construction-time scenario variants of the two fixed Operations libraries."""

SCENARIOS = {
    "fieldops": ("mesh_convergence", "tight_windows", "repeated_supply_shocks", "mesh_supply_shocks"),
    "procureops": ("irregular_bom", "quality_coupling", "repeated_supply_shocks", "quality_reuse"),
}


def validate(scenario, domain, program, revision, groups, service_scale=None):
    if scenario is None:
        return
    expected = "pressure_branches_v3" if domain == "fieldops" else "assembly_network_v3"
    if scenario not in SCENARIOS.get(domain, ()) or program != expected or revision:
        raise ValueError("Use a domain-matching V3 named scenario without a revision override")
    minimum = 5 if service_scale and domain == "fieldops" else 8
    if groups < minimum:
        raise ValueError(f"Named Operations scenario requires at least {minimum} stages")


def configure(p, groups, rng, scenario):
    if scenario is None:
        return
    components = {"mesh_supply_shocks": ("mesh_convergence", "repeated_supply_shocks"),
                  "quality_reuse": ("irregular_bom", "quality_coupling")}
    if scenario in components:
        project_id = p["project_id"]
        for component in components[scenario]:
            configure(p, groups, rng, component)
        p["project_id"] = project_id + "-" + scenario
        p["business_scenario"] = scenario
        return
    p["business_scenario"] = scenario
    p["project_id"] += "-" + scenario
    if p["domain"] == "fieldops":
        if scenario == "mesh_convergence":
            from .field_layout import PARALLEL_SERVICE_SCALE, REGIONAL_SERVICE_SCALE, HIGHBAND_SERVICE_SCALE
            parallel = p.get("service_scale") == PARALLEL_SERVICE_SCALE
            regional = p.get("service_scale") in {REGIONAL_SERVICE_SCALE, HIGHBAND_SERVICE_SCALE}
            split = (groups + 1) // 2
            p["service_rules"]["topology"] = "parallel_braided_subnetworks" if parallel else "three_feeders_braided_join"
            if parallel:
                p["service_rules"]["subnetworks"] = [list(range(1, split + 1)), list(range(split + 1, groups + 1))]
            if regional:
                # Separate feeder regions bound physical pressure loss while
                # adding complete commissioning and certification obligations.
                count = (groups + 15) // 16
                size, remainder = divmod(groups, count)
                regions, start = [], 1
                for index in range(count):
                    end = start + size + (index < remainder)
                    regions.append(list(range(start, end)))
                    start = end
                p["service_rules"].update(topology="regional_braided_subnetworks",
                                          subnetworks=regions)
            for stage, service in enumerate(p["services"].values(), 1):
                start, end = ((1, split) if stage <= split else (split + 1, groups)) if parallel else (1, groups)
                if regional:
                    region = next(r for r in regions if stage in r)
                    start, end = region[0], region[-1]
                local_stage = stage - start + 1
                parents = [] if local_stage <= 3 else [stage-3]
                if local_stage >= 6:
                    parents = sorted(set(parents + [stage-2, stage-5]))
                if stage == end:
                    parents = sorted(set(parents + [stage-1, start]))
                service["parent_stages"] = parents
                service["remote_feed_stages"] = [parent for parent in parents if stage-parent >= 3]
                for key in service["device_ids"]:
                    p["devices"][key].update(predecessors=[device for parent in parents
                        for device in p["services"][str(parent)]["device_ids"]],
                                              earliest_start=((local_stage-1)//3)*8)
            returns = {}
            for service in p["services"].values():
                stage = service["stage"]
                inlet = min(returns[s] for s in service["parent_stages"]) if service["parent_stages"] else 112
                inlet -= sum(p["services"][str(remote)]["minimum_flow"]//2 for remote in service["remote_feed_stages"])
                returns[stage] = inlet-2-service["minimum_flow"]
                for key in service["device_ids"]:
                    p["devices"][key]["minimum_pressure"] = inlet-2-p["service_rules"]["pressure_margin"]
            p["goal"] = p["goal"].split(" This project has two root feeders")[0] + (
                " This project has three root feeders and braided multi-parent services. "
                "Use the public parent_stages of each service, not an inferred fixed stage offset. "
                "The terminal service joins recent branches and an old root. Any feasible topological order is legal.")
            p["service_rules"]["pressure_rule"] = "minimum upstream return pressure minus sum(floor(feed flow/2)) over remote_feed_stages"
            p["goal"] = p["goal"].replace(
                "minus floor(remote feed flow/2) when the declared remote feed exists",
                "minus the sum of floor(feed flow/2) for every service in remote_feed_stages").replace(
                "Where a declared remote feed exists, the remote feed contributes an additional loss of floor(remote flow/2). ", "")
            p["goal"] += " The per-service remote_feed_stages list replaces the fixed remote_feed_gap rule."
            if parallel or regional:
                p["goal"] = p["goal"].replace(
                    "This project has three root feeders and braided multi-parent services.",
                    (f"This regional commissioning project has {len(regions)} feeder subnetworks, each with three root feeders and braided multi-parent services."
                     if regional else "This project has two parallel subnetworks, each with three root feeders and braided multi-parent services.")).replace(
                    "The terminal service joins recent branches and an old root.",
                    "Each subnetwork's terminal service joins its recent branches and an old root.")
                scope = "All" if regional else "Both"
                p["goal"] += (f" {scope} subnetworks share the published supply, flow capacity, crews, windows and spare stock. "
                              f"Supply corrections affect {scope.lower()} subnetworks. Accept the complete system only after "
                              f"every service in {scope.lower()} subnetworks is active with current evidence.")
        elif scenario == "tight_windows":
            p["crews"]["crew-B"]["qualified"] = False
            for device in p["devices"].values():
                device["duration"] = 3
            for index, window in enumerate(p["windows"].values()):
                window["end"] = window["start"] + (2 if index % 3 == 0 else 4)
            p["goal"] += (
                " Some published windows are too short for a repair plus its test. "
                "Check actual duration and crew qualification; a listed window is not a feasibility guarantee.")
        else:
            p["scheduled_changes"] = [
                {"kind": "supply_pressure", "after_restorations": 3, "pressure": 116, "applied": False},
                {"kind": "supply_pressure", "after_restorations": 8, "pressure": 112, "applied": False},
            ]
            for i in range(groups*2):
                p["windows"][f"TW{i+1}"] = {"start": groups*24+i*8, "end": groups*24+i*8+6}
            p["goal"] += (
                " More than one supply correction may occur at restoration milestones. "
                "A recovered certification can become stale again; physical repair and consumed stock persist.")
    else:
        if scenario == "irregular_bom":
            transfers = []
            for origin in range(1, groups):
                candidates = list(range(origin+1, groups+1))
                rng.shuffle(candidates)
                for index, consumer in enumerate(sorted(candidates[:2])):
                    transfers.append({"origin_stage": origin, "consumer_stage": consumer,
                                      "quantity": 2 if index == 0 else 1})
            p["assembly_rules"]["material_transfers"] = transfers
            p["goal"] = p["goal"].split(" Assembly has multiple material reuse obligations:")[0] + (
                " Each public material_transfers row reserves an exact quantity from origin_stage for consumer_stage. "
                "Assemble the unreserved current quantity plus all transfers maturing at the current stage. "
                "These irregular origin/consumer relationships override the legacy delay list. "
                "Bind exact receipt lots and conditioning evidence; every physical unit is consumed once. "
                "Settlement requires the resulting kit. Mature retention claims remain separate and can be "
                "collected with claim-retention without creating purchases.")
        elif scenario == "quality_coupling":
            p["suppliers"]["SUP-A"]["component_grade"] = 2
            p["suppliers"]["SUP-B"]["component_grade"] = 3
            p["contract"]["opening_cash_cents"] = 800
            p["goal"] += (
                " The least expensive supplier provides low-grade material. "
                "Its receipts can require paid processing many stages later; buying a higher grade is a legal alternative.")
        else:
            p["scheduled_changes"] = [
                {"kind": "stage_supply", "stage": 4, "target": "SUP-A", "applied": False},
                {"kind": "stage_supply", "stage": 6, "target": "SUP-B", "applied": False},
            ]
            p["contract"]["opening_cash_cents"] = 1000
            p["goal"] += (
                " Multiple suppliers may lose undelivered capacity at separate ordering milestones. "
                "A backup used after one outage can fail later. Preserve received lots and refund only canceled commitments.")
