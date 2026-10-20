"""Public scheduled-funding arithmetic; the evaluator has its own reconstruction."""

from copy import deepcopy


def apply_liquidity(brief, parents, account):
    result = deepcopy(account)
    policy = brief["content"]["funding_policy"]
    ready = max((p["finish_day"] for p in parents), default=0)
    upfront = policy["upfront_grant_cents"]
    if type(upfront) is not int or not 0 <= upfront <= policy["local_grant_cents"]:
        raise ValueError("Invalid upfront grant")
    tranches = [{"available_day": 0, "amount_cents": upfront},
        {"available_day": ready + policy["grant_balance_lag_days"], "amount_cents": policy["local_grant_cents"] - upfront}]
    tranches = [r for r in tranches if r["amount_cents"]]
    if "grant_installments" in policy:
        installments = policy["grant_installments"]
        if not installments or any(type(r["amount_cents"]) is not int or r["amount_cents"] <= 0 or
                                   type(r["ready_offset_days"]) is not int or r["ready_offset_days"] < 0 for r in installments):
            raise ValueError("Invalid grant installments")
        if sum(r["amount_cents"] for r in installments) != policy["local_grant_cents"] - upfront:
            raise ValueError("Grant installments must conserve the balance")
        tranches = ([{"available_day": 0, "amount_cents": upfront}] if upfront else []) + [
            {"available_day": ready + r["ready_offset_days"], "amount_cents": r["amount_cents"]} for r in installments]
    for row, parent in zip(result["incoming_transfers"], sorted(parents, key=lambda p: p["node_id"])):
        transfer = next(t for t in parent["funding"]["outgoing_transfers"] if t["node_id"] == brief["content"]["node_id"] and t["maturity_phase"] == brief["content"]["phase"])
        row["available_day"] = transfer["available_day"]
    receipts = tranches + result["incoming_transfers"]
    days = sorted({ready} | {r["available_day"] for r in receipts if r["available_day"] >= ready})
    start = next((d for d in days if sum(r["amount_cents"] for r in receipts if r["available_day"] <= d) >= result["local_spend_cents"] + policy["minimum_retained_cents"]), None)
    if start is None:
        raise ValueError("Scheduled funding cannot cover work and reserve")
    available = sum(r["amount_cents"] for r in receipts if r["available_day"] <= start)
    all_available = max((r["available_day"] for r in receipts if r["amount_cents"]), default=0)
    finish = start + sum(i["work_days"] for i in brief["content"]["line_items"])
    for transfer, consumer in zip(result["outgoing_transfers"], policy["consumers"]):
        transfer["available_day"] = max(finish, all_available) + consumer["release_lag_days"]
    result.update(local_tranches=tranches, operational_ready_day=ready, funded_start_day=start,
        available_at_start_cents=available, pending_at_start_cents=result["available_cents"] - available,
        all_income_available_day=all_available)
    return result
