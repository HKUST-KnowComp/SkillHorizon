"""Real passenger/cargo scaling, with business roles independent of identifiers."""

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import math
import random

from integrations.internal_mcp.io import read_json, write_json
from .declaration_build import build as build_declaration

REVISION = "logisticsops_portfolio150_v3"


def build(source, output, *, seed, cohorts, legs, scenario, dependency_pattern="distributed", background=False,
          cargo_profile="distributed", disruption_leg=None):
    if cargo_profile not in {"distributed", "through_origin"}:
        raise ValueError("Unknown cargo profile")
    public = build_declaration(source, output, seed=seed, cohorts=cohorts, legs=legs,
        custody_span=legs-1, scenario=scenario, dependency_pattern=dependency_pattern, background=background)
    output = Path(output)
    world = read_json(output / "checkpoint/world.json")
    private = read_json(output / "private/initial_obligations.json")
    rng = random.Random(f"logisticsops_portfolio150_v1:{seed}")
    people = public["requests"]
    net = public["transfer_network"]
    # Accessibility is a passenger requirement, not a cohort-index role.
    role_rng = random.Random(f"logisticsops_accessibility_v1:{seed}")
    own_services = [s for s in world["tables"]["special_service"] if s.get("task_id") == public["task_id"]]
    service_template = deepcopy(own_services[0])
    world["tables"]["special_service"] = [s for s in world["tables"]["special_service"] if s.get("task_id") != public["task_id"]]
    bookings = {b["booking_id"]: b for b in world["tables"]["booking"]}
    service_ids = []
    for person in people:
        person["required_ground_services"] = ["checked_baggage"]
        if role_rng.random() < 0.5:
            person["required_ground_services"].append("wheelchair")
            for booking_id in person["bookings"]:
                booking = bookings[booking_id]
                service_id = f"{booking_id}-wheelchair"
                service = {**deepcopy(service_template), "service_id": service_id,
                    "booking_id": booking_id, "passenger_id": person["passenger_id"],
                    "flight_id": booking["flight_id"], "booking_revision": booking["revision"]}
                world["tables"]["special_service"].append(service)
                service_ids.append(service_id)
    private["initial_ids"]["special_service"] = service_ids
    for group in net["groups"]:
        role_rng.shuffle(group["passenger_ids"])
    if disruption_leg is not None:
        if type(disruption_leg) is not int or not 3<=disruption_leg<=legs or "service_disruption" not in net:
            raise ValueError("Disruption leg must be an actual third or later ground stage")
        net["service_disruption"]["trigger_ground_leg"]=disruption_leg
        net["service_disruption"]["policy"]=net["service_disruption"]["policy"].replace("the first final-leg ground transfer",f"the first leg-{disruption_leg} ground transfer")
    if cargo_profile=="through_origin":
        # A through-ticket baggage contract sends all first-origin bags to the
        # final connection, adding real custody obligations rather than reads.
        for obligation in net["baggage_obligations"]:
            if obligation["origin_leg"]==1:obligation["delivery_leg"]=legs
    public["scenario"]["cargo_profile"]=cargo_profile
    # Keep shared-vehicle competition while allowing larger real passenger groups.
    for leg in range(2, legs+1):
        templates = [v for v in net["vehicles"] if v["leg"] == leg]
        for i in range(len(templates), max(3, math.ceil(cohorts/2))):
            vehicle = deepcopy(templates[1 + i % 2])
            vehicle.update(vehicle_id=f"extra-vehicle-{leg}-{i}", base_travel_minutes=rng.choice([22,25,28,30]))
            net["vehicles"].append(vehicle)
    # Vary tight-deadline demand and allocate the corresponding scarce final seats.
    final = public["route"][-1]
    flights = {f["flight_id"]:f for f in world["tables"]["flight"]}
    early = flights[final["alternative_flight_ids"][0]]
    late = flights[final["alternative_flight_ids"][1]]
    urgent = set(r["passenger_id"] for r in rng.sample(people, rng.randint(1, cohorts)))
    for r in people:
        base = early if r["passenger_id"] in urgent else flights[final["alternative_flight_ids"][-1]]
        r["final_deadline"] = base["scheduled_arrival"]
    for flight, capacity in [(early,len(urgent)),(late,len(people)-len(urgent))]:
        flight["total_seats"]["economy"] = flight["available_seats"]["economy"] = capacity
        seats = [s for s in world["tables"]["seat"] if s["flight_id"] == flight["flight_id"]]
        world["tables"]["seat"] = [s for s in world["tables"]["seat"] if s["flight_id"] != flight["flight_id"]]
        for i in range(capacity):
            s=deepcopy(seats[0]); s.update(seat_id=f"{flight['flight_id']}-s{i}",seat_number=f"{i+1}E")
            world["tables"]["seat"].append(s)
    # Public schedule summaries allow lazy feasible-candidate discovery without
    # guessing times from IDs or fetching every full flight before any work.
    for route in public["route"]:
        route["alternative_schedules"] = [{k: flights[fid][k] for k in
            ("flight_id", "scheduled_departure", "scheduled_arrival")}
            for fid in route["alternative_flight_ids"]]
    # Handling classes are independent draws, not cargo index/seed parity.
    if "handling_policy" in net:
        transported=[o for o in net["baggage_obligations"] if o["delivery_leg"] > o["origin_leg"]+1]
        for o in transported:
            o["required_handling"]=rng.choice(["standard","fragile","temperature_controlled"])
        if scenario=="specialist_disruption":
            rng.choice([o for o in transported if o["origin_leg"]==1])["required_handling"]="fragile"
        weights={b["baggage_id"]:b["weight_kg"] for b in world["tables"]["baggage"]}
        for s in net["freight_services"]:
            if "fragile" in s.get("handling_capabilities",[]) and s.get("availability")!="standby":
                s["capacity_kg"]=max(8,sum(weights[o["bag_id"]] for o in transported
                    if o["origin_leg"]<=s["from_hub"] and o["delivery_leg"]>s["to_hub"] and o["required_handling"]=="fragile"))
    public["instance_revision"]=REVISION
    public["scenario"].update(scope="evaluation_seed_holdout_same_mechanisms",seed=seed)
    public["policy"]["public_cache_contract"] = (
        "Creation responses contain the complete recorded clearance, release, trip or manifest. "
        "Records do not silently mutate. Local booking changes affect the named booking, its services and flight inventory. "
        "Carrier bulletins identify service changes; dependency closure determines stale custody. "
        "Known contents may be retained, but historical presence is not evidence of current validity.")
    public["policy"]["network_state_query"] = (
        "Transfer network definitions and published flight schedules are immutable. "
        "Use read_transfer_network(include_static=false) for complete current record inventories "
        "and service availability, combining them with definitions observed in read_task. "
        "Flight seat availability remains dynamic; get_flight_status provides current inventory.")
    # Opaque task-local identifiers and independent list order remove role/index shortcuts.
    mapping={}
    keys={"passenger":"passenger_id","flight":"flight_id","booking":"booking_id","seat":"seat_id",
          "baggage":"baggage_id","payment":"payment_id","special_service":"service_id"}
    def identities(rows,key,prefix):
        for row in rows:
            if row[key] not in mapping:
                mapping[row[key]]=f"{prefix}-{rng.getrandbits(48):012x}"
    for kind,key in keys.items():
        identities([r for r in world["tables"][kind] if r.get("task_id")==public["task_id"]],key,kind[:3])
    for collection,key,prefix in [("groups","group_id","grp"),("vehicles","vehicle_id","veh"),
            ("baggage_obligations","obligation_id","cargo"),("freight_services","service_id","svc")]:
        identities(net[collection],key,prefix)
    def remap(value):
        if isinstance(value,str): return mapping.get(value,value)
        if isinstance(value,list): return [remap(v) for v in value]
        if isinstance(value,dict): return {mapping.get(k,k):remap(v) for k,v in value.items()}
        return value
    public,world,private=remap(public),remap(world),remap(private)
    for r in public["route"]:
        rng.shuffle(r["alternative_flight_ids"])
        rng.shuffle(r["alternative_schedules"])
    rng.shuffle(public["requests"])
    for key in ("groups","vehicles","baggage_obligations","freight_services"):
        rng.shuffle(public["transfer_network"][key])
    for rows in world["tables"].values(): rng.shuffle(rows)
    for flight in world["tables"]["flight"]:
        if flight.get("task_id")==public["task_id"]:
            flight["flight_number"]=f"LX{rng.randint(10000,99999)}"
    private["public"]=deepcopy(public)
    for name in ("checkpoint","runtime"): write_json(output/name/"world.json",world)
    write_json(output/"public/task.json",public)
    write_json(output/"private/initial_obligations.json",private)
    return public
