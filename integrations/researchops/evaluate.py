"""Independent reconstruction of publication identity, evidence and exported review obligations.

No runtime, witness, skill or script imports. Frozen acquisitions define source facts; ordinary
operation events define the actually performed work. Valid-but-incomplete artifacts are not success.
"""

from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path

from pybtex.database import parse_string
from pybtex.exceptions import PybtexError
from pybtex.richtext import Text

from integrations.internal_mcp.io import read_json


PRIMARY = {"paper": "paper_id", "author": "author_id", "collection": "collection_id", "collection_paper": "link_id", "tag": "tag_id",
    "paper_tag": "link_id", "citation": "citation_id", "reading_list": "list_id", "reading_list_entry": "link_id", "annotation": "annotation_id"}
FIELDS = ("title", "abstract", "authors", "year", "venue", "venue_type", "doi", "arxiv_id", "url", "keywords", "study_type")
RELATIONS = (("collection_paper", "collection_id", "collection", "paper_count"),
    ("reading_list_entry", "list_id", "reading_list", "paper_count"), ("paper_tag", "tag_id", "tag", "usage_count"))


def identifier(value):
    value = value.strip().lower()
    prefixes = ("https://doi.org/", "http://doi.org/", "doi:")
    return next((value[len(p):].strip() for p in prefixes if value.startswith(p)), value)


def review_goal_outcome(public, row, round_index, tables, sources, current, artifacts):
    """Read-only review rules, evaluated against the caller's exact historical cut."""
    def source(key, revision=None):
        return sources[key]["versions"][str(current[key] if revision is None else revision)]

    def valid_annotation(row):
        paper = tables["paper"][row["paper_id"]]
        version = source(row["source_key"], row["source_revision"])
        passages = [p for p in version["passages"] if p["passage_id"] == row["passage_id"]]
        if len(passages) != 1:
            return False
        text = passages[0]["text"]
        return (paper["status"] == "active" and paper["source_key"] == row["source_key"] and row["source_passage_text"] == text
            and 0 <= row["start"] < row["end"] <= len(text) and row["content"] == text[row["start"]:row["end"]])

    def annotation_current(row):
        return any(p["passage_id"] == row["passage_id"] and p["text"] == row["source_passage_text"] for p in source(row["source_key"])["passages"])

    failures, metrics = [], None

    def require(condition, code):
        if not condition:
            failures.append(code)

    bib = artifacts[row["bibliography_artifact_id"]]
    entries = {e["paper_id"]: e for e in bib["entries"]}
    rendered = [f"# Synthetic literature review, round {round_index}", ""]
    supplied = {s["section_id"]: s for s in row["sections"]}
    require(len(supplied) == len(row["sections"]) and set(supplied) == {s["section_id"] for s in public["sections"]}, "REVIEW_SECTION_COVERAGE")
    expected_union = set()
    active_papers = [p for p in tables["paper"].values() if p["status"] == "active"]
    require(len(active_papers) == len({p["source_key"] for p in active_papers}), "UNRECONCILED_PUBLICATION_ALIASES")
    for section in public["sections"]:
        criteria = section["criteria"]
        expected = {key for key in sources if source(key)["metadata"]["year"] >= criteria["year_min"]
            and source(key)["metadata"]["study_type"] == criteria["study_type"] and criteria["topic"] in source(key)["metadata"]["keywords"]}
        expected_union |= expected
        for kind, field, parent in (("collection_paper", "collection_id", section["collection_id"]),
            ("reading_list_entry", "list_id", section["reading_list_id"]), ("paper_tag", "tag_id", section["tag_id"])):
            rows = [r for r in tables[kind].values() if r[field] == parent and r["status"] == "active"]
            works = [tables["paper"][r["paper_id"]]["source_key"] for r in rows]
            require(len(works) == len(set(works)) and set(works) == expected, "SECTION_MEMBERSHIP_IDENTITY:" + kind)
        if section["section_id"] not in supplied:
            continue
        items = supplied[section["section_id"]]["items"]
        works = [i["source_key"] for i in items]
        require(len(works) == len(set(works)) and set(works) == expected, "REVIEW_WORK_COVERAGE")
        for item in items:
            paper, annotation = tables["paper"][item["paper_id"]], tables["annotation"][item["annotation_id"]]
            require(item["source_key"] == paper["source_key"] and item["annotation"] == annotation, "REVIEW_ANNOTATION_SNAPSHOT")
            require(valid_annotation(annotation) and annotation_current(annotation) and annotation["paper_id"] == item["paper_id"], "REVIEW_CURRENT_SOURCE_OWNERSHIP")
            full = next(p["text"] for p in source(paper["source_key"])["passages"] if p["passage_id"] == "results")
            require(annotation["passage_id"] == "results" and annotation["content"] == full and annotation["start"] == 0 and annotation["end"] == len(full), "REVIEW_FULL_RESULTS_EVIDENCE")
            require(item["paper_id"] in entries and entries[item["paper_id"]]["citation_key"] == item["citation_key"], "REVIEW_CITATION_KEY_IDENTITY")
            if item["paper_id"] in entries:
                entry = entries[item["paper_id"]]
                require(entry["catalog_revision"] == paper["catalog_revision"] and entry["source_revision"] == current[paper["source_key"]], "REVIEW_STALE_BIBLIOGRAPHY")
            chosen = [tables["citation"][i] for i in item["citation_ids"]]
            require(chosen == item["citations"], "REVIEW_CITATION_SNAPSHOTS")
            actual = []
            for citation in chosen:
                a = tables["annotation"][citation["annotation_id"]]
                require(citation["status"] == "active" and annotation_current(a) and citation["citing_paper_id"] == item["paper_id"], "REVIEW_STALE_OR_WRONG_CITATION")
                actual.append((a["passage_id"], citation["target_source_key"], a["content"]))
            expected_refs = [(p["passage_id"], identifier(target), p["text"]) for p in source(paper["source_key"])["passages"] for target in p["cited_dois"]]
            require(sorted(actual) == sorted(expected_refs), "REVIEW_EXACT_REFERENCE_CONTEXTS")
    for section in row["sections"]:
        rendered += ["## " + section["title"], ""]
        for item in section["items"]:
            rendered += [item["annotation"]["content"] + " [@" + item["citation_key"] + "]", ""]
        if "synthesis_policy" in public:
            rendered += ["Synthesis: " + json.dumps(section["synthesis"], sort_keys=True), ""]
    if "synthesis_policy" in public:
        from .synthesis_evaluate import verify
        numerical_errors, metrics = verify(public, row, {key: source(key) for key in sources}, tables["annotation"], tables["paper"])
        failures.extend(numerical_errors)
        rendered += ["## Cross-section synthesis", "", json.dumps(row["cross_section_synthesis"], sort_keys=True), ""]
    rendered += ["## Bibliography", "", "```bibtex", bib["content"].rstrip(), "```", ""]
    require(row["content"] == "\n".join(rendered), "REVIEW_RENDERED_CONTENT")
    require({e["source_key"] for e in bib["entries"]} == expected_union, "BIBLIOGRAPHY_EXACT_WORK_SET")
    require(bib["round"] == round_index, "BIBLIOGRAPHY_WRONG_ROUND")
    return sorted(set(failures)), metrics


def _evaluate(task, runtime):
    task, runtime = Path(task), Path(runtime).resolve()
    frozen = read_json(task / "private/initial_obligations.json")
    public, initial = frozen["public"], frozen["initial_state"]
    world = read_json(runtime / "world.json")
    state = world["library"]
    tables = {kind: {r[PRIMARY[kind]]: deepcopy(r) for r in rows} for kind, rows in frozen["initial_tables"].items()}
    errors, evaluations, synthesis_metrics = [], {}, {}
    sources, current, records, aliases, artifacts, committed = {}, {}, {}, {}, {}, {}
    round_index, now, previous_sequence = -1, "2026-10-01T08:00:00Z", -1
    event_ids = set()

    def check(condition, code):
        if not condition:
            errors.append(code)

    check(read_json(task / "public/task.json") == public, "PUBLIC_FROZEN_OBLIGATIONS")

    def content_file(relative, content):
        path = runtime / relative
        check(not Path(relative).is_absolute() and path.resolve().is_relative_to(runtime) and not path.is_symlink(), "FILE_PATH_SCOPE")
        if Path(relative).is_absolute() or not path.resolve().is_relative_to(runtime) or path.is_symlink():
            return
        check(path.is_file() and path.read_text(encoding="utf-8") == content, "ACTUAL_FILE_BYTES")

    def source(key, revision=None):
        return sources[key]["versions"][str(current[key] if revision is None else revision)]

    def valid_annotation(row):
        paper = tables["paper"][row["paper_id"]]
        version = source(row["source_key"], row["source_revision"])
        passages = [p for p in version["passages"] if p["passage_id"] == row["passage_id"]]
        if len(passages) != 1:
            return False
        text = passages[0]["text"]
        return (paper["status"] == "active" and paper["source_key"] == row["source_key"] and row["source_passage_text"] == text
            and 0 <= row["start"] < row["end"] <= len(text) and row["content"] == text[row["start"]:row["end"]])

    def annotation_current(row):
        return any(p["passage_id"] == row["passage_id"] and p["text"] == row["source_passage_text"] for p in source(row["source_key"])["passages"])

    def recount():
        for kind, field, parent_kind, count_field in RELATIONS:
            for parent in tables[parent_kind].values():
                active = [r for r in tables[kind].values() if r[field] == parent[PRIMARY[parent_kind]] and r["status"] == "active"]
                parent[count_field] = len(active)
                if kind == "reading_list_entry":
                    for position, row in enumerate(sorted(active, key=lambda r: (r["position"], r["link_id"])), 1):
                        row["position"] = position
        for author in tables["author"].values():
            author["paper_count"] = sum(p["status"] == "active" and author["author_id"] in p["authors"] for p in tables["paper"].values())

    def review_outcome(row):
        failures, metrics = review_goal_outcome(public, row, round_index, tables, sources, current, artifacts)
        if metrics is not None:
            synthesis_metrics[row["artifact_id"]] = metrics
        return failures

    for event in state["events"]:
        check(event["event_id"] not in event_ids and event["sequence"] > previous_sequence, "EVENT_ID_OR_ORDER")
        event_ids.add(event["event_id"])
        previous_sequence = event["sequence"]
        kind, data = event["type"], event["data"]
        if kind == "batch.acquired":
            round_index += 1
            batch = initial["batches"][round_index]
            now = (datetime.fromisoformat("2026-10-01T08:00:00+00:00") + timedelta(days=round_index)).isoformat().replace("+00:00", "Z")
            check(data["batch_id"] == batch["batch_id"] and data["round"] == round_index and data["paper_ids"] == [p["paper_id"] for p in batch["records"]]
                and data["publisher_correction"] == batch["correction"], "FROZEN_ACQUISITION")
            if round_index:
                prior = artifacts[data["prior_review_artifact_id"]]
                check(prior["kind"] == "review" and prior["round"] == round_index - 1, "ACQUISITION_PRIOR_REVIEW")
                committed[str(round_index - 1)] = prior["artifact_id"]
            else:
                check(data["prior_review_artifact_id"] is None, "INITIAL_PRIOR_REVIEW")
            for key, value in batch["works"].items():
                check(key not in sources, "SOURCE_REACQUIRED")
                sources[key], current[key] = deepcopy(value), 1
            correction = batch["correction"]
            if correction:
                sources[correction["source_key"]]["versions"][str(correction["version"]["revision"])] = deepcopy(correction["version"])
                current[correction["source_key"]] = correction["version"]["revision"]
            for paper in batch["records"]:
                check(paper["paper_id"] not in tables["paper"] and paper["source_record_id"] not in records, "ACQUISITION_ID_COLLISION")
                records[paper["source_record_id"]] = deepcopy(paper)
                tables["paper"][paper["paper_id"]] = {**deepcopy(paper), "created_at": now, "updated_at": now}
        elif kind == "catalog.source_applied":
            before = tables["paper"][data["before"]["paper_id"]]
            check(before == data["before"] and before["status"] == "active", "SOURCE_APPLY_PRESTATE")
            expected = {**deepcopy(before), **deepcopy(source(before["source_key"])["metadata"]), "source_revision": current[before["source_key"]],
                "catalog_revision": before["catalog_revision"] + 1, "bibtex_generated": False, "bibtex_key": None, "updated_at": now}
            check(data["after"] == expected, "AUTHORITATIVE_METADATA_UPDATE")
            tables["paper"][before["paper_id"]] = expected
        elif kind == "catalog.read_status":
            row = tables["paper"][data["paper_id"]]
            check(row["status"] == "active" and data["read_status"] in {"unread", "reading", "read"}, "READ_STATUS_OPERATION")
            row.update(read_status=data["read_status"], updated_at=now)
        elif kind == "membership.changed":
            relation, row = data["kind"], data["row"]
            field, parent = next((f, p) for k, f, p, _ in RELATIONS if k == relation)
            check(row[field] in tables[parent] and tables["paper"][row["paper_id"]]["status"] == "active", "MEMBERSHIP_ENDPOINTS")
            old = tables[relation].get(row["link_id"])
            if old is None:
                check(row["status"] == "active" and row["added_at"] == now and row["task_id"] == public["task_id"], "MEMBERSHIP_CREATION")
                check(not any(r[field] == row[field] and r["paper_id"] == row["paper_id"] and r["status"] == "active" for r in tables[relation].values()), "DUPLICATE_ACTIVE_MEMBERSHIP")
            else:
                check(old["status"] == "active" and row == {**old, "status": "removed"}, "MEMBERSHIP_RETIREMENT")
            tables[relation][row["link_id"]] = deepcopy(row)
        elif kind == "annotation.created":
            check(data["annotation_id"] not in tables["annotation"] and valid_annotation(data), "ACTUAL_ANNOTATION_SOURCE_SPAN")
            check(data["original_paper_id"] == data["paper_id"] and data["created_at"] == now and data["updated_at"] == now
                and data["task_id"] == public["task_id"] and data["page_number"] is None, "ANNOTATION_CREATION_IDENTITY")
            tables["annotation"][data["annotation_id"]] = deepcopy(data)
        elif kind == "citation.created":
            annotation = tables["annotation"][data["annotation_id"]]
            target = tables["paper"][data["cited_paper_id"]]
            passage = next(p for p in source(annotation["source_key"], annotation["source_revision"])["passages"] if p["passage_id"] == annotation["passage_id"])
            check(data["citation_id"] not in tables["citation"] and data["citing_paper_id"] == annotation["paper_id"] and target["status"] == "active"
                and data["cited_paper_id"] != data["citing_paper_id"] and data["source_key"] == annotation["source_key"]
                and data["target_source_key"] == target["source_key"] and data["context"] == annotation["content"]
                and target["source_key"] in [identifier(v) for v in passage["cited_dois"]] and target["source_key"] in annotation["content"].lower(), "ACTUAL_CITATION_CONTEXT_AND_TARGET")
            check(data["status"] == "active" and data["created_at"] == now and data["task_id"] == public["task_id"], "CITATION_CREATION_IDENTITY")
            check(not any(c["status"] == "active" and c["annotation_id"] == data["annotation_id"] and c["cited_paper_id"] == data["cited_paper_id"]
                for c in tables["citation"].values()), "DUPLICATE_CITATION_OPERATION")
            tables["citation"][data["citation_id"]] = deepcopy(data)
        elif kind == "citation.retired":
            old = tables["citation"][data["citation_id"]]
            check(old["status"] == "active" and data == {**old, "status": "removed"}, "CITATION_RETIREMENT")
            old["status"] = "removed"
        elif kind == "publications.merged":
            primary, secondary = tables["paper"][data["primary_paper_id"]], tables["paper"][data["secondary_paper_id"]]
            check(primary["status"] == secondary["status"] == "active" and primary["paper_id"] != secondary["paper_id"], "MERGE_ACTIVE_IDENTITIES")
            check(identifier(records[primary["source_record_id"]]["doi"]) == identifier(records[secondary["source_record_id"]]["doi"])
                == primary["source_key"] == secondary["source_key"] == data["source_key"], "MERGE_AUTHORITATIVE_WORK_IDENTITY")
            expected_kinds = {"paper", "collection_paper", "reading_list_entry", "paper_tag", "annotation", "citation"}
            check(set(data["before"]) == set(data["after"]) == expected_kinds, "MERGE_TABLE_SCOPE")
            delta = data.get("snapshot_scope") == "changed_rows_v1"
            check(not delta or public.get("instance_revision") in {"researchops_portfolio150_v1", "researchops_portfolio150_v2"}, "MERGE_SNAPSHOT_PROFILE")
            premerge = {k: deepcopy(list(tables[k].values())) for k in expected_kinds}
            if not delta:
                for table in expected_kinds:
                    check(data["before"][table] == premerge[table], "MERGE_PRESTATE:" + table)
            aliases[secondary["paper_id"]] = primary["paper_id"]
            secondary.update(status="merged", merged_into=primary["paper_id"], updated_at=now)
            for relation, field, _, _ in RELATIONS:
                for row in tables[relation].values():
                    if row["status"] == "active" and row["paper_id"] == secondary["paper_id"]:
                        twin = next((r for r in tables[relation].values() if r["status"] == "active" and r["paper_id"] == primary["paper_id"] and r[field] == row[field]), None)
                        row["paper_id"] = primary["paper_id"]
                        if twin:
                            row.update(status="merged_duplicate", merged_into=twin["link_id"])
            for annotation in tables["annotation"].values():
                if annotation["paper_id"] == secondary["paper_id"]:
                    annotation.update(paper_id=primary["paper_id"], updated_at=now)
            for citation in tables["citation"].values():
                affected = False
                for endpoint in ("citing_paper_id", "cited_paper_id"):
                    if citation[endpoint] == secondary["paper_id"]:
                        citation[endpoint], affected = primary["paper_id"], True
                if affected and citation["status"] == "active" and citation["citing_paper_id"] == citation["cited_paper_id"]:
                    citation["status"] = "same_work_reference"
            for table in expected_kinds:
                after_rows = list(tables[table].values())
                if delta:
                    changed = {r[PRIMARY[table]] for r, a in zip(premerge[table], after_rows) if r != a}
                    check(data["before"][table] == [r for r in premerge[table] if r[PRIMARY[table]] in changed], "MERGE_PRESTATE:" + table)
                    after_rows = [r for r in after_rows if r[PRIMARY[table]] in changed]
                check(data["after"][table] == after_rows, "MERGE_EXACT_OWNERSHIP_TRANSFER:" + table)
        elif kind == "artifact.created":
            check(data["artifact_id"] not in artifacts and data["round"] == round_index and data["created_at"] == now
                and data["size_bytes"] == len(data["content"].encode()), "ARTIFACT_CREATION_IDENTITY")
            content_file(data["path"], data["content"])
            if data["kind"] == "bibliography":
                parsed = parse_string(data["content"], "bibtex")
                check(set(parsed.entries) == {e["citation_key"] for e in data["entries"]} and len(parsed.entries) == len(data["entries"]), "BIBTEX_PARSED_ENTRY_KEYS")
                check(len({e["source_key"] for e in data["entries"]}) == len(data["entries"]), "BIBTEX_DUPLICATE_WORK")
                for binding in data["entries"]:
                    paper = tables["paper"][binding["paper_id"]]
                    check(paper["status"] == "active" and binding["source_key"] == paper["source_key"] and binding["source_revision"] == current[paper["source_key"]]
                        == paper["source_revision"] and binding["catalog_revision"] == paper["catalog_revision"], "BIBTEX_ACTUAL_VERSION_BINDING")
                    check(binding["metadata"] == {k: paper[k] for k in FIELDS}, "BIBTEX_METADATA_SNAPSHOT")
                    names = [tables["author"][i]["name"] for i in paper["authors"]]
                    check(binding["author_names"] == names, "BIBTEX_ORDERED_AUTHOR_BINDING")
                    entry = parsed.entries[binding["citation_key"]]
                    expected_type = {"conference": "inproceedings", "journal": "article"}.get(paper["venue_type"], "misc")
                    venue_field = {"inproceedings": "booktitle", "article": "journal"}.get(expected_type, "howpublished")
                    expected_fields = {"title": paper["title"], "year": str(paper["year"]), venue_field: paper["venue"], "doi": identifier(paper["doi"]), "url": paper["url"]}
                    check(entry.type == expected_type and {k: Text.from_latex(v).render_as("text") for k, v in entry.fields.items()} == expected_fields, "BIBTEX_PARSED_METADATA_FIDELITY")
                    # Synthetic fixture names have unambiguous given/family tokens, in fixed source order.
                    check([" ".join(p.first_names + p.middle_names + p.last_names) for p in entry.persons["author"]] == names, "BIBTEX_PARSED_AUTHOR_ORDER")
            elif data["kind"] == "review":
                evaluations[data["artifact_id"]] = review_outcome(data)
            else:
                check(False, "UNKNOWN_ARTIFACT_KIND")
            artifacts[data["artifact_id"]] = deepcopy(data)
        else:
            check(False, "UNKNOWN_BUSINESS_EVENT")
        check(event["round"] == round_index and event["occurred_at"] == now, "EVENT_ROUND_OR_CLOCK")
        recount()

    for kind in PRIMARY:
        check(world["tables"][kind] == list(tables[kind].values()), "RECONSTRUCTED_TABLE:" + kind)
    check(state["round"] == round_index and world["clock"] == now and world["task_id"] == public["task_id"], "WORLD_ROUND_CLOCK_IDENTITY")
    check(state["batches"] == initial["batches"] and state["source_records"] == records and state["source_current"] == current, "FROZEN_SOURCE_HISTORY")
    check(state["aliases"] == aliases and state["artifacts"] == artifacts and state["committed_reviews"] == committed, "RECONSTRUCTED_LIBRARY_HISTORY")
    actual_sources = deepcopy(state["sources"])
    source_paths = set()
    for value in actual_sources.values():
        for version in value["versions"].values():
            relative = version.pop("path")
            check(relative not in source_paths, "DISTINCT_SOURCE_VERSION_FILES")
            source_paths.add(relative)
            content_file(relative, "\n\n".join(p["text"] for p in version["passages"]))
    check(actual_sources == sources, "FROZEN_SOURCE_TEXT_AND_METADATA")
    outcomes = []
    for index in range(public["rounds"]):
        chosen = committed.get(str(index))
        if chosen is None and index == round_index:
            candidates = [a["artifact_id"] for a in artifacts.values() if a["kind"] == "review" and a["round"] == index]
            chosen = candidates[-1] if candidates else None
        failures = (review_outcome(artifacts[chosen]) if index == round_index else evaluations[chosen]) if chosen else ["ROUND_REVIEW_NOT_CREATED"]
        outcomes.append({"round": index, "artifact_id": chosen, "accepted": not failures, "errors": failures})
    integrity = not errors
    task_errors = sorted(set(e for outcome in outcomes for e in outcome["errors"]))
    return {"accepted": integrity and all(r["accepted"] for r in outcomes) and round_index == public["rounds"] - 1,
        "integrity_valid": integrity, "errors": sorted(set(errors + task_errors)), "integrity_errors": sorted(set(errors)),
        "rounds": outcomes, "completed_rounds": sum(r["accepted"] for r in outcomes), "required_rounds": public["rounds"],
        "source_works": len(sources), "catalog_records": len(tables["paper"]), "canonical_records": sum(p["status"] == "active" for p in tables["paper"].values()),
        "annotations": len(tables["annotation"]), "citation_contexts": len(tables["citation"]), "artifacts": len(artifacts),
        "business_events": len(state["events"]), "provider_calls": 0, "evidence_kind": "independent_zero_model_library_reconstruction",
        **({"synthesis_metrics_by_artifact": synthesis_metrics} if synthesis_metrics else {})}


def evaluate(task, runtime):
    try:
        return _evaluate(task, runtime)
    except (KeyError, ValueError, TypeError, IndexError, StopIteration, OSError, PybtexError) as error:
        return {"accepted": False, "integrity_valid": False, "errors": ["MALFORMED_LIBRARY_EVIDENCE:" + type(error).__name__], "provider_calls": 0}
