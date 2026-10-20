"""Quantity conservation, budget debits/refunds and partial supplier fulfillment."""
from collections import defaultdict


def _lines(r, rows):
    r.require(bool(rows), "QUANTITY", "Supply at least one line")
    seen = set()
    for row in rows:
        r.require(set(row) == {"request_id", "supplier_id", "quantity"}, "ARGUMENTS", "Line fields are request_id, supplier_id, quantity")
        r.require(row["request_id"] in r.project["requests"] and row["supplier_id"] in r.project["suppliers"], "UNKNOWN_OBJECT", "Unknown request or supplier")
        r.require(row["supplier_id"] in r.project["requests"][row["request_id"]].get("eligible_suppliers", r.project["suppliers"]),
                  "SUPPLIER_ELIGIBILITY", "Supplier is not qualified for this request")
        r.require(type(row["quantity"]) is int and row["quantity"] > 0, "QUANTITY", "Quantities must be positive integers")
        key = (row["request_id"], row["supplier_id"])
        r.require(key not in seen, "DUPLICATE_INPUT", "Combine repeated request/supplier pairs within one operation")
        seen.add(key)


def _committed(p):
    spend, quantities, suppliers = defaultdict(int), defaultdict(int), defaultdict(int)
    for order in p["orders"].values():
        for line in order["lines"]:
            quantity = line["quantity"] - line["canceled"]
            spend[line["request_id"]] += quantity * line["unit_price_cents"]
            quantities[line["request_id"]] += quantity
            suppliers[line["supplier_id"]] += quantity
    return spend, quantities, suppliers


def _check_purchase(r, budget_id, lines, previous_settlement_id=None, retention_ids=None):
    p = r.project
    _lines(r, lines)
    if "contract" in p:
        from .contracts import funding
        funding(r, lines, previous_settlement_id, retention_ids)
    budget = r.artifact(budget_id, "budget")["data"]["allocations"]
    r.require(budget_id == p["budget_id"], "STALE_ARTIFACT", "Use the current budget allocation")
    spend, quantities, suppliers = _committed(p)
    committed_spend = dict(spend)
    new_spend = defaultdict(int)
    limits = {row["request_id"]: row["budget_cents"] for row in budget}
    for row in lines:
        req, supplier, quantity = row["request_id"], row["supplier_id"], row["quantity"]
        cost = quantity * p["suppliers"][supplier]["unit_price_cents"]
        spend[req] += cost
        new_spend[req] += cost
        quantities[req] += quantity
        suppliers[supplier] += quantity
    from .assembly import fees
    processing = fees(p) if "assembly_rules" in p else 0
    if any(value > limits[key] for key, value in spend.items()) or sum(spend.values()) + processing > p["total_budget_cents"]:
        def breakdown(committed, added, limit):
            return {"committed_cents": committed, "new_purchase_cents": added,
                    "total_after_purchase_cents": committed + added, "limit_cents": limit,
                    "remaining_before_purchase_cents": limit - committed,
                    "excess_cents": max(0, committed + added - limit)}
        r.require(False, "BUDGET_EXCEEDED",
            "Existing committed spend plus new purchase cost exceeds a cumulative request cap or the project total. "
            "Received purchases and outstanding uncanceled orders remain committed; budget allocations are not additional funds.",
            {"budget_id": budget_id, "budget_semantics": "replacement_cumulative_caps",
             "requests": [{"request_id": key, **breakdown(committed_spend.get(key, 0), new_spend[key], limits[key])}
                          for key in p["requests"]],
             "processing_cents": processing,
             "project": breakdown(sum(committed_spend.values())+processing, sum(new_spend.values()), p["total_budget_cents"])})
    r.require(all(value <= p["requests"][key]["quantity"] for key, value in quantities.items()), "OVER_ORDER", "Purchase exceeds uncommitted demand")
    r.require(all(value <= p["suppliers"][key]["capacity"] for key, value in suppliers.items()), "SUPPLY_CAPACITY", "Purchase exceeds current supplier capacity")
    pending = [{"request_id": row["request_id"], "supplier_id": row["supplier_id"],
                "quantity": row["quantity"] - row["received"] - row["canceled"]}
               for order in p["orders"].values() for row in order["lines"]
               if row["quantity"] > row["received"] + row["canceled"]]
    r.require(delivery_schedule(p, [*pending, *lines]) is not None, "DELIVERY_DEADLINE",
              "Proposed and outstanding quantities cannot fit supplier slots before request deadlines")


def delivery_schedule(p, lines):
    """Earliest-deadline scheduling for fixed suppliers, with identical unit jobs."""
    occupied = defaultdict(int)
    for artifact in p["artifacts"].values():
        if artifact["kind"] == "receipt":
            for row in artifact["data"]["quantities"]:
                occupied[row["supplier_id"], artifact["data"]["delivery_slot"]] += row["quantity"]
    result = []
    for row in sorted(lines, key=lambda v: (p["requests"][v["request_id"]]["delivery_deadline"], v["request_id"], v["supplier_id"])):
        supplier = p["suppliers"][row["supplier_id"]]
        remaining = row["quantity"]
        from .contracts import earliest_slot
        for slot in range(max(supplier["earliest_slot"], earliest_slot(p, row["request_id"])), p["requests"][row["request_id"]]["delivery_deadline"] + 1):
            amount = min(remaining, supplier["per_slot_capacity"] - occupied[row["supplier_id"], slot])
            if amount > 0:
                result.append({**row, "quantity": amount, "delivery_slot": slot})
                occupied[row["supplier_id"], slot] += amount
                remaining -= amount
            if remaining == 0:
                break
        if remaining:
            return None
    return result


def execute(r, name, a, use_script):
    p = r.project
    if name == "claim-retention":
        from .contracts import collect_retention
        return collect_retention(r, a)
    if name in {"condition-components", "assemble-kit"}:
        from .assembly import condition, assemble
        return condition(r, a) if name == "condition-components" else assemble(r, a)
    if name == "settle-contract":
        from .contracts import settle
        return settle(r, a)
    if name == "allocate-budget":
        rows = a["allocations"]
        r.require(len(rows) == len(p["requests"]) and all(set(row) == {"request_id", "budget_cents"} for row in rows) and
                  {row["request_id"] for row in rows} == set(p["requests"]), "BUDGET_COVERAGE", "Allocate once for every request")
        r.require(all(type(row["budget_cents"]) is int and row["budget_cents"] >= 0 for row in rows),
                  "ARGUMENTS", "budget_cents must be a nonnegative integer")
        total = sum(row["budget_cents"] for row in rows)
        r.require(total <= p["total_budget_cents"], "BUDGET_EXCEEDED", "Sum of replacement cumulative caps exceeds the project total budget",
                  {"allocation_total_cents": total, "project_limit_cents": p["total_budget_cents"],
                   "excess_cents": max(0, total - p["total_budget_cents"])})
        spend, _, _ = _committed(p)
        shortfalls = [{"request_id": row["request_id"], "committed_cents": spend[row["request_id"]],
                       "proposed_limit_cents": row["budget_cents"],
                       "shortfall_cents": spend[row["request_id"]] - row["budget_cents"]}
                      for row in rows if row["budget_cents"] < spend[row["request_id"]]]
        r.require(not shortfalls, "BUDGET_COMMITTED",
                  "Replacement cumulative caps must still cover received purchases and outstanding uncanceled orders. Reallocation does not erase committed spend.",
                  {"requests": shortfalls})
        output = r.emit("budget", {"allocations": rows})
        p["budget_id"] = output["artifact_id"]
        return output
    if name == "plan-purchase":
        selected_funding = {k: a[k] for k in ("previous_settlement_id", "retention_ids")} if "contract" in p else {}
        _check_purchase(r, a["budget_id"], a["lines"], **selected_funding)
        priced = r.arithmetic(name, {"lines": a["lines"], "prices": {key: s["unit_price_cents"] for key, s in p["suppliers"].items()}}, use_script)
        return r.emit("purchase_plan", {**a, **priced}, [a["budget_id"],
            *([a["previous_settlement_id"]] if a.get("previous_settlement_id") else []), *a.get("retention_ids", []),
            *[key for key,v in p["artifacts"].items() if v["kind"] == "retention_collection" and
              v["data"]["stage"] == len(p.get("settlements", {}))+1]])
    if name == "place-orders":
        plan = r.artifact(a["plan_id"], "purchase_plan")["data"]
        r.require(not any(order["plan_id"] == a["plan_id"] for order in p["orders"].values()), "ALREADY_PLACED", "This plan already placed orders")
        lines = [{k: row[k] for k in ("request_id", "supplier_id", "quantity")} for row in plan["lines"]]
        selected_funding = {k: plan[k] for k in ("previous_settlement_id", "retention_ids")} if "contract" in p else {}
        _check_purchase(r, plan["budget_id"], lines, **selected_funding)
        r.require(all(row["cost_cents"] == row["quantity"] * p["suppliers"][row["supplier_id"]]["unit_price_cents"] for row in plan["lines"]),
                  "PRICE_CHANGED", "Reprice against current supplier terms")
        output = r.emit("order", {"plan_id": a["plan_id"], "lines": lines}, [a["plan_id"]])
        p["orders"][output["artifact_id"]] = {"plan_id": a["plan_id"], "budget_id": plan["budget_id"], "lines": [
            {**row, "unit_price_cents": p["suppliers"][row["supplier_id"]]["unit_price_cents"], "received": 0, "canceled": 0} for row in lines]}
        if "contract" in p:
            p["orders"][output["artifact_id"]]["funding"] = selected_funding
        if "assembly_rules" in p:
            for line in p["orders"][output["artifact_id"]]["lines"]:
                line["component_grade"] = p["suppliers"][line["supplier_id"]]["component_grade"]
        return output
    if name in {"receive-shipment", "cancel-remainder"}:
        _lines(r, a["quantities"])
        r.require(a["order_id"] in p["orders"], "UNKNOWN_ARTIFACT", "Unknown placed order")
        order = p["orders"][a["order_id"]]
        selected = []
        for row in a["quantities"]:
            matches = [line for line in order["lines"] if line["request_id"] == row["request_id"] and line["supplier_id"] == row["supplier_id"]]
            r.require(len(matches) == 1, "OBJECT_MISMATCH", "Quantity belongs to another order")
            line = matches[0]
            r.require(row["quantity"] <= line["quantity"] - line["received"] - line["canceled"], "QUANTITY", "Quantity exceeds undelivered, uncanceled remainder")
            selected.append((line, row))
        if name == "receive-shipment":
            from .contracts import earliest_slot
            r.require(all(a["delivery_slot"] >= earliest_slot(p, row["request_id"]) for _, row in selected),
                      "DELIVERY_SLOT", "Delivery precedes stage release or preceding settlement")
            r.require(type(a["delivery_slot"]) is int and a["delivery_slot"] >= 1, "DELIVERY_SLOT", "Use a positive delivery slot")
            r.require(all(a["delivery_slot"] <= p["requests"][row["request_id"]]["delivery_deadline"] for _, row in selected),
                      "DELIVERY_DEADLINE", "Delivery misses a request's public deadline")
            by_supplier = defaultdict(int)
            for _, row in selected:
                by_supplier[row["supplier_id"]] += row["quantity"]
            for supplier_id, quantity in by_supplier.items():
                supplier = p["suppliers"][supplier_id]
                r.require(a["delivery_slot"] >= supplier["earliest_slot"], "DELIVERY_SLOT", "Supplier is not available in this slot")
                delivered = sum(line["received"] for value in p["orders"].values() for line in value["lines"] if line["supplier_id"] == supplier_id)
                in_slot = sum(row["quantity"] for artifact in p["artifacts"].values() if artifact["kind"] == "receipt" and
                              artifact["data"]["delivery_slot"] == a["delivery_slot"] for row in artifact["data"]["quantities"] if row["supplier_id"] == supplier_id)
                r.require(delivered + quantity <= supplier["capacity"], "SUPPLY_CAPACITY", "Supplier cannot fulfill this quantity after the public supply update")
                r.require(in_slot + quantity <= supplier["per_slot_capacity"], "DELIVERY_CAPACITY", "Split fulfillment across suppliers or delivery slots")
            for line, row in selected:
                line["received"] += row["quantity"]
            return r.emit("receipt", a, [a["order_id"]])
        refund = sum(line["unit_price_cents"] * row["quantity"] for line, row in selected)
        for line, row in selected:
            line["canceled"] += row["quantity"]
        return r.emit("cancellation", {**a, "refund_cents": refund}, [a["order_id"]])
    if name == "accept-deliveries":
        receipts = [r.artifact(v, "receipt") for v in a["receipt_ids"]]
        totals = defaultdict(int)
        for receipt in receipts:
            for row in receipt["data"]["quantities"]:
                totals[row["request_id"]] += row["quantity"]
        r.require(dict(totals) == {key: value["quantity"] for key, value in p["requests"].items()}, "DELIVERY_COVERAGE", "Receipts must deliver exactly the requested quantities")
        _, healthy = check_delivery_contract(r)
        r.require(healthy, "DELIVERY_CONTENT", "Joint delivery acceptance failed: receipts, commitments, budgets, "
                  "supplier terms, settlements or assembly do not reconcile. Read the joint-acceptance public contract for scope.")
        output = r.emit("acceptance", {"receipt_ids": a["receipt_ids"], "delivered": dict(totals)}, a["receipt_ids"])
        p["acceptance"] = output["artifact_id"]
        return output
    raise ValueError("Unknown ProcureOps operation")


def check_delivery_contract(r):
    """Public project-contract conditions, independent of the task evaluator binding."""
    p = r.project
    received, receipt_lines, slot_totals, cancellations = defaultdict(int), defaultdict(int), defaultdict(int), defaultdict(int)
    content_ok = True
    for artifact in p["artifacts"].values():
        if artifact["kind"] == "receipt":
            for row in artifact["data"]["quantities"]:
                content_ok &= (artifact["data"]["order_id"] in p["orders"] and type(row["quantity"]) is int and row["quantity"] > 0)
                content_ok &= artifact["data"]["delivery_slot"] <= p["requests"][row["request_id"]]["delivery_deadline"]
                received[row["request_id"]] += row["quantity"]
                receipt_lines[(artifact["data"]["order_id"], row["request_id"], row["supplier_id"])] += row["quantity"]
                slot_totals[(row["supplier_id"], artifact["data"]["delivery_slot"])] += row["quantity"]
        elif artifact["kind"] == "cancellation":
            refund = 0
            order = p["orders"][artifact["data"]["order_id"]]
            for row in artifact["data"]["quantities"]:
                line = next((v for v in order["lines"] if (v["request_id"], v["supplier_id"]) == (row["request_id"], row["supplier_id"])), None)
                if line is None:
                    content_ok = False
                    continue
                cancellations[(artifact["data"]["order_id"], row["request_id"], row["supplier_id"])] += row["quantity"]
                refund += row["quantity"] * line["unit_price_cents"]
            content_ok &= refund == artifact["data"]["refund_cents"]
    cases = {key: {"success": received[key] == value["quantity"]} for key, value in p["requests"].items()}
    spend, quantities, suppliers = _committed(p)
    good = content_ok and bool(p["budget_id"]) and all(v["success"] for v in cases.values()) and sum(spend.values()) <= p["total_budget_cents"]
    if p["budget_id"]:
        budgets = {row["request_id"]: row["budget_cents"] for row in r.artifact(p["budget_id"], "budget")["data"]["allocations"]}
        good &= all(spend[key] <= budgets[key] for key in spend)
        good &= set(budgets) == set(p["requests"]) and sum(budgets.values()) <= p["total_budget_cents"]
    for order_id, order in p["orders"].items():
        for line in order["lines"]:
            good &= line["supplier_id"] in p["requests"][line["request_id"]].get("eligible_suppliers", p["suppliers"])
            good &= (line["received"] == receipt_lines[(order_id, line["request_id"], line["supplier_id"])] and
                     line["canceled"] == cancellations[(order_id, line["request_id"], line["supplier_id"])] and
                     line["received"] + line["canceled"] == line["quantity"] and line["received"] >= 0 and line["canceled"] >= 0 and
                     line["unit_price_cents"] == p["suppliers"][line["supplier_id"]]["unit_price_cents"])
    good &= all(value <= p["suppliers"][key]["capacity"] for key, value in suppliers.items())
    good &= all(slot >= p["suppliers"][supplier]["earliest_slot"] and value <= p["suppliers"][supplier]["per_slot_capacity"]
                for (supplier, slot), value in slot_totals.items())
    if "contract" in p:
        from .contracts import verify, earliest_slot
        good &= verify(p)
        good &= all(a["data"]["delivery_slot"] >= earliest_slot(p, row["request_id"])
                    for a in p["artifacts"].values() if a["kind"] == "receipt" for row in a["data"]["quantities"])
    if "assembly_rules" in p:
        from .assembly import verify as verify_assembly, fees
        good &= verify_assembly(p) and sum(spend.values())+fees(p) <= p["total_budget_cents"]
        good &= all(line["component_grade"] == p["suppliers"][line["supplier_id"]]["component_grade"]
                    for order in p["orders"].values() for line in order["lines"])
    return cases, bool(good)


def evaluate(r):
    """Offline business outcome: contract compliance and a sound acceptance record."""
    cases, good = check_delivery_contract(r)
    p = r.project
    if not p["acceptance"]:
        return cases, False
    accepted = r.artifact(p["acceptance"], "acceptance")["data"]
    totals = defaultdict(int)
    good &= len(accepted["receipt_ids"]) == len(set(accepted["receipt_ids"]))
    for identifier in accepted["receipt_ids"]:
        for row in r.artifact(identifier, "receipt")["data"]["quantities"]:
            totals[row["request_id"]] += row["quantity"]
    good &= dict(totals) == accepted["delivered"] == {key: value["quantity"] for key, value in p["requests"].items()}
    return cases, bool(good)
