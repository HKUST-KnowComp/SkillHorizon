"""Merge supplied public contract data; no workspace or evaluator access."""

import json
import sys


def merge(inputs):
    requirements, publications = inputs["requirements"], inputs["publications"]
    by_page = {p["page_id"]: p for p in publications}
    if len(by_page) != len(publications) or set(by_page) != set(requirements["parent_page_ids"]):
        raise ValueError("Supply exactly the declared parent publications")
    fields = dict(requirements["local_fields"])
    imports = []
    for page_id in requirements["parent_page_ids"]:
        parent = by_page[page_id]
        for name, definition in parent["contract"]["fields"].items():
            if name in fields and fields[name] != definition:
                raise ValueError("Conflicting field definition: " + name)
            fields[name] = definition
        imports.append({key: parent[key] for key in ("page_id", "page_version", "publication_id")})
    limits = {key: min([requirements["local_limits"][key]] + [p["contract"]["limits"][key] for p in publications])
              for key in ("batch_size", "retention_days")}
    if any(type(value) is not int or value <= 0 for value in limits.values()):
        raise ValueError("Limits must be positive integers")
    contract = {"fields": fields, "limits": limits, "imports": imports}
    if "capacity_policy" in requirements:
        policy = requirements["capacity_policy"]
        incoming = []
        for publication in publications:
            quotas = [r for r in publication["contract"]["capacity"]["allocations"] if r["consumer_page_id"] == policy["page_id"] and r["activation_stage"] == policy["stage"]]
            if len(quotas) != 1:
                raise ValueError("Missing or duplicate consumer-stage quota")
            incoming.append(quotas[0]["units"])
        batch = min(requirements["local_limits"]["batch_size"], policy["local_capacity_units"] + sum(incoming))
        limits["batch_size"] = batch
        denominator = sum(r["weight"] for r in policy["consumers"])
        allocations = [{"consumer_page_id": r["consumer_page_id"], "activation_stage": r["activation_stage"],
            "units": min(r["max_units"], (batch * r["weight"]) // denominator)} for r in policy["consumers"]]
        contract["capacity"] = {"incoming_units": sum(incoming), "local_units": policy["local_capacity_units"],
            "allocations": allocations, "uncommitted_units": batch - sum(r["units"] for r in allocations)}
    return {"contract": contract, "current_state_verified": False}


if __name__ == "__main__":
    try:
        print(json.dumps(merge(json.load(sys.stdin)), allow_nan=False))
    except (KeyError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
