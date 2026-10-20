"""Cross-region customer credit, derived from outstanding invoice contents."""
from collections import defaultdict


def assess(r, a):
    from .obligations import prior_chain, period_number
    joined = [r._artifact(key, "joined", parameter=f"joined_ids[{i}]")
              for i, key in enumerate(a["joined_ids"])]
    r._require(len(joined) == len(r.project["regions"]) and
               {v["region"] for v in joined} == set(r.project["regions"]) and
               len({v["period"] for v in joined}) == 1,
               "REGION_COVERAGE", "Assess exactly one current demand table per region and period")
    period = joined[0]["period"]
    chain = prior_chain(r, a["previous_settlement_id"], period)
    selected = [r._artifact(key, "obligation_settlement", parameter=f"settlement_ids[{i}]")
                for i, key in enumerate(a["settlement_ids"])]
    required = {book["artifact_id"] for book in chain if any(row["record_type"] == "invoice" and
                period_number(row["due_period"]) > period_number(period) for row in r._rows(book))}
    r._require(len(a["settlement_ids"]) == len(set(a["settlement_ids"])) and
               required <= set(a["settlement_ids"]) <= {book["artifact_id"] for book in chain},
               "CREDIT_LEDGER_COVERAGE", "Select historical ledgers covering outstanding invoices from this predecessor chain")
    r._require(a["limits_source_id"] == r.project["funding"]["credit_limits_source"],
               "SOURCE_SCOPE", "Use the project customer-credit source")
    source = r._source(a["limits_source_id"], "credit_limits", parameter="limits_source_id")
    limits = r._rows(source)
    r._require(len({v["customer_id"] for v in limits}) == len(limits) and
               all(type(v["credit_limit_cents"]) is int and v["credit_limit_cents"] >= 0 for v in limits),
               "CREDIT_LIMIT", "Customer limits must be unique and nonnegative")
    demand, exposure = defaultdict(int), defaultdict(int)
    for item in joined:
        for row in r._rows(item):
            demand[row["customer_id"], item["region"]] += row["usd_cents"]
    r._require({key[0] for key in demand} <= {row["customer_id"] for row in limits},
               "CREDIT_LIMIT", "Every current customer needs a public limit")
    for book in selected:
        for row in r._rows(book):
            if row["record_type"] == "invoice" and period_number(row["due_period"]) > period_number(period):
                exposure[row["customer_id"]] += row["amount_cents"]
    rows = []
    for limit in sorted(limits, key=lambda row: row["customer_id"]):
        customer = limit["customer_id"]
        headroom = max(0, limit["credit_limit_cents"]-exposure[customer])
        regions = sorted(r.project["regions"])
        total = sum(demand[customer, region] for region in regions)
        quota = {region: headroom*demand[customer, region]//total if total else 0 for region in regions}
        remainder = headroom-sum(quota.values()) if total else 0
        for region in regions:
            if remainder and demand[customer, region]:
                quota[region] += 1
                remainder -= 1
            rows.append({"period": period, "region": region, "customer_id": customer,
                         "credit_limit_cents": limit["credit_limit_cents"],
                         "outstanding_cents": exposure[customer], "credit_quota_cents": quota[region]})
    return r._commit("assess-credit", a, "credit_assessment", rows,
                     r._artifact_refs(a["joined_ids"]+list(dict.fromkeys(a["settlement_ids"] +
                         ([a["previous_settlement_id"]] if chain else [])))) +
                     [r._source_ref(a["limits_source_id"])], period=period)
