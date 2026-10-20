"""Construct versioned synthetic publication acquisitions without claiming real paper facts."""

import argparse
from copy import deepcopy
from pathlib import Path
import random
import shutil
import sys

from integrations.internal_mcp.io import materialize_skills, read_json, write_json


KEYS = {"paper": "paper_id", "author": "author_id", "collection": "collection_id", "collection_paper": "link_id", "tag": "tag_id",
    "paper_tag": "link_id", "citation": "citation_id", "reading_list": "list_id", "reading_list_entry": "link_id", "annotation": "annotation_id"}
FILES = {k: k + "s" for k in KEYS} | {"reading_list_entry": "reading_list_entries"}
COMPOSITE = {"collection_paper": ("collection_id", "paper_id"), "paper_tag": ("paper_id", "tag_id"), "reading_list_entry": ("list_id", "paper_id")}
FORMAT = "skillhorizon.c11.living_library.v1"
ORIGIN = "2026-10-01T08:00:00Z"
SKILLS = Path(__file__).with_name("skills")


def build(source, output, *, seed=11, sections=2, rounds=2, studies_per_section=1, alias_copies=1, distractors_per_section=1, background=True, synthesis_network=False, construction_profile=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Use a fresh isolated output")
    if construction_profile not in (None, "researchops150_v1"):
        raise ValueError("Unknown construction profile")
    maximum_rounds = 8 if construction_profile else (6 if synthesis_network else 4)
    maximum_studies = 40 if construction_profile else 3
    minimum_rounds = 1 if synthesis_network else 2
    if type(seed) is not int or any(type(v) is not int or not low <= v <= high for v, low, high in
        ((sections, 2, 4), (rounds, minimum_rounds, maximum_rounds), (studies_per_section, 1, maximum_studies), (alias_copies, 0, 2), (distractors_per_section, 0, 2))):
        raise ValueError(f"Use 2..4 sections, {minimum_rounds}..{maximum_rounds} rounds, 1..3 studies, 0..2 aliases and 0..2 distractors")
    original = {k: read_json(source / f"data/database/{stem}.json") for k, stem in FILES.items()}
    for kind, fields in COMPOSITE.items():
        original[kind] = [{**r, "link_id": ":".join(str(r[k]) for k in fields)} for r in original[kind]]
    task_id = f"c11-{'synthesis' if synthesis_network else 'library'}-{seed}-{sections}s-{rounds}r-{studies_per_section}n-{alias_copies}a-{distractors_per_section}d"
    rng = random.Random(seed)
    tables = {k: [] for k in KEYS}

    def add(kind, identifier, values):
        row = {**deepcopy(values), KEYS[kind]: identifier, "task_id": task_id}
        tables[kind].append(row)
        return row

    author_ids = []
    for i, name in enumerate(("Ada Chen", "Zoe Smith", "Evan Chen", "Mira Doe")):
        identifier = f"author-{task_id}-{i}"
        author_ids.append(identifier)
        add("author", identifier, {"name": name, "affiliation": "Synthetic Bibliography Lab", "email": "", "homepage": "",
            "h_index": 0, "paper_count": 0, "created_at": ORIGIN, "updated_at": ORIGIN})
    section_specs = []
    for i in range(sections):
        sid = f"section-{task_id}-{i}"
        topic = f"synthetic-topic-{i}"
        collection_id, list_id, tag_id = f"collection-{sid}", f"reading-list-{sid}", f"tag-{sid}"
        add("collection", collection_id, {"name": topic + " evidence", "description": "Current canonical study collection", "max_papers": (2048 if construction_profile else 100),
            "paper_count": 0, "created_at": ORIGIN, "updated_at": ORIGIN})
        add("reading_list", list_id, {"name": topic + " reading", "description": "Current selected work order", "deadline": None, "visibility": "private",
            "paper_count": 0, "created_at": ORIGIN, "updated_at": ORIGIN})
        add("tag", tag_id, {"name": topic, "color": "#287569", "parent_tag_id": None, "usage_count": 0, "created_at": ORIGIN})
        section_specs.append({"section_id": sid, "title": f"Synthetic topic {i}", "topic": topic, "collection_id": collection_id,
            "reading_list_id": list_id, "tag_id": tag_id, "criteria": {"year_min": 2020, "study_type": "empirical", "topic": topic},
            "coverage": "Include every acquired canonical work whose current metadata satisfies this section's criteria, including works also matching other sections. Quote its full current results passage and preserve actual outgoing reference evidence. Coverage is distinct from eligibility for numerical pooling."})
    batches, previous_shared, previous_section = [], None, {}
    for round_index in range(rounds):
        batch_id = f"batch-{task_id}-{round_index}"
        works, records = {}, []
        proposals = [("shared", [s["topic"] for s in section_specs], True, None)]
        for i, section in enumerate(section_specs):
            proposals += [(f"section-{i}-study-{j}", [section["topic"]], True, i) for j in range(studies_per_section)]
            proposals += [(f"section-{i}-near-title-{j}", [section["topic"]], False, i) for j in range(distractors_per_section)]
        new_section = {}
        for number, (name, topics, eligible, section_index) in enumerate(proposals):
            doi = f"10.99999/{task_id}.{round_index}.{number}"
            work_id = f"work-{task_id}-{round_index}-{number}"
            title = f"Synthetic Evidence & Allocation: {topics[0]} round {round_index} study {number}"
            references = []
            if previous_shared:
                references.append(previous_shared)
            if section_index is not None and section_index in previous_section:
                references.append(previous_section[section_index])
            references = list(dict.fromkeys(references))
            value = rng.randint(15, 90)
            passages = [{"passage_id": "abstract", "text": f"This synthetic fixture studies {', '.join(topics)}. It is {'an empirical study' if eligible else 'a historical commentary'}, not a real scientific publication.", "cited_dois": []},
                {"passage_id": "results", "text": f"In synthetic cohort {round_index}-{number}, the measured allocation score is {value} points across {rng.randint(20, 80)} simulated items.", "cited_dois": []}]
            passages += [{"passage_id": f"reference-{j}", "text": f"This synthetic study builds on the published procedure identified by DOI {ref}.", "cited_dois": [ref]} for j, ref in enumerate(references)]
            metadata = {"title": title, "abstract": passages[0]["text"], "authors": author_ids[:2] if number % 2 == 0 else author_ids[2:],
                "year": 2024 + round_index if eligible else 2018, "venue": "Synthetic Proceedings", "venue_type": "conference", "doi": doi,
                "arxiv_id": f"2610.{round_index * 100 + number:05d}", "url": "https://synthetic.invalid/" + work_id,
                "keywords": topics, "study_type": "empirical" if eligible else "commentary"}
            works[doi] = {"source_key": doi, "acquisition_round": round_index, "versions": {"1": {"revision": 1, "metadata": metadata, "passages": passages}}}
            copies = 1 + (alias_copies if eligible and (name == "shared" or name == "section-0-study-0") else 0)
            for copy in range(copies):
                identifier = f"paper-{task_id}-{round_index}-{number}-{copy}"
                row = {"paper_id": identifier, **deepcopy(metadata), "source_record_id": f"record-{identifier}", "source_key": doi,
                    "source_revision": 1, "catalog_revision": 1, "status": "active", "merged_into": None,
                    "pdf_stored": False, "pdf_path": None, "read_status": "unread", "source": "import", "bibtex_key": None,
                    "bibtex_generated": False, "created_at": ORIGIN, "updated_at": ORIGIN, "task_id": task_id}
                if copy:
                    row["doi"] = "https://doi.org/" + doi.upper()
                    row["arxiv_id"] += "v1"
                records.append(row)
            if name == "shared":
                new_shared = doi
            elif eligible and section_index is not None and section_index not in new_section:
                new_section[section_index] = doi
        correction = None
        if previous_shared and alias_copies:
            prior_record = next(r for r in batches[-1]["records"] if r["source_key"] == previous_shared)
            late = deepcopy(prior_record)
            late["paper_id"] = f"paper-{task_id}-{round_index}-late-alias"
            late["source_record_id"] = "record-" + late["paper_id"]
            late["doi"] = "doi:" + previous_shared.upper()
            records.append(late)
        if round_index == 1:
            prior = batches[0]["works"][previous_shared]["versions"]["1"]
            revision = deepcopy(prior)
            revision["revision"] = 2
            revision["metadata"]["title"] += " (corrected)"
            revision["metadata"]["venue"] = "Synthetic Proceedings: Corrected Edition"
            revision["passages"][1]["text"] += " The corrected analysis uses an additional calibration cohort."
            correction = {"source_key": previous_shared, "version": revision}
        batches.append({"batch_id": batch_id, "round": round_index, "works": works, "records": records, "correction": correction})
        previous_shared, previous_section = new_shared, new_section
    skills = [{"name": p.name, "files": [str(f.relative_to(p)) for f in sorted(p.rglob("*")) if f.is_file() and f.suffix in {".md", ".py"}]}
        for p in sorted(SKILLS.iterdir()) if p.is_dir()] if SKILLS.exists() else []
    public = {"format": FORMAT, "task_id": task_id, "actor": "library-curator", "sections": section_specs, "rounds": rounds,
        "batches": [{"batch_id": b["batch_id"], "round": b["round"], "record_count": len(b["records"]), "has_publisher_correction": b["correction"] is not None} for b in batches],
        "skills": skills, "goal": "Build a living synthetic literature review across every acquisition round. Reconcile publication aliases, curate each section's matching works, "
            "bind actual source passages and references, and create current versioned bibliography/review artifacts. Commit the previous round's actual review when acquiring the next batch. "
            "Historical artifacts remain unchanged; source corrections require current evidence and metadata, while unchanged passages may be reused.",
        "policy": {"skill_protocol": "observational_v1", "identity_rule": "Use immutable acquisition DOI: strip https://doi.org/, http://doi.org/ or doi:, then lowercase and trim. Title similarity is insufficient.",
            "selection_rule": "Evaluate every acquired work against each section independently: current metadata.year >= criteria.year_min, metadata.study_type == criteria.study_type, and criteria.topic in metadata.keywords. Include each matching canonical work in that section's collection, reading list, tag and review, once per canonical identity. A work can belong to several sections. A results role or numerical exclusion does not override these metadata coverage criteria.",
            "source_type": "synthetic_versioned_text_not_real_papers_or_fabricated_PDFs", "bibliography_format": "BibTeX via Pybtex 0.26.1",
            "source_updates": "Changed quotes invalidate evidence; unchanged passage text may be reused across revisions.",
            "committed_rounds": "A review committed to acquisition history cannot be replaced by a later artifact; inspect before advancing."}}
    state = {"next_id": 1, "round": -1, "batches": batches, "sources": {}, "source_current": {}, "source_records": {}, "aliases": {},
        "artifacts": {}, "committed_reviews": {}, "events": [], "skill_runs": {}, "active_skill_run": None}
    world = {"format": FORMAT, "task_id": task_id, "tables": tables, "library": state, "sequence": 0, "clock": ORIGIN}
    write_json(output / "background.json", original if background else {k: [] for k in KEYS})
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", {"public": public, "initial_tables": tables, "initial_state": state})
    write_json(output / "checkpoint/world.json", world)
    write_json(output / "controller.json", {"source": str(source), "provider_calls": 0, "qualification": "UNVERIFIED"})
    shutil.copytree(output / "checkpoint", output / "runtime")
    if SKILLS.exists():
        materialize_skills(SKILLS, output / "public/skills")
    write_json(output / "mcp.json", {"mcpServers": {"c11-library": {"command": sys.executable, "args": ["-m", "integrations.researchops.server", str(output), str(output / "runtime")],
        "cwd": str(Path(__file__).resolve().parents[2])}}})
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    for name, default in (("seed", 11), ("sections", 2), ("rounds", 2), ("studies-per-section", 1), ("alias-copies", 1), ("distractors-per-section", 1)):
        parser.add_argument("--" + name, type=int, default=default)
    args = vars(parser.parse_args())
    print(build(**args)["task_id"])
