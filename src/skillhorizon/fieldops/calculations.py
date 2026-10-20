"""Public arithmetic helpers shipped in two optional scripts."""

def calculate(operation, data):
    if operation == "test-device":
        residual = max(0, data["severity"] - data["repair_gain"]) + data["load"] // 25
        return {"residual": residual, "delivered_pressure": data["rated_pressure"] - residual,
                "test_load": data["load"]}
    if operation == "plan-purchase":
        rows = [{**line, "cost_cents": line["quantity"] * data["prices"][line["supplier_id"]]}
                for line in data["lines"]]
        return {"lines": rows, "total_cost_cents": sum(r["cost_cents"] for r in rows)}
    raise ValueError("Unknown public arithmetic operation")
