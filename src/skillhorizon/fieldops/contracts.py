"""Sequential procurement liquidity, delivery settlement and retained payments."""


def stage_of(project, lines):
    stages = {project["requests"][row["request_id"]]["stage"] for row in lines}
    return next(iter(stages)) if len(stages) == 1 else None


def stage_orders(p, stage):
    return [o for o in p["orders"].values() if stage_of(p, o["lines"]) == stage]


def explicit_claims(p, stage):
    return {key for v in p["artifacts"].values() if v["kind"] == "retention_collection" and
            v["data"]["stage"] == stage for key in v["data"]["retention_ids"]}


def collect_retention(r, a):
    p = r.project
    stage = len(p["settlements"])+1
    cutoff = min(stage, p["contract"]["stages"])
    ids = a["retention_ids"]
    r.require(bool(ids) and len(ids) == len(set(ids)), "RETENTION", "Select distinct nonempty retention claims")
    consumed = {key for o in p["orders"].values() for key in o["funding"]["retention_ids"]}
    consumed.update(key for v in p["artifacts"].values() if v["kind"] == "retention_collection"
                    for key in v["data"]["retention_ids"])
    credit = 0
    for key in ids:
        claim = r.artifact(key, "contract_settlement")["data"]
        r.require(key not in consumed and claim["maturity_stage"] <= cutoff,
                  "RETENTION", "Claim is already credited or not yet due within the project horizon")
        credit += claim["retention_cents"]
    previous = p["settlements"].get(str(stage-1), "")
    return r.emit("retention_collection", {**a, "stage": stage, "credit_cents": credit,
                  "previous_settlement_id": previous}, [*ids, *([previous] if previous else [])])


def funding(r, lines, previous_settlement_id, retention_ids):
    p = r.project
    stage = stage_of(p, lines)
    r.require(stage == len(p["settlements"]) + 1, "CONTRACT_STAGE", "Purchase only the next unsettled contract stage")
    expected = p["settlements"].get(str(stage - 1), "")
    r.require(previous_settlement_id == expected, "SETTLEMENT_CHAIN", "Bind the immediately preceding delivery settlement")
    previous = r.artifact(expected, "contract_settlement")["data"] if expected else None
    cash = previous["closing_cash_cents"] if previous else p["contract"]["opening_cash_cents"]
    direct = explicit_claims(p, stage)
    cash += sum(r.artifact(key)["data"]["retention_cents"] for key in direct)
    r.require(isinstance(retention_ids, list) and len(retention_ids) == len(set(retention_ids)), "RETENTION", "Select distinct retention records")
    consumed = {key for identifier in p["settlements"].values() for key in r.artifact(identifier)["data"]["retention_ids"]}
    for key in retention_ids:
        claim = r.artifact(key, "contract_settlement")["data"]
        r.require(claim["maturity_stage"] <= stage and key not in consumed | direct, "RETENTION", "Retention is future-due or already credited")
        cash += claim["retention_cents"]
    orders = stage_orders(p, stage)
    selected = {key for order in orders for key in order["funding"]["retention_ids"]}
    r.require(selected <= set(retention_ids), "FUNDING_BINDING", "Retain the funding claims already credited to this open stage")
    committed = sum((line["quantity"] - line["canceled"]) * line["unit_price_cents"] for o in orders for line in o["lines"])
    if "assembly_rules" in p:
        from .assembly import fees
        committed += fees(p, stage)
    added = sum(row["quantity"] * p["suppliers"][row["supplier_id"]]["unit_price_cents"] for row in lines)
    r.require(committed + added <= cash, "LIQUIDITY", "Available cash, not the total budget, must cover this stage's committed and proposed purchases",
              {"available_cash_cents": cash, "committed_cents": committed, "proposed_cents": added,
               "shortfall_cents": max(0, committed + added - cash)})


def earliest_slot(p, request_id):
    request = p["requests"][request_id]
    earliest = request.get("earliest_slot", 1)
    if "contract" in p:
        previous = p["settlements"].get(str(request["stage"] - 1))
        if previous:
            earliest = max(earliest, p["artifacts"][previous]["data"]["last_delivery_slot"] + 1)
    return earliest


def settle(r, a):
    p, stage = r.project, a["stage"]
    r.require(stage == len(p["settlements"]) + 1 and stage <= p["contract"]["stages"], "CONTRACT_STAGE", "Settle the next complete stage once")
    previous = p["settlements"].get(str(stage - 1), "")
    r.require(a["previous_settlement_id"] == previous, "SETTLEMENT_CHAIN", "Bind the preceding stage settlement")
    receipts = [r.artifact(key, "receipt") for key in a["receipt_ids"]]
    required = {key: req["quantity"] for key, req in p["requests"].items() if req["stage"] == stage}
    totals = dict.fromkeys(required, 0)
    for receipt in receipts:
        for row in receipt["data"]["quantities"]:
            r.require(row["request_id"] in required, "STAGE_RECEIPTS", "Receipt belongs to another contract stage")
            totals[row["request_id"]] += row["quantity"]
    r.require(totals == required, "STAGE_RECEIPTS", "Supply exact stage delivery quantities")
    orders = stage_orders(p, stage)
    r.require(all(line["received"] + line["canceled"] == line["quantity"] for o in orders for line in o["lines"]),
              "OPEN_COMMITMENTS", "Resolve the stage's outstanding commitments before settlement")
    retentions = sorted({key for o in orders for key in o["funding"]["retention_ids"]} | explicit_claims(p, stage))
    opening = r.artifact(previous)["data"]["closing_cash_cents"] if previous else p["contract"]["opening_cash_cents"]
    credit = sum(r.artifact(key)["data"]["retention_cents"] for key in retentions)
    cost = sum(line["received"] * line["unit_price_cents"] for o in orders for line in o["lines"])
    extra_dependencies, extra = [], {}
    if "assembly_rules" in p:
        from .assembly import fees
        r.require(a["kit_id"] == p["kits"].get(str(stage)), "CONTRACT_KIT", "Settle only the exact completed stage kit")
        r.artifact(a["kit_id"], "contract_kit")
        processing = fees(p, stage)
        extra = {"processing_cents": processing, "purchase_cents": cost}
        cost += processing
        extra_dependencies = [a["kit_id"], *[key for key,v in p["artifacts"].items()
                              if v["kind"] == "conditioning" and v["data"]["stage"] == stage]]
    quantity = sum(required.values())
    reimbursement = quantity * p["contract"]["immediate_cents_per_unit"]
    r.require(opening + credit >= cost, "LIQUIDITY", "Settlement cannot retrospectively fund an unaffordable delivery")
    result = r.emit("contract_settlement", {**a, **extra, "retention_ids": retentions, "opening_cash_cents": opening,
        "retention_credit_cents": credit, "spend_cents": cost, "reimbursement_cents": reimbursement,
        "closing_cash_cents": opening + credit - cost + reimbursement,
        "retention_cents": quantity * p["contract"]["retention_cents_per_unit"],
        "maturity_stage": stage + p["contract"]["retention_delay_stages"],
        "last_delivery_slot": max(v["data"]["delivery_slot"] for v in receipts)},
        [*([previous] if previous else []), *retentions, *a["receipt_ids"], *extra_dependencies,
         *[key for key,v in p["artifacts"].items() if v["kind"] == "retention_collection" and v["data"]["stage"] == stage]])
    p["settlements"][str(stage)] = result["artifact_id"]
    return result


def verify(p):
    """Independent ledger arithmetic; does not call purchase/settlement admission."""
    if len(p["settlements"]) != p["contract"]["stages"]:
        return False
    if not verify_collections(p):
        return False
    cash, previous, claimed = p["contract"]["opening_cash_cents"], "", set()
    for stage in range(1, p["contract"]["stages"] + 1):
        identifier = p["settlements"][str(stage)]
        record = p["artifacts"][identifier]
        data = record["data"]
        expected = {key: value["quantity"] for key, value in p["requests"].items() if value["stage"] == stage}
        deliveries, delivered_ids, slots = dict.fromkeys(expected, 0), [], []
        for key, artifact in p["artifacts"].items():
            if artifact["kind"] == "receipt" and all(row["request_id"] in expected for row in artifact["data"]["quantities"]):
                delivered_ids.append(key)
                slots.append(artifact["data"]["delivery_slot"])
                for row in artifact["data"]["quantities"]:
                    deliveries[row["request_id"]] += row["quantity"]
        orders = stage_orders(p, stage)
        claims = {key for order in orders for key in order["funding"]["retention_ids"]}
        claims.update(key for a in p["artifacts"].values() if a["kind"] == "retention_collection" and
                      a["data"]["stage"] == stage for key in a["data"]["retention_ids"])
        if (record["kind"] != "contract_settlement" or data["stage"] != stage or
                data["previous_settlement_id"] != previous or data["opening_cash_cents"] != cash or
                set(data["receipt_ids"]) != set(delivered_ids) or len(data["receipt_ids"]) != len(delivered_ids) or
                deliveries != expected or set(data["retention_ids"]) != claims or len(data["retention_ids"]) != len(claims) or claims & claimed or
                any(order["funding"]["previous_settlement_id"] != previous or
                    len(order["funding"]["retention_ids"]) != len(set(order["funding"]["retention_ids"])) for order in orders)):
            return False
        credit = 0
        for key in claims:
            claim = p["artifacts"][key]["data"]
            if claim["stage"] >= stage or claim["maturity_stage"] > stage:
                return False
            credit += claim["retention_cents"]
        cost = sum(line["received"] * line["unit_price_cents"] for o in orders for line in o["lines"])
        if "assembly_rules" in p:
            processing = sum(v["data"]["cost_cents"] for v in p["artifacts"].values()
                             if v["kind"] == "conditioning" and v["data"]["stage"] == stage)
            if (data.get("kit_id") != p["kits"].get(str(stage)) or data.get("purchase_cents") != cost or
                    data.get("processing_cents") != processing):
                return False
            cost += processing
        reimbursement = sum(expected.values()) * p["contract"]["immediate_cents_per_unit"]
        if (cash + credit < cost or data["retention_credit_cents"] != credit or data["spend_cents"] != cost or
                data["reimbursement_cents"] != reimbursement or data["closing_cash_cents"] != cash+credit-cost+reimbursement or
                data["retention_cents"] != sum(expected.values()) * p["contract"]["retention_cents_per_unit"] or
                data["maturity_stage"] != stage+p["contract"]["retention_delay_stages"] or data["last_delivery_slot"] != max(slots)):
            return False
        claimed.update(claims)
        cash, previous = data["closing_cash_cents"], identifier
    claimed.update(explicit_claims(p, p["contract"]["stages"]+1))
    return claimed == {key for key in p["settlements"].values() if
                       p["artifacts"][key]["data"]["maturity_stage"] <= p["contract"]["stages"]}


def verify_collections(p):
    """Check collection timing and credit ownership in commitment order."""
    seen, claimed, stage_claims = {}, set(), set()
    stage, previous = 1, ""
    for key, artifact in p["artifacts"].items():
        d, kind = artifact["data"], artifact["kind"]
        if kind == "order":
            for claim in p["orders"][key]["funding"]["retention_ids"]:
                if claim not in seen or (claim in claimed and claim not in stage_claims):
                    return False
                claimed.add(claim)
                stage_claims.add(claim)
        elif kind == "retention_collection":
            ids = d["retention_ids"]
            if (not ids or len(ids) != len(set(ids)) or d["stage"] != stage or
                    d["previous_settlement_id"] != previous or any(cid not in seen or cid in claimed or
                    seen[cid]["maturity_stage"] > min(stage, p["contract"]["stages"]) for cid in ids)):
                return False
            if d["credit_cents"] != sum(seen[cid]["retention_cents"] for cid in ids):
                return False
            claimed.update(ids)
        elif kind == "contract_settlement":
            seen[key] = d
            stage, previous, stage_claims = stage+1, key, set()
    return True
