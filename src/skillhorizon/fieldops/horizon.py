"""Opt-in domain-specific long-horizon construction, never model-side planning."""

PROGRAMS = {"pressure_network_v1": "fieldops", "staged_contracts_v1": "procureops",
            "pressure_services_v2": "fieldops", "assembly_contracts_v2": "procureops",
            "pressure_branches_v3": "fieldops", "assembly_network_v3": "procureops"}


def configure(project, groups, rng, program, revision):
    if program is None:
        return
    from .field_layout import SERVICE_LIMITS
    expanded = project.get("service_scale") in SERVICE_LIMITS and project["domain"] == "fieldops"
    if program in {"pressure_services_v2", "assembly_contracts_v2", "pressure_branches_v3", "assembly_network_v3"}:
        if program in {"pressure_branches_v3", "assembly_network_v3"} and groups < (5 if expanded else 8):
            raise ValueError("Branch or multi-origin program has too few groups")
        base = "pressure_network_v1" if program in {"pressure_services_v2", "pressure_branches_v3"} else "staged_contracts_v1"
        configure(project, groups, rng, base, revision)
        from .dependency_programs import configure_dependencies
        configure_dependencies(project, groups, rng, program)
        return
    if program not in PROGRAMS or PROGRAMS[program] != project["domain"] or groups < (5 if expanded else 6):
        raise ValueError("Use a domain-matching horizon program with sufficient groups")
    if project["composition"] != "standard" or project["resource_profile"] != "relaxed":
        raise ValueError("Horizon programs define their own resources; do not combine legacy composition/resource overrides")
    if program == "staged_contracts_v1" and project["family"] != "split_fulfillment":
        raise ValueError("Staged contracts require split_fulfillment")
    p = project
    p["business_program"] = program
    p["project_id"] += "-" + program
    if program == "pressure_network_v1":
        if p["family"] != "staged_maintenance":
            raise ValueError("Pressure networks require staged_maintenance")
        p["network"] = {"supply_pressure": 120, "revision": 1, "supply_history": {"1": 120},
                        "effective_from": 0, "effective_history": {"1": 0},
                        "measurement": "upstream_min_minus_local_residual"}
        p["minimum_upstream_pressure"] = 0
        from .field_layout import stage_layout
        layout = stage_layout(p.get("device_counts", [2] * groups))
        for group in range(groups):
            severities = {"low": rng.randrange(6, 10), "high": rng.randrange(10, 13)}
            row = layout[str(group + 1)]
            for key, role in zip(row["device_ids"], row["severity_roles"]):
                p["devices"][key].update(severity=severities[role],
                    rated_pressure=200, minimum_pressure=108 - 2 * group,
                    predecessors=list(layout[str(group)]["device_ids"]) if group else [])
        for i in range(groups):
            p["windows"][f"RW{i+1}"] = {"start": groups * 8 + i * 8, "end": groups * 8 + i * 8 + 6}
        spares = sum(role == "high" for row in layout.values() for role in row["severity_roles"])
        p["parts"]["spare-A"].update(quantity=spares, initial_quantity=spares)
        p["parts"]["spare-B"].update(quantity=0, initial_quantity=0)
        p["scheduled_change"] = {"kind": "supply_pressure", "after_restorations": 4, "pressure": 112, "applied": False} if revision else None
        p["goal"] += (" The network is a synthetic pressure-loss model: each stage's inlet is the minimum of its current "
                      "upstream restoration pressures, or the supply pressure at the root. Local residual is subtracted "
                      "from that inlet, capped by device rated pressure. A supply update invalidates network certifications, "
                      "not physical repairs or spent spares. Re-isolate affected devices in later available windows; "
                      "a window cannot precede the current supply update's effective_from time. "
                      "retest-device can reuse an unchanged physical repair without consuming another spare. "
                      "Use exact current upstream restoration records; historical records remain available.")
    else:
        for stage, request in enumerate(p["requests"].values(), 1):
            request.update(stage=stage, eligible_suppliers=["SUP-A", "SUP-B", "SUP-C"], earliest_slot=(stage-1)*8+1, delivery_deadline=stage*8+8)
        total = sum(r["quantity"] for r in p["requests"].values())
        p["suppliers"] = {key: {"unit_price_cents": price, "capacity": total, "revision": 1,
                                "per_slot_capacity": 3, "earliest_slot": 1}
                          for key, price in (("SUP-A", 100), ("SUP-B", 110), ("SUP-C", 125))}
        p["total_budget_cents"] = total * 130
        p["contract"] = {"stages": groups, "opening_cash_cents": 650, "immediate_cents_per_unit": 100,
                         "retention_cents_per_unit": 25, "retention_delay_stages": 3}
        p["settlements"] = {}
        p["scheduled_change"] = {"kind": "stage_supply", "stage": 4, "target": "SUP-A", "applied": False} if revision else None
        p["goal"] += (" Requests are sequential contract stages. Begin a stage only after the preceding delivery settlement. "
                      "Stage purchase plans explicitly bind the previous settlement and any matured retention records. "
                      "Cumulative spending caps are not liquid cash. A settled stage reimburses the public immediate "
                      "amount per delivered unit and creates a separate retention claim maturing after three stages. "
                      "Use each claim in at most one stage; future claims are not cash. Reconcile exact stage receipts "
                      "with settle-contract before funding the next stage. Public supply updates may affect placed "
                      "but undelivered orders; cancel and replace them without erasing delivered goods. "
                      "Settle all retention claims matured within the horizon; leave future claims recorded as outstanding. "
                      "Final acceptance requires every stage settlement, not only receipts.")
