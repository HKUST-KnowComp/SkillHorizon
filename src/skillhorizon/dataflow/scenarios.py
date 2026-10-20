"""Named business scenarios, applied only while constructing a fresh world."""

SCENARIOS = ("credit_freeze", "receivables_rollover", "mixed_restatements", "credit_rollover", "credit_horizon")


def validate(scenario, *, business_program, periods, family, revision, event_kind, event_program,
             credit_event_customer=None, credit_event_trigger_period=None):
    custom_credit = credit_event_customer is not None or credit_event_trigger_period is not None
    if custom_credit and scenario not in {"credit_freeze", "credit_rollover", "credit_horizon"}:
        raise ValueError("Credit event parameters require a credit-correction scenario")
    if scenario is None:
        return
    minimum = 3 if scenario in {"credit_horizon", "receivables_rollover"} else 8
    if credit_event_trigger_period is not None:
        minimum = 3
    if (scenario not in SCENARIOS or business_program != "credit_sales_v2"
            or type(periods) is not int or periods < minimum):
        raise ValueError(f"DataOps scenario requires credit_sales_v2 and at least {minimum} periods")
    if revision or event_kind or event_program:
        raise ValueError("Named scenarios own their event schedule; do not combine event overrides")
    if scenario == "mixed_restatements" and family != "heterogeneous_merge":
        raise ValueError("mixed_restatements requires heterogeneous_merge")
    if credit_event_customer is not None and credit_event_customer not in {"C01", "C02", "C03"}:
        raise ValueError("Credit correction must target an authorized customer C01..C03")
    if credit_event_trigger_period is not None and (type(credit_event_trigger_period) is not int
            or not 2 <= credit_event_trigger_period < periods):
        raise ValueError("Credit trigger needs a preceding period and at least one following period")


def configure(root, p, scenario, *, credit_event_customer=None, credit_event_trigger_period=None):
    if scenario is None:
        return
    from .runtime import read_table_file, write_table
    if scenario in {"credit_rollover", "credit_horizon"}:
        project_id, goal = p["project_id"], p["goal"]
        configure(root, p, "receivables_rollover")
        p["project_id"], p["goal"] = project_id, goal
        configure(root, p, "credit_freeze", credit_event_customer=credit_event_customer,
                  credit_event_trigger_period=credit_event_trigger_period)
        if scenario == "credit_horizon":
            p["scheduled_changes"][0]["trigger_period"] = f"P{credit_event_trigger_period or 2:02d}"
            source = p["sources"][p["funding"]["invoice_terms_source"]]
            rows = read_table_file(root/source["path"])
            for row in rows:
                region = p["regions"].index(row["region"])
                customer = int(row["customer_id"][1:])-1
                row["delay_periods"] = (1, 2, 3)[(region+customer) % 3]
            write_table(root/source["path"], rows, list(rows[0]))
        p["project_id"] = project_id + "-" + scenario
        p["business_scenario"] = scenario
        p["goal"] += " Customer payment dates and dispute routes differ by region; use each invoice's original terms."
        return
    p["business_scenario"] = scenario
    p["project_id"] += "-" + scenario
    p["goal"] += (" Settlement milestones may reveal corrections to older business sources. "
                  "Recompute affected accounting from current source revisions; preserve unaffected work. "
                  "A correction restates this synthetic scenario, not a new cash transfer.")
    if scenario == "credit_freeze":
        p["scheduled_changes"] = [{"kind": "credit_limit_correction", "source_id": "customer-credit-limits",
            "customer_id": credit_event_customer or "C01", "region": None, "period": "P01",
            "trigger_period": f"P{credit_event_trigger_period or 4:02d}",
            "trigger": "period_settlement_ready", "applied": False}]
    elif scenario == "receivables_rollover":
        source = p["sources"][p["funding"]["invoice_terms_source"]]
        rows = read_table_file(root/source["path"])
        for row in rows:
            region = p["regions"].index(row["region"])
            customer = int(row["customer_id"][1:])-1
            row["delay_periods"] = (1, 3, 6)[(region+customer) % 3]
            row["payment_route"] = "disputed" if (region+customer) % 2 else "normal"
        write_table(root/source["path"], rows, list(rows[0]))
        p["goal"] += " Customer payment dates and dispute routes differ by region; use each invoice's original terms."
    else:
        p["scheduled_changes"] = [
            {"kind": "rate_change", "source_id": "exchange-rates-P02", "region": None,
             "period": "P02", "trigger_period": "P04", "trigger": "period_settlement_ready", "applied": False},
            {"kind": "consent_revocation", "source_id": "consent-R02", "customer_id": "C02", "region": "R02",
             "period": "P01", "trigger_period": "P06", "trigger": "period_settlement_ready", "applied": False},
        ]
