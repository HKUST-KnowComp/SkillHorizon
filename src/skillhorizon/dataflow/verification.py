"""Offline release-content cross-check using rational arithmetic and public CSVs.

No runtime, table_ops, reference trajectory or stored answer is imported here.
Lineage and procedural acceptance remain separate runtime checks.
"""

import csv
from fractions import Fraction
from pathlib import Path

from ..util import load_json

RELEASE_FIELDS = {"region", "period", "order_count", "usd_cents",
                  "cumulative_order_count", "cumulative_usd_cents"}


def _rows(root, entity):
    path = (root / entity["path"]).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Source or publication path is outside the workspace")
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def release_from_public_sources(root, *, project=None, funding_artifacts=None, obligation_accounting=None):
    root = Path(root)
    project = load_json(root / "project.json") if project is None else project
    sources = project["sources"]
    packages = {(w["region"], w["period"]): w for w in project["work_packages"]}
    required = {(r, p) for r in project["regions"] for p in project["periods"]}
    if len(packages) != len(project["work_packages"]) or set(packages) != required:
        raise ValueError("Invalid work-package coverage")
    result = []
    authorized_orders = {}
    authorized_details = {}
    running = {region: [0, 0] for region in project["regions"]}
    for period in project["periods"]:
        for region in project["regions"]:
            package = packages[region, period]
            consent = sources[package["consent_source"]]
            rate_source = sources[package["rates_source"]]
            source = sources[package["orders_source"]]
            if consent["kind"] != "consent" or rate_source["kind"] != "rates":
                raise ValueError("Wrong public source kind")
            allowed = {(row["customer_id"], row["region"]) for row in _rows(root, consent)
                       if row["analytics_allowed"] == "yes"}
            rates = {}
            for row in _rows(root, rate_source):
                numerator, denominator = int(row["numerator"]), int(row["denominator"])
                if numerator <= 0 or denominator <= 0 or row["currency"] in rates:
                    raise ValueError("Invalid exchange rate")
                rates[row["currency"]] = Fraction(numerator, denominator)
            raw = _rows(root, source)
            unit = "major"
            if source["kind"] == "external_orders":
                schema = source["business_schema"]
                unit = schema["amount_unit"]
                if unit not in {"major", "minor"} or schema["conflict_policy"] != "latest_revision":
                    raise ValueError("Unsupported external source rule")
                mapping = {row["external_customer"]: row["customer_id"]
                           for row in _rows(root, sources[schema["key_map_source_id"]]) if row["region"] == region}
                latest = {}
                for row in raw:
                    normalized = {key: row[column].strip() for key, column in schema["column_map"].items()}
                    normalized["customer_id"] = mapping[normalized["customer_id"]]
                    version = int(normalized.pop("record_revision"))
                    previous = latest.get(normalized["order_id"])
                    if previous and previous[0] == version and previous[1] != normalized:
                        raise ValueError("Conflicting source revisions")
                    if previous is None or version > previous[0]:
                        latest[normalized["order_id"]] = (version, normalized)
                raw = [value[1] for value in latest.values()]
            elif source["kind"] != "orders":
                raise ValueError("Wrong order source kind")
            unique = {}
            for row in raw:
                normalized = {key: row[key].strip() for key in ("order_id", "customer_id", "region", "period", "currency")}
                amount = Fraction(row["amount"]) * (100 if unit == "major" else 1)
                if amount < 0 or amount.denominator != 1:
                    raise ValueError("Invalid minor-unit amount")
                normalized["cents"] = int(amount)
                previous = unique.get(normalized["order_id"])
                if previous is not None and previous != normalized:
                    raise ValueError("Conflicting duplicate order")
                unique[normalized["order_id"]] = normalized
            count = total = 0
            authorized_orders[region, period] = []
            authorized_details[region, period] = []
            for row in unique.values():
                if (row["region"], row["period"]) != (region, period):
                    raise ValueError("Order outside its work package")
                if (row["customer_id"], row["region"]) not in allowed:
                    continue
                value = row["cents"] * rates[row["currency"]]
                quotient, remainder = divmod(value.numerator, value.denominator)
                usd = quotient + int(2 * remainder >= value.denominator)
                total += usd
                authorized_orders[region, period].append((row["order_id"], usd))
                authorized_details[region, period].append({**{key: row[key] for key in ("order_id", "region", "period", "customer_id")}, "usd_cents": usd})
                count += 1
            running[region][0] += count
            running[region][1] += total
            result.append({"region": region, "period": period, "order_count": count, "usd_cents": total,
                           "cumulative_order_count": running[region][0], "cumulative_usd_cents": running[region][1]})
    if project.get("business_program") in {"obligation_sales_v1", "credit_sales_v2"}:
        from .obligation_verification import account
        accounting = account(project, authorized_details, lambda identifier: _rows(root, sources[identifier]))
        if obligation_accounting is not None:
            obligation_accounting.update(accounting)
        return accounting["release"]
    if project.get("business_program") == "funded_sales_v1":
        return _funded_release(root, project, authorized_orders, funding_artifacts)
    return result


def _funded_release(root, project, orders, artifacts=None):
    # Independent source-only accounting; never consume runtime allocation/settlement outputs.
    funding = project["funding"]
    cash = int(_rows(root, project["sources"][funding["treasury_source"]])[0]["cash_cents"])
    receivables = 0
    totals = {region: [0, 0] for region in project["regions"]}
    output = []
    for period in project["periods"]:
        cash += receivables
        demand = {region: sum(amount for _, amount in orders[region, period]) for region in project["regions"]}
        total = sum(demand.values())
        budgets = {region: int(Fraction(cash * value, total)) if total else 0 for region, value in demand.items()}
        residual = cash - sum(budgets.values()) if total else 0
        for region in sorted(region for region, value in demand.items() if value > 0):
            if residual:
                budgets[region] += 1
                residual -= 1
        if artifacts is not None:
            artifacts["allocation", None, period] = [
                {"region": region, "period": period, "demand_usd_cents": demand[region],
                 "budget_cents": budgets[region], "opening_cash_cents": cash} for region in sorted(demand)]
        spent = revenue = 0
        for region in project["regions"]:
            remaining = budgets[region]
            count = sales = 0
            realized = []
            for identifier, amount in sorted(orders[region, period]):
                cost = -(-amount // 2)
                accepted = remaining >= cost
                realized.append({"order_id": identifier, "usd_cents": amount, "fulfilled": int(accepted),
                                 "cost_cents": cost if accepted else 0})
                if remaining < cost:
                    continue
                remaining -= cost
                spent += cost
                count += 1
                sales += amount
            if artifacts is not None:
                artifacts["fulfillment", region, period] = realized
            revenue += sales
            totals[region][0] += count
            totals[region][1] += sales
            output.append({"region": region, "period": period, "order_count": count, "usd_cents": sales,
                           "cumulative_order_count": totals[region][0], "cumulative_usd_cents": totals[region][1]})
        terms = _rows(root, project["sources"][funding["terms_sources"][period]])[0]
        fraction = Fraction(int(terms["numerator"]), int(terms["denominator"]))
        if not 0 <= fraction <= 1 or cash < 0:
            raise ValueError("Invalid public financing terms")
        collected = int(revenue * fraction)
        if artifacts is not None:
            artifacts["settlement", None, period] = [{"period": period, "opening_cash_cents": cash,
                "spent_cents": spent, "revenue_cents": revenue, "collected_cents": collected,
                "receivable_cents": revenue - collected, "closing_cash_cents": cash - spent + collected}]
        cash += collected - spent
        receivables = revenue - collected
    return output


def funding_content_from_public_sources(root, project):
    artifacts = {}
    release_from_public_sources(root, project=project, funding_artifacts=artifacts)
    return artifacts


def obligation_accounting_from_sources(root, project):
    result = {}
    release_from_public_sources(root, project=project, obligation_accounting=result)
    return result


def verify_release_content(root):
    root = Path(root)
    project = load_json(root / "project.json")
    base = {"method": "independent_public_csv_rational_v1", "visibility": "offline_only",
            "scope": "Publication schema, row coverage and numeric contents; not a replacement for lineage or procedural checks"}
    publication = project.get("publication")
    if publication is None:
        return {**base, "checked": False, "passed": None, "reason": "no_publication"}
    try:
        artifact = project["artifacts"][publication]
        if artifact["kind"] != "publication":
            raise ValueError("Terminal artifact is not a publication")
        actual = _rows(root, artifact)
        for row in actual:
            if set(row) != RELEASE_FIELDS:
                raise ValueError("Publication schema mismatch")
            for key in RELEASE_FIELDS - {"region", "period"}:
                row[key] = int(row[key])
        expected = release_from_public_sources(root)
        sort = lambda rows: sorted(rows, key=lambda row: (row["region"], row["period"]))
        passed = sort(actual) == sort(expected)
        return {**base, "checked": True, "passed": passed, "reason": "matched" if passed else "content_mismatch"}
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
        return {**base, "checked": True, "passed": False, "reason": "invalid_source_or_publication",
                "error_type": type(exc).__name__}
