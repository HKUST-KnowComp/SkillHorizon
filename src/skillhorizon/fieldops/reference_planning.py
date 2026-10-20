"""Bounded construction-only planning from public state, never exposed as a tool."""
from .procureops import _committed, delivery_schedule


def purchase_lines(project, budgets, alternate=False):
    spend, committed, used = _committed(project)
    suppliers = list(project["suppliers"])
    available = tuple(project["suppliers"][s]["capacity"] - used[s] for s in suppliers)
    if min(available) < 0:
        raise ValueError("Cancel overcommitted supply before planning")
    requests = list(project["requests"])
    pending = [{"request_id": row["request_id"], "supplier_id": row["supplier_id"],
                "quantity": row["quantity"] - row["received"] - row["canceled"]}
               for order in project["orders"].values() for row in order["lines"]
               if row["quantity"] > row["received"] + row["canceled"]]
    visited = 0

    def solve(index, capacity, chosen):
        nonlocal visited
        visited += 1
        if visited > 100000:
            raise ValueError("Construction allocation search exceeded its explicit 100000-state bound")
        if index == len(requests):
            return chosen if delivery_schedule(project, [*pending, *chosen]) is not None else None
        key = requests[index]
        request = project["requests"][key]
        eligible = sorted(request.get("eligible_suppliers", suppliers),
                          key=lambda s: project["suppliers"][s]["unit_price_cents"], reverse=alternate)
        demand = request["quantity"] - committed[key]

        def distribute(position, remaining, caps, rows, cost):
            if position == len(eligible):
                if remaining == 0:
                    return solve(index + 1, caps, [*chosen, *rows])
                return None
            supplier = eligible[position]
            slot = suppliers.index(supplier)
            price = project["suppliers"][supplier]["unit_price_cents"]
            for amount in range(min(remaining, caps[slot]), -1, -1):
                if spend[key] + cost + amount * price > budgets[key]:
                    continue
                updated = list(caps)
                updated[slot] -= amount
                extra = [{"request_id": key, "supplier_id": supplier, "quantity": amount}] if amount else []
                result = distribute(position + 1, remaining - amount, tuple(updated), [*rows, *extra], cost + amount * price)
                if result is not None:
                    return result
            return None
        return distribute(0, demand, capacity, [], 0)

    result = solve(0, available, [])
    if result is None:
        raise ValueError("No budget/capacity/deadline-feasible allocation exists in the public state")
    return result
