"""Read-only local Library goals on the exact first-completion business cut."""

from copy import deepcopy

from pybtex.database import parse_string
from pybtex.exceptions import PybtexError
from pybtex.richtext import Text

from .declaration_build import LOCAL_GOALS
from .evaluate import FIELDS, PRIMARY, identifier, review_goal_outcome
from .kernel import BusinessError


class C11Goals:
    def __init__(self, runtime, initial):
        self.runtime, self.initial = runtime, initial
        self.state, self.public = runtime.state, runtime.public
        self.tables = {kind: {row[PRIMARY[kind]]: row for row in rows}
                       for kind, rows in runtime.world["tables"].items() if kind in PRIMARY}

    def objects(self, kind):
        if kind in {"paper", "annotation", "citation"}:
            return self.tables[kind]
        if kind == "section":
            return {s["section_id"]: s for s in self.public["sections"]}
        if kind == "source_key":
            return self.state["sources"]
        if kind in {"bibliography", "review"}:
            return {key: row for key, row in self.state["artifacts"].items() if row["kind"] == kind}
        return None

    def references(self, refs, check):
        if not isinstance(refs, list):
            raise ValueError("Library references must be a list")
        result = []
        for ref in refs:
            if (not isinstance(ref, dict) or set(ref) != {"kind", "id"}
                    or not all(isinstance(value, str) and value for value in ref.values())):
                raise ValueError("Library references require kind/id strings")
            rows = self.objects(ref["kind"])
            check("REFERENCE_KIND", rows is not None)
            row = rows.get(ref["id"]) if rows is not None else None
            check("REFERENCE_EXISTS", row is not None)
            if row is not None:
                if ref["kind"] in {"paper", "annotation", "citation"}:
                    check("REFERENCE_TASK", row.get("task_id") == self.public["task_id"])
                    if row.get("task_id") != self.public["task_id"]:
                        continue
                result.append((ref["kind"], ref["id"], row))
        return result

    def expected(self, section):
        criteria = section["criteria"]
        return {key for key in self.state["sources"]
                if (version := self.runtime.source_version(key))["metadata"]["year"] >= criteria["year_min"]
                and version["metadata"]["study_type"] == criteria["study_type"]
                and criteria["topic"] in version["metadata"]["keywords"]}

    def annotation(self, row, check):
        paper = self.tables["paper"][row["paper_id"]]
        source = self.runtime.source_version(row["source_key"], row["source_revision"])
        passages = [p for p in source["passages"] if p["passage_id"] == row["passage_id"]]
        check("ANNOTATION_CANONICAL_OWNER", paper["status"] == "active" and paper["source_key"] == row["source_key"])
        check("ANNOTATION_SOURCE_PASSAGE", len(passages) == 1)
        if len(passages) == 1:
            text = passages[0]["text"]
            check("ANNOTATION_EXACT_SPAN", row["source_passage_text"] == text
                  and type(row["start"]) is int and type(row["end"]) is int
                  and 0 <= row["start"] < row["end"] <= len(text)
                  and row["content"] == text[row["start"]:row["end"]])
        check("ANNOTATION_CURRENT_WHOLE_PASSAGE", self.runtime.annotation_current(row["annotation_id"]))
        check("ANNOTATION_ACTUAL_SOURCE_FILE", self.runtime.read_file(source["path"])
              == "\n\n".join(p["text"] for p in source["passages"]))

    def canonical(self, key, check):
        papers = [p for p in self.tables["paper"].values() if p["source_key"] == key]
        active = [p for p in papers if p["status"] == "active"]
        check("ONE_CANONICAL_WORK", len(active) == 1)
        if len(active) != 1:
            return
        canonical = active[0]["paper_id"]
        for paper in papers:
            original = self.state["source_records"][paper["source_record_id"]]
            check("AUTHORITATIVE_DOI_IDENTITY", identifier(original["doi"]) == key)
            check("PRESERVED_ALIAS_RESOLUTION", self.runtime.resolve(paper["paper_id"]) == canonical)
            if paper["paper_id"] == canonical:
                continue
            for kind in ("collection_paper", "reading_list_entry", "paper_tag", "annotation", "citation"):
                fields = ("citing_paper_id", "cited_paper_id") if kind == "citation" else ("paper_id",)
                for row in self.tables[kind].values():
                    relevant = kind in {"annotation", "citation"} or row["status"] == "active"
                    check("CANONICAL_OWNERSHIP:" + kind, not relevant or all(row[f] != paper["paper_id"] for f in fields))

    def section(self, row, check):
        expected = self.expected(row)
        for kind, field, parent in (("collection_paper", "collection_id", row["collection_id"]),
                                    ("reading_list_entry", "list_id", row["reading_list_id"]),
                                    ("paper_tag", "tag_id", row["tag_id"])):
            links = [link for link in self.tables[kind].values() if link[field] == parent and link["status"] == "active"]
            papers = [self.tables["paper"][link["paper_id"]] for link in links]
            works = [paper["source_key"] for paper in papers]
            check("SECTION_CANONICAL_MEMBERS:" + kind, all(p["status"] == "active" for p in papers))
            check("SECTION_EXACT_WORKS:" + kind, len(works) == len(set(works)) and set(works) == expected)

    def citation(self, row, check):
        annotation = self.tables["annotation"][row["annotation_id"]]
        self.annotation(annotation, check)
        citing, cited = (self.tables["paper"][row[field]] for field in ("citing_paper_id", "cited_paper_id"))
        check("CURRENT_CITATION_OWNER", row["status"] == "active" and citing["status"] == cited["status"] == "active"
              and annotation["paper_id"] == citing["paper_id"] and row["target_source_key"] == cited["source_key"])
        contexts = [(p["passage_id"], identifier(target), p["text"])
                    for p in self.runtime.source_version(citing["source_key"])["passages"] for target in p["cited_dois"]]
        check("EXACT_CITATION_CONTEXT", (annotation["passage_id"], row["target_source_key"], annotation["content"]) in contexts)

    def citation_network(self, key, check):
        paper = self.runtime.paper(key)
        expected = [(p["passage_id"], identifier(target), p["text"])
                    for p in self.runtime.source_version(paper["source_key"])["passages"] for target in p["cited_dois"]]
        rows = [r for r in self.tables["citation"].values() if r["citing_paper_id"] == paper["paper_id"] and r["status"] == "active"]
        actual = []
        for row in rows:
            self.citation(row, check)
            annotation = self.tables["annotation"][row["annotation_id"]]
            actual.append((annotation["passage_id"], row["target_source_key"], annotation["content"]))
        check("ALL_CURRENT_REFERENCE_CONTEXTS", sorted(actual) == sorted(expected))

    def actual_artifact(self, row, check):
        check("ACTUAL_ARTIFACT_BYTES", self.runtime.read_file(row["path"]) == row["content"]
              and row["size_bytes"] == len(row["content"].encode()))
        check("CURRENT_ARTIFACT_ROUND", row["round"] == self.state["round"])

    def bibliography(self, row, check, sections=None):
        self.actual_artifact(row, check)
        entries = row["entries"]
        expected = set().union(*(self.expected(s) for s in (self.public["sections"] if sections is None else sections)))
        works, keys = [e["source_key"] for e in entries], [e["citation_key"] for e in entries]
        check("BIBLIOGRAPHY_EXACT_WORK_SET", set(works) == expected and len(works) == len(set(works)))
        check("BIBLIOGRAPHY_UNIQUE_KEYS", len(keys) == len({key.lower() for key in keys}))
        parsed = parse_string(row["content"], "bibtex")
        check("BIBTEX_PARSED_KEYS", set(parsed.entries) == set(keys) and len(parsed.entries) == len(entries))
        for binding in entries:
            paper = self.tables["paper"][binding["paper_id"]]
            check("BIBLIOGRAPHY_CURRENT_BINDING", paper["status"] == "active" and binding["source_key"] == paper["source_key"]
                  and binding["source_revision"] == self.state["source_current"][paper["source_key"]] == paper["source_revision"]
                  and binding["catalog_revision"] == paper["catalog_revision"])
            check("BIBLIOGRAPHY_METADATA", binding["metadata"] == {k: paper[k] for k in FIELDS})
            names = [self.tables["author"][key]["name"] for key in paper["authors"]]
            check("BIBLIOGRAPHY_AUTHORS", binding["author_names"] == names)
            entry = parsed.entries.get(binding["citation_key"])
            if entry is None:
                continue
            entry_type = {"conference": "inproceedings", "journal": "article"}.get(paper["venue_type"], "misc")
            venue = {"inproceedings": "booktitle", "article": "journal"}.get(entry_type, "howpublished")
            fields = {"title": paper["title"], "year": str(paper["year"]), venue: paper["venue"],
                      "doi": identifier(paper["doi"]), "url": paper["url"]}
            check("BIBTEX_PARSED_METADATA", entry.type == entry_type
                  and {k: Text.from_latex(v).render_as("text") for k, v in entry.fields.items()} == fields)
            check("BIBTEX_AUTHOR_ORDER", [" ".join(p.first_names + p.middle_names + p.last_names)
                  for p in entry.persons.get("author", [])] == names)

    def review(self, row, check):
        self.actual_artifact(row, check)
        bib = self.state["artifacts"][row["bibliography_artifact_id"]]
        check("REVIEW_ACTUAL_BIBLIOGRAPHY", bib["kind"] == "bibliography")
        self.bibliography(bib, check)
        errors, _ = review_goal_outcome(self.public, row, self.state["round"], self.tables,
                                        self.state["sources"], self.state["source_current"], self.state["artifacts"])
        check("FULL_REVIEW_RULES", not errors)
        for error in errors:
            check(error, False)

    def assess(self, claim):
        skill = claim["skill_id"]
        if skill not in LOCAL_GOALS:
            raise ValueError("Unknown Library skill")
        clauses = []

        def check(rule, passed):
            clauses.append({"rule": rule, "passed": passed})

        scope = self.references(claim.get("scope", []), check)
        outputs = self.references(claim.get("outputs", []), check)
        result = {**deepcopy(claim), "process_adherence": "unobserved"}
        if skill == "inspect-library-request":
            return {**result, "goal_verdict": "unknown", "clauses": clauses, "reason": "UNDERSTANDING_UNOBSERVED"}
        kind, method = {
            "reconcile-publication-identities": ("source_key", self.canonical),
            "curate-section-collections": ("section", self.section),
            "annotate-source-evidence": ("annotation", self.annotation),
            "maintain-citation-network": ("paper", self.citation_network),
            "build-versioned-bibliography": ("bibliography", self.bibliography),
            "assemble-literature-handoff": ("review", self.review),
        }[skill]
        allowed = {kind}
        if kind == "source_key" or kind == "annotation":
            allowed.add("paper")
        if skill == "maintain-citation-network":
            allowed.add("citation")
        # Explicit sections override the documented whole-task default.
        if kind == "bibliography":
            allowed.add("section")
        for ref_kind, _, _ in scope + outputs:
            check("REFERENCE_KIND_FOR_GOAL", ref_kind in allowed)
        if kind == "bibliography":
            for ref_kind, _, _ in outputs:
                check("OUTPUT_ARTIFACT_KIND", ref_kind == kind)
        selected = {key: row for ref_kind, key, row in scope + outputs if ref_kind == kind}
        if kind == "source_key":
            selected.update({row["source_key"]: self.state["sources"][row["source_key"]]
                             for ref_kind, _, row in scope + outputs if ref_kind == "paper"})
        if skill == "maintain-citation-network":
            for ref_kind, _, row in scope + outputs:
                if ref_kind == "citation":
                    self.citation(row, check)
                    selected[row["citing_paper_id"]] = self.tables["paper"][row["citing_paper_id"]]
            selected = {self.runtime.resolve(key): row for key, row in selected.items()}
        anchors = {key for ref_kind, key, _ in scope if ref_kind == kind}
        if skill == "maintain-citation-network":
            anchors = {self.runtime.resolve(key) for key in anchors}
        if kind == "source_key":
            anchors.update(row["source_key"] for ref_kind, _, row in scope if ref_kind == "paper")
        if len(anchors) == 1:
            check("OUTPUT_TARGET", set(selected).issubset(anchors))
        if kind == "annotation":
            papers = {self.runtime.resolve(key) for ref_kind, key, _ in scope + outputs if ref_kind == "paper"}
            if papers and selected:
                check("ANNOTATION_OUTPUT_OWNERS", {row["paper_id"] for row in selected.values()} == papers)
        for key, row in selected.items():
            try:
                if kind == "bibliography":
                    sections = [row for ref_kind, _, row in scope if ref_kind == "section"]
                    self.bibliography(row, check, sections=sections or None)
                else:
                    method(key if kind == "source_key" or skill == "maintain-citation-network" else row, check)
            except (BusinessError, PybtexError) as error:
                check("LOCAL_GOAL:" + type(error).__name__, False)
        if len(selected) != 1:
            check("UNIQUE_GOAL_TARGET", None)
        verdict = ("contradicted" if any(c["passed"] is False for c in clauses) else
                   "unknown" if any(c["passed"] is None for c in clauses) else "supported")
        return {**result, "clauses": clauses, "goal_verdict": verdict,
                "reason": {"supported": "CURRENT_LOCAL_GOAL_SATISFIED", "contradicted": "LOCAL_GOAL_CONTRADICTED",
                           "unknown": "MISSING_OR_AMBIGUOUS_TARGET"}[verdict]}
