"""Persistent receivables and evidence-dependent collection. Public runtime only."""

from .obligation_skills import FIELDS


def period_number(period):
    if not isinstance(period, str) or len(period) < 3 or period[0] != "P" or not period[1:].isdigit():
        raise ValueError("Use a public period identifier")
    return int(period[1:])


def receipt_row(invoice, period):
    return {"period": period, "origin_period": invoice["period"],
            **{key: invoice[key] for key in ("invoice_id", "region", "customer_id", "order_id", "amount_cents", "payment_route")}}


def prior_chain(runtime, identifier, period):
    index = runtime.project["periods"].index(period)
    result = []
    while index:
        item = runtime._artifact(identifier, "obligation_settlement", parameter="previous_settlement_id")
        runtime._require(item["period"] == runtime.project["periods"][index - 1], "PERIOD_DEPENDENCY",
                         "Use a complete chronological settlement chain")
        result.append(item)
        allocation = runtime._artifact(item["arguments"]["allocation_id"], "allocation")
        identifier = allocation["arguments"]["previous_settlement_id"]
        index -= 1
    runtime._require(identifier == "", "PERIOD_DEPENDENCY", "The first period has no predecessor")
    return result


def execute_obligations(r, name, a, use_script):
    def artifact(key, kind):
        return r._artifact(a[key], kind, parameter=key)

    def regional(key, kind):
        values = [r._artifact(value, kind, parameter=f"{key}[{i}]") for i, value in enumerate(a[key])]
        r._require(len(values) == len(r.project["regions"]) and
                   {v["region"] for v in values} == set(r.project["regions"]) and len({v["period"] for v in values}) == 1,
                   "REGION_COVERAGE", "Supply one current input per region from one period")
        return values

    if name in {"collect-invoices", "reconcile-disputed-invoices"}:
        period = a["period"]
        r._require(period in r.project["periods"], "PERIOD_SCOPE", "Collection period is outside the project")
        r._require(bool(a["invoice_ids"]), "EMPTY_COLLECTION", "Select at least one invoice")
        books = [r._artifact(value, "obligation_settlement", parameter=f"settlement_ids[{i}]")
                 for i, value in enumerate(a["settlement_ids"])]
        invoices = {}
        for book in books:
            r._require(period_number(book["period"]) < period_number(period), "PERIOD_DEPENDENCY", "Invoices must originate before collection")
            for row in r._rows(book):
                if row["record_type"] == "invoice":
                    old = invoices.get(row["invoice_id"])
                    r._require(old is None or old == row, "INVOICE_CONFLICT", "Conflicting invoice versions")
                    invoices[row["invoice_id"]] = row
        disputed = name == "reconcile-disputed-invoices"
        evidence = []
        if disputed:
            for i, identifier in enumerate(a["fulfillment_ids"]):
                evidence.extend(r._rows(r._artifact(identifier, "fulfillment", parameter=f"fulfillment_ids[{i}]")))
        selected = []
        for identifier in a["invoice_ids"]:
            r._require(identifier in invoices, "UNKNOWN_INVOICE", "Invoice not found in selected ledgers")
            row = invoices[identifier]
            r._require(row["due_period"] == period, "INVOICE_NOT_DUE", "Collection must occur in the invoice's due period")
            r._require(row["payment_route"] == ("disputed" if disputed else "normal"), "PAYMENT_ROUTE",
                       "This operation does not apply to the invoice's payment route")
            if disputed:
                r._require(any(e["fulfilled"] == 1 and e["usd_cents"] == row["amount_cents"] and
                               all(e[k] == row[k] for k in ("order_id", "customer_id", "region", "period")) for e in evidence),
                           "FULFILLMENT_EVIDENCE", "Evidence does not establish this invoice's fulfilled order and amount")
            selected.append(receipt_row(row, period))
        inputs = r._artifact_refs(a["settlement_ids"] + (a["fulfillment_ids"] if disputed else []))
        return r._commit(name, a, "dispute_resolution" if disputed else "collection",
                         sorted(selected, key=lambda row: row["invoice_id"]), inputs, period=period)

    if name == "allocate-working-capital":
        summaries = regional("summary_ids", "summary")
        period = summaries[0]["period"]
        chain = prior_chain(r, a["previous_settlement_id"], period)
        treasury = r._source(a["treasury_source_id"], "treasury", parameter="treasury_source_id")
        r._require(a["treasury_source_id"] == r.project["funding"]["treasury_source"], "SOURCE_SCOPE", "Use the project treasury")
        cash = (next(row["closing_cash_cents"] for row in r._rows(chain[0]) if row["record_type"] == "cash")
                if chain else r._rows(treasury)[0]["cash_cents"])
        due = {row["invoice_id"]: receipt_row(row, period) for book in chain for row in r._rows(book)
               if row["record_type"] == "invoice" and row["due_period"] == period}
        receipts = {}
        for i, identifier in enumerate(a["collection_ids"]):
            item = r._artifact(identifier, parameter=f"collection_ids[{i}]")
            r._require(item["kind"] in {"collection", "dispute_resolution"} and item["period"] == period,
                       "COLLECTION_SCOPE", "Use current collection records for this period")
            for row in r._rows(item):
                r._require(row["invoice_id"] not in receipts, "DOUBLE_COLLECTION", "Credit each invoice once")
                r._require(due.get(row["invoice_id"]) == row, "COLLECTION_CONTENT", "Receipt is not a matching due obligation")
                r._require(item["kind"] == ("collection" if row["payment_route"] == "normal" else "dispute_resolution"),
                           "PAYMENT_ROUTE", "Receipt does not satisfy the required collection route")
                receipts[row["invoice_id"]] = row
        r._require(set(receipts) == set(due), "DUE_COLLECTION_COVERAGE", "Due invoice collections are incomplete")
        cash += sum(row["amount_cents"] for row in receipts.values())
        rows = r._compute(name, [[row for s in summaries for row in r._rows(s)]], {"opening_cash_cents": cash}, use_script)
        inputs = r._artifact_refs(a["summary_ids"] + a["collection_ids"] + ([a["previous_settlement_id"]] if chain else []))
        if "credit_limits_source" in r.project["funding"]:
            assessment = artifact("credit_assessment_id", "credit_assessment")
            r._require(assessment["period"] == period and
                       assessment["arguments"]["previous_settlement_id"] == a["previous_settlement_id"] and
                       set(assessment["arguments"]["joined_ids"]) == {s["arguments"]["joined_id"] for s in summaries},
                       "CREDIT_BINDING", "Bind the assessment for this exact demand set and prior settlement")
            inputs += r._artifact_refs([a["credit_assessment_id"]])
        if not chain:
            inputs.append(r._source_ref(a["treasury_source_id"]))
        return r._commit(name, a, "allocation", rows, inputs, period=period)

    if name == "settle-period":
        fulfilled, allocation = regional("fulfillment_ids", "fulfillment"), artifact("allocation_id", "allocation")
        period = allocation["period"]
        r._require(all(item["period"] == period and item["arguments"]["allocation_id"] == a["allocation_id"] for item in fulfilled),
                   "ALLOCATION_MISMATCH", "Settle one allocation's regional fulfillment")
        source = r._source(a["terms_source_id"], "invoice_terms", parameter="terms_source_id")
        r._require(a["terms_source_id"] == r.project["funding"]["invoice_terms_source"], "SOURCE_SCOPE", "Use the project invoice terms")
        terms = {(row["region"], row["customer_id"]): row for row in r._rows(source)}
        orders = [row for item in fulfilled for row in r._rows(item) if row["fulfilled"]]
        cash = r._rows(allocation)[0]["opening_cash_cents"] - sum(row["cost_cents"] for row in orders)
        r._require(cash >= 0, "CASH_BALANCE", "Spending exceeds available cash")
        rows = [{"record_type": "cash", "period": period, "region": "", "customer_id": "", "order_id": "", "invoice_id": "",
                 "due_period": "", "payment_route": "", "amount_cents": 0, "closing_cash_cents": cash}]
        for order in sorted(orders, key=lambda row: row["order_id"]):
            if not order["usd_cents"]:
                continue
            term = terms[order["region"], order["customer_id"]]
            r._require(1 <= term["delay_periods"] <= 24 and term["payment_route"] in {"normal", "disputed"},
                       "PAYMENT_TERMS", "Use positive bounded invoice maturity and a supported payment route")
            rows.append({"record_type": "invoice", **{key: order[key] for key in ("period", "region", "customer_id", "order_id")},
                         "invoice_id": f"INV-{order['order_id']}", "due_period": f"P{period_number(period) + term['delay_periods']:02d}",
                         "payment_route": term["payment_route"], "amount_cents": order["usd_cents"], "closing_cash_cents": 0})
        return r._commit(name, a, "obligation_settlement", rows,
                         r._artifact_refs(a["fulfillment_ids"] + [a["allocation_id"]]) + [r._source_ref(a["terms_source_id"])], period=period)

    if name == "close-funded-period":
        settlement = artifact("settlement_id", "obligation_settlement")
        period = settlement["period"]
        index = r.project["periods"].index(period)
        previous = []
        if index:
            prior = artifact("previous_rollup_id", "rollup")
            r._require(prior["period"] == r.project["periods"][index - 1], "PERIOD_DEPENDENCY", "Use the immediately preceding rollup")
            previous = r._rows(prior)
        else:
            r._require(a["previous_rollup_id"] == "", "PERIOD_DEPENDENCY", "First period has no prior rollup")
        summaries = []
        for identifier in settlement["arguments"]["fulfillment_ids"]:
            item = r._artifact(identifier, "fulfillment")
            realized = [row for row in r._rows(item) if row["fulfilled"]]
            summaries.append({"region": item["region"], "period": period, "order_count": len(realized),
                              "usd_cents": sum(row["usd_cents"] for row in realized)})
        rows = r._compute("rollup-period", [summaries, previous], {}, False)
        return r._commit(name, a, "rollup", rows,
                         r._artifact_refs([a["settlement_id"]] + ([a["previous_rollup_id"]] if index else [])), period=period)
    raise ValueError("Unknown obligation operation")
