"""Independent invoice accounting from public sources, not runtime artifacts."""

from fractions import Fraction


def account(project, orders, source_rows):
    funding = project["funding"]
    cash = int(source_rows(funding["treasury_source"])[0]["cash_cents"])
    terms = {(row["region"], row["customer_id"]): row for row in source_rows(funding["invoice_terms_source"])}
    limits = ({row["customer_id"]: int(row["credit_limit_cents"]) for row in source_rows(funding["credit_limits_source"])}
              if "credit_limits_source" in funding else None)
    unpaid, artifacts, collections, release = [], {}, {}, []
    running = {region: [0, 0] for region in project["regions"]}
    for period in project["periods"]:
        due = [invoice for invoice in unpaid if invoice["due_period"] == period]
        cash += sum(invoice["amount_cents"] for invoice in due)
        unpaid = [invoice for invoice in unpaid if invoice["due_period"] != period]
        for invoice in due:
            collections[period, invoice["invoice_id"]] = {
                "period": period, "origin_period": invoice["period"],
                **{key: invoice[key] for key in ("invoice_id", "region", "customer_id", "order_id", "amount_cents", "payment_route")}}
        demand = {region: sum(row["usd_cents"] for row in orders[region, period]) for region in project["regions"]}
        total = sum(demand.values())
        budgets = {region: int(Fraction(cash * value, total)) if total else 0 for region, value in demand.items()}
        remainder = cash - sum(budgets.values()) if total else 0
        for region in sorted(r for r, value in demand.items() if value > 0):
            if remainder:
                budgets[region] += 1
                remainder -= 1
        artifacts["allocation", None, period] = [{"region": region, "period": period, "demand_usd_cents": demand[region],
            "budget_cents": budgets[region], "opening_cash_cents": cash} for region in sorted(demand)]
        quotas = {}
        if limits is not None:
            assessed = []
            for customer, limit in sorted(limits.items()):
                exposure = sum(row["amount_cents"] for row in unpaid if row["customer_id"] == customer)
                wanted = {region: sum(row["usd_cents"] for row in orders[region, period]
                                      if row["customer_id"] == customer) for region in project["regions"]}
                total_wanted = sum(wanted.values())
                available_credit = max(0, limit-exposure)
                shares = {region: int(Fraction(value*available_credit, total_wanted)) if total_wanted else 0
                          for region, value in wanted.items()}
                extra = available_credit-sum(shares.values()) if total_wanted else 0
                for region in sorted(region for region in wanted if wanted[region]):
                    if extra:
                        shares[region] += 1
                        extra -= 1
                for region in sorted(wanted):
                    quotas[region, customer] = shares[region]
                    assessed.append({"period": period, "region": region, "customer_id": customer,
                        "credit_limit_cents": limit, "outstanding_cents": exposure, "credit_quota_cents": shares[region]})
            artifacts["credit_assessment", None, period] = assessed
        invoices = []
        for region in project["regions"]:
            available = budgets[region]
            realized = []
            for row in sorted(orders[region, period], key=lambda row: row["order_id"]):
                cost = -(-row["usd_cents"] // 2)
                accepted = cost <= available and (limits is None or row["usd_cents"] <= quotas[region, row["customer_id"]])
                if accepted and limits is not None:
                    quotas[region, row["customer_id"]] -= row["usd_cents"]
                spent = cost if accepted else 0
                available -= spent
                cash -= spent
                realized.append({**row, "cost_cents": spent, "fulfilled": int(accepted)})
                if accepted and row["usd_cents"] > 0:
                    term = terms[region, row["customer_id"]]
                    delay = int(term["delay_periods"])
                    if not 1 <= delay <= 24 or term["payment_route"] not in {"normal", "disputed"}:
                        raise ValueError("Invalid invoice terms")
                    invoices.append({"record_type": "invoice", **{key: row[key] for key in ("region", "period", "customer_id", "order_id")},
                        "invoice_id": "INV-" + row["order_id"], "due_period": "P" + str(int(period[1:]) + delay).zfill(2),
                        "payment_route": term["payment_route"], "amount_cents": row["usd_cents"], "closing_cash_cents": 0})
            artifacts["fulfillment", region, period] = realized
            count = sum(row["fulfilled"] for row in realized)
            sales = sum(row["usd_cents"] for row in realized if row["fulfilled"])
            running[region][0] += count
            running[region][1] += sales
            release.append({"region": region, "period": period, "order_count": count, "usd_cents": sales,
                "cumulative_order_count": running[region][0], "cumulative_usd_cents": running[region][1]})
        unpaid.extend(invoices)
        artifacts["obligation_settlement", None, period] = [
            {"record_type": "cash", "period": period, "region": "", "customer_id": "", "order_id": "", "invoice_id": "",
             "due_period": "", "payment_route": "", "amount_cents": 0, "closing_cash_cents": cash},
            *sorted(invoices, key=lambda row: row["order_id"])]
    return {"release": release, "artifacts": artifacts, "collections": collections,
            "outstanding_invoices": unpaid, "closing_cash_cents": cash}


def check_artifact(runtime, artifact, accounting=None):
    from .verification import obligation_accounting_from_sources
    from .runtime import ordered
    accounting = accounting if accounting is not None else obligation_accounting_from_sources(runtime.root, runtime.project)
    actual = runtime._rows(artifact)
    if artifact["kind"] in {"collection", "dispute_resolution"}:
        ids = artifact["arguments"]["invoice_ids"]
        expected = [accounting["collections"].get((artifact["period"], identifier)) for identifier in ids]
        route = "normal" if artifact["kind"] == "collection" else "disputed"
        return bool(ids) and len(ids) == len(set(ids)) and all(row and row["payment_route"] == route for row in expected) and ordered(actual) == ordered(expected)
    expected = accounting["artifacts"].get((artifact["kind"], artifact["region"], artifact["period"]))
    return None if expected is None else ordered(actual) == ordered(expected)


def validate_ancestors(runtime, rollup_ids):
    from .verification import obligation_accounting_from_sources
    accounting = obligation_accounting_from_sources(runtime.root, runtime.project)
    pending, seen = list(rollup_ids), set()
    while pending:
        identifier = pending.pop()
        if identifier in seen:
            continue
        seen.add(identifier)
        artifact = runtime._artifact(identifier)
        pending.extend(p["id"] for p in artifact["inputs"] if p["type"] == "artifact")
        runtime._require(check_artifact(runtime, artifact, accounting) is not False, "CONTENT_MISMATCH",
                         "Obligation accounting does not reconcile with public sources")
