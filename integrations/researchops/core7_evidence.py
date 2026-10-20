"""Read-only C11 static history evidence, NOT public negative controls.

Usage (from the suite source root, with no builder/runtime/provider imports)::

    python -B -m integrations.researchops.core7_evidence TASK RUNTIME

JSON is written only to stdout; exit 2 means coverage_missing, not a business
failure. Required inputs are TASK/public/task.json, RUNTIME/world.json, and the
exact RUNTIME-relative source-version and review-artifact files named in that world.
The world must retain library.sources (all versions), source_current, batches,
events, artifacts, committed_reviews and tables.annotation/paper. A summary or
final review alone is insufficient. No private evaluator inputs are required.

For the frozen Core7 cell, TASK is
/workspace/runs/skillhorizon-harbor-mainline-20260917/core7-enrichment-20260917-v1/
cells/C11-ambiguous_withdrawal-s7/construction/task and RUNTIME is TASK/runtime;
the owner must verify that exact existing path and its original source identity.
This module and sibling synthesis_evaluate.py must come from the reviewed source
package. Do not overlay the original frozen source/world to run this extractor.
The planned single-world CPU8 extraction is limited to 60 seconds by the owner,
only after code review; this module neither launches nor authorizes that run.

Only explicit acquisition/commit/version/binding relations establish evidence.
Event ordering locates these relations; elapsed time and update counts prove
nothing. Static consistency cannot prove public-call rejection, historical file
immutability against coordinated rewrites, or full business/replay integrity.
Scope is limited to the three CLAIMS below. Numerical/binding consistency reuses
synthesis_evaluate.verify; this is not a new evaluator. Bibliography, citations,
membership and Markdown rendering are not revalidated. No fixture fallback,
environment reconstruction, repair or additional coverage is attempted.
"""

import argparse
from copy import deepcopy
import json
from pathlib import Path

from .synthesis_evaluate import verify


CLAIMS = ("revised_ancestor_current_dependency", "unrelated_annotation_id_reuse",
          "historical_reviews_readable")


class EvidenceGap(ValueError):
    """The saved inputs cannot establish the requested evidence."""


def need(condition, code):
    if not condition:
        raise EvidenceGap(code)


def indexed(rows, key):
    result = {row[key]: row for row in rows}
    need(len(result) == len(rows), "duplicate:" + key)
    return result


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def persisted(runtime, relative, content):
    path = Path(relative)
    need(not path.is_absolute() and ".." not in path.parts and path.parts,
         "unsafe_relative_path")
    candidate = runtime / path
    need(not any((runtime / Path(*path.parts[:i])).is_symlink()
                 for i in range(1, len(path.parts) + 1)), "symlink_input")
    need(candidate.resolve().is_relative_to(runtime), "path_outside_runtime")
    try:
        actual = candidate.read_bytes()
    except OSError:
        raise EvidenceGap("evidence_gap:required_file_unreadable:" + str(path)) from None
    need(actual == content.encode("utf-8"),
         "persisted_content_mismatch:" + str(path))


def results(version):
    passages = indexed(version["passages"], "passage_id")
    return passages["results"]["text"]


def selected(version, section):
    metadata, criteria = version["metadata"], section["criteria"]
    return (metadata["year"] >= criteria["year_min"]
            and metadata["study_type"] == criteria["study_type"]
            and criteria["topic"] in metadata["keywords"])


def dependencies(key, sources, visiting=None):
    """Derive dependencies from source text, never from the submitted chain."""
    visiting = set() if visiting is None else visiting
    need(key not in visiting, "cyclic_dependency:" + key)
    value = json.loads(results(sources[key]))
    if value["role"] == "study":
        parents = [value["calibration_doi"]]
    else:
        need(value["role"] == "calibration", "unknown_measurement_role")
        parents = [p["source_key"] for p in value["parent_calibrations"]]
    need(len(parents) == len(set(parents)), "duplicate_dependency:" + key)
    closure = {key}
    for parent in parents:
        closure |= dependencies(parent, sources, visiting | {key})
    return closure


def history(public, world, runtime):
    """Project saved provenance; do not execute business operations or a solver."""
    state = world["library"]
    need(world["task_id"] == public["task_id"], "task_identity_mismatch")
    need(type(public["rounds"]) is int and public["rounds"] > 0, "invalid_round_count")
    public_batches = indexed(public["batches"], "round")
    batches = indexed(state["batches"], "round")
    need(set(public_batches) == set(batches) == set(range(public["rounds"])),
         "incomplete_batch_requirements")
    versions, current, papers, annotations = {}, {}, {}, {}
    artifacts, snapshots, commits, changes = {}, {}, {}, []
    event_ids, sequence, round_index = set(), -1, -1
    paths = set()

    def register(key, revision, original):
        actual = state["sources"][key]["versions"][str(revision)]
        need(type(revision) is int and actual["revision"] == revision, "invalid_source_revision")
        need({k: v for k, v in actual.items() if k != "path"} == original,
             "source_acquisition_mismatch:" + key)
        need(actual["path"] not in paths, "source_file_reused")
        paths.add(actual["path"])
        persisted(runtime, actual["path"], "\n\n".join(p["text"] for p in actual["passages"]))
        need((key, revision) not in versions, "source_revision_replaced:" + key)
        versions[key, revision] = actual
        current[key] = revision

    for event in state["events"]:
        need(event["event_id"] not in event_ids and type(event["sequence"]) is int
             and event["sequence"] > sequence, "ambiguous_event_identity_or_order")
        event_ids.add(event["event_id"])
        sequence = event["sequence"]
        kind, data = event["type"], event["data"]
        if kind == "batch.acquired":
            round_index += 1
            batch, requirement = batches[round_index], public_batches[round_index]
            need(data["round"] == round_index and data["batch_id"] == batch["batch_id"]
                 == requirement["batch_id"] and data["publisher_correction"] == batch["correction"]
                 and data["paper_ids"] == [p["paper_id"] for p in batch["records"]]
                 and requirement["has_publisher_correction"] == (batch["correction"] is not None),
                 "acquisition_binding_mismatch")
            prior = data["prior_review_artifact_id"]
            if round_index:
                need(prior in snapshots and artifacts[prior]["round"] == round_index - 1,
                     "missing_explicit_prior_review")
                commits[str(round_index - 1)] = prior
            else:
                need(prior is None, "unexpected_initial_prior_review")
            for key, source in batch["works"].items():
                need(key not in current and set(source["versions"]) == {"1"}, "ambiguous_initial_source")
                register(key, 1, source["versions"]["1"])
            correction = batch["correction"]
            if correction:
                key, new = correction["source_key"], correction["version"]["revision"]
                old = current[key]
                need(type(new) is int and new > old and prior is not None, "unbound_source_correction")
                register(key, new, correction["version"])
                changes.append({"event_id": event["event_id"], "round": round_index,
                                "source_key": key, "old_revision": old, "new_revision": new,
                                "prior_review_id": prior})
            for paper in batch["records"]:
                need(paper["paper_id"] not in papers, "duplicate_acquired_paper")
                papers[paper["paper_id"]] = deepcopy(paper)
        elif kind == "catalog.source_applied":
            identifier = data["after"]["paper_id"]
            need(identifier in papers and data["after"]["source_key"] == papers[identifier]["source_key"],
                 "catalog_source_identity_changed")
            papers[identifier] = deepcopy(data["after"])
        elif kind == "annotation.created":
            identifier = data["annotation_id"]
            need(identifier not in annotations, "duplicate_annotation_creation")
            need((data["source_key"], data["source_revision"]) in versions,
                 "annotation_revision_not_yet_acquired")
            version = versions[data["source_key"], data["source_revision"]]
            text = indexed(version["passages"], "passage_id")[data["passage_id"]]["text"]
            paper = papers[data["paper_id"]]
            need(paper["source_key"] == data["source_key"] and paper["status"] == "active"
                 and data["source_passage_text"] == text
                 and type(data["start"]) is int and type(data["end"]) is int
                 and 0 <= data["start"] < data["end"] <= len(text)
                 and data["content"] == text[data["start"]:data["end"]],
                 "annotation_source_span_mismatch:" + identifier)
            annotations[identifier] = deepcopy(data)
        elif kind == "publications.merged":
            # Alias transfers may change paper ownership, but not source/span identity.
            before = indexed(data["before"]["annotation"], "annotation_id")
            after = indexed(data["after"]["annotation"], "annotation_id")
            need(before == annotations and set(before) == set(after), "annotation_merge_history_gap")
            for identifier, row in before.items():
                need({k: v for k, v in row.items() if k not in {"paper_id", "updated_at"}}
                     == {k: v for k, v in after[identifier].items() if k not in {"paper_id", "updated_at"}},
                     "annotation_content_changed_in_merge")
            annotations = deepcopy(after)
            papers = deepcopy(indexed(data["after"]["paper"], "paper_id"))
        elif kind == "artifact.created" and data["kind"] == "review":
            identifier = data["artifact_id"]
            need(identifier not in artifacts and data == state["artifacts"][identifier]
                 and data["round"] == round_index, "artifact_snapshot_mismatch")
            need(data["path"] not in paths, "artifact_file_reused")
            paths.add(data["path"])
            persisted(runtime, data["path"], data["content"])
            need(data["size_bytes"] == len(data["content"].encode("utf-8")), "artifact_size_mismatch")
            artifacts[identifier] = data
            snapshots[identifier] = {"review": data, "event_id": event["event_id"],
                "sources": {key: versions[key, rev] for key, rev in current.items()},
                "annotations": deepcopy(annotations), "papers": deepcopy(papers)}
        need(event["round"] == round_index, "event_round_mismatch")

    need(round_index == state["round"] == public["rounds"] - 1, "incomplete_acquisition_history")
    need(current == state["source_current"] and commits == state["committed_reviews"],
         "terminal_source_or_commit_mismatch")
    need(annotations == indexed(world["tables"]["annotation"], "annotation_id"),
         "terminal_annotation_history_mismatch")
    final_papers = indexed(world["tables"]["paper"], "paper_id")
    need(set(final_papers) == set(papers) and all(
        all(final_papers[key][field] == row[field] for field in ("source_key", "status"))
        for key, row in papers.items()), "terminal_paper_identity_mismatch")
    need(set(artifacts) == {key for key, row in state["artifacts"].items() if row["kind"] == "review"},
         "review_creation_event_missing")
    need(set(versions) == {(key, int(rev)) for key, source in state["sources"].items()
                           for rev in source["versions"]}, "unbound_source_history")
    last = [identifier for identifier, row in artifacts.items()
            if row["kind"] == "review" and row["round"] == round_index]
    need(last, "final_review_missing")
    chosen = {int(r): snapshots[key] for r, key in commits.items()}
    chosen[round_index] = snapshots[last[-1]]
    need(set(chosen) == set(range(public["rounds"])), "review_round_missing")
    return chosen, changes


def validate_review(public, snapshot):
    row, sources = snapshot["review"], snapshot["sources"]
    annotations, papers = snapshot["annotations"], snapshot["papers"]
    sections = indexed(row["sections"], "section_id")
    need(set(sections) == {s["section_id"] for s in public["sections"]}, "review_section_coverage")
    items_by_section = {}
    for section in public["sections"]:
        items = indexed(sections[section["section_id"]]["items"], "source_key")
        need(set(items) == {key for key, version in sources.items() if selected(version, section)},
             "review_source_coverage")
        for key, item in items.items():
            annotation = annotations[item["annotation_id"]]
            text = results(sources[key])
            need(item["annotation"] == annotation
                 and item["paper_id"] == annotation["paper_id"]
                 and papers[item["paper_id"]]["source_key"] == annotation["source_key"] == key
                 and papers[item["paper_id"]]["status"] == "active"
                 and annotation["passage_id"] == "results" and annotation["source_passage_text"] == text
                 and annotation["content"] == text and annotation["start"] == 0 and annotation["end"] == len(text),
                 "review_annotation_not_current:" + item["annotation_id"])
        items_by_section[section["section_id"]] = items
    errors, _ = verify(public, row, sources, annotations, papers)
    need(not errors, "synthesis_evidence:" + ",".join(errors))
    return items_by_section


def claim(evidence, gaps, *, empty="coverage_missing", reason="no_witness"):
    return {"status": "coverage_missing" if gaps else "passed" if evidence else empty,
            "evidence": evidence, "gaps": gaps, "reason": reason if not evidence and not gaps else None}


def extract(task, runtime=None):
    """Return scoped static evidence; never write inputs or invoke business tools."""
    task = Path(task).resolve()
    runtime = (task / "runtime" if runtime is None else Path(runtime)).resolve()
    report = {"scope": "static_history_evidence_not_public_negative_controls", "provider_calls": 0,
        "task_path": str(task), "runtime_path": str(runtime), "status": "coverage_missing",
        "claims": {}, "limitations": ["No public calls, replay or full business evaluation performed.",
        "Consistent saved snapshots are not external proof against coordinated history rewrites.",
        "Bibliography, citations, membership and Markdown rendering are not revalidated.",
        "Unrelated reuse excludes sources whose dependency closure includes the changed source."]}
    try:
        public, world = read_json(task / "public/task.json"), read_json(runtime / "world.json")
        report["task_id"] = public["task_id"]
        need(public["format"] == "skillhorizon.c11.calibration_network.v2"
             and "calibration_graph" in public["synthesis_policy"],
             "public_calibration_requirement_missing_or_unsupported")
        indexed(public["sections"], "section_id")
        chosen, changes = history(public, world, runtime)
        items = {r: validate_review(public, snapshot) for r, snapshot in chosen.items()}
        ancestor, ancestor_gaps, reuse, reuse_gaps, historical = [], [], [], [], []
        meaningful_changes = 0
        for r, snapshot in sorted(chosen.items()):
            row = snapshot["review"]
            if r < public["rounds"] - 1:
                historical.append({"round": r, "review_id": row["artifact_id"],
                    "creation_event_id": snapshot["event_id"], "path": row["path"],
                    "commit_round": r + 1, "content_matches_saved_creation_and_file": True})
        for change in changes:
            r, key = change["round"], change["source_key"]
            before, after = chosen[r - 1], chosen[r]
            old_sources, new_sources = before["sources"], after["sources"]
            need(before["review"]["artifact_id"] == change["prior_review_id"], "correction_prior_review_mismatch")
            need(old_sources[key]["revision"] == change["old_revision"]
                 and new_sources[key]["revision"] == change["new_revision"], "review_correction_version_mismatch")
            old_text, new_text = results(old_sources[key]), results(new_sources[key])
            if old_text == new_text:
                continue
            meaningful_changes += 1
            context = {**change, "review_id": after["review"]["artifact_id"],
                       "before_round": r - 1, "after_round": r}
            old_value, new_value = json.loads(old_text), json.loads(new_text)
            map_fields = ("gain_numerator", "gain_denominator", "offset_points", "parent_calibrations")
            revised_map = (old_value["role"] == new_value["role"] == "calibration"
                           and any(old_value[f] != new_value[f] for f in map_fields))
            for section in public["sections"]:
                sid = section["section_id"]
                old_items, new_items = items[r - 1][sid], items[r][sid]
                contributions = next(s for s in after["review"]["sections"] if s["section_id"] == sid)["synthesis"]["contributions"]
                if revised_map:
                    for contribution in contributions:
                        anchor = contribution["calibration_source_key"]
                        closure = dependencies(anchor, new_sources)
                        if key not in closure:
                            continue
                        binding = indexed(contribution["calibration_chain"], "source_key")[key]
                        item = {**context, "section_id": sid, "study_source_key": contribution["source_key"],
                            "calibration_source_key": anchor, "ancestor_source_key": key,
                            "strict_ancestor": anchor != key, "dependency_source_keys": sorted(closure),
                            "current_annotation_id": binding["annotation_id"]}
                        if key not in old_items or old_items[key]["annotation_id"] == binding["annotation_id"]:
                            ancestor_gaps.append({**item, "reason": "old_to_current_ancestor_binding_not_proven"})
                        else:
                            ancestor.append({**item, "prior_annotation_id": old_items[key]["annotation_id"]})
                for source_key in sorted(set(old_items) & set(new_items)):
                    if (results(old_sources[source_key]) != results(new_sources[source_key])
                            or key in dependencies(source_key, old_sources) | dependencies(source_key, new_sources)):
                        continue
                    old_id, new_id = old_items[source_key]["annotation_id"], new_items[source_key]["annotation_id"]
                    evidence = {**context, "section_id": sid, "source_key": source_key,
                        "prior_annotation_id": old_id, "current_annotation_id": new_id,
                        "source_revision_before": old_sources[source_key]["revision"],
                        "source_revision_after": new_sources[source_key]["revision"]}
                    if old_id == new_id:
                        reuse.append(evidence)
                    else:
                        reuse_gaps.append({**evidence, "reason": "unchanged_unrelated_annotation_recreated"})
        report["claims"] = {
            CLAIMS[0]: claim(ancestor, ancestor_gaps, reason="no_explicit_used_ancestor_revision_witness"),
            CLAIMS[1]: claim(reuse, reuse_gaps, empty="not_applicable" if meaningful_changes else "coverage_missing",
                reason="no_unrelated_eligible_unchanged_object" if meaningful_changes else "no_source_change_pair"),
            CLAIMS[2]: claim(historical, [], empty="not_applicable" if public["rounds"] == 1 else "coverage_missing",
                reason="no_prior_round_review")}
        report["status"] = "coverage_missing" if any(
            row["status"] == "coverage_missing" for row in report["claims"].values()) else "passed"
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, StopIteration, RecursionError) as error:
        # Do not leak arbitrary malformed input strings or invent missing history.
        reason = str(error) if isinstance(error, EvidenceGap) else "input_evidence_unavailable:" + type(error).__name__
        report["claims"] = {name: claim([], [{"reason": reason}]) for name in CLAIMS}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task", type=Path)
    parser.add_argument("runtime", type=Path, nargs="?")
    args = parser.parse_args(argv)
    report = extract(args.task, args.runtime)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2 if report["status"] == "coverage_missing" else 0


if __name__ == "__main__":
    raise SystemExit(main())
