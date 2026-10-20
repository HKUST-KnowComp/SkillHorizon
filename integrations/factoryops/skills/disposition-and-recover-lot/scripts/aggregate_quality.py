"""Aggregate exact per-unit QC coverage from supplied observations only."""

import json
import math
import sys


def compute(data):
    units, metrics = data["unit_ids"], data["metric_names"]
    if not units or not metrics or len(units) != len(set(units)) or len(metrics) != len(set(metrics)):
        raise ValueError("Invalid expected unit or metric set")
    expected = {(u, m) for u in units for m in metrics}
    rows = {}
    for row in data["checks"]:
        pair = (row["unit_id"], row["metric_name"])
        if pair not in expected or pair in rows or any(row[k] != data[k] for k in ("run_id", "release_id", "profile_id")):
            raise ValueError("QC identity or coverage mismatch")
        values = [row[k] for k in ("expected_min", "measured_value", "expected_max")]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values) or values[0] > values[2]:
            raise ValueError("Invalid QC values")
        rows[pair] = values[0] <= values[1] <= values[2]
    if set(rows) != expected:
        raise ValueError("Missing mandatory observation")
    accepted = [u for u in units if all(rows[u, m] for m in metrics)]
    failed = [u for u in units if u not in accepted]
    return {"accepted_unit_ids": accepted, "quarantined_unit_ids": failed, "produced_quantity": len(units), "defective_unit_count": len(failed)}


if __name__ == "__main__":
    print(json.dumps(compute(json.load(sys.stdin)), allow_nan=False))
