"""Public business rules for service commissioning and cross-stage material use."""


def configure_dependencies(p, groups, rng, program):
    p["project_id"] = p["project_id"].rsplit("-", 1)[0] + "-" + program
    p["business_program"] = program
    if program in {"pressure_services_v2", "pressure_branches_v3"}:
        p["network"]["measurement"] = "service_return_pressure_with_remote_feed_loss"
        p["services"] = {}
        demands = [rng.randrange(2, 5) for _ in range(groups)]
        p["service_rules"] = {"capacity_units": sum(demands) + 2, "remote_feed_gap": 3,
            "design_supply_pressure": 112, "pressure_margin": 3,
            "pressure_rule": "minimum upstream return pressure minus half the remote feed flow, rounded down"}
        if program == "pressure_branches_v3":
            p["service_rules"].update(remote_feed_gap=4, topology="two_feeders_with_remote_feeds_and_terminal_join")
        for i in range(groups):
            p["windows"][f"SW{i+1}"] = {"start": groups*16+i*8, "end": groups*16+i*8+6}
        for stage, demand in enumerate(demands, 1):
            parents = sorted({stage-1, stage-3} - {0}) if stage > 3 else ([stage-1] if stage > 1 else [])
            if program == "pressure_branches_v3":
                parents = [s for s in (stage-4, stage-2) if s > 0]
                if stage == groups:
                    parents = sorted({stage-1, stage-2, stage-4})
            layout = p["device_layout"]["stages"]
            devices = list(layout[str(stage)]["device_ids"])
            p["services"][str(stage)] = {"stage": stage, "device_ids": devices,
                "parent_stages": parents, "minimum_flow": demand, "maximum_flow": demand+2,
                "flow_units": 0, "current_id": None, "active": False}
            for key in devices:
                p["devices"][key].update(stage=stage,
                    predecessors=[device for parent in parents for device in layout[str(parent)]["device_ids"]])
                if program == "pressure_branches_v3":
                    p["devices"][key]["earliest_start"] = ((stage-1)//2)*8
        design_returns = {}
        for service in p["services"].values():
            stage = service["stage"]
            inlet = min(design_returns[s] for s in service["parent_stages"]) if service["parent_stages"] else p["service_rules"]["design_supply_pressure"]
            remote = stage-p["service_rules"]["remote_feed_gap"]
            if remote in service["parent_stages"]:
                inlet -= p["services"][str(remote)]["minimum_flow"]//2
            design_pressure = inlet-2
            design_returns[stage] = design_pressure-service["minimum_flow"]
            for key in service["device_ids"]:
                p["devices"][key]["minimum_pressure"] = design_pressure-p["service_rules"]["pressure_margin"]
        p["goal"] = (" Restore every device and commission every service, then accept the complete system. "
            "Respect exact isolation boundary closure, qualified crews, finite spares and maintenance windows. "
            "Repairs consume device.duration hours and tests consume one hour, even when failed; devices in one "
            "boundary may be worked in parallel. Reads do not advance business time. Initial root inlet is the "
            "current public supply pressure. Other stages use the minimum of their upstream service return "
            "pressures, minus floor(remote feed flow/2) when the declared remote feed exists. Local device "
            "test residual is max(0,severity-repair gain)+floor(test_load/25); measured pressure is "
            "min(rated pressure,inlet)-residual. Device pressure limits are set from the public design supply, "
            "minimum-flow operating points and pressure margin, not one uniform low threshold. "
            "Commission every repaired service with commission-service. Each service reserves a flow "
            "between its public minimum and maximum against one shared capacity. Its return pressure equals "
            "restored pressure minus reserved flow. Later isolation plans bind both current upstream restorations "
            "and current upstream service records. Beyond stage three, the remote feed contributes an additional "
            "loss of floor(remote flow/2). Recommissioning a changed flow invalidates descendant certifications "
            "and service records, not physical repairs; reserved flows persist until explicitly replaced. "
            "A source update may occur during execution and similarly suspends services without refunding their "
            "reservations. Re-isolate and use retest-device to reuse unchanged repairs; do not consume new spares "
            "merely to renew certifications. Maintenance must follow both source and upstream-service effective "
            "times. Do not change flow while descendant maintenance is open. Old artifacts remain readable but "
            "current commitments require current upstream records. Finish the final acceptance run.")
        if program == "pressure_branches_v3":
            p["goal"] = p["goal"].replace("Beyond stage three", "Where a declared remote feed exists").replace(
                "beyond stage three", "where a declared remote feed exists")
            p["goal"] += (
                " This project has two root feeders and separate odd/even service branches, not a mandatory "
                "total stage order. Consult each service's parent_stages: ordinary services use the prior "
                "same-branch service and, when present, the service four stages earlier. The terminal service "
                "joins both branches and its remote feed. Any topological schedule satisfying public windows "
                "and shared crew/flow constraints is legal. A changed branch must be recertified through its "
                "downstream join; unrelated sibling certifications remain usable.")
    else:
        p["assembly_rules"] = {"reuse_delay_stages": 3, "minimum_grade": 3,
            "conditioning_cost_cents_per_unit": 5, "conditioning_units_per_stage": 8}
        if program == "assembly_network_v3":
            p["assembly_rules"].pop("reuse_delay_stages")
            p["assembly_rules"]["reuse_delays_stages"] = [2, 5]
        p["kits"] = {}
        for key, supplier in p["suppliers"].items():
            supplier["component_grade"] = 2 if key == "SUP-B" else 3
        p["goal"] += (" Contract completion requires assemble-kit, not only receipt delivery. For a stage with a "
            "future consumer within the horizon, keep one unit of that stage's material for reuse_delay_stages "
            "later and assemble the rest now; terminal stages use all their current material. Stages after the "
            "reuse delay must also consume one unit from that exact older stage. Inputs identify receipt_id, "
            "request_id, supplier_id and quantity; units cannot be consumed twice. Low-grade components need "
            "condition-components before use, at the public per-unit cash cost and per-stage work limit. "
            "Both conditioning and assembly operate only in the current unsettled stage. Bind the completed "
            "kit_id at settlement; processing costs reduce closing cash and count toward the total project cap.")
        if program == "assembly_network_v3":
            p["goal"] = p["goal"].split(" Contract completion requires assemble-kit")[0] + (
                " Assembly has multiple material reuse obligations: for EACH delay in reuse_delays_stages, "
                "reserve one unit of the current stage's receipts if that consumer lies within the horizon. "
                "Consume all remaining current units now, plus one unit from each exact older stage whose "
                "reuse delay matures now. For delays 2 and 5, later kits therefore join three distinct origins. "
                "Every receipt unit can be consumed only once across all kits. Actual supplier grades determine "
                "which units need condition-components, constrained by stage work capacity, cash and total budget. "
                "Bind sufficient conditioning evidence and the completed kit_id at settlement. Processing fees "
                "are real consumed cash; abandoning a run does not refund them. Retention maturity remains "
                "separate from material maturity. claim-retention can collect distinct matured, uncredited "
                "claims before, during or after purchasing, and after the final settlement for closeout. "
                "It never creates a purchase or changes old settlements. After final settlement, only claims "
                "that matured within the project horizon can be collected; future claims remain outstanding. "
                "Purchase retention_ids must not repeat claims already collected explicitly.")
