"""Library identity, current evidence and real immutable text/bibliography artifacts."""

from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import re

import jsonschema
from pybtex.database import BibliographyData, Entry, Person, parse_string
from pybtex.richtext import Text

from integrations.internal_mcp.io import load_backend, read_json, write_json, controller_source
from integrations.internal_mcp.tables import TableState
from .build import KEYS, ORIGIN


READS = {"search_papers", "get_paper", "get_citation_network"}
NATIVE_NAMES = {"paper": "paper", "papers": "paper", "author": "author", "authors": "author", "citation": "citation", "citations": "citation", "paper_tag": "paper_tag", "tag": "tag"}
METADATA = ("title", "abstract", "authors", "year", "venue", "venue_type", "doi", "arxiv_id", "url", "keywords", "study_type")


class BusinessError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise BusinessError(code)


def doi(value):
    value = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    return value


def schema(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required, "additionalProperties": False}


class NativeView:
    def __init__(self, library):
        self.library = library

    def list_entities(self, kind):
        actual = NATIVE_NAMES[kind]
        rows = self.library.sm.list_entities(actual)
        if actual in {"paper", "paper_tag", "citation"}:
            rows = [r for r in rows if r.get("status", "active") == "active"]
        if actual == "citation":
            rows = [r for r in rows if r.get("task_id") != self.library.public["task_id"] or self.library.annotation_current(r["annotation_id"])]
        return rows

    def get_entity(self, kind, identifier):
        actual = NATIVE_NAMES[kind]
        row = self.library.sm.get_entity(actual, identifier)
        return row if row and row.get("status", "active") == "active" else None


class Library:
    def __init__(self, task, runtime):
        self.task, self.runtime = Path(task).resolve(), Path(runtime).resolve()
        self.source = controller_source(self.task)
        if self.runtime.is_relative_to(self.source) or self.source.is_relative_to(self.runtime):
            raise ValueError("Use isolated runtime")
        self.backend = load_backend(self.source)
        self.public = read_json(self.task / "public/task.json")
        self.world = read_json(self.runtime / "world.json")
        require(self.world["task_id"] == self.public["task_id"], "TASK_RUNTIME_MISMATCH")
        self.sm = TableState(self.world, read_json(self.task / "background.json"), KEYS)
        self.view = NativeView(self)
        self.metadata = {n: deepcopy(self.backend._TOOL_METADATA[n]) for n in sorted(READS)}
        for entry in self.metadata.values():
            entry["input_schema"]["additionalProperties"] = False
        text = {"type": "string", "minLength": 1, "maxLength": 2000}
        ids = {"type": "array", "items": text, "uniqueItems": True, "maxItems": 256}
        integer = {"type": "integer", "minimum": 1}
        section_content = schema({"section_id": text, "title": text, "items": {"type": "array", "maxItems": 256,
            "items": schema({"paper_id": text, "annotation_id": text, "citation_ids": ids, "citation_key": text})}})
        definitions = {
            "read_library_workspace": ("Read declared section obligations, acquired round and actual artifact identities, not expected selections.", schema({})),
            "acquire_library_batch": ("Acquire the next source batch; later batches bind an actual prior review, not an evaluator success flag.", schema({"batch_id": text, "prior_review_artifact_id": text}, ["batch_id"])),
            "read_catalog_record": ("Read an active or merged task catalog record, including original identity and version bindings.", schema({"paper_id": text})),
            "read_source_record": ("Read the immutable acquisition record for an acquired paper, including identity evidence.", schema({"paper_id": text})),
            "read_source_text": ("Read actual versioned synthetic text and authoritative metadata, never fabricated historical PDF bytes.", schema({"paper_id": text, "source_revision": integer}, ["paper_id"])),
            "apply_source_revision": ("Update current catalog metadata from an actual released source revision; preserve old artifacts and unchanged passages.", schema({"paper_id": text, "source_revision": integer})),
            "merge_duplicate_papers": ("Merge only publications with matching immutable source identity. Transfer ownership, preserve contexts and retain aliases/history.", schema({"primary_paper_id": text, "secondary_paper_id": text})),
            "read_collection": ("Read actual section collection and reading-list membership, not a gold relevance set.", schema({"collection_id": text})),
            "add_paper_to_collection": ("Add any active task paper to a task collection; relevance is not validated here.", schema({"collection_id": text, "paper_id": text})),
            "remove_paper_from_collection": ("Remove a collection membership while retaining its historical record.", schema({"collection_id": text, "paper_id": text})),
            "add_paper_to_reading_list": ("Append an active paper to a reading list; preserves explicit membership identity and order.", schema({"list_id": text, "paper_id": text})),
            "remove_paper_from_reading_list": ("Remove an active reading-list membership and compact the remaining order.", schema({"list_id": text, "paper_id": text})),
            "assign_tag_to_paper": ("Assign an actual task tag without deciding whether the paper belongs in a requested section.", schema({"tag_id": text, "paper_id": text})),
            "remove_tag_from_paper": ("Retire a tag relation without deleting its history.", schema({"tag_id": text, "paper_id": text})),
            "set_paper_read_status": ("Record a reading-status claim; does not prove evidence extraction.", schema({"paper_id": text, "read_status": {"enum": ["unread", "reading", "read"]}})),
            "add_annotation": ("Bind actual text from the selected source passage and revision. Omit both start and end to quote the entire passage; otherwise supply both zero-based character offsets, end exclusive. Stale but real quotes remain distinguishable. This does not select relevant evidence for you.", schema({"paper_id": text,
                "source_revision": integer, "passage_id": text, "start": {"type": "integer", "minimum": 0}, "end": integer, "note": {"type": "string", "maxLength": 4000}}, ["paper_id", "source_revision", "passage_id"])),
            "read_annotation": ("Read exact quote, original ownership and whether the whole source passage is still unchanged.", schema({"annotation_id": text})),
            "list_annotations": ("List actual task annotations for one canonical paper.", schema({"paper_id": text})),
            "add_citation": ("Bind a citation to an actual reference annotation and a source-identified target work; caller cannot invent its evidence.", schema({"annotation_id": text, "cited_paper_id": text})),
            "remove_citation": ("Retire a citation while preserving its actual source context and identity.", schema({"citation_id": text})),
            "list_citation_evidence": ("Read all current and historical task citation contexts for one canonical source paper.", schema({"paper_id": text})),
            "generate_bibtex": ("Save a real versioned Pybtex artifact; return identity and version/key bindings, not relevance grading. read_library_artifact retrieves full content and metadata.", schema({"entries": {"type": "array", "minItems": 1, "maxItems": 256, "items": schema({"paper_id": text, "citation_key": text})}})),
            "read_library_artifact": ("Read exact saved bibliography/review content and its immutable source mappings.", schema({"artifact_id": text})),
            "compose_literature_review": ("Render an actual review from supplied section evidence and an existing bibliography; validates references, not required coverage.", schema({"bibliography_artifact_id": text,
                "sections": {"type": "array", "minItems": 1, "maxItems": 4, "items": section_content},
                "include_content": {"type": "boolean", "description": "Return saved full content (default true), or a small creation receipt; read_library_artifact retrieves the unchanged content."}},
                ["bibliography_artifact_id", "sections"])),
        }
        self.metadata.update({n: {"description": d, "input_schema": s} for n, (d, s) in definitions.items()})
        self.metadata["add_annotation"]["description"] += " Returns the exact quote; read_annotation also retrieves the original full passage."
        if "synthesis_policy" in self.public:
            from .synthesis_contract import schemas
            section_schema, global_schema = schemas(calibration_graph="calibration_graph" in self.public["synthesis_policy"])
            inputs = self.metadata["compose_literature_review"]["input_schema"]
            sections = inputs["properties"]["sections"]["items"]
            sections["properties"]["synthesis"] = section_schema
            sections["required"].append("synthesis")
            inputs["properties"]["cross_section_synthesis"] = global_schema
            inputs["required"].append("cross_section_synthesis")
            self.metadata["compose_literature_review"]["description"] += " Synthesis follows the public nested schema. Numbers and evidence joins remain authored content, not auto-corrected or certified by this rendering tool."
        self.validate_state()
        self.validate_files()

    @property
    def state(self):
        return self.world["library"]

    def now(self):
        return self.world["clock"]

    def next_id(self, kind):
        value = self.state["next_id"]
        self.state["next_id"] += 1
        from integrations.internal_mcp.topology import runtime_identifier
        return runtime_identifier(self.public, kind, value, f"{kind}-{self.public['task_id']}-{value}")

    def owned(self, kind, identifier):
        if kind == "section":
            rows = [s for s in self.public["sections"] if s["section_id"] == identifier]
            require(len(rows) == 1, "SECTION_NOT_FOUND")
            return deepcopy(rows[0])
        row = self.sm.get_entity(kind, identifier)
        require(row is not None, kind.upper() + "_NOT_FOUND")
        require(row.get("task_id") == self.public["task_id"], "BACKGROUND_READ_ONLY")
        return row

    def resolve(self, identifier):
        seen = set()
        while identifier in self.state["aliases"]:
            require(identifier not in seen, "ALIAS_CYCLE")
            seen.add(identifier)
            identifier = self.state["aliases"][identifier]
        return identifier

    def paper(self, identifier):
        row = self.owned("paper", self.resolve(identifier))
        require(row["status"] == "active", "PAPER_NOT_ACTIVE")
        return row

    def source_version(self, source_key, revision=None):
        require(source_key in self.state["sources"], "SOURCE_NOT_ACQUIRED")
        revision = self.state["source_current"][source_key] if revision is None else revision
        require(str(revision) in self.state["sources"][source_key]["versions"], "SOURCE_REVISION_NOT_RELEASED")
        return deepcopy(self.state["sources"][source_key]["versions"][str(revision)])

    def event(self, kind, data):
        row = {"event_id": self.next_id("event"), "type": kind, "round": self.state["round"], "sequence": self.world["sequence"], "occurred_at": self.now(), "data": deepcopy(data)}
        self.state["events"].append(row)

    def annotation_current(self, identifier):
        row = self.owned("annotation", identifier)
        current = self.source_version(row["source_key"])
        matches = [p for p in current["passages"] if p["passage_id"] == row["passage_id"]]
        return len(matches) == 1 and matches[0]["text"] == row["source_passage_text"]

    def native(self, name, arguments):
        args = deepcopy(arguments)
        if "paper_id" in args and args["paper_id"] in self.state["aliases"]:
            args["paper_id"] = self.resolve(args["paper_id"])
        result = self.backend.call_tool(name, state_manager=self.view, **args)["content"]
        if result.get("message", "").startswith("TOOL_EXECUTION_ERROR"):
            raise RuntimeError(result["message"])
        require(result["status"] == "success", result.get("message", "NATIVE_TOOL_ERROR"))
        data = result["data"]
        if name == "get_paper":
            row = self.sm.get_entity("paper", args["paper_id"])
            if row.get("task_id") == self.public["task_id"]:
                data.update({k: row[k] for k in ("source_key", "source_revision", "catalog_revision", "status", "study_type")})
                data["latest_source_revision"] = self.state["source_current"][row["source_key"]]
        return data

    def dispatch(self, name, args):
        if name in READS:
            return self.native(name, args)
        if name == "read_library_workspace":
            return {"round": self.state["round"], "sections": deepcopy(self.public["sections"]), "batches": deepcopy(self.public["batches"]),
                "committed_reviews": deepcopy(self.state["committed_reviews"]), "artifacts": [{k: a[k] for k in ("artifact_id", "kind", "round", "created_at")} for a in self.state["artifacts"].values()]}
        if name == "acquire_library_batch":
            return self.acquire(args)
        if name == "read_catalog_record":
            row = self.owned("paper", args["paper_id"])
            return {"record": row, "canonical_paper_id": self.resolve(row["paper_id"])}
        if name == "read_source_record":
            row = self.owned("paper", args["paper_id"])
            return deepcopy(self.state["source_records"][row["source_record_id"]])
        if name == "read_source_text":
            row = self.paper(args["paper_id"])
            version = self.source_version(row["source_key"], args.get("source_revision"))
            content = self.read_file(version["path"])
            require(content == "\n\n".join(p["text"] for p in version["passages"]), "SOURCE_FILE_CONTENT_MISMATCH")
            return {"source_key": row["source_key"], "paper_id": row["paper_id"], "catalog_source_revision": row["source_revision"], **version,
                "text": content}
        if name == "apply_source_revision":
            row = self.paper(args["paper_id"])
            require(args["source_revision"] == self.state["source_current"][row["source_key"]], "ONLY_CURRENT_SOURCE_CAN_UPDATE_CATALOG")
            source = self.source_version(row["source_key"], args["source_revision"])
            if all(row[k] == source["metadata"][k] for k in METADATA) and row["source_revision"] == args["source_revision"]:
                return row
            after = self.sm.update_entity("paper", row["paper_id"], {**{k: source["metadata"][k] for k in METADATA}, "source_revision": args["source_revision"],
                "catalog_revision": row["catalog_revision"] + 1, "bibtex_generated": False, "bibtex_key": None, "updated_at": self.now()})
            self.event("catalog.source_applied", {"before": row, "after": after})
            return after
        if name == "merge_duplicate_papers":
            return self.merge(args)
        if name == "read_collection":
            collection = self.owned("collection", args["collection_id"])
            section = next(s for s in self.public["sections"] if s["collection_id"] == collection["collection_id"])
            return {"collection": collection, "memberships": [r for r in self.world["tables"]["collection_paper"] if r["collection_id"] == collection["collection_id"] and r["status"] == "active"],
                "reading_list": self.owned("reading_list", section["reading_list_id"]), "reading_entries": sorted([r for r in self.world["tables"]["reading_list_entry"] if r["list_id"] == section["reading_list_id"] and r["status"] == "active"], key=lambda r: r["position"])}
        links = {"add_paper_to_collection": ("collection_paper", "collection", "collection_id", True), "remove_paper_from_collection": ("collection_paper", "collection", "collection_id", False),
            "add_paper_to_reading_list": ("reading_list_entry", "reading_list", "list_id", True), "remove_paper_from_reading_list": ("reading_list_entry", "reading_list", "list_id", False),
            "assign_tag_to_paper": ("paper_tag", "tag", "tag_id", True), "remove_tag_from_paper": ("paper_tag", "tag", "tag_id", False)}
        if name in links:
            return self.link(args, *links[name])
        if name == "set_paper_read_status":
            row = self.paper(args["paper_id"])
            after = self.sm.update_entity("paper", row["paper_id"], {"read_status": args["read_status"], "updated_at": self.now()})
            self.event("catalog.read_status", {"paper_id": row["paper_id"], "read_status": args["read_status"]})
            return after
        if name == "add_annotation":
            row = self.paper(args["paper_id"])
            source = self.source_version(row["source_key"], args["source_revision"])
            passages = [p for p in source["passages"] if p["passage_id"] == args["passage_id"]]
            require(len(passages) == 1, "SOURCE_PASSAGE_NOT_FOUND")
            passage = passages[0]
            require(("start" in args) == ("end" in args), "ANNOTATION_SPAN_PAIR_REQUIRED")
            start, end = args.get("start", 0), args.get("end", len(passage["text"]))
            require(0 <= start < end <= len(passage["text"]), "ANNOTATION_SPAN")
            record = self.sm.add_entity("annotation", {"annotation_id": self.next_id("annotation"), "paper_id": row["paper_id"], "original_paper_id": row["paper_id"],
                "source_key": row["source_key"], "source_revision": args["source_revision"], "passage_id": args["passage_id"], "start": start, "end": end,
                "content": passage["text"][start:end], "source_passage_text": passage["text"], "note": args.get("note", ""),
                "page_number": None, "annotation_type": "highlight", "created_at": self.now(), "updated_at": self.now()})
            self.event("annotation.created", record)
            return {**record, "current_for_source": self.annotation_current(record["annotation_id"])}
        if name == "read_annotation":
            row = self.owned("annotation", args["annotation_id"])
            return {**row, "current_for_source": self.annotation_current(row["annotation_id"])}
        if name == "list_annotations":
            row = self.paper(args["paper_id"])
            return {"paper_id": row["paper_id"], "annotations": [{**deepcopy(a), "current_for_source": self.annotation_current(a["annotation_id"])} for a in self.world["tables"]["annotation"] if a["paper_id"] == row["paper_id"]]}
        if name == "add_citation":
            annotation = self.owned("annotation", args["annotation_id"])
            citing, cited = self.paper(annotation["paper_id"]), self.paper(args["cited_paper_id"])
            require(citing["paper_id"] != cited["paper_id"], "SAME_CATALOG_RECORD_CITATION")
            source = self.source_version(annotation["source_key"], annotation["source_revision"])
            passage = next(p for p in source["passages"] if p["passage_id"] == annotation["passage_id"])
            require(cited["source_key"] in [doi(d) for d in passage["cited_dois"]] and cited["source_key"] in annotation["content"].lower(), "CITATION_NOT_IN_ACTUAL_QUOTE")
            existing = [c for c in self.world["tables"]["citation"] if c["annotation_id"] == annotation["annotation_id"] and c["cited_paper_id"] == cited["paper_id"] and c["status"] == "active"]
            require(not existing, "DUPLICATE_CITATION_CONTEXT")
            record = self.sm.add_entity("citation", {"citation_id": self.next_id("citation"), "citing_paper_id": citing["paper_id"], "cited_paper_id": cited["paper_id"],
                "annotation_id": annotation["annotation_id"], "source_key": citing["source_key"], "target_source_key": cited["source_key"], "context": annotation["content"],
                "status": "active", "created_at": self.now()})
            self.event("citation.created", record)
            return record
        if name == "remove_citation":
            row = self.owned("citation", args["citation_id"])
            require(row["status"] == "active", "CITATION_NOT_ACTIVE")
            after = self.sm.update_entity("citation", row["citation_id"], {"status": "removed"})
            self.event("citation.retired", after)
            return after
        if name == "list_citation_evidence":
            row = self.paper(args["paper_id"])
            return {"citations": [{**deepcopy(c), "current_for_source": self.annotation_current(c["annotation_id"])} for c in self.world["tables"]["citation"] if c["citing_paper_id"] == row["paper_id"]]}
        if name == "generate_bibtex":
            return self.bibliography(args["entries"])
        if name == "read_library_artifact":
            require(args["artifact_id"] in self.state["artifacts"], "ARTIFACT_NOT_FOUND")
            artifact = deepcopy(self.state["artifacts"][args["artifact_id"]])
            require(self.read_file(artifact["path"]) == artifact["content"], "ARTIFACT_FILE_CONTENT_MISMATCH")
            return artifact
        if name == "compose_literature_review":
            artifact = self.review(args)
            if args.get("include_content", True):
                return artifact
            return {**{key: artifact[key] for key in ("artifact_id", "kind", "path", "round")}, "content_included": False}
        raise BusinessError("TOOL_NOT_IMPLEMENTED")

    def acquire(self, args):
        next_round = self.state["round"] + 1
        require(next_round < len(self.state["batches"]), "ALL_BATCHES_ACQUIRED")
        batch = self.state["batches"][next_round]
        require(batch["batch_id"] == args["batch_id"], "ACQUISITION_ORDER")
        if next_round:
            prior = self.state["artifacts"].get(args.get("prior_review_artifact_id"))
            require(prior is not None and prior["kind"] == "review" and prior["round"] == self.state["round"], "ACTUAL_PRIOR_REVIEW_REQUIRED")
            self.state["committed_reviews"][str(self.state["round"])] = prior["artifact_id"]
        else:
            require("prior_review_artifact_id" not in args, "NO_PRIOR_REVIEW_FOR_FIRST_BATCH")
        self.state["round"] = next_round
        self.world["clock"] = (datetime.fromisoformat(ORIGIN.replace("Z", "+00:00")) + timedelta(days=next_round)).isoformat().replace("+00:00", "Z")
        for key, source in batch["works"].items():
            require(key not in self.state["sources"], "SOURCE_ALREADY_ACQUIRED")
            self.state["sources"][key] = deepcopy(source)
            for version in self.state["sources"][key]["versions"].values():
                version["path"] = f"sources/{self.next_id('source')}.txt"
            self.state["source_current"][key] = 1
        if batch["correction"]:
            correction = batch["correction"]
            self.state["sources"][correction["source_key"]]["versions"][str(correction["version"]["revision"])] = deepcopy(correction["version"])
            self.state["sources"][correction["source_key"]]["versions"][str(correction["version"]["revision"])]["path"] = f"sources/{self.next_id('source')}.txt"
            self.state["source_current"][correction["source_key"]] = correction["version"]["revision"]
        for row in batch["records"]:
            self.state["source_records"][row["source_record_id"]] = deepcopy(row)
            self.sm.add_entity("paper", {**deepcopy(row), "created_at": self.now(), "updated_at": self.now()})
        record = {"batch_id": batch["batch_id"], "round": next_round, "paper_ids": [p["paper_id"] for p in batch["records"]],
            "prior_review_artifact_id": args.get("prior_review_artifact_id"), "publisher_correction": deepcopy(batch["correction"])}
        self.event("batch.acquired", record)
        return record

    def link(self, args, kind, parent_kind, field, adding):
        parent, paper = self.owned(parent_kind, args[field]), self.paper(args["paper_id"])
        rows = [r for r in self.world["tables"][kind] if r[field] == args[field] and r["paper_id"] == paper["paper_id"] and r["status"] == "active"]
        require(not rows if adding else len(rows) == 1, "MEMBERSHIP_ALREADY_PRESENT" if adding else "MEMBERSHIP_NOT_FOUND")
        if adding:
            if kind == "collection_paper":
                require(sum(r[field] == args[field] and r["status"] == "active" for r in self.world["tables"][kind]) < parent["max_papers"], "COLLECTION_CAPACITY")
            values = {"link_id": self.next_id("link"), field: args[field], "paper_id": paper["paper_id"], "status": "active", "added_at": self.now()}
            if kind == "paper_tag":
                values["assigned_at"] = self.now()
            if kind == "reading_list_entry":
                values.update(position=1 + sum(r[field] == args[field] and r["status"] == "active" for r in self.world["tables"][kind]), priority="normal")
            row = self.sm.add_entity(kind, values)
        else:
            row = self.sm.update_entity(kind, rows[0]["link_id"], {"status": "removed"})
        self.event("membership.changed", {"kind": kind, "row": row})
        return row

    def merge(self, args):
        primary, secondary = self.paper(args["primary_paper_id"]), self.paper(args["secondary_paper_id"])
        require(primary["paper_id"] != secondary["paper_id"], "SAME_CANONICAL_PAPER")
        original = [self.state["source_records"][p["source_record_id"]] for p in (primary, secondary)]
        require(doi(original[0]["doi"]) == doi(original[1]["doi"]) == primary["source_key"] == secondary["source_key"], "DISTINCT_AUTHORITATIVE_PUBLICATIONS")
        before = {k: deepcopy(self.world["tables"][k]) for k in ("paper", "collection_paper", "reading_list_entry", "paper_tag", "annotation", "citation")}
        self.state["aliases"][secondary["paper_id"]] = primary["paper_id"]
        self.sm.update_entity("paper", secondary["paper_id"], {"status": "merged", "merged_into": primary["paper_id"], "updated_at": self.now()})
        for kind, field in (("collection_paper", "collection_id"), ("reading_list_entry", "list_id"), ("paper_tag", "tag_id")):
            for row in list(self.world["tables"][kind]):
                if row["paper_id"] != secondary["paper_id"] or row["status"] != "active":
                    continue
                other = next((r for r in self.world["tables"][kind] if r["paper_id"] == primary["paper_id"] and r[field] == row[field] and r["status"] == "active"), None)
                values = {"paper_id": primary["paper_id"]}
                if other:
                    values.update(status="merged_duplicate", merged_into=other["link_id"])
                self.sm.update_entity(kind, row["link_id"], values)
        for row in list(self.world["tables"]["annotation"]):
            if row["paper_id"] == secondary["paper_id"]:
                self.sm.update_entity("annotation", row["annotation_id"], {"paper_id": primary["paper_id"], "updated_at": self.now()})
        for row in list(self.world["tables"]["citation"]):
            values = {k: primary["paper_id"] if row[k] == secondary["paper_id"] else row[k] for k in ("citing_paper_id", "cited_paper_id")}
            if values != {k: row[k] for k in values}:
                if values["citing_paper_id"] == values["cited_paper_id"] and row["status"] == "active":
                    values["status"] = "same_work_reference"
                self.sm.update_entity("citation", row["citation_id"], values)
        after = {k: deepcopy(self.world["tables"][k]) for k in before}
        scope = {}
        if self.public.get("instance_revision") in {"researchops_portfolio150_v1", "researchops_portfolio150_v2"}:
            # Only changed rows are evidence of the ownership transfer. Unrelated
            # catalog snapshots would grow quadratically with late aliases.
            changed = {k: {r[KEYS[k]] for r, a in zip(before[k], after[k]) if r != a} for k in before}
            before = {k: [r for r in rows if r[KEYS[k]] in changed[k]] for k, rows in before.items()}
            after = {k: [r for r in rows if r[KEYS[k]] in changed[k]] for k, rows in after.items()}
            scope = {"snapshot_scope": "changed_rows_v1"}
        self.event("publications.merged", {"primary_paper_id": primary["paper_id"], "secondary_paper_id": secondary["paper_id"], "source_key": primary["source_key"],
            "before": before, "after": after, **scope})
        return {"canonical_paper_id": primary["paper_id"], "alias_paper_id": secondary["paper_id"], "source_key": primary["source_key"]}

    def save_artifact(self, kind, content, values, extension):
        identifier = self.next_id("artifact")
        relative = f"artifacts/{identifier}.{extension}"
        row = {"artifact_id": identifier, "kind": kind, "round": self.state["round"], "created_at": self.now(), "path": relative,
            "content": content, "size_bytes": len(content.encode()), **deepcopy(values)}
        self.state["artifacts"][identifier] = row
        self.event("artifact.created", row)
        return deepcopy(row)

    def bibliography(self, requested):
        require(self.state["round"] >= 0, "NO_ACQUISITION")
        entries, bindings, seen_work = {}, [], set()
        for item in requested:
            paper = self.paper(item["paper_id"])
            key = item["citation_key"]
            require(re.fullmatch(r"[A-Za-z][A-Za-z0-9:_-]{0,99}", key) is not None and key.lower() not in {k.lower() for k in entries}, "CITATION_KEY_INVALID_OR_DUPLICATE")
            require(paper["source_key"] not in seen_work, "DUPLICATE_WORK_IN_BIBLIOGRAPHY")
            require(paper["source_revision"] == self.state["source_current"][paper["source_key"]], "CATALOG_SOURCE_REVISION_STALE")
            seen_work.add(paper["source_key"])
            authors = [self.owned("author", i) for i in paper["authors"]]
            entry_type = {"conference": "inproceedings", "journal": "article", "preprint": "misc"}.get(paper["venue_type"], "misc")
            venue_field = "booktitle" if entry_type == "inproceedings" else "journal" if entry_type == "article" else "howpublished"
            fields = {"title": paper["title"], "year": str(paper["year"]), venue_field: paper["venue"], "doi": doi(paper["doi"]), "url": paper["url"]}
            entries[key] = Entry(entry_type, fields=fields, persons={"author": [Person(a["name"]) for a in authors]})
            bindings.append({"paper_id": paper["paper_id"], "source_key": paper["source_key"], "source_revision": paper["source_revision"], "catalog_revision": paper["catalog_revision"],
                "citation_key": key, "metadata": {k: deepcopy(paper[k]) for k in METADATA}, "author_names": [a["name"] for a in authors]})
        try:
            content = BibliographyData(entries).to_string("bibtex")
            parsed = parse_string(content, "bibtex")
            require(len(parsed.entries) == len(entries), "BIBLIOGRAPHY_PARSE_CARDINALITY")
            for key, entry in entries.items():
                require(all(Text.from_latex(parsed.entries[key].fields[field]).render_as("text") == value
                    for field, value in entry.fields.items()), "BIBLIOGRAPHY_TEXT_FIDELITY")
        except BusinessError:
            raise
        except Exception:
            raise BusinessError("BIBLIOGRAPHY_SERIALIZATION_FAILED") from None
        return self.save_artifact("bibliography", content, {"entries": bindings}, "bib")

    def review(self, args):
        bibliography = self.state["artifacts"].get(args["bibliography_artifact_id"])
        require(bibliography is not None and bibliography["kind"] == "bibliography" and bibliography["round"] == self.state["round"], "CURRENT_ROUND_BIBLIOGRAPHY_REQUIRED")
        mapping = {e["paper_id"]: e for e in bibliography["entries"]}
        require(len({s["section_id"] for s in args["sections"]}) == len(args["sections"]), "DUPLICATE_REVIEW_SECTION")
        sections, lines = [], [f"# Synthetic literature review, round {self.state['round']}", ""]
        for section in args["sections"]:
            self.owned("section", section["section_id"])
            lines += ["## " + section["title"], ""]
            snapshots = []
            for item in section["items"]:
                paper, annotation = self.paper(item["paper_id"]), self.owned("annotation", item["annotation_id"])
                require(paper["paper_id"] in mapping and mapping[paper["paper_id"]]["citation_key"] == item["citation_key"], "REVIEW_BIBLIOGRAPHY_KEY_JOIN")
                binding = mapping[paper["paper_id"]]
                require(binding["catalog_revision"] == paper["catalog_revision"] and binding["source_revision"] == self.state["source_current"][paper["source_key"]], "BIBLIOGRAPHY_BINDING_STALE")
                require(annotation["paper_id"] == paper["paper_id"] and self.annotation_current(annotation["annotation_id"]), "REVIEW_ANNOTATION_OWNERSHIP_OR_STALE_PASSAGE")
                citations = [self.owned("citation", i) for i in item["citation_ids"]]
                require(all(c["citing_paper_id"] == paper["paper_id"] and c["status"] == "active" and self.annotation_current(c["annotation_id"]) for c in citations), "REVIEW_CITATION_EVIDENCE")
                snapshots.append({**deepcopy(item), "paper_id": paper["paper_id"], "source_key": paper["source_key"], "annotation": annotation, "citations": citations})
                lines += [annotation["content"] + " [@" + item["citation_key"] + "]", ""]
            sections.append({"section_id": section["section_id"], "title": section["title"], "items": snapshots})
            if "synthesis_policy" in self.public:
                sections[-1]["synthesis"] = deepcopy(section["synthesis"])
                lines += ["Synthesis: " + json.dumps(section["synthesis"], sort_keys=True), ""]
        extra = {}
        if "synthesis_policy" in self.public:
            extra["cross_section_synthesis"] = deepcopy(args["cross_section_synthesis"])
            lines += ["## Cross-section synthesis", "", json.dumps(args["cross_section_synthesis"], sort_keys=True), ""]
        lines += ["## Bibliography", "", "```bibtex", bibliography["content"].rstrip(), "```", ""]
        return self.save_artifact("review", "\n".join(lines), {"bibliography_artifact_id": bibliography["artifact_id"], "sections": sections, **extra}, "md")

    def recount(self):
        for kind, field, parent_kind, count_field in (("collection_paper", "collection_id", "collection", "paper_count"), ("reading_list_entry", "list_id", "reading_list", "paper_count"), ("paper_tag", "tag_id", "tag", "usage_count")):
            for parent in self.world["tables"][parent_kind]:
                rows = [r for r in self.world["tables"][kind] if r[field] == parent[KEYS[parent_kind]] and r["status"] == "active"]
                self.sm.update_entity(parent_kind, parent[KEYS[parent_kind]], {count_field: len(rows)})
                if kind == "reading_list_entry":
                    for position, row in enumerate(sorted(rows, key=lambda r: (r["position"], r["link_id"])), 1):
                        self.sm.update_entity(kind, row["link_id"], {"position": position})
        for author in self.world["tables"]["author"]:
            count = sum(p["status"] == "active" and author["author_id"] in p["authors"] for p in self.world["tables"]["paper"])
            self.sm.update_entity("author", author["author_id"], {"paper_count": count})

    def validate_state(self):
        for kind, rows in self.world["tables"].items():
            require(len({r[KEYS[kind]] for r in rows}) == len(rows) and all(r.get("task_id") == self.public["task_id"] for r in rows), "ENTITY_IDENTITY")
        for alias in self.state["aliases"]:
            self.paper(alias)
        for kind, field in (("collection_paper", "collection_id"), ("reading_list_entry", "list_id"), ("paper_tag", "tag_id")):
            active = [r for r in self.world["tables"][kind] if r["status"] == "active"]
            require(len({(r[field], r["paper_id"]) for r in active}) == len(active), "COMPOSITE_MEMBERSHIP_IDENTITY")
            require(all(self.owned("paper", r["paper_id"])["status"] == "active" for r in active), "MEMBERSHIP_OWNER_NOT_CANONICAL")
        for row in self.world["tables"]["annotation"]:
            require(self.owned("paper", row["paper_id"])["source_key"] == row["source_key"], "ANNOTATION_WORK_OWNERSHIP")

    def record_call(self, name, arguments, result):
        pass

    def result_data(self, name, data):
        if name == "add_annotation":
            return {k: deepcopy(v) for k, v in data.items() if k != "source_passage_text"}
        if name == "generate_bibtex":
            return {**{k: deepcopy(v) for k, v in data.items() if k not in {"content", "entries"}},
                "content_included": False,
                "entries": [{k: deepcopy(v) for k, v in entry.items() if k not in {"metadata", "author_names"}}
                            for entry in data["entries"]]}
        if name == "set_paper_read_status":
            return {k: deepcopy(data[k]) for k in ("paper_id", "source_key", "source_revision", "catalog_revision", "status", "read_status", "updated_at")}
        return data

    @staticmethod
    def files(world):
        records = {a["path"]: a["content"] for a in world["library"]["artifacts"].values()}
        for source in world["library"]["sources"].values():
            for version in source["versions"].values():
                records[version["path"]] = "\n\n".join(p["text"] for p in version["passages"])
        return records

    def file_path(self, relative):
        path = self.runtime / relative
        require(not Path(relative).is_absolute() and path.resolve().is_relative_to(self.runtime), "RUNTIME_FILE_PATH")
        require(not path.is_symlink(), "RUNTIME_FILE_SYMLINK")
        return path

    def read_file(self, relative):
        path = self.file_path(relative)
        require(path.is_file(), "RUNTIME_FILE_MISSING")
        return path.read_text(encoding="utf-8")

    def validate_files(self):
        for relative, content in self.files(self.world).items():
            require(self.read_file(relative) == content, "RUNTIME_FILE_CONTENT_MISMATCH")

    def persist(self, before, *, write_world=True):
        previous, current = self.files(before), self.files(self.world)
        require(all(current.get(path) == content for path, content in previous.items()), "IMMUTABLE_FILE_REWRITTEN")
        created = []
        try:
            for relative in current.keys() - previous.keys():
                path = self.file_path(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                # A crash before the atomic state commit can leave an identical orphan.
                if path.exists():
                    require(path.read_text(encoding="utf-8") == current[relative], "ORPHAN_FILE_CONFLICT")
                    continue
                with path.open("x", encoding="utf-8") as handle:
                    created.append(path)
                    handle.write(current[relative])
            if write_world:
                write_json(self.runtime / "world.json", self.world)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise

    def snapshot(self):
        return deepcopy(self.world)

    def call(self, name, arguments):
        before = self.snapshot()
        try:
            require(name in self.metadata, "TOOL_NOT_EXPOSED")
            try:
                json.dumps(arguments, allow_nan=False)
                jsonschema.validate(arguments, self.metadata[name]["input_schema"])
            except (ValueError, TypeError, jsonschema.ValidationError):
                raise BusinessError("ARGUMENT_SCHEMA") from None
            data = self.dispatch(name, deepcopy(arguments))
            if name not in READS:
                self.recount()
                self.validate_state()
            result = {"status": "success", "data": self.result_data(name, data), "message": ""}
            if name in {"compose_literature_review", "submit_review_draft"}:
                result["message"] = (
                    "Review saved as an immutable artifact. Supplied bindings validated; "
                    "coverage and business correctness not checked. This round is not locked yet: "
                    "submit a new review to correct it. Acquiring the next batch locks the review "
                    "you select; locked earlier rounds cannot be replaced. "
                    "For the final round, the latest review is used."
                )
            elif name == "acquire_library_batch" and arguments.get("prior_review_artifact_id"):
                result["message"] = (
                    f"Previous-round review {arguments['prior_review_artifact_id']} is now locked "
                    "in acquisition history and cannot be replaced. This commitment does not "
                    "certify coverage or business correctness."
                )
        except BusinessError as error:
            self.world = before
            self.sm.bind(self.world)
            result = {"status": "error", "data": {}, "message": str(error)}
        except Exception:
            self.world = before
            self.sm.bind(self.world)
            raise
        try:
            self.record_call(name, arguments, result)
            self.world["sequence"] += 1
            self.persist(before)
        except Exception:
            self.world = before
            self.sm.bind(self.world)
            raise
        return result
