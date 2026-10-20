"""Persistent flow reservations and content-dependent service commissioning."""


def inlet_from_records(p, stage, identifiers):
    records = [p["artifacts"][key]["data"] for key in identifiers]
    inlet = min(d["return_pressure"] for d in records) if records else p["network"]["supply_pressure"]
    remote_stages = p["services"][str(stage)].get("remote_feed_stages", [stage-p["service_rules"]["remote_feed_gap"]])
    return inlet - sum(d["flow_units"]//2 for d in records if d["stage"] in remote_stages)


def plan_inputs(r, a):
    p = r.project
    stages = {p["devices"][key]["stage"] for key in a["device_ids"]}
    r.require(len(stages) == 1, "SERVICE_STAGE", "Isolate exactly one service boundary")
    stage = next(iter(stages))
    parents = p["services"][str(stage)]["parent_stages"]
    expected = {p["services"][str(s)]["current_id"] for s in parents}
    records = [r.artifact(key, "service_commission") for key in a["upstream_service_ids"]]
    r.require(len(records) == len(parents) and set(a["upstream_service_ids"]) == expected and
              all(p["services"][str(s)]["active"] for s in parents),
              "SERVICE_DEPENDENCY", "Use every exact current active upstream service record")
    r.require(all(v["data"]["supply_revision"] == p["network"]["revision"] for v in records),
              "STALE_ARTIFACT", "Upstream services must be recertified after a source change")
    r.require(all(p["windows"][a["window_id"]]["start"] >= v["data"]["effective_from"] for v in records),
              "MAINTENANCE_WINDOW", "Maintenance must follow the upstream service configuration")
    return inlet_from_records(p, stage, a["upstream_service_ids"])


def descendants(p, stage):
    affected, pending = set(), [stage]
    while pending:
        source = pending.pop()
        for key, service in p["services"].items():
            if source in service["parent_stages"] and service["stage"] not in affected:
                affected.add(service["stage"])
                pending.append(service["stage"])
    return sorted(affected)


def suspend_descendants(p, stage):
    affected = descendants(p, stage)
    invalidated = [s for s in affected if p["services"][str(s)]["active"] or
                   any(p["devices"][key]["status"] == "restored" for key in p["services"][str(s)]["device_ids"])]
    for s in affected:
        service = p["services"][str(s)]
        service["active"] = False
        for key in service["device_ids"]:
            if p["devices"][key]["status"] == "restored":
                p["devices"][key]["status"] = "needs_revalidation"
    p["acceptance"] = None
    return invalidated


def commission(r, a):
    p = r.project
    r.require(str(a["stage"]) in p["services"], "SERVICE_STAGE", "Unknown service stage")
    service = p["services"][str(a["stage"])]
    records = [r.artifact(key, "restoration") for key in a["restoration_ids"]]
    covered = [d for record in records for d in record["data"]["device_ids"]]
    r.require(sorted(covered) == sorted(service["device_ids"]) and all(
        p["devices"][d]["status"] == "restored" and p["devices"][d].get("restoration_id") == record["artifact_id"]
        for record in records for d in record["data"]["device_ids"]), "SERVICE_RESTORATIONS", "Bind exact current restored devices")
    from .fieldops import _passing_current_test
    r.require(all(_passing_current_test(r, r.artifact(p["devices"][d]["test_id"])) for d in covered),
              "SERVICE_RESTORATIONS", "Service commissioning requires passing current measurements")
    r.require(service["minimum_flow"] <= a["flow_units"] <= service["maximum_flow"],
              "SERVICE_FLOW", "Flow must satisfy this service's public operating range")
    reserved = sum(s["flow_units"] for s in p["services"].values()) - service["flow_units"]
    r.require(reserved + a["flow_units"] <= p["service_rules"]["capacity_units"],
              "FLOW_CAPACITY", "Other service reservations, including suspended ones, still consume shared capacity")
    upstream = [p["services"][str(s)]["current_id"] for s in service["parent_stages"]]
    r.require(all(p["services"][str(s)]["active"] for s in service["parent_stages"]),
              "SERVICE_DEPENDENCY", "Commission upstream services first")
    pressure = min(v["data"]["delivered_pressure"] for v in records) - a["flow_units"]
    r.require(pressure > 0, "SERVICE_PRESSURE", "A service must deliver positive return pressure")
    old = service["current_id"]
    changed = bool(old and (service["flow_units"] != a["flow_units"] or
                           p["artifacts"][old]["data"]["return_pressure"] != pressure))
    r.require(not service["active"] or changed, "SERVICE_UNCHANGED", "The current service already has this operating point")
    if changed:
        affected_stages = descendants(p, a["stage"])
        r.require(not any(i["open"] and any(p["devices"][key]["stage"] in affected_stages for key in i["device_ids"])
                          for i in p["isolations"].values()), "RESOURCE_CONFLICT",
                  "Do not change flow while descendant maintenance is in progress")
    affected = suspend_descendants(p, a["stage"]) if changed else []
    effective = max(v["data"]["window_end"] for v in records)
    if changed:
        effective = max(effective, max(v["end"] for v in p["isolations"].values()))
    output = r.emit("service_commission", {**a, "upstream_service_ids": upstream,
        "return_pressure": pressure, "supply_revision": p["network"]["revision"],
        "effective_from": effective, "supersedes": old, "invalidated_service_stages": affected},
        [*a["restoration_ids"], *upstream, *([old] if old else [])])
    service.update(current_id=output["artifact_id"], active=True, flow_units=a["flow_units"])
    if affected:
        p["notices"].append({"type": "service_operating_point_changed", "changed_service_stage": a["stage"],
            "affected_service_stages": affected, "state_changed": True})
    return output


def verify(p):
    """Check historical arithmetic and the active service frontier independently."""
    total, history = 0, {}
    for a in p["artifacts"].values():
        if a["kind"] != "service_commission":
            continue
        d = a["data"]
        service = p["services"][str(d["stage"])]
        restores = [p["artifacts"][key]["data"] for key in d["restoration_ids"]]
        upstream = [p["artifacts"][key]["data"] for key in d["upstream_service_ids"]]
        if (sorted(x for rec in restores for x in rec["device_ids"]) != sorted(service["device_ids"]) or
                sorted(v["stage"] for v in upstream) != service["parent_stages"] or
                not service["minimum_flow"] <= d["flow_units"] <= service["maximum_flow"] or
                d["effective_from"] < max(v["window_end"] for v in restores) or
                d["return_pressure"] != min(v["delivered_pressure"] for v in restores)-d["flow_units"] or
                any(v["supply_revision"] != d["supply_revision"] for v in [*restores, *upstream])):
            return False
        if d["supersedes"] != history.get(d["stage"], {}).get("id"):
            return False
        history[d["stage"]] = {"id": a["artifact_id"], "flow": d["flow_units"]}
        if sum(v["flow"] for v in history.values()) > p["service_rules"]["capacity_units"]:
            return False
    for service in p["services"].values():
        if not service["active"] or not service["current_id"]:
            return False
        d = p["artifacts"][service["current_id"]]["data"]
        if (d["flow_units"] != service["flow_units"] or d["supply_revision"] != p["network"]["revision"] or
                set(d["restoration_ids"]) != {p["devices"][key]["restoration_id"] for key in service["device_ids"]} or
                set(d["upstream_service_ids"]) != {p["services"][str(s)]["current_id"] for s in service["parent_stages"]}):
            return False
        total += service["flow_units"]
    return total <= p["service_rules"]["capacity_units"]
