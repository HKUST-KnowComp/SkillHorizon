"""Public deterministic table operations, also shipped with optional skill scripts."""

from decimal import Decimal
import sqlite3


def money(value):
    cents = Decimal(str(value)) * 100
    if not cents.is_finite() or cents != cents.to_integral_value() or cents < 0:
        raise ValueError("Amounts must be nonnegative with at most two decimal places")
    return int(cents)


def query(tables, sql):
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        for name, (columns, rows) in tables.items():
            db.execute(f"CREATE TABLE {name} ({columns})")
            if rows:
                db.executemany(f"INSERT INTO {name} VALUES ({','.join('?' for _ in rows[0])})", rows)
        return [dict(row) for row in db.execute(sql)]


def compute(operation, tables, options):
    if operation == "allocate-working-capital":
        summaries = sorted(tables[0], key=lambda row: row["region"])
        cash = options["opening_cash_cents"]
        total = sum(row["usd_cents"] for row in summaries)
        budgets = [cash * row["usd_cents"] // total if total else 0 for row in summaries]
        positive = [i for i, row in enumerate(summaries) if row["usd_cents"] > 0]
        for i in positive[:cash - sum(budgets)] if total else []:
            budgets[i] += 1
        return [{"region": row["region"], "period": row["period"], "demand_usd_cents": row["usd_cents"],
                 "budget_cents": budget, "opening_cash_cents": cash} for row, budget in zip(summaries, budgets)]
    if operation == "fulfill-orders":
        remaining = options["budget_cents"]
        credit = dict(options["credit_quotas"]) if "credit_quotas" in options else None
        output = []
        for row in sorted(tables[0], key=lambda row: row["order_id"]):
            cost = (row["usd_cents"] + 1) // 2
            fulfilled = int(cost <= remaining and (credit is None or row["usd_cents"] <= credit[row["customer_id"]]))
            spent = cost if fulfilled else 0
            remaining -= spent
            if fulfilled and credit is not None:
                credit[row["customer_id"]] -= row["usd_cents"]
            output.append({**row, "fulfilled": fulfilled, "cost_cents": spent})
        return output
    if operation == "settle-period":
        spent = sum(row["cost_cents"] for row in tables[0])
        revenue = sum(row["usd_cents"] for row in tables[0] if row["fulfilled"])
        collected = revenue * options["numerator"] // options["denominator"]
        return [{"period": options["period"], "opening_cash_cents": options["opening_cash_cents"],
                 "spent_cents": spent, "revenue_cents": revenue, "collected_cents": collected,
                 "receivable_cents": revenue - collected,
                 "closing_cash_cents": options["opening_cash_cents"] - spent + collected}]
    if operation == "harmonize-orders":
        mapping = options["column_map"]
        keys = {"order_id", "customer_id", "region", "period", "currency", "amount", "record_revision"}
        if set(mapping) != keys or options["amount_unit"] not in {"major", "minor"}:
            raise ValueError("Supply the complete canonical column mapping and major/minor amount unit")
        if options["conflict_policy"] != "latest_revision":
            raise ValueError("Conflicting feed records are resolved by latest_revision")
        identities = {(r["external_customer"], r["region"]): r["customer_id"] for r in tables[1]}
        chosen = {}
        for raw in tables[0]:
            value = {k: str(raw[column]).strip() for k, column in mapping.items()}
            revision = int(value.pop("record_revision"))
            amount = value.pop("amount")
            value["amount_cents"] = money(amount) if options["amount_unit"] == "major" else int(amount)
            if value["amount_cents"] < 0:
                raise ValueError("Negative sales value")
            value["customer_id"] = identities[(value["customer_id"], value["region"])]
            old = chosen.get(value["order_id"])
            if old and old[0] == revision and old[1] != value:
                raise ValueError("Same-version source conflict")
            if old is None or revision > old[0]:
                chosen[value["order_id"]] = (revision, value)
        return [chosen[key][1] for key in sorted(chosen)]
    if operation == "authorize":
        keys = options["join_keys"]
        if len(keys) != 2 or set(keys) != {"customer_id", "region"}:
            raise ValueError("Authorization requires both customer_id and region")
        allowed = {tuple(row[k] for k in keys) for row in tables[1]}
        return [row for row in tables[0] if tuple(row[k] for k in keys) in allowed]
    if operation == "convert":
        if options["rounding"] != "half_up":
            raise ValueError("The release contract requires per-order half_up rounding")
        rates = {r["currency"]: r for r in tables[1]}
        result = []
        for row in tables[0]:
            rate = rates[row["currency"]]
            numerator, denominator = int(rate["numerator"]), int(rate["denominator"])
            usd = (int(row["amount_cents"]) * numerator * 2 + denominator) // (2 * denominator)
            result.append({k: row[k] for k in ("order_id", "customer_id", "region", "period")} | {"usd_cents": usd})
        return sorted(result, key=lambda r: r["order_id"])
    if operation == "rollup-batch":
        totals, output = {}, []
        for row in sorted(tables[0], key=lambda r: (r["period"], r["region"])):
            old = totals.setdefault(row["region"], [0, 0])
            old[0] += row["order_count"]
            old[1] += row["usd_cents"]
            output.append({**row, "cumulative_order_count": old[0], "cumulative_usd_cents": old[1]})
        return output
    if operation == "normalize-orders":
        if options["deduplication"] not in {"first", "last"}:
            raise ValueError("deduplication must be first or last")
        by_id = {}
        for raw in tables[0]:
            row = {k: str(raw[k]).strip() for k in ("order_id", "customer_id", "region", "period", "currency")}
            row["amount_cents"] = money(raw["amount"])
            previous = by_id.get(row["order_id"])
            if previous is not None and previous != row:
                raise ValueError("Conflicting duplicate orders require source correction")
            if previous is None or options["deduplication"] == "last":
                by_id[row["order_id"]] = row
        return sorted(by_id.values(), key=lambda row: row["order_id"])
    if operation == "prepare-consent":
        return sorted([{"customer_id": r["customer_id"], "region": r["region"]}
                       for r in tables[0] if r["analytics_allowed"] == "yes"], key=lambda r: r["customer_id"])
    if operation == "prepare-rates":
        rows = [{"currency": r["currency"], "numerator": int(r["numerator"]),
                 "denominator": int(r["denominator"])} for r in tables[0]]
        if any(r["numerator"] <= 0 or r["denominator"] <= 0 for r in rows):
            raise ValueError("Exchange rates must be positive")
        return sorted(rows, key=lambda r: r["currency"])
    if operation == "join-orders":
        orders, consent, rates = tables
        missing = {r["currency"] for r in orders} - {r["currency"] for r in rates}
        if missing:
            raise ValueError("Missing exchange rate: " + ", ".join(sorted(missing)))
        return query({
            "orders": ("order_id TEXT, customer_id TEXT, region TEXT, period TEXT, currency TEXT, amount_cents INTEGER",
                       [(r["order_id"], r["customer_id"], r["region"], r["period"], r["currency"], int(r["amount_cents"])) for r in orders]),
            "consent": ("customer_id TEXT, region TEXT", [(r["customer_id"], r["region"]) for r in consent]),
            "rates": ("currency TEXT, numerator INTEGER, denominator INTEGER",
                      [(r["currency"], int(r["numerator"]), int(r["denominator"])) for r in rates]),
        }, """SELECT o.order_id, o.customer_id, o.region, o.period,
                   CAST((o.amount_cents * r.numerator * 2 + r.denominator) / (2 * r.denominator) AS INTEGER) AS usd_cents
                FROM orders o JOIN consent c ON o.customer_id=c.customer_id AND o.region=c.region
                JOIN rates r ON o.currency=r.currency ORDER BY o.order_id""")
    if operation == "summarize-region":
        rows = query({"sales": ("period TEXT, region TEXT, usd_cents INTEGER",
                    [(r["period"], r["region"], int(r["usd_cents"])) for r in tables[0]])},
                     "SELECT period, region, COUNT(*) AS order_count, SUM(usd_cents) AS usd_cents FROM sales GROUP BY period, region")
        return rows or [{"period": options["period"], "region": options["region"], "order_count": 0, "usd_cents": 0}]
    if operation == "rollup-period":
        summaries, previous = tables
        old = {r["region"]: r for r in previous}
        return [{**r, "order_count": int(r["order_count"]), "usd_cents": int(r["usd_cents"]),
                 "cumulative_order_count": int(r["order_count"]) + int(old.get(r["region"], {}).get("cumulative_order_count", 0)),
                 "cumulative_usd_cents": int(r["usd_cents"]) + int(old.get(r["region"], {}).get("cumulative_usd_cents", 0))}
                for r in sorted(summaries, key=lambda r: r["region"])]
    raise ValueError("Unsupported table operation")
