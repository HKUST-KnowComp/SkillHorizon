"""Find a candidate equipment interval using supplied reservation data only."""

from datetime import datetime, timedelta, timezone
import json
import sys


def date(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timezone required")
    return result.astimezone(timezone.utc)


def compute(data):
    duration = data["duration_seconds"]
    if type(duration) is not int or duration <= 0:
        raise ValueError("Positive integer duration required")
    current = date(data["earliest"])
    intervals = []
    for row in data["reservations"]:
        if any(row[k] == data["resources"][k] for k in ("cell_id", "tool_id", "fixture_id")):
            start, end = date(row["scheduled_start"]), date(row["scheduled_end"])
            if start >= end:
                raise ValueError("Invalid reservation interval")
            intervals.append((start, end))
    for start, end in sorted(intervals):
        if current + timedelta(seconds=duration) <= start:
            break
        if current < end:
            current = end
    return {"start_at": current.isoformat().replace("+00:00", "Z"), "end_at": (current + timedelta(seconds=duration)).isoformat().replace("+00:00", "Z")}


if __name__ == "__main__":
    print(json.dumps(compute(json.load(sys.stdin)), allow_nan=False))
