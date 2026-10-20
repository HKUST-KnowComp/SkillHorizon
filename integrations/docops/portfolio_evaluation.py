"""Independent frozen-obligation portfolio accounting; no runtime/script imports."""

from collections import defaultdict
from copy import deepcopy
import json


def equivalent_capacity(actual, expected):
    """Compare report sets independently of the actor's calculation code."""
    unordered = {"allocations", "portfolio", "sources", "withheld_sources", "uncommitted_sources", "allowed_purposes"}
    def encode(value, field=None):
        if isinstance(value, dict):
            return {k: encode(v, k) for k, v in value.items()}
        if isinstance(value, list):
            rows = [encode(v) for v in value]
            return sorted(rows, key=lambda v: json.dumps(v, sort_keys=True)) if field in unordered else rows
        return value
    return encode(actual) == encode(expected)


def expected_capacity(requirement, parent_contracts):
    rule, policy = requirement["portfolio_policy"], requirement["capacity_policy"]
    rows = [{**rule["local_grant"], "units": policy["local_capacity_units"]}]
    incoming = 0
    for parent in parent_contracts:
        quotas = [q for q in parent["capacity"]["allocations"] if q["consumer_page_id"] == policy["page_id"]]
        if len(quotas) != 1 or quotas[0]["activation_stage"] != policy["stage"]:
            raise ValueError("Frozen consumer binding mismatch")
        incoming += quotas[0]["units"]
        rows.extend(quotas[0]["sources"])
    totals, rights = defaultdict(int), {}
    for row in rows:
        identity = row["origin_page_id"]
        metadata = {k: deepcopy(v) for k, v in row.items() if k != "units"}
        if identity in rights and rights[identity] != metadata:
            raise ValueError("Frozen origin rights conflict")
        if policy["stage"] > row["expires_stage"] or rule["purpose"] not in row["allowed_purposes"]:
            raise ValueError("Frozen origin authorization mismatch")
        rights[identity] = metadata
        totals[identity] += row["units"]
    order = sorted(totals, key=lambda origin: (rights[origin]["expires_stage"], origin))
    selected, withheld, consumed = [], [], 0
    for origin in order:
        amount = max(0, min(totals[origin], requirement["local_limits"]["batch_size"] - consumed))
        if amount:
            selected.append({**rights[origin], "units": amount})
        if totals[origin] > amount:
            withheld.append({**rights[origin], "units": totals[origin] - amount})
        consumed += amount
    reservations = {c["consumer_page_id"]: [] for c in policy["consumers"]}
    uncommitted = []
    for tranche in selected:
        candidates = [c for c in policy["consumers"] if c["activation_stage"] <= tranche["expires_stage"]
                      and c["purpose"] in tranche["allowed_purposes"]]
        weights = sum(c["weight"] for c in candidates)
        remainder = tranche["units"]
        for consumer in candidates:
            reservation = reservations[consumer["consumer_page_id"]]
            available = consumer["max_units"] - sum(r["units"] for r in reservation)
            proportional, _ = divmod(tranche["units"] * consumer["weight"], weights)
            allocated = min(available, proportional)
            if allocated:
                reservation.append({**tranche, "units": allocated})
                remainder -= allocated
        if remainder:
            uncommitted.append({**tranche, "units": remainder})
    allocations = [{"consumer_page_id": c["consumer_page_id"], "activation_stage": c["activation_stage"],
                    "sources": reservations[c["consumer_page_id"]], "units": sum(s["units"] for s in reservations[c["consumer_page_id"]])}
                   for c in policy["consumers"]]
    return consumed, {"local_units": policy["local_capacity_units"], "incoming_units": incoming, "portfolio": selected,
        "withheld_sources": withheld, "allocations": allocations, "uncommitted_sources": uncommitted,
        "uncommitted_units": sum(s["units"] for s in uncommitted)}


def metrics(expected, requirements):
    ages, joins, fanout = [], [], defaultdict(set)
    for page_id, contract in expected.items():
        sources = contract["capacity"]["portfolio"]
        stage = requirements[page_id]["capacity_policy"]["stage"]
        joins.append(len({s["origin_page_id"] for s in sources if s["origin_page_id"] != page_id}))
        for source in sources:
            if source["origin_page_id"] != page_id:
                ages.append(stage - source["origin_stage"])
                fanout[source["origin_page_id"]].add(page_id)
    return {"max_original_source_age_stages": max(ages, default=0), "max_inherited_origins_joined": max(joins, default=0),
            "max_distinct_downstream_origin_consumers": max(map(len, fanout.values()), default=0),
            "scope": "Frozen source accounting, not proof of model behavior or minimal horizon"}
