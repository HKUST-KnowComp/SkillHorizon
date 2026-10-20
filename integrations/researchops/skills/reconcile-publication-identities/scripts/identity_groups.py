"""Group supplied acquisition evidence; never mutate or inspect an environment."""
import json
import sys


def normalize(value):
    value = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if value.startswith(prefix):
            return value[len(prefix):].strip()
    return value


payload = json.load(sys.stdin)
groups = {}
for row in payload["records"]:
    key = normalize(row["doi"])
    if not key:
        raise ValueError("Missing identity evidence")
    groups.setdefault(key, []).append(row["paper_id"])
print(json.dumps({"groups": [{"source_key": key, "paper_ids": sorted(ids)} for key, ids in sorted(groups.items())]}))
