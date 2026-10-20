from datetime import datetime, timedelta
import json
import sys


def parse(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timezone required")
    return result


def allocate(inputs):
    requests = inputs["requests"]
    if len({r["post_id"] for r in requests}) != len(requests):
        raise ValueError("Duplicate post")
    occupied = [(r["account_id"], parse(r["at"])) for r in inputs.get("occupied", [])]
    now = parse(inputs["now"])
    result = []
    for request in sorted(requests, key=lambda r: (parse(r["latest"]), parse(r["earliest"]), r["post_id"])):
        target = max(now, parse(request["earliest"]))
        for _ in range(len(occupied) + 1):
            conflicts = [t for account, t in occupied if account == request["account_id"] and abs((target - t).total_seconds()) < 3600]
            if not conflicts:
                break
            target = max(conflicts) + timedelta(hours=1)
        if target > parse(request["latest"]):
            raise ValueError("No slot found by greedy proposal")
        occupied.append((request["account_id"], target))
        result.append({"post_id": request["post_id"], "scheduled_for": target.isoformat().replace("+00:00", "Z")})
    return {"schedule": result, "reservations_created": False}


if __name__ == "__main__":
    try:
        print(json.dumps(allocate(json.load(sys.stdin)), allow_nan=False))
    except (ValueError, TypeError, KeyError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
