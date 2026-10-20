"""Versioned measurements with shared, nonadjacent calibration dependencies."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
from skillhorizon.construction_provenance import observe_call
import random
import shutil

from integrations.internal_mcp.io import read_json, write_json
from .build import build as base_build


FORMAT = "skillhorizon.c11.synthesis_network.v1"


def build(source, output, *, seed=11, sections=3, rounds=4, studies_per_section=1, alias_copies=1, distractors_per_section=1, background=True, construction_profile=None):
    output = Path(output)
    public = observe_call(base_build, source, output, seed=seed, sections=sections, rounds=rounds, studies_per_section=studies_per_section,
        alias_copies=alias_copies, distractors_per_section=distractors_per_section, background=background, construction_profile=construction_profile, synthesis_network=True)
    world = read_json(output / "checkpoint/world.json")
    batches = world["library"]["batches"]
    shared = [next(iter(b["works"])) for b in batches]
    rng = random.Random(seed + 1101)
    topics = [s["topic"] for s in public["sections"]]
    for stage, batch in enumerate(batches):
        for number, (key, work) in enumerate(batch["works"].items()):
            version = work["versions"]["1"]
            if number == 0:
                value = {"role": "calibration", "metric": "allocation_score", "population": "synthetic_items", "offset_points": rng.choice((-2, -1, 1, 2)), "origin_round": stage}
            else:
                unit = ("points", "fraction", "basis_points")[(number + stage) % 3]
                score = rng.randint(30, 85)
                anchor = shared[max(0, stage - (1 + number % 3))]
                value = {"role": "study", "metric": "allocation_score", "population": "synthetic_items", "sample_size": rng.randint(20, 90),
                    "value_numerator": score * (100 if unit == "basis_points" else 1), "value_denominator": 100 if unit == "fraction" else 1,
                    "unit": unit, "calibration_doi": anchor, "origin_round": stage}
                if version["metadata"]["study_type"] == "commentary" or (stage == 2 and number == len(batch["works"]) - 2):
                    value.update(metric="latency", unit="milliseconds")
                version["passages"].append({"passage_id": "calibration-reference", "text": "This synthetic measurement uses the calibration identified by DOI " + anchor + ".", "cited_dois": [anchor]})
                if number == 1:
                    # A genuinely shared empirical study must be counted once in the global synthesis.
                    version["metadata"]["keywords"] = topics
            next(p for p in version["passages"] if p["passage_id"] == "results")["text"] = json.dumps(value, sort_keys=True)
        for record in batch["records"]:
            if record["source_key"] in batch["works"]:
                record["keywords"] = deepcopy(batch["works"][record["source_key"]]["versions"]["1"]["metadata"]["keywords"])
        if batch["correction"]:
            target = batch["correction"]["source_key"]
            origin = next(b["works"][target]["versions"]["1"] for b in batches if target in b["works"])
            revised = deepcopy(origin)
            revised["revision"] = 2
            revised["metadata"]["title"] += " (corrected)"
            revised["metadata"]["venue"] = "Synthetic Proceedings: Corrected Edition"
            passage = next(p for p in revised["passages"] if p["passage_id"] == "results")
            corrected = json.loads(passage["text"])
            corrected["offset_points"] += 2
            passage["text"] = json.dumps(corrected, sort_keys=True)
            batch["correction"]["version"] = revised
    public["format"] = world["format"] = FORMAT
    public["synthesis_policy"] = {"metric": "allocation_score", "population": "synthetic_items", "unit_factors_to_points": {"points": [1, 1], "fraction": [100, 1], "basis_points": [1, 100]},
        "rule": "Parse actual current results evidence. For compatible studies, convert the rational observed value to points, subtract the current named calibration offset, and weight by sample_size. Calibration documents are references, not extra study samples. Other metrics/populations are excluded from pooling but retained in source coverage with a reason. Give exact rational weighted sums/means, actual study and calibration annotation identities, pairwise section-mean differences, and a deduplicated global estimate across canonical study identities."}
    public["goal"] += " Compute evidence-bound calibrated, sample-weighted section and global syntheses; propagate old calibration corrections even when dependent study passages are unchanged."
    if rounds==1:
        public["goal"]="Complete this single acquisition round with current source coverage, bibliography and review annotations. Compute exact calibrated sample-weighted section estimates and a deduplicated global estimate from actual shared study evidence. This task has no publisher correction or later acquisition round."
        public["dependency_scope"]="within_round_calibration_and_cross_section_overlap_no_update_recovery"
    public["policy"]["coverage_and_pooling"] = {
        "coverage": "Determine coverage from each section's metadata criteria, before deciding statistical eligibility. In this synthetic catalog, metadata.study_type and the results payload role are separate fields: calibration records may satisfy the empirical metadata criterion.",
        "partition": "Every covered canonical work must appear exactly once in that section's contributions or exclusions. A matching calibration stays in collection, reading list, tag and review evidence, but belongs to exclusions with reason calibration_reference. A retracted study stays covered but is excluded with reason retracted_study. Other metrics/populations stay covered but are excluded with reason incompatible_metric_or_population. Only active compatible studies contribute sample_size and calibrated points.",
        "overlap": "Apply the partition separately in every matching section. A shared study contributes once to each applicable section, then once across the union of canonical studies for cross_section_synthesis. Do not remove it from another section to deduplicate the global pool.",
        "serialization": "Put each section's numerical result at sections[i].synthesis. Put global totals and contrasts only inside cross_section_synthesis, not at the top level. Never repeat a JSON key in one object. Shape validation is not a business correctness check."}
    path = output / "public/skills/assemble-literature-handoff/SKILL.md"
    path.write_text(path.read_text() + "\n\n## Quantitative evidence synthesis\n\nRead each current results annotation and the study's named calibration evidence, including much earlier acquisition rounds. "
        "Normalize units, subtract the calibration offset, and combine compatible estimates by their actual sample sizes. Keep calibration/reference and incompatible-metric records out of the sample denominator. "
        "Carry exact source and annotation identities into the synthesis. Deduplicate a shared study across sections before the global mean and compare section means without treating them as disjoint. "
        "A changed calibration can invalidate a numerical result even when the dependent study quote is unchanged. The optional calculator receives only supplied public evidence.\n", encoding="utf-8")
    path.write_text(path.read_text() + "\nUse the exact nested synthesis and cross_section_synthesis schemas advertised by compose_literature_review. Fractions are numerator/denominator integer objects, not strings. "
        "Section results use contributions, exclusions, total_sample_size, weighted_sum_points and mean_points; global results use unique_study_source_keys, total_sample_size, weighted_sum_points, mean_points and section_contrasts. "
        "Passing shape validation does not establish source coverage or correct arithmetic. scripts/synthesize.py is optional and uses only supplied policy, sections and evidence.\n", encoding="utf-8")
    path.write_text(path.read_text() + "\n## Coverage before pooling\n\n" + "\n\n".join(public["policy"]["coverage_and_pooling"].values()) + "\n", encoding="utf-8")
    if rounds==1:
        path.write_text(path.read_text()+"\nThis instance has one acquisition round and no correction event. Update-handling rules apply only to instances with later rounds.\n",encoding="utf-8")
    script = output / "public/skills/assemble-literature-handoff/scripts/synthesize.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(Path(__file__).with_name("synthesis_script.py"), script)
    next(s for s in public["skills"] if s["name"] == "assemble-literature-handoff")["files"].append("scripts/synthesize.py")
    for folder in ("checkpoint", "runtime"):
        write_json(output / folder / "world.json", world)
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", {"public": public, "initial_tables": world["tables"], "initial_state": world["library"]})
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    for name, default in (("seed", 11), ("sections", 3), ("rounds", 4), ("studies-per-section", 1), ("alias-copies", 1), ("distractors-per-section", 1)):
        parser.add_argument("--" + name, type=int, default=default)
    print(build(**vars(parser.parse_args()))["task_id"])
