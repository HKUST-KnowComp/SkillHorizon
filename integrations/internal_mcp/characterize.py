"""Offline corpus audit. Never copies hidden structure into actor messages."""

import argparse
from pathlib import Path

from .io import read_json, write_json
from .structure import business_nodes, graph_metrics, instance_identity, trace_profile

ROOT = Path(__file__).resolve().parents[2]
SCOPES = {
    "C09": "Passenger legs, origin baggage obligations and transfer joins; physical freight legs and capacity conflicts not expanded. Cross-group statistic excludes multi-cohort transfer nodes.",
    "C25": "Current initial requirement parent publications; origin rights and amendments are extra constraints, not extra graph nodes.",
    "C06": "Declared campaign learning parents; history-dependent audience overlap is not a frozen data edge.",
    "C47": "Logical reserved workload jobs across business units; time coverage and shared budgets are additional constraints.",
    "C50": "Material order BOM parents; per-unit fan-in, rework and equipment conflicts are additional constraints.",
    "C01": "Dispatch parent acknowledgements across cases; installments, ACL and amendment recovery are additional constraints.",
    "C11": "Canonical initial source calibration graph; later corrections, section joins and committed reviews are additional constraints. No cross-group assignment.",
}


def characterize(output):
    if output.exists():
        raise ValueError("Use a fresh characterization report")
    rows = []
    catalog = read_json(Path(__file__).with_name("scenario_catalog.json"))
    for domain in catalog["environments"]:
        identifier = domain["environment_id"]
        for candidate in read_json(ROOT / domain["qualification"] / "CANDIDATES.json"):
            bundle = Path(candidate["bundle"])
            public, world = read_json(bundle / "public/task.json"), read_json(bundle / "checkpoint/world.json")
            nodes = business_nodes(identifier, public, world)
            findings, skill_files = [], 0
            for skill in public["skills"]:
                for file in skill["files"]:
                    path = bundle / "public/skills" / skill["name"] / file
                    text = path.read_text()
                    skill_files += 1
                    # Exact instance IDs are a high-precision check, not semantic leakage certification.
                    identifiers = {public["task_id"], *(n["id"] for n in nodes if len(n["id"]) >= 12)}
                    hits = sorted(value for value in identifiers if value in text)
                    if hits:
                        findings.append({"skill": skill["name"], "file": file, "instance_ids": hits})
            rows.append({"identity": instance_identity(identifier, public), "family": candidate["family"],
                "bundle": str(bundle), "structural_scope": SCOPES[identifier],
                "business_graph": graph_metrics(nodes), "business_nodes": nodes,
                "reference_trace": candidate["trace"],
                "reference": trace_profile(read_json(Path(candidate["trace"]))),
                "skill_package": {"skills": len(public["skills"]), "files": skill_files,
                    "exact_instance_id_findings": findings, "semantic_leakage_review": "not_established"}})
    result = {"scope": "construction_development_offline_characterization", "provider_calls": 0,
        "model_behavior_evidence": False, "rows": rows,
        "comparison_limit": "Node granularity differs by domain. Do not rank difficulty using raw graph depth or reference calls alone."}
    write_json(output, result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    result = characterize(parser.parse_args().output)
    for row in result["rows"]:
        print(row["identity"]["environment_id"], row["family"], row["business_graph"]["nodes"],
              row["business_graph"]["longest_path_nodes"], row["reference"]["primitive_business_calls"],
              len(row["skill_package"]["exact_instance_id_findings"]))
