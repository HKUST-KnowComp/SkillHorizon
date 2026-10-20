"""Public-tool-only feasible curator; not a required skill router or a grading oracle."""


class WitnessCallLimit(BaseException):
    def __init__(self, calls):
        super().__init__("Library public reference call limit reached")
        self.calls = calls


def normalize(value):
    value = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if value.startswith(prefix):
            return value[len(prefix):].strip()
    return value


async def run(call, *, alternative=False, use_scripts=True, exercise_recovery=False, stop_at=None,
              efficient_reads=False, skill_protocol=None, max_calls=None):
    declared = skill_protocol == "skill_declaration_v1"
    if skill_protocol not in (None, "skill_declaration_v1") or (
            declared and (type(max_calls) is not int or max_calls < 1)) or (not declared and max_calls is not None):
        raise ValueError("Declaration reference requires an explicit positive call limit")
    calls = 0
    if declared:
        original = call

        async def call(name, **arguments):
            nonlocal calls
            if calls >= max_calls:
                raise WitnessCallLimit(calls)
            calls += 1
            try:
                return await original(name, **arguments)
            except BaseException as error:
                error.witness_calls = calls
                raise

    read_skills, evidence_cache = set(), {}
    task = await call("read_task")
    if declared and (task.get("format") != "skillhorizon.c11.calibration_network.v2"
                     or task.get("runtime_identity") != "task_local_v1"
                     or task.get("policy", {}).get("skill_protocol") != skill_protocol):
        raise ValueError("Declaration reference requires the actual task-local Calibration V2 profile")
    workspace = await call("read_library_workspace")
    sections = list(reversed(task["sections"])) if alternative else task["sections"]

    async def begin(skill, section=None):
        if not efficient_reads or skill not in read_skills:
            await call("read_skill_file", skill_id=skill)
            read_skills.add(skill)
        if declared:
            return None
        return (await call("begin_skill", skill_id=skill, section_ids=[section] if section else []))["run_id"]

    async def finish(run_id, outputs=()):
        if not declared:
            await call("finish_skill", run_id=run_id, output_ids=list(outputs))

    prior = None
    start = max(0, workspace["round"])
    for round_index in range(start, task["rounds"]):
        inspection = await begin("inspect-library-request")
        if workspace["round"] < round_index:
            args = {"batch_id": task["batches"][round_index]["batch_id"]}
            if round_index:
                args["prior_review_artifact_id"] = prior
            await call("acquire_library_batch", **args)
        papers, offset = [], 0
        while True:
            page = await call("search_papers", query="Synthetic Evidence & Allocation", offset=offset, limit=100, sort_order="asc")
            papers += [await call("get_paper", paper_id=p["paper_id"]) for p in page["papers"]]
            offset += len(page["papers"])
            if offset >= page["total_count"]:
                break
            if not page["papers"]:
                raise AssertionError("Catalog pagination stopped early")
        records = [await call("read_source_record", paper_id=p["paper_id"]) for p in papers]
        await finish(inspection, [p["paper_id"] for p in papers])
        identity = await begin("reconcile-publication-identities")
        if use_scripts:
            grouped = (await call("run_skill_script", skill_id="reconcile-publication-identities", inputs={"records": records}))["groups"]
        else:
            groups = {}
            for record in records:
                groups.setdefault(normalize(record["doi"]), []).append(record["paper_id"])
            grouped = [{"source_key": key, "paper_ids": sorted(ids)} for key, ids in sorted(groups.items())]
        canonical = {}
        for group in grouped:
            ids = sorted(group["paper_ids"], reverse=alternative)
            for secondary in ids[1:]:
                await call("merge_duplicate_papers", primary_paper_id=ids[0], secondary_paper_id=secondary)
            paper = await call("get_paper", paper_id=ids[0])
            if paper["source_revision"] != paper["latest_source_revision"] or normalize(paper["doi"]) != paper["doi"]:
                await call("apply_source_revision", paper_id=ids[0], source_revision=paper["latest_source_revision"])
                paper = await call("get_paper", paper_id=ids[0])
            canonical[group["source_key"]] = paper
        await finish(identity, [p["paper_id"] for p in canonical.values()])
        if stop_at == "merged" and round_index == 1:
            return
        sources, annotations, citations = {}, {}, {}
        for key, paper in canonical.items():
            cache_key = (key, paper["paper_id"], paper["source_revision"])
            if efficient_reads and cache_key in evidence_cache:
                sources[key], annotations[key], citations[key] = evidence_cache[cache_key]
                continue
            annotation_skill = await begin("annotate-source-evidence")
            source = await call("read_source_text", paper_id=paper["paper_id"])
            sources[key] = source
            existing = (await call("list_annotations", paper_id=paper["paper_id"]))["annotations"]
            annotations[key] = {}
            for passage in source["passages"]:
                if passage["passage_id"] == "abstract":
                    continue
                current = next((a for a in existing if a["passage_id"] == passage["passage_id"] and a["current_for_source"] and a["start"] == 0 and a["end"] == len(passage["text"])), None)
                if current is None:
                    current = await call("add_annotation", paper_id=paper["paper_id"], source_revision=source["revision"], passage_id=passage["passage_id"], start=0, end=len(passage["text"]))
                annotations[key][passage["passage_id"]] = current
            await call("set_paper_read_status", paper_id=paper["paper_id"], read_status="read")
            await finish(annotation_skill, [a["annotation_id"] for a in annotations[key].values()])
            network_skill = await begin("maintain-citation-network")
            old = (await call("list_citation_evidence", paper_id=paper["paper_id"]))["citations"]
            for citation in old:
                if citation["status"] == "active" and not citation["current_for_source"]:
                    await call("remove_citation", citation_id=citation["citation_id"])
            citations[key] = []
            for passage in source["passages"]:
                for cited_doi in passage["cited_dois"]:
                    target = canonical[normalize(cited_doi)]
                    current = next((c for c in old if c["status"] == "active" and c["current_for_source"] and c["cited_paper_id"] == target["paper_id"] and c["annotation_id"] == annotations[key][passage["passage_id"]]["annotation_id"]), None)
                    if current is None:
                        current = await call("add_citation", annotation_id=annotations[key][passage["passage_id"]]["annotation_id"], cited_paper_id=target["paper_id"])
                    citations[key].append(current["citation_id"])
            await call("get_citation_network", paper_id=paper["paper_id"], depth=1)
            await finish(network_skill, citations[key])
            evidence_cache[cache_key] = (sources[key], annotations[key], citations[key])
        selections = {}
        for section in sections:
            skill = await begin("curate-section-collections", section["section_id"])
            criteria = section["criteria"]
            selected = [p for p in canonical.values() if p["year"] >= criteria["year_min"] and p["study_type"] == criteria["study_type"] and criteria["topic"] in p["keywords"]]
            selections[section["section_id"]] = selected
            if exercise_recovery and round_index == 0:
                wrong = next(p for p in canonical.values() if p["study_type"] == "commentary")
                await call("add_paper_to_collection", collection_id=section["collection_id"], paper_id=wrong["paper_id"])
            collection = await call("read_collection", collection_id=section["collection_id"])
            wanted = {p["paper_id"] for p in selected}
            for rows, add_name, remove_name, field, parent in ((collection["memberships"], "add_paper_to_collection", "remove_paper_from_collection", "collection_id", section["collection_id"]),
                (collection["reading_entries"], "add_paper_to_reading_list", "remove_paper_from_reading_list", "list_id", section["reading_list_id"])):
                existing = {r["paper_id"] for r in rows}
                for paper_id in sorted(existing - wanted):
                    await call(remove_name, **{field: parent, "paper_id": paper_id})
                for paper_id in sorted(wanted - existing, reverse=alternative):
                    await call(add_name, **{field: parent, "paper_id": paper_id})
            for paper in selected:
                actual = await call("get_paper", paper_id=paper["paper_id"])
                if section["topic"] not in actual["tags"]:
                    await call("assign_tag_to_paper", paper_id=paper["paper_id"], tag_id=section["tag_id"])
            await finish(skill, [p["paper_id"] for p in selected])
        if stop_at == "curated" and round_index == 0:
            return
        bib_skill = await begin("build-versioned-bibliography")
        selected = {p["paper_id"]: p for rows in selections.values() for p in rows}
        if use_scripts:
            entries = (await call("run_skill_script", skill_id="build-versioned-bibliography", inputs={"papers": list(selected.values())}))["entries"]
        else:
            entries = [{"paper_id": pid, "citation_key": "Study" + str(i)} for i, pid in enumerate(sorted(selected), 1)]
        bibliography = await call("generate_bibtex", entries=entries)
        await finish(bib_skill, [bibliography["artifact_id"]])
        if stop_at == "bibliography" and round_index == 0:
            return
        review_skill = await begin("assemble-literature-handoff")
        keys = {e["paper_id"]: e["citation_key"] for e in bibliography["entries"]}
        requested = [{"section_id": s["section_id"], "title": s["title"], "items": [{"paper_id": p["paper_id"], "annotation_id": annotations[p["source_key"]]["results"]["annotation_id"],
            "citation_ids": citations[p["source_key"]], "citation_key": keys[p["paper_id"]]} for p in selections[s["section_id"]]]} for s in sections]
        extra = {}
        if "synthesis_policy" in task:
            inputs = {"policy": task["synthesis_policy"], "evidence": [{"source_key": key, "annotation_id": rows["results"]["annotation_id"],
                "quote": rows["results"]["content"]} for key, rows in annotations.items()],
                "sections": [{"section_id": s["section_id"], "source_keys": [p["source_key"] for p in selections[s["section_id"]]]} for s in sections]}
            if use_scripts:
                synthesized = await call("run_skill_script", skill_id="assemble-literature-handoff", inputs=inputs)
            else:
                # This is a Python expert policy without an environment script call, not an LLM arm.
                from .synthesis_script import calculate
                synthesized = calculate(inputs)
            for section in requested:
                section["synthesis"] = synthesized["sections"][section["section_id"]]
            extra["cross_section_synthesis"] = synthesized["global"]
        if efficient_reads:
            extra["include_content"] = False
        review = await call("compose_literature_review", bibliography_artifact_id=bibliography["artifact_id"], sections=requested, **extra)
        await finish(review_skill, [review["artifact_id"]])
        prior = review["artifact_id"]
        workspace = await call("read_library_workspace")
