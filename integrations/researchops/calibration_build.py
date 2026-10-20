"""Multi-parent calibration maps carried by current source documents."""

import argparse
from copy import deepcopy
import json
import random
from pathlib import Path
from skillhorizon.construction_provenance import construction_entry, observe_call

from integrations.internal_mcp.io import read_json, write_json
from .synthesis_build import build as synthesis_build
from integrations.internal_mcp.topology import disclose_pattern, disclose_runtime_identity

FORMAT = "skillhorizon.c11.calibration_network.v2"
FAMILIES = ("standard", "identity_compatibility", "study_retraction", "ambiguous_withdrawal")


@construction_entry
def build(source, output, *, seed=11, sections=3, rounds=4, studies_per_section=1, alias_copies=1, distractors_per_section=1, background=True, scenario="standard", dependency_pattern="staggered", runtime_identity="legacy", construction_profile=None):
    disclose_runtime_identity({}, runtime_identity)
    if dependency_pattern not in ("staggered", "distributed"):
        raise ValueError("Unknown calibration dependency pattern")
    if scenario not in FAMILIES:
        raise ValueError("Unknown calibration scenario")
    if type(rounds) is not int or rounds<2:
        raise ValueError("Calibration scenarios require at least two rounds")
    if scenario in ("identity_compatibility", "ambiguous_withdrawal") and (alias_copies < 1 or distractors_per_section < 1):
        raise ValueError("Identity/compatibility scenarios require actual aliases and distractors")
    if scenario in ("study_retraction", "ambiguous_withdrawal") and rounds < 3:
        raise ValueError("Retraction follows an already committed correction round")
    output = Path(output)
    public = observe_call(synthesis_build, source, output, seed=seed, sections=sections, rounds=rounds, studies_per_section=studies_per_section,
        alias_copies=alias_copies, distractors_per_section=distractors_per_section, background=background, construction_profile=construction_profile)
    world = read_json(output / "checkpoint/world.json")
    batches = world["library"]["batches"]
    anchors = [next(iter(batch["works"])) for batch in batches]
    versions = {}
    for stage, batch in enumerate(batches):
        anchored_compatible_study = False
        for number, (key, work) in enumerate(batch["works"].items()):
            version = work["versions"]["1"]
            result = next(p for p in version["passages"] if p["passage_id"] == "results")
            value = json.loads(result["text"])
            if value["role"] == "calibration":
                origins = list(range(max(0, stage - 2), stage))
                if dependency_pattern == "distributed" and stage >= 3:
                    origins = sorted({stage - 1, random.Random(f"calibration-distributed-v1:{seed}:{stage}").randrange(stage - 1)})
                parents = [{"source_key": anchors[p], "weight": 1 + i} for i, p in enumerate(origins)]
                value.update(gain_numerator=5 + stage % 3, gain_denominator=5, parent_calibrations=parents)
                if parents:
                    version["passages"].append({"passage_id": "calibration-chain", "text": "This synthetic instrument map is calibrated against " + ", ".join(p["source_key"] for p in parents) + ".", "cited_dois": [p["source_key"] for p in parents]})
            elif not anchored_compatible_study and value["metric"] == "allocation_score" and value["population"] == "synthetic_items":
                anchored_compatible_study = True
                value["calibration_doi"] = anchors[stage]
                reference = next(p for p in version["passages"] if p["passage_id"] == "calibration-reference")
                reference.update(text="This synthetic measurement uses calibration DOI " + anchors[stage] + ".", cited_dois=[anchors[stage]])
            result["text"] = json.dumps(value, sort_keys=True)
            versions[key] = version
        if not anchored_compatible_study:
            raise ValueError("Each round needs an actual compatible study using its derived calibration")
        if batch["correction"]:
            correction = batch["correction"]
            revised = deepcopy(versions[correction["source_key"]])
            previous = correction["version"]
            revised["revision"] = previous["revision"]
            revised["metadata"] = deepcopy(previous["metadata"])
            result = next(p for p in revised["passages"] if p["passage_id"] == "results")
            value = json.loads(result["text"])
            value["offset_points"] = json.loads(next(p["text"] for p in previous["passages"] if p["passage_id"] == "results"))["offset_points"]
            result["text"] = json.dumps(value, sort_keys=True)
            correction["version"] = revised
    public["format"] = world["format"] = FORMAT
    disclose_pattern(public, dependency_pattern, seed)
    disclose_runtime_identity(public, runtime_identity)
    public["synthesis_policy"]["calibration_graph"] = {
        "mapping": "observed = effective_gain * reference + effective_offset",
        "composition": "Root gain/offset are local. A derived calibration takes the weight-normalized mean of parent effective gains and offsets, then composes its local map: G=local_gain*mean(parent_G), B=local_gain*mean(parent_B)+local_offset. Study points=(unit_normalized_value-B)/G.",
        "evidence": "Bind the current results annotation of every ancestor, including the named calibration, once per contribution. A correction changes derived maps even if descendant source text is unchanged. Parent origins must precede the child calibration; no duplicate or cyclic parents.",
    }
    public["synthesis_policy"]["rule"] = public["synthesis_policy"]["rule"].replace("subtract the current named calibration offset", "invert the complete current affine calibration graph")
    public["goal"] += " Resolve multi-parent calibration maps and current ancestor evidence before interpreting study values."
    if scenario != "standard":
        from .calibration_scenarios import configure
        configure(world, public, scenario, seed)
    public["scenario"] = {"family": scenario, "construction_seed": seed,
        "world_size": {"sections": sections, "rounds": rounds, "studies_per_section": studies_per_section,
            "alias_copies": alias_copies, "distractors_per_section": distractors_per_section, "background": background},
        "mechanisms": {"standard": ["derived_calibration_dag", "ancestor_correction"],
            "identity_compatibility": ["canonical_alias_deduplication", "eligible_but_incompatible_evidence", "mixed_population"],
            "study_retraction": ["ancestor_correction", "later_study_retraction", "immutable_prior_reviews", "selective_annotation_reuse"],
            "ambiguous_withdrawal": ["canonical_alias_deduplication", "eligible_but_incompatible_evidence", "mixed_population",
                "ancestor_correction", "later_study_retraction", "immutable_prior_reviews", "selective_annotation_reuse"]}[scenario],
        "provenance": "synthetic_construction_development_zero_model"}
    if scenario == "ambiguous_withdrawal":
        public["scenario"]["composition_of"] = ["identity_compatibility", "study_retraction"]
    path = output / "public/skills/assemble-literature-handoff/SKILL.md"
    path.write_text(path.read_text().replace("subtract the calibration offset", "invert the complete calibration map") + "\n\n## Derived calibration networks\n\nCalibration results may name earlier calibration parents, positive weights and a local gain. Resolve the whole DAG from current annotated evidence. Average the parent effective maps by weight, then compose the local map. Invert the resulting map for each study. Bind every actual ancestor annotation, not only the direct reference. Recompute after a source correction even when descendant text is unchanged. These are synthetic coordinate maps, not empirical meta-analysis uncertainty estimates.\n", encoding="utf-8")
    path.write_text(path.read_text() + "\n## Evidence withdrawal\n\nAn explicit evidence_status of retracted excludes the study from numerical pooling, with reason retracted_study. Retain its current source coverage and withdrawal evidence. Absence of evidence_status means active. Do not overwrite committed reviews or recreate unchanged passage annotations just because another source changed.\n", encoding="utf-8")
    for folder in ("checkpoint", "runtime"):
        write_json(output / folder / "world.json", world)
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", {"public": public, "initial_tables": world["tables"], "initial_state": world["library"]})
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--scenario", choices=FAMILIES, default="standard")
    parser.add_argument("--dependency-pattern", choices=("staggered", "distributed"), default="staggered")
    parser.add_argument("--runtime-identity", choices=("legacy", "task_local_v1"), default="legacy")
    for name, default in (("seed", 11), ("sections", 3), ("rounds", 4), ("studies-per-section", 1), ("alias-copies", 1), ("distractors-per-section", 1)):
        parser.add_argument("--" + name, type=int, default=default)
    print(build(**vars(parser.parse_args()))["task_id"])
