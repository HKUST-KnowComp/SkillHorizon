"""Runtime contract checks for actual consumer quotas; no evaluator imports."""


def capacity(requirements, publications):
    policy = requirements["capacity_policy"]
    incoming = 0
    for publication in publications:
        matches = [row for row in publication["contract"]["capacity"]["allocations"] if row["consumer_page_id"] == policy["page_id"]]
        if len(matches) != 1 or matches[0]["activation_stage"] != policy["stage"]:
            raise ValueError("Exact consumer and activation-stage quota required")
        incoming += matches[0]["units"]
    batch = min(requirements["local_limits"]["batch_size"], policy["local_capacity_units"] + incoming)
    total_weight = sum(c["weight"] for c in policy["consumers"])
    allocations = [{"consumer_page_id": c["consumer_page_id"], "activation_stage": c["activation_stage"],
        "units": min(c["max_units"], batch * c["weight"] // total_weight)} for c in policy["consumers"]]
    return batch, {"local_units": policy["local_capacity_units"], "incoming_units": incoming, "allocations": allocations,
        "uncommitted_units": batch - sum(c["units"] for c in allocations)}
