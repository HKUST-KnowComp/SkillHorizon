import json
from fractions import Fraction
import sys


def compare(inputs):
    rows = inputs["items"]
    if not rows or len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Supply distinct nonempty items")
    scores = []
    for row in rows:
        if not isinstance(row["id"], str) or not row["id"] or any(type(row[k]) is not int or row[k] < 0 for k in ("clicks", "impressions")):
            raise ValueError("Invalid item identity or count")
        if row["clicks"] > row["impressions"]:
            raise ValueError("Clicks exceed impressions")
        value = Fraction(row["clicks"], row["impressions"]) if row["impressions"] else Fraction(0)
        scores.append((row["id"], value))
    scores.sort(key=lambda pair: (-pair[1], pair[0]))
    return {"winning_id": scores[0][0], "scores": [{"id": i, "numerator": r.numerator, "denominator": r.denominator} for i, r in scores], "state_verified": False}


if __name__ == "__main__":
    try:
        print(json.dumps(compare(json.load(sys.stdin)), allow_nan=False))
    except (ValueError, TypeError, KeyError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
