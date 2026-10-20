"""Explicit airport-to-airport custody and shared freight capacity."""

import argparse
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from skillhorizon.construction_provenance import construction_entry, observe_call

from integrations.internal_mcp.io import read_json, write_json
from .network_build import build as build_network
from integrations.internal_mcp.topology import disclose_runtime_identity


FORMAT = "skillhorizon.c09.freight_network.v2"
FAMILIES = ("standard", "handling_restricted", "carrier_disruption", "specialist_disruption")


@construction_entry
def build(source, output, *, seed=9, cohorts=3, legs=4, background=True, custody_span=3, scenario="standard", dependency_pattern="staggered", runtime_identity="legacy"):
    disclose_runtime_identity({}, runtime_identity)
    if type(legs) is not int or not 3 <= legs <= 8:
        raise ValueError("Freight custody networks require 3..8 legs")
    if scenario not in FAMILIES:
        raise ValueError("Unknown freight scenario")
    if type(custody_span) is not int or not 1 <= custody_span <= 7:
        raise ValueError("custody_span must be 1..7")
    output = Path(output)
    public = observe_call(build_network, source, output, seed=seed, cohorts=cohorts, legs=legs, background=background, dependency_pattern=dependency_pattern)
    disclose_runtime_identity(public, runtime_identity)
    world = read_json(output / "checkpoint/world.json")
    if custody_span != 3:
        owner = max(public["requests"], key=lambda r: (r["final_deadline"], r["passenger_id"]))
        bag = next(o for o in public["transfer_network"]["baggage_obligations"]
                   if o["passenger_id"] == owner["passenger_id"] and o["origin_leg"] == 1)
        bag["delivery_leg"] = min(legs, 1 + custody_span)
    if scenario == "specialist_disruption" and not any(
            o["origin_leg"] == 1 and o["delivery_leg"] > 2
            for o in public["transfer_network"]["baggage_obligations"]):
        # The combined scenario explicitly requires an actual first-hub carrier
        # obligation. A random topology may otherwise make every first bag local.
        owner = max(public["requests"], key=lambda r: (r["final_deadline"], r["passenger_id"]))
        bag = next(o for o in public["transfer_network"]["baggage_obligations"]
                   if o["passenger_id"] == owner["passenger_id"] and o["origin_leg"] == 1)
        bag["delivery_leg"] = min(legs, max(3, 1 + custody_span))
    services = []
    total_weight = sum(b["weight_kg"] for b in world["tables"]["baggage"] if b.get("task_id") == public["task_id"])
    for hub in range(1, legs - 1):
        route = public["route"][hub]
        flight = next(f for f in world["tables"]["flight"] if f["flight_id"] == route["alternative_flight_ids"][0])
        departure = datetime.fromisoformat(flight["scheduled_departure"].replace("Z", "+00:00")) - timedelta(minutes=30)
        arrival = datetime.fromisoformat(flight["scheduled_arrival"].replace("Z", "+00:00")) - timedelta(minutes=15)
        for name, capacity, delay in (("priority", 48, 0), ("consolidated", max(160, total_weight), 50)):
            services.append({"service_id": f"freight-{hub}-{name}", "from_hub": hub, "to_hub": hub + 1,
                "departure_airport": flight["departure_airport"], "arrival_airport": flight["arrival_airport"],
                "departure_time": (departure + timedelta(minutes=delay)).isoformat(),
                "arrival_time": (arrival + timedelta(minutes=delay)).isoformat(), "capacity_kg": capacity,
                "handover_minutes": 10})
    public["format"] = world["format"] = FORMAT
    public["goal"] += " Through-baggage must physically reach each intervening airport on actual capacity-limited freight plans before its destination ground transfer."
    public["transfer_network"]["freight_services"] = services
    if scenario in ("handling_restricted", "specialist_disruption"):
        obligations = public["transfer_network"]["baggage_obligations"]
        transported = sorted((o for o in obligations if o["delivery_leg"] > o["origin_leg"] + 1),
                             key=lambda o: (o["origin_leg"], o["obligation_id"]))
        classes = ("fragile", "temperature_controlled", "standard")
        for obligation in obligations:
            obligation["required_handling"] = "standard"
        for index, obligation in enumerate(transported):
            obligation["required_handling"] = classes[(index + seed) % len(classes)]
        if scenario == "specialist_disruption":
            affected = next((o for o in transported if o["origin_leg"] == 1 and o["delivery_leg"] > 2), None)
            if affected is None:
                raise ValueError("Specialist disruption needs actual first-hub through-baggage")
            affected["required_handling"] = "fragile"
        weights = {b["baggage_id"]: b["weight_kg"] for b in world["tables"]["baggage"]}
        for service in services:
            priority = service["service_id"].endswith("priority")
            service["handling_capabilities"] = ["standard", "fragile" if priority else "temperature_controlled"]
            if priority:
                dedicated = sum(weights[o["bag_id"]] for o in obligations
                    if o["origin_leg"] <= service["from_hub"] and o["delivery_leg"] > service["to_hub"]
                    and o["required_handling"] == "fragile")
                service["capacity_kg"] = max(8, dedicated)
    if scenario != "standard":
        previous_task_id = public["task_id"]
        public["task_id"] += "-" + scenario.replace("_", "-")
        def retag(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "task_id" and item == previous_task_id:
                        value[key] = public["task_id"]
                    else:
                        retag(item)
            elif isinstance(value, list):
                for item in value:
                    retag(item)
        retag(world)
    if scenario in ("handling_restricted", "specialist_disruption"):
        public["scenario"] = {"family": scenario, "seed": seed,
            "mechanisms": ["handling_eligibility", "shared_capacity", "multi_origin_custody"],
            "scope": "construction_development"}
        public["goal"] += " Use freight services certified for each bag's public handling requirement; reserve scarce specialist capacity across all passenger groups."
        public["transfer_network"]["handling_policy"] = (
            "A service must list the required_handling of every carried obligation in handling_capabilities. "
            "Standard bags may use either service, but fragile and temperature-controlled bags are not interchangeable. "
            "Capacity is shared by all active manifests; no allocation or repair is made automatically.")
    if scenario in ("carrier_disruption", "specialist_disruption"):
        replacement = deepcopy(services[0])
        replacement.update(service_id="freight-1-contingency", capacity_kg=max(160, total_weight), availability="standby")
        for field in ("departure_time", "arrival_time"):
            replacement[field] = (datetime.fromisoformat(replacement[field]) + timedelta(minutes=10)).isoformat()
        services.append(replacement)
        public["scenario"] = {"family": scenario, "seed": seed,
            "mechanisms": ["carrier_withdrawal", "selective_custody_repair", "unaffected_branch_reuse"],
            "scope": "construction_development"}
        public["transfer_network"]["service_disruption"] = {
            "event_id": "carrier-operations-bulletin", "trigger_ground_leg": legs,
            "withdrawn_service_id": "freight-1-priority", "activated_service_id": replacement["service_id"],
            "policy": "After the first final-leg ground transfer is planned, the priority carrier withdraws its first-hub service and a standby service opens. "
                      "All plans are predeparture reservations. Retain their history; withdrawn services invalidate downstream custody. "
                      "No plan is cancelled or repaired automatically. Inspect the current network and explicitly revise affected plans."}
        world["transfer_network"]["service_events"] = []
        public["goal"] += " Incorporate carrier operations bulletins into the final predeparture plan; preserve valid unaffected arrangements."
    if scenario == "specialist_disruption":
        public["scenario"].update(composition_of=["handling_restricted", "carrier_disruption"],
            mechanisms=["handling_eligibility", "shared_capacity", "multi_origin_custody",
                        "carrier_withdrawal", "selective_custody_repair", "unaffected_branch_reuse"])
    if scenario == "standard":
        public["scenario"] = {"family": scenario, "seed": seed,
            "mechanisms": ["multi_origin_custody", "shared_capacity", "cross_cohort_transfers"],
            "scope": "construction_development"}
    public["scenario"]["world_size"] = {"cohorts": cohorts, "legs": legs, "custody_span": custody_span}
    public["transfer_network"]["freight_policy"] = (
        "A release after leg k is at hub k. Ground boarding for leg j takes place at hub j-1. "
        "Future-due bags must move one hub at a time. Each freight plan supplies exact release identities and actual prior-plan identities; "
        "bags at their release hub need no prior plan. Shared service capacity is consumed by all active plans across passenger groups. "
        "No duplicate movement of a bag on the same hub edge. Cancelled plans remain history and make downstream custody stale.")
    skill = "forward-through-baggage"
    path = output / "public/skills" / skill / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Forward through-baggage between airports\n\nRead actual source releases and freight plans. "
        "Identify each bag's current hub and delivery obligation; do not forward a bag already due at this hub. "
        "Join old incoming manifests with new local releases when appropriate. Choose a feasible public freight service, "
        "respect its shared remaining weight capacity and readiness time, and supply the actual preceding manifest IDs. "
        "A manifest can serve several later passenger groups. Preserve future-due bags and unaffected plans. "
        "If a plan is cancelled, cancel its dependent plans before rebuilding that custody closure; never repair records in place.\n", encoding="utf-8")
    if scenario in ("handling_restricted", "specialist_disruption"):
        path.write_text(path.read_text() + "\n## Handling-constrained allocation\n\nRead the handling requirement of each actual bag obligation and every service's capabilities. "
            "Consider all pending groups before allocating shared specialist capacity: a flexible standard bag must not strand a bag with only one eligible service. "
            "Join only compatible bags in each manifest and keep their actual custody predecessors. Capacity feasibility alone does not establish handling eligibility.\n", encoding="utf-8")
    if scenario in ("carrier_disruption", "specialist_disruption"):
        path.write_text(path.read_text() + "\n## Carrier changes\n\nConsult current service availability and operations bulletins. "
            "A historical manifest on a withdrawn service is not current custody. Trace its consumers across freight, ground transfer, clearance and release records. "
            "Cancel affected active plans explicitly, allocate eligible replacement capacity, and retain unaffected arrangements.\n", encoding="utf-8")
    public["skills"].append({"name": skill, "files": ["SKILL.md"]})
    path = output / "public/skills/schedule-cross-cohort-transfer/SKILL.md"
    path.write_text(path.read_text() + "\nAlso bind the actual freight manifests bringing older bags to this airport. "
        "Wait for their arrival and handover. An origin release alone does not prove that a bag is at a later airport.\n", encoding="utf-8")
    world["transfer_network"]["forwardings"] = {}
    private = read_json(output / "private/initial_obligations.json")
    private["public"] = deepcopy(public)
    for folder in ("checkpoint", "runtime"):
        write_json(output / folder / "world.json", world)
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", private)
    config = read_json(output / "mcp.json")
    config["mcpServers"]["c09-airline"]["args"][1] = "integrations.logisticsops.freight_server"
    write_json(output / "mcp.json", config)
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=9)
    parser.add_argument("--cohorts", type=int, default=3)
    parser.add_argument("--legs", type=int, default=4)
    parser.add_argument("--custody-span", type=int, default=3)
    parser.add_argument("--scenario", choices=FAMILIES, default="standard")
    parser.add_argument("--dependency-pattern", choices=("staggered", "distributed"), default="staggered")
    parser.add_argument("--runtime-identity", choices=("legacy", "task_local_v1"), default="legacy")
    print(build(**vars(parser.parse_args()))["task_id"])
