"""Public origin-preserving quota rules and optional isolated merge script."""

from copy import deepcopy
import json
import sys


def portfolio_value(value, key=None):
    """Canonicalize unordered report collections without dropping duplicates."""
    if isinstance(value, dict):
        return {k: portfolio_value(v, k) for k, v in value.items()}
    if isinstance(value, list):
        rows = [portfolio_value(v) for v in value]
        if key in {"allocations", "portfolio", "sources", "withheld_sources", "uncommitted_sources", "allowed_purposes"}:
            rows.sort(key=lambda v: json.dumps(v, sort_keys=True))
        return rows
    return value


def capacity(requirements, publications):
    policy = requirements["capacity_policy"]
    portfolio = requirements["portfolio_policy"]
    origins = {}
    incoming = 0

    def add(source):
        source = deepcopy(source)
        if requirements.get("portfolio_representation") == "unordered_collections_v2":
            source["allowed_purposes"] = sorted(source["allowed_purposes"])
        units = source.pop("units")
        if type(units) is not int or units < 0:
            raise ValueError("Nonnegative integer origin units required")
        if portfolio["purpose"] not in source["allowed_purposes"] or policy["stage"] > source["expires_stage"]:
            raise ValueError("Origin is expired or not authorized for this purpose")
        identifier = source["origin_page_id"]
        if identifier in origins:
            prior = {k: v for k, v in origins[identifier].items() if k != "units"}
            if source != prior:
                raise ValueError("Conflicting rights for one origin")
            origins[identifier]["units"] += units
        elif units:
            origins[identifier] = {**source, "units": units}

    for publication in publications:
        matches = [q for q in publication["contract"]["capacity"]["allocations"] if q["consumer_page_id"] == policy["page_id"]]
        if len(matches) != 1 or matches[0]["activation_stage"] != policy["stage"]:
            raise ValueError("Exact consumer-stage allocation required")
        quota = matches[0]
        if sum(s["units"] for s in quota["sources"]) != quota["units"]:
            raise ValueError("Quota source quantities do not balance")
        if len({s["origin_page_id"] for s in quota["sources"]}) != len(quota["sources"]):
            raise ValueError("Duplicate origin within one parent allocation")
        for source in quota["sources"]:
            add(source)
        incoming += quota["units"]
    grant = {**portfolio["local_grant"], "units": policy["local_capacity_units"]}
    add(grant)
    ordered = sorted(origins.values(), key=lambda s: (s["expires_stage"], s["origin_page_id"]))
    retained, withheld, remaining = [], [], requirements["local_limits"]["batch_size"]
    for source in ordered:
        used = min(source["units"], remaining)
        if used:
            retained.append({**source, "units": used})
        if used < source["units"]:
            withheld.append({**source, "units": source["units"] - used})
        remaining -= used
    batch = sum(s["units"] for s in retained)
    allocations = [{"consumer_page_id": c["consumer_page_id"], "activation_stage": c["activation_stage"], "units": 0, "sources": []}
                   for c in policy["consumers"]]
    residual = []
    for source in retained:
        eligible = [(c, q) for c, q in zip(policy["consumers"], allocations)
                    if c["purpose"] in source["allowed_purposes"] and c["activation_stage"] <= source["expires_stage"]]
        denominator = sum(c["weight"] for c, _ in eligible)
        assigned = 0
        for consumer, quota in eligible:
            units = min(consumer["max_units"] - quota["units"], source["units"] * consumer["weight"] // denominator)
            if units:
                quota["sources"].append({**source, "units": units})
                quota["units"] += units
                assigned += units
        if assigned < source["units"]:
            residual.append({**source, "units": source["units"] - assigned})
    return batch, {"local_units": policy["local_capacity_units"], "incoming_units": incoming,
                   "portfolio": retained, "withheld_sources": withheld, "allocations": allocations,
                   "uncommitted_sources": residual, "uncommitted_units": sum(s["units"] for s in residual)}


def merge(inputs):
    requirements, publications = inputs["requirements"], inputs["publications"]
    by_page = {p["page_id"]: p for p in publications}
    if len(by_page) != len(publications) or set(by_page) != set(requirements["parent_page_ids"]):
        raise ValueError("Supply exactly the declared parent publications")
    fields = deepcopy(requirements["local_fields"])
    for publication in publications:
        for name, definition in publication["contract"]["fields"].items():
            if name in fields and fields[name] != definition:
                raise ValueError("Incompatible field definition")
            fields[name] = definition
    batch, allocation = capacity(requirements, publications)
    return {"contract": {"fields": fields, "limits": {"batch_size": batch,
        "retention_days": min([requirements["local_limits"]["retention_days"]] + [p["contract"]["limits"]["retention_days"] for p in publications])},
        "imports": [{k: by_page[pid][k] for k in ("page_id", "page_version", "publication_id")} for pid in requirements["parent_page_ids"]],
        "capacity": allocation}, "current_state_verified": False}


if __name__ == "__main__":
    try:
        print(json.dumps(merge(json.load(sys.stdin)), allow_nan=False))
    except (KeyError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
