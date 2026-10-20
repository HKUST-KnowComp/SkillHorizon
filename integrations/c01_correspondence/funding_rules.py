"""Counterpart checks, not actor-visible computed dispatch answers."""


def funding(node, brief, parents):
    policy = brief["content"]["funding_policy"]
    incoming = []
    for parent in sorted(parents, key=lambda p: p["node_id"]):
        candidates = [t for t in parent["funding"]["outgoing_transfers"] if t["node_id"] == node["node_id"] and t["maturity_phase"] == node["phase"]]
        if len(candidates) != 1:
            raise ValueError("Exact earmarked funding transfer required")
        incoming.append({"source_node_id": parent["node_id"], "source_dispatch_id": parent["dispatch_message_id"], "amount_cents": candidates[0]["amount_cents"]})
    received = sum(r["amount_cents"] for r in incoming)
    available = policy["local_grant_cents"] + received
    spend = sum(r["quantity"] * r["unit_cost_cents"] for r in brief["content"]["line_items"])
    free = available - spend - policy["minimum_retained_cents"]
    if free < 0:
        raise ValueError("Funding cannot cover declared local work and retained reserve")
    denominator = sum(c["weight"] for c in policy["consumers"])
    outgoing = [{"node_id": c["node_id"], "maturity_phase": c["maturity_phase"], "amount_cents": free * c["weight"] // denominator} for c in policy["consumers"]]
    result = {"local_grant_cents": policy["local_grant_cents"], "incoming_transfers": incoming, "incoming_cents": received,
        "available_cents": available, "local_spend_cents": spend, "outgoing_transfers": outgoing,
        "retained_cents": available - spend - sum(c["amount_cents"] for c in outgoing)}
    if "upfront_grant_cents" in policy:
        from .liquidity_rules import apply_liquidity
        result = apply_liquidity(brief, parents, result)
    return result
