"""Business bindings for cash-constrained, cross-period fulfillment."""

FIELDS = {
    "allocation": ["region", "period", "demand_usd_cents", "budget_cents", "opening_cash_cents"],
    "fulfillment": ["order_id", "customer_id", "region", "period", "usd_cents", "fulfilled", "cost_cents"],
    "settlement": ["period", "opening_cash_cents", "spent_cents", "revenue_cents", "collected_cents",
                   "receivable_cents", "closing_cash_cents"],
}
INTEGER_FIELDS = {field for fields in FIELDS.values() for field in fields
                  if field.endswith("_cents") or field == "fulfilled"} | {"cash_cents"}


def validate_financing(runtime, rollup_ids):
    from .verification import funding_content_from_public_sources
    expected = funding_content_from_public_sources(runtime.root, runtime.project)
    pending, visited = list(rollup_ids), set()
    while pending:
        identifier = pending.pop()
        if identifier in visited:
            continue
        visited.add(identifier)
        artifact = runtime._artifact(identifier)
        pending.extend(p["id"] for p in artifact["inputs"] if p["type"] == "artifact")
        kind, region, period = artifact["kind"], artifact["region"], artifact["period"]
        if kind not in FIELDS:
            continue
        wanted = expected[kind, region, period]
        if kind == "fulfillment":
            _, joined, _ = runtime.expected_region(region, period)
            by_id = {row["order_id"]: row for row in joined}
            wanted = [{**by_id[row["order_id"]], **row} for row in wanted]
        columns = FIELDS[kind]
        canonical = lambda rows: sorted(tuple(row[c] for c in columns) for row in rows)
        runtime._require(canonical(runtime._rows(artifact)) == canonical(wanted), "CONTENT_MISMATCH",
                         "Financing records do not reconcile with current public business sources")


def execute_funding(runtime, name, a, use_script):
    r = runtime
    r._require(r.project.get("business_program") in {"funded_sales_v1", "obligation_sales_v1", "credit_sales_v2"}, "BUSINESS_PROGRAM",
               "Working-capital operations require a funded sales project")
    region = None

    def artifact(key, kind):
        return r._artifact(a[key], kind, parameter=key)

    def collection(key, kind):
        values = [r._artifact(value, kind, parameter=f"{key}[{i}]") for i, value in enumerate(a[key])]
        r._require(len(values) == len(r.project["regions"]) and
                   {v["region"] for v in values} == set(r.project["regions"]) and
                   len({v["period"] for v in values}) == 1,
                   "REGION_COVERAGE", "Supply one current input per region from the same period")
        return values

    def previous(key, kind, period):
        index = r.project["periods"].index(period)
        if not index:
            r._require(a[key] == "", "PERIOD_DEPENDENCY", "The first period has no preceding balance")
            return None
        prior = artifact(key, kind)
        r._require(prior["period"] == r.project["periods"][index - 1], "PERIOD_DEPENDENCY",
                   "Use the immediately preceding period's current closing record")
        return prior

    if name == "allocate-working-capital":
        summaries = collection("summary_ids", "summary")
        period = summaries[0]["period"]
        treasury = r._source(a["treasury_source_id"], "treasury", parameter="treasury_source_id")
        r._require(a["treasury_source_id"] == r.project["funding"]["treasury_source"],
                   "SOURCE_SCOPE", "Use this project's opening treasury")
        prior = previous("previous_settlement_id", "settlement", period)
        cash = (r._rows(prior)[0]["closing_cash_cents"] + r._rows(prior)[0]["receivable_cents"]
                if prior else r._rows(treasury)[0]["cash_cents"])
        r._require(cash >= 0, "CASH_BALANCE", "Opening cash cannot be negative")
        inputs = r._artifact_refs(a["summary_ids"] + ([a["previous_settlement_id"]] if prior else []))
        if not prior:
            inputs.append(r._source_ref(a["treasury_source_id"]))
        rows = r._compute(name, [[row for s in summaries for row in r._rows(s)]],
                          {"opening_cash_cents": cash}, use_script)
        kind = "allocation"
    elif name == "fulfill-orders":
        joined, allocation = artifact("joined_id", "joined"), artifact("allocation_id", "allocation")
        region, period = joined["region"], joined["period"]
        r._require(allocation["period"] == period, "WORK_PACKAGE_MISMATCH", "Allocation belongs to another period")
        demand = next(r._artifact(value, "summary") for value in allocation["arguments"]["summary_ids"]
                      if r.project["artifacts"][value]["region"] == region)
        demand_join = r._artifact(demand["arguments"]["joined_id"], "joined")
        r._require(sorted(r._rows(demand_join), key=lambda row: row["order_id"]) ==
                   sorted(r._rows(joined), key=lambda row: row["order_id"]),
                   "CONTENT_MISMATCH", "Fulfillment must use the allocated regional demand")
        budget = next(row["budget_cents"] for row in r._rows(allocation) if row["region"] == region)
        options = {"budget_cents": budget}
        if "credit_limits_source" in r.project["funding"]:
            assessment = r._artifact(allocation["arguments"]["credit_assessment_id"], "credit_assessment")
            options["credit_quotas"] = {row["customer_id"]: row["credit_quota_cents"]
                                       for row in r._rows(assessment) if row["region"] == region}
        rows = r._compute(name, [r._rows(joined)], options, use_script)
        inputs = r._artifact_refs([a["joined_id"], a["allocation_id"]])
        kind = "fulfillment"
    elif name == "settle-period":
        fulfilled, allocation = collection("fulfillment_ids", "fulfillment"), artifact("allocation_id", "allocation")
        period = allocation["period"]
        r._require(all(v["period"] == period and v["arguments"]["allocation_id"] == a["allocation_id"]
                       for v in fulfilled), "ALLOCATION_MISMATCH", "Settle fulfillment from one allocation and period")
        terms = r._source(a["terms_source_id"], "collection_terms", parameter="terms_source_id")
        r._require(terms["period"] == period, "WORK_PACKAGE_MISMATCH", "Collection terms belong to another period")
        term = r._rows(terms)[0]
        r._require(0 <= term["numerator"] <= term["denominator"] and term["denominator"] > 0,
                   "COLLECTION_TERMS", "Collection fraction must lie between zero and one")
        options = {**term, "period": period, "opening_cash_cents": r._rows(allocation)[0]["opening_cash_cents"]}
        rows = r._compute(name, [[row for v in fulfilled for row in r._rows(v)]], options, use_script)
        r._require(rows[0]["spent_cents"] <= options["opening_cash_cents"], "CASH_BALANCE", "Spending exceeds available cash")
        inputs = r._artifact_refs(a["fulfillment_ids"] + [a["allocation_id"]]) + [r._source_ref(a["terms_source_id"])]
        kind = "settlement"
    elif name == "close-funded-period":
        settlement = artifact("settlement_id", "settlement")
        period = settlement["period"]
        prior = previous("previous_rollup_id", "rollup", period)
        summaries = []
        for value in settlement["arguments"]["fulfillment_ids"]:
            item = r._artifact(value, "fulfillment")
            realized = [row for row in r._rows(item) if row["fulfilled"]]
            summaries.append({"region": item["region"], "period": period, "order_count": len(realized),
                              "usd_cents": sum(row["usd_cents"] for row in realized)})
        rows = r._compute("rollup-period", [summaries, r._rows(prior) if prior else []], {}, False)
        inputs = r._artifact_refs([a["settlement_id"]] + ([a["previous_rollup_id"]] if prior else []))
        kind = "rollup"
    else:
        raise ValueError("Unknown funded sales operation")
    return r._commit(name, a, kind, rows, inputs, region, period)
