"""Independent initial-world contracts for the three lighter business programs."""
from collections import Counter
import random

from .programs import TRANSFER, WORKLOAD, ASSEMBLY, SHORT_PROGRAMS


def checks(profile, public, world, private):
    from .profile import compare, field, MISSING
    program, p = profile["environment_format"], profile["parameters"]
    if SHORT_PROGRAMS.get(profile["environment_id"]) != program:
        return []
    rows = [compare("short.private.public", public, field(private, "public")),
            compare("short.private.initial_tables", field(world, "tables"), field(private, "initial_tables"))]

    def fact(name, function):
        try:
            actual = function()
        except KeyError:
            actual = MISSING
        except (TypeError, ValueError, IndexError, StopIteration, ZeroDivisionError):
            actual = False
        rows.append(compare("short." + name, True, actual))

    if program == TRANSFER:
        from integrations.logisticsops.transfer_profile_build import KEYS
        fact("initial_ids", lambda: private["initial_ids"] == {
            kind: [r[KEYS[kind]] for r in world["tables"][kind]]
            for kind in ("booking", "baggage", "special_service", "payment")})
        fact("passenger_grid", lambda: len(public["requests"]) == 2 * p["cohorts"] and
             len({r["passenger_id"] for r in public["requests"]}) == 2 * p["cohorts"] and
             sorted(Counter(r["cohort_id"] for r in public["requests"]).values()) == [2] * p["cohorts"] and
             {r["passenger_id"] for r in public["requests"]} ==
             {r["passenger_id"] for r in world["tables"]["passenger"]})
        fact("booking_grid", lambda: len(public["route"]) == p["legs"] and
             [r["leg"] for r in public["route"]] == list(range(1, p["legs"] + 1)) and
             all(len(r["bookings"]) == p["legs"] for r in public["requests"]) and
             Counter((b["booking_id"], b["passenger_id"]) for b in world["tables"]["booking"]) ==
             Counter((key, r["passenger_id"]) for r in public["requests"] for key in r["bookings"]))

        def baggage():
            obligations = public["transfer_network"]["baggage_obligations"]
            expected = {}
            bags = {b["booking_id"]: b for b in world["tables"]["baggage"]}
            for index, request in enumerate(public["requests"]):
                for origin in range(p["legs"] - 1):
                    age = 1 + (index + 2 * origin) % min(3, p["legs"] - origin - 1)
                    if p["dependency_pattern"] == "distributed":
                        age = random.Random(f'baggage-distributed-v1:{p["seed"]}:{index}:{origin}').randrange(1, p["legs"] - origin)
                    booking = request["bookings"][origin]
                    expected[f"cargo-{index}-{origin}"] = (request["passenger_id"], bags[booking]["baggage_id"],
                                                           booking, origin + 1, origin + age + 1, 10)
            return len(obligations) == len(expected) and {
                o["obligation_id"]: tuple(o[k] for k in ("passenger_id", "bag_id", "origin_booking_id",
                                                        "origin_leg", "delivery_leg", "handling_minutes"))
                for o in obligations} == expected
        fact("baggage_origins", baggage)

        def groups():
            expected = {}
            people = public["requests"]
            for leg in range(1, p["legs"]):
                rotation = 1 + (leg - 1) % (p["cohorts"] - 1)
                if p["dependency_pattern"] == "distributed":
                    rotation = random.Random(f'freight-distributed-v1:{p["seed"]}:{leg}').randrange(1, p["cohorts"])
                for cohort in range(p["cohorts"]):
                    expected[f"ground-{leg+1}-{cohort}"] = (leg + 1, [people[2*cohort]["passenger_id"],
                        people[2*((cohort+rotation) % p["cohorts"])+1]["passenger_id"]], 20)
            actual = public["transfer_network"]["groups"]
            return len(actual) == len(expected) and {r["group_id"]:
                (r["leg"], r["passenger_ids"], r["boarding_buffer_minutes"]) for r in actual} == expected
        fact("transfer_groups", groups)
        fact("vehicles", lambda: Counter((v["leg"], v["passenger_capacity"], v["weight_capacity_kg"],
             v["base_travel_minutes"], v["wheelchair_capacity"], v["turnaround_minutes"])
             for v in public["transfer_network"]["vehicles"]) == Counter(
             (leg, 2, capacity, duration, 2, 10) for leg in range(2, p["legs"]+1)
             for capacity, duration in ((36, 15), (160, 25), (160, 30))))
        fact("vehicle_identities", lambda: Counter(v["vehicle_id"] for v in public["transfer_network"]["vehicles"]) ==
             Counter(f"vehicle-{leg}-{i}" for leg in range(2, p["legs"]+1) for i in range(3)))
    elif program == WORKLOAD:
        for name in ("resources", "usage", "scheduled_updates"):
            rows.append(compare("short.private." + name, field(world, "finops", name), field(private, name)))
        fact("unit_count", lambda: len(public["unit_ids"]) == p["units"] == len(set(public["unit_ids"])) and
             set(public["unit_ids"]) == {r["business_unit_id"] for r in world["tables"]["business_unit"]})
        fact("period_count", lambda: len(public["periods"]) == p["periods"] ==
             len({r["period_id"] for r in public["periods"]}))
        fact("resource_grid", lambda: Counter(r["business_unit_id"] for r in world["finops"]["resources"].values()
             if r["service_category"] == "compute") == Counter({u: p["resources_per_unit"] for u in public["unit_ids"]}) and
             sum(r["service_category"] == "storage" for r in world["finops"]["resources"].values()) == 1 and
             public["resource_ids"] == list(world["finops"]["resources"]))
        fact("usage_grid", lambda: Counter((r["period_id"], r["resource_id"]) for r in world["finops"]["usage"].values()) ==
             Counter((period["period_id"], key) for period in public["periods"] for key in world["finops"]["resources"]))

        def jobs():
            compute = [r for r in world["finops"]["resources"].values() if r["service_category"] == "compute"]
            expected = {(stage+1, r["resource_id"]): (f"job-{stage}-{i}", period["period_id"], 180+20*((i+p["seed"]) % 4))
                        for stage, period in enumerate(public["periods"]) for i, r in enumerate(compute)}
            actual = public["workload_policy"]["jobs"]
            return len(actual) == len(expected) and {(j["stage"], j["resource_id"]):
                (j["job_id"], j["period_id"], j["local_records"]) for j in actual} == expected
        fact("job_grid", jobs)

        def consumers():
            compute = [r for r in world["finops"]["resources"].values() if r["service_category"] == "compute"]
            jobs = public["workload_policy"]["jobs"]
            by_key = {(j["stage"], j["resource_id"]): j for j in jobs}
            for job in jobs:
                index = next(i for i, r in enumerate(compute) if r["resource_id"] == job["resource_id"])
                lane, member = divmod(index, p["resources_per_unit"])
                ages = list(range(1, min(3, p["periods"] - job["stage"]) + 1))
                rng = random.Random(f'workload-distributed-v1:{p["seed"]}:{job["job_id"]}')
                if p["dependency_pattern"] == "distributed" and ages:
                    later = list(range(2, p["periods"] - job["stage"] + 1))
                    ages = [1, *sorted(rng.sample(later, min(2, len(later))))]
                expected = []
                for age in ages:
                    target_lane = (lane + 1 + (age-1) % (p["units"]-1)) % p["units"]
                    target = target_lane * p["resources_per_unit"] + (member+age) % p["resources_per_unit"]
                    if p["dependency_pattern"] == "distributed":
                        target_lane = rng.choice([i for i in range(p["units"]) if i != lane])
                        target = target_lane * p["resources_per_unit"] + rng.randrange(p["resources_per_unit"])
                    child = by_key[job["stage"]+age, compute[target]["resource_id"]]
                    expected.append({"job_id": child["job_id"], "delivery_stage": child["stage"], "weight": age})
                if job["consumers"] != expected:
                    return False
            return True
        fact("consumer_relationships", consumers)
        def cross_unit():
            resources = world["finops"]["resources"]
            jobs = {j["job_id"]: j for j in public["workload_policy"]["jobs"]}
            return all(resources[job["resource_id"]]["business_unit_id"] !=
                       resources[jobs[c["job_id"]]["resource_id"]]["business_unit_id"]
                       for job in jobs.values() for c in job["consumers"])
        fact("cross_unit_consumers", cross_unit)
    elif program == ASSEMBLY:
        rows.append(compare("short.private.initial_state", field(world, "production"), field(private, "initial_state")))
        fact("order_grid", lambda: Counter((o["level"], o["lane"]) for o in world["production"]["orders"].values()) ==
             Counter((level, lane) for level in range(p["levels"]) for lane in range(p["terminal_lanes"] if level==p["levels"]-1 else p["supplier_lanes"])) and
             public["orders"] == list(world["production"]["orders"].values()) and
             public["order_ids"] == list(world["production"]["orders"]) and
             public["terminal_order_ids"] == [o["order_id"] for o in public["orders"] if o["level"] == p["levels"]-1])

        def bom():
            from integrations.internal_mcp.topology import parent_positions
            orders = world["production"]["orders"]
            grid = {(o["level"], o["lane"]): key for key, o in orders.items()}
            for key, o in orders.items():
                level, lane = o["level"], o["lane"]
                if p["supplier_lanes"]==1:
                    edges=[] if level==0 else [(0,0,2)]
                elif p["dependency_pattern"] != "staggered":
                    edges = [(s, i, 1+(p["seed"]+lane+s+i) % 2)
                             for s, i in parent_positions(level, lane, 2, p["seed"], p["dependency_pattern"])]
                else:
                    edges = [] if level == 0 else [(0, lane, 2), (0, 1-lane, 1)] if level == 1 else (
                        [(1, lane, 1), (0, 1-lane, 1+lane)] if level == 2 else
                        [(level-1, lane, 1), (level-2, 1-lane, 1), (0, lane, 1+lane)])
                expected = [{"parent_order_id": grid[s, i], "units_per_output": n} for s, i, n in edges]
                if (key != o["order_id"] or o["bom"] != expected or o["parent_order_ids"] != [e["parent_order_id"] for e in expected]
                        or o["components_per_output"] != (sum(e["units_per_output"] for e in expected) or 1)
                        or o["terminal"] != (level == p["levels"]-1)):
                    return False
            return True
        fact("bom_relationships", bom)

        def quantities():
            state = world["production"]
            orders = state["orders"]
            quantities = {key: p["terminal_quantity"] if o["level"] == p["levels"]-1 else 0 for key, o in orders.items()}
            for key, o in sorted(orders.items(), key=lambda item: -item[1]["level"]):
                for edge in o["bom"]:
                    quantities[edge["parent_order_id"]] += quantities[key] * edge["units_per_output"]
            return all(type(o["output_quantity"]) is int and o["output_quantity"] == quantities[key]
                       for key, o in orders.items())
        fact("quantity_propagation", quantities)
        fact("response_coverage", lambda: set(world["production"]["response_profiles"]) == set(world["production"]["orders"]) and
             all(set(world["production"]["response_profiles"][key]) == {str(i) for i in range(o["output_quantity"])}
                 for key, o in world["production"]["orders"].items()))
        def response_metrics():
            state = world["production"]
            profiles = {r["profile_id"]: r for r in world["tables"]["quality_check_profile"]}
            for key, order in state["orders"].items():
                names = {r["metric_name"] for r in profiles[order["profile_id"]]["metric_definitions"]}
                if not names or any(set(row) != names or any(type(v) is not int or v not in (-1, 0, 1) for v in row.values())
                                    for row in state["response_profiles"][key].values()):
                    return False
            return True
        fact("response_metrics", response_metrics)
        fact("raw_unit_coverage", lambda: all(len(o["raw_lot_ids"]) == 1 and
             len(world["production"]["lots"][o["raw_lot_ids"][0]]["unit_ids"]) == o["output_quantity"] and
             all(world["production"]["units"][key]["lot_id"] == o["raw_lot_ids"][0]
                 for key in world["production"]["lots"][o["raw_lot_ids"][0]]["unit_ids"])
             for o in world["production"]["orders"].values() if o["level"] == 0))
        fact("raw_lot_coverage", lambda: set(world["production"]["lots"])==
             {lot for o in world["production"]["orders"].values() if o["level"]==0 for lot in o["raw_lot_ids"]})
        fact("raw_unit_identities", lambda: all(key == row["unit_id"] for key, row in world["production"]["units"].items()) and
             Counter(key for lot in world["production"]["lots"].values() for key in lot["unit_ids"]) ==
             Counter(world["production"]["units"].keys()))
    return rows
