"""Prepare deterministic readable keys from supplied public paper metadata."""
import json
import re
import sys


papers = json.load(sys.stdin)["papers"]
entries, used = [], set()
for paper in sorted(papers, key=lambda p: (p["title"], p["paper_id"])):
    name = paper["authors"][0]["name"].split()[-1] if paper["authors"] else "Study"
    base = re.sub("[^A-Za-z0-9]", "", name) or "Study"
    if not base[0].isalpha():
        base = "Study" + base
    base = base[:70] + str(paper["year"])
    key, suffix = base, 1
    while key.lower() in used:
        key = base + "_" + str(suffix)
        suffix += 1
    used.add(key.lower())
    entries.append({"paper_id": paper["paper_id"], "citation_key": key})
print(json.dumps({"entries": entries}))
