"""Finite material lots, paid conditioning and cross-stage contract assembly."""
from collections import Counter


def lot_key(row):
    return row["receipt_id"], row["request_id"], row["supplier_id"]


def inventory(p):
    lots = {}
    for key, a in p["artifacts"].items():
        if a["kind"] == "receipt":
            for row in a["data"]["quantities"]:
                order = p["orders"][a["data"]["order_id"]]
                line = next(v for v in order["lines"] if (v["request_id"], v["supplier_id"]) ==
                            (row["request_id"], row["supplier_id"]))
                lots[key, row["request_id"], row["supplier_id"]] = {
                    "quantity": row["quantity"], "grade": line["component_grade"],
                    "stage": p["requests"][row["request_id"]]["stage"]}
    return lots


def quantities(p, kind):
    result = Counter()
    for a in p["artifacts"].values():
        if a["kind"] == kind:
            result.update({lot_key(row): row["quantity"] for row in a["data"]["inputs"]})
    return result


def fees(p, stage=None):
    return sum(a["data"]["cost_cents"] for a in p["artifacts"].values() if a["kind"] == "conditioning" and
               (stage is None or a["data"]["stage"] == stage))


def bill(p, stage):
    rules, total = p["assembly_rules"], p["contract"]["stages"]
    delays = rules.get("reuse_delays_stages", [rules.get("reuse_delay_stages")])
    request = next(v for v in p["requests"].values() if v["stage"] == stage)
    if "material_transfers" in rules:
        required = Counter({stage: request["quantity"]})
        for row in rules["material_transfers"]:
            if row["origin_stage"] == stage:
                required[stage] -= row["quantity"]
            if row["consumer_stage"] == stage:
                required[row["origin_stage"]] += row["quantity"]
        return dict(required)
    required = {stage: request["quantity"] - sum(stage + delay <= total for delay in delays)}
    for delay in delays:
        if stage > delay:
            required[stage-delay] = 1
    return required


def selected_inputs(r, a):
    p, rows = r.project, a["inputs"]
    r.require(a["stage"] == len(p["settlements"])+1 and a["stage"] <= p["contract"]["stages"],
              "CONTRACT_STAGE", "Work only on the current unsettled assembly stage")
    r.require(bool(rows) and all(set(row) == {"receipt_id", "request_id", "supplier_id", "quantity"} and
              type(row["quantity"]) is int and row["quantity"] > 0 for row in rows), "MATERIAL_INPUT", "Use positive complete lot allocations")
    r.require(len({lot_key(row) for row in rows}) == len(rows), "MATERIAL_INPUT", "Combine repeated lot allocations")
    lots, used = inventory(p), quantities(p, "contract_kit")
    for row in rows:
        key = lot_key(row)
        r.require(key in lots and used[key]+row["quantity"] <= lots[key]["quantity"],
                  "MATERIAL_STOCK", "The actual receipt lot has insufficient unconsumed units")
    return lots


def condition(r, a):
    p = r.project
    lots = selected_inputs(r, a)
    processed = quantities(p, "conditioning")
    for row in a["inputs"]:
        key = lot_key(row)
        r.require(lots[key]["grade"] < p["assembly_rules"]["minimum_grade"] and
                  processed[key]+row["quantity"] <= lots[key]["quantity"],
                  "MATERIAL_GRADE", "Condition only low-grade units that have not already been processed")
    units = sum(row["quantity"] for row in a["inputs"])
    prior_units = sum(v["data"]["work_units"] for v in p["artifacts"].values() if v["kind"] == "conditioning" and v["data"]["stage"] == a["stage"])
    r.require(prior_units+units <= p["assembly_rules"]["conditioning_units_per_stage"],
              "CONDITIONING_CAPACITY", "Conditioning exceeds this stage's work capacity")
    cost = units*p["assembly_rules"]["conditioning_cost_cents_per_unit"]
    from .contracts import stage_orders, explicit_claims
    orders = stage_orders(p, a["stage"])
    previous = p["settlements"].get(str(a["stage"]-1))
    cash = p["artifacts"][previous]["data"]["closing_cash_cents"] if previous else p["contract"]["opening_cash_cents"]
    claims = {key for order in orders for key in order["funding"]["retention_ids"]}
    claims.update(explicit_claims(p, a["stage"]))
    cash += sum(p["artifacts"][key]["data"]["retention_cents"] for key in claims)
    commitments = sum((line["quantity"]-line["canceled"])*line["unit_price_cents"] for order in orders for line in order["lines"])
    r.require(commitments+fees(p, a["stage"])+cost <= cash, "LIQUIDITY", "Processing requires cash after current purchase commitments")
    from .procureops import _committed
    r.require(sum(_committed(p)[0].values())+fees(p)+cost <= p["total_budget_cents"],
              "BUDGET_EXCEEDED", "Processing and purchasing share the project total cap")
    return r.emit("conditioning", {**a, "cost_cents": cost, "work_units": units,
        "output_grade": p["assembly_rules"]["minimum_grade"]}, sorted({row["receipt_id"] for row in a["inputs"]}) +
        ([previous] if previous else []) + sorted(claims) +
        [key for key,v in p["artifacts"].items() if v["kind"] == "retention_collection" and v["data"]["stage"] == a["stage"]])


def assemble(r, a):
    p = r.project
    lots = selected_inputs(r, a)
    r.require(str(a["stage"]) not in p["kits"], "ALREADY_ASSEMBLED", "A stage kit already consumed these materials")
    origins = Counter()
    for row in a["inputs"]:
        origins[lots[lot_key(row)]["stage"]] += row["quantity"]
    r.require(dict(origins) == bill(p, a["stage"]), "MATERIAL_ORIGIN", "Meet the exact current and older-stage bill of materials")
    records = [r.artifact(key, "conditioning") for key in a["conditioning_ids"]]
    certified, used = Counter(), quantities(p, "contract_kit")
    low = {lot_key(row) for row in a["inputs"] if lots[lot_key(row)]["grade"] < p["assembly_rules"]["minimum_grade"]}
    r.require(len(a["conditioning_ids"]) == len(set(a["conditioning_ids"])), "MATERIAL_GRADE", "Do not repeat conditioning records")
    for record in records:
        r.require(record["data"]["stage"] <= a["stage"] and any(lot_key(row) in low for row in record["data"]["inputs"]),
                  "MATERIAL_GRADE", "Use relevant committed conditioning records")
        for row in record["data"]["inputs"]:
            certified[lot_key(row)] += row["quantity"]
    for row in a["inputs"]:
        key = lot_key(row)
        r.require(key not in low or certified[key] >= used[key]+row["quantity"],
                  "MATERIAL_GRADE", "Low-grade units need enough conditioning, including units previously consumed from this lot")
    output = r.emit("contract_kit", {**a, "origin_quantities": {str(k): v for k,v in origins.items()},
        "material_units": sum(origins.values())}, sorted({row["receipt_id"] for row in a["inputs"]})+a["conditioning_ids"])
    p["kits"][str(a["stage"])] = output["artifact_id"]
    return output


def verify(p):
    """Reconstruct lot/work conservation in artifact commitment order."""
    if len(p["kits"]) != p["contract"]["stages"]:
        return False
    lots, used, processed, work = inventory(p), Counter(), Counter(), Counter()
    seen_conditioning, seen_receipts, settled = {}, set(), 0
    cash, committed, processing, claims = p["contract"]["opening_cash_cents"], 0, 0, set()
    for key, artifact in p["artifacts"].items():
        d, kind = artifact["data"], artifact["kind"]
        if kind == "order":
            order = p["orders"][key]
            if any(p["requests"][row["request_id"]]["stage"] != settled+1 for row in order["lines"]):
                return False
            committed += sum(row["quantity"]*row["unit_price_cents"] for row in order["lines"])
            claims.update(order["funding"]["retention_ids"])
        if kind == "retention_collection":
            if d["stage"] != settled+1:
                return False
            claims.update(d["retention_ids"])
        if kind == "cancellation":
            committed -= d["refund_cents"]
        if kind == "conditioning":
            processing += d["cost_cents"]
        if kind in {"order", "cancellation", "conditioning"}:
            credit = sum(p["artifacts"][cid]["data"]["retention_cents"] for cid in claims)
            if committed < 0 or committed+processing > cash+credit:
                return False
        if kind == "receipt":
            seen_receipts.add(key)
        if kind == "contract_settlement":
            settled += 1
            if d.get("kit_id") != p["kits"].get(str(settled)):
                return False
            cash, committed, processing, claims = d["closing_cash_cents"], 0, 0, set()
        if kind not in {"conditioning", "contract_kit"}:
            continue
        rows, stage = d["inputs"], d["stage"]
        if stage != settled+1 or len({lot_key(row) for row in rows}) != len(rows):
            return False
        if any(lot_key(row) not in lots or row["receipt_id"] not in seen_receipts or
               type(row["quantity"]) is not int or row["quantity"] <= 0 or
               used[lot_key(row)]+row["quantity"] > lots[lot_key(row)]["quantity"] for row in rows):
            return False
        if kind == "conditioning":
            units = sum(row["quantity"] for row in rows)
            if (d["work_units"] != units or d["cost_cents"] != units*p["assembly_rules"]["conditioning_cost_cents_per_unit"] or
                    d["output_grade"] != p["assembly_rules"]["minimum_grade"]):
                return False
            work[stage] += units
            for row in rows:
                k = lot_key(row)
                processed[k] += row["quantity"]
                if lots[k]["grade"] >= p["assembly_rules"]["minimum_grade"] or processed[k] > lots[k]["quantity"]:
                    return False
            seen_conditioning[key] = d
        else:
            origins, certified = Counter(), Counter()
            if len(set(d["conditioning_ids"])) != len(d["conditioning_ids"]):
                return False
            for cid in d["conditioning_ids"]:
                if cid not in seen_conditioning:
                    return False
                for row in seen_conditioning[cid]["inputs"]:
                    certified[lot_key(row)] += row["quantity"]
            for row in rows:
                k = lot_key(row)
                used[k] += row["quantity"]
                origins[lots[k]["stage"]] += row["quantity"]
                if lots[k]["grade"] < p["assembly_rules"]["minimum_grade"] and certified[k] < used[k]:
                    return False
            rules = p["assembly_rules"]
            delays = rules.get("reuse_delays_stages", [rules.get("reuse_delay_stages")])
            q = next(v["quantity"] for v in p["requests"].values() if v["stage"] == stage)
            expected = {stage: q-sum(stage+delay <= p["contract"]["stages"] for delay in delays)}
            for delay in delays:
                if stage > delay:
                    expected[stage-delay] = 1
            if "material_transfers" in rules:
                transfers = rules["material_transfers"]
                expected = {stage: q-sum(t["quantity"] for t in transfers if t["origin_stage"] == stage)}
                for transfer in transfers:
                    if transfer["consumer_stage"] == stage:
                        origin = transfer["origin_stage"]
                        expected[origin] = expected.get(origin, 0)+transfer["quantity"]
            if (p["kits"].get(str(stage)) != key or dict(origins) != expected or
                    {int(k): v for k,v in d["origin_quantities"].items()} != expected or
                    d["material_units"] != sum(expected.values())):
                return False
    return (all(used[key] == lot["quantity"] for key,lot in lots.items()) and
            all(v <= p["assembly_rules"]["conditioning_units_per_stage"] for v in work.values()))
