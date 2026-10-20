"""Versioned document review and publication over the original C25 tools."""

from copy import deepcopy
from datetime import datetime, timedelta
import importlib
import json
from pathlib import Path
from unittest.mock import patch

import jsonschema

from integrations.internal_mcp.io import load_backend, read_json, write_json, controller_source
from integrations.internal_mcp.tables import TableState
from integrations.internal_mcp.parameter_contract import apply_public_enums


KEYS = {"workspace": "workspace_id", "user": "user_id", "product_area": "area_id", "project": "project_id",
        "database_view": "view_id", "page": "page_id", "page_block": "block_id", "database_item": "item_id",
        "review_request": "review_id", "comment_thread": "thread_id", "comment": "comment_id",
        "decision_record": "decision_id", "stakeholder": "stakeholder_id", "relation": "relation_id", "activity_event": "event_id"}
READS = {"list_workspaces", "list_projects", "list_database_views", "list_pages", "get_page_detail", "search_pages",
         "list_review_requests", "list_comment_threads", "list_decision_records", "list_stakeholders", "inspect_activity_events"}
WRITES = {"revise_page_block", "request_page_review", "record_decision", "add_comment", "resolve_comment_thread",
          "approve_review_and_transition_page", "publish_decision_summary", "update_page_status"}


class BusinessError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise BusinessError(code)


def schema(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required,
            "additionalProperties": False}


def decode_contract(content, *, capacity_network=False, portfolio_network=False):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON member")
            result[key] = value
        return result

    result = json.loads(content, object_pairs_hook=unique, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    field = schema({"type": {"enum": ["string", "integer", "number", "boolean"]}, "unit": {"type": "string"}})
    properties = {
        "fields": {"type": "object", "minProperties": 1, "additionalProperties": field},
        "limits": schema({"batch_size": {"type": "integer", "minimum": 1}, "retention_days": {"type": "integer", "minimum": 1}}),
        "imports": {"type": "array", "maxItems": 8, "items": schema({"page_id": {"type": "string"},
            "page_version": {"type": "integer", "minimum": 1}, "publication_id": {"type": "string"}})}}
    if capacity_network:
        number = {"type": "integer", "minimum": 0}
        properties["capacity"] = schema({"local_units": number, "incoming_units": number, "uncommitted_units": number,
            "allocations": {"type": "array", "items": schema({"consumer_page_id": {"type": "string"},
                "activation_stage": {"type": "integer", "minimum": 1}, "units": number})}})
        if portfolio_network:
            source = schema({"origin_page_id": {"type": "string", "minLength": 1},
                "origin_stage": {"type": "integer", "minimum": 1}, "expires_stage": {"type": "integer", "minimum": 1},
                "allowed_purposes": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "minLength": 1}},
                "units": {"type": "integer", "minimum": 1}})
            sources = {"type": "array", "items": source}
            allocation = properties["capacity"]["properties"]["allocations"]["items"]
            allocation["properties"]["sources"] = sources
            allocation["required"].append("sources")
            for key in ("portfolio", "withheld_sources", "uncommitted_sources"):
                properties["capacity"]["properties"][key] = sources
                properties["capacity"]["required"].append(key)
    jsonschema.validate(result, schema(properties))
    require(len({row["page_id"] for row in result["imports"]}) == len(result["imports"]), "DUPLICATE_PARENT_IMPORT")
    return result


class Planning:
    def __init__(self, task, runtime):
        self.task, self.runtime = Path(task).resolve(), Path(runtime).resolve()
        self.controller = read_json(self.task / "controller.json")
        self.source = controller_source(self.task)
        if self.runtime.is_relative_to(self.source) or self.source.is_relative_to(self.runtime):
            raise ValueError("Use an isolated runtime")
        self.backend = load_backend(self.source)
        self.logic = importlib.import_module("mcp_servers.tool_impl")
        self.public = read_json(self.task / "public/task.json")
        self.world = read_json(self.runtime / "world.json")
        require(self.world["task_id"] == self.public["task_id"], "TASK_RUNTIME_MISMATCH")
        self.background = read_json(self.task / "background.json")
        self.sm = TableState(self.world, self.background, KEYS)
        self.metadata = {name: deepcopy(item) for name, item in self.backend.get_tool_metadata().items() if name in READS | WRITES}
        for item in self.metadata.values():
            item["input_schema"]["additionalProperties"] = False
        self.metadata["approve_review_and_transition_page"]["input_schema"]["properties"].pop("close_open_threads", None)
        self.metadata["publish_decision_summary"]["input_schema"]["properties"].pop("accept_proposed", None)
        self.metadata["record_decision"]["input_schema"]["properties"]["status"] = {"enum": ["proposed"]}
        self.metadata["update_page_status"]["input_schema"]["properties"]["new_status"] = {"enum": ["draft", "blocked", "archived"]}
        for name in ("record_decision", "request_page_review", "publish_decision_summary", "add_comment"):
            for key, value in self.metadata[name]["input_schema"]["properties"].items():
                if value.get("type") == "array":
                    value.update({"items": {"type": "string", "minLength": 1}, "maxItems": 20, "uniqueItems": True})
        self.metadata["publish_decision_summary"]["description"] = (
            "Publish an immutable reviewed-page and decision summary artifact. Does not edit approved page blocks or auto-approve trackers.")
        self.metadata["approve_review_and_transition_page"]["description"] = (
            "Accept an actual passing current-version review and its proposed decisions; cannot manufacture reviewer signoffs.")
        text = {"type": "string", "minLength": 1}
        extras = {
            "run_document_review": ("Run declared engineering/security contract checks and record environment reviewer decisions for a submitted review.", schema({"review_id": text})),
            "read_review": ("Read actual reviewer decisions, version bindings and public rule diagnostics.", schema({"review_id": text})),
            "read_publication": ("Read an immutable published document/decision artifact, including historical versions.", schema({"publication_id": text})),
            "list_publications": ("List publication history in creation order; records may be stale.", schema({"page_id": text,
                "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                "include_contract": {"type": "boolean", "default": True,
                    "description": "False returns publication headers; retrieve a selected immutable artifact with read_publication."}}, [])),
            "synchronize_tracker": ("Bind one task tracker to an actual current publication of its own page.", schema({"item_id": text, "publication_id": text})),
            "poll_planning_updates": ("Poll the declared post-delivery planning change window. Before all initial trackers are current, no update is released.", schema({})),
        }
        self.metadata.update({key: {"description": description, "input_schema": inputs} for key, (description, inputs) in extras.items()})
        apply_public_enums("C25", self.metadata)
        self.metadata["resolve_comment_thread"]["description"] = (
            "Close a repaired objection with accepted, deferred or documented, or use reopen to reopen it. "
            "Review-rule objections must pass their current underlying checks before any closing action.")
        self.compact_responses = self.public.get("policy", {}).get("response_profile") == "document_receipts_v2"
        if self.compact_responses:
            for name in ("read_publication", "read_review"):
                self.metadata[name]["input_schema"]["properties"]["include_snapshot"] = {
                    "type": "boolean", "default": False,
                    "description": "Include the full immutable document snapshot; otherwise return bindings, content/diagnostics and status without duplicate page blocks."}
            self.metadata["revise_page_block"]["description"] += " Returns an edit receipt and invalidated page IDs, without echoing submitted content."
            self.metadata["run_document_review"]["description"] += " Returns review bindings, decisions and checks without the already submitted document snapshot."
            self.metadata["publish_decision_summary"]["description"] += " Returns the complete contract and publication bindings once; use read_publication(include_snapshot=true) for page blocks."
        self.validate()

    @property
    def state(self):
        return self.world["planning"]

    def now(self):
        return (datetime.fromisoformat(self.world["clock_origin"].replace("Z", "+00:00")) +
                timedelta(seconds=self.world["sequence"])).isoformat().replace("+00:00", "Z")

    def owned(self, kind, identifier):
        row = self.sm.get_entity(kind, identifier)
        require(row is not None, f"{kind.upper()}_NOT_FOUND")
        require(row.get("task_id") == self.world["task_id"], "OUTSIDE_TASK_SCOPE")
        return row

    def actor(self, identifier):
        require(identifier == self.public["editor_user_id"], "ACTOR_IDENTITY_MISMATCH")
        require(self.owned("user", identifier)["status"] == "active", "ACTOR_INACTIVE")

    def update(self, kind, identifier, values):
        return self.sm.update_entity(kind, identifier, values)

    def add(self, kind, values):
        return self.sm.add_entity(kind, values)

    def next_id(self, kind):
        value = self.state["next_id"]
        self.state["next_id"] += 1
        from integrations.internal_mcp.topology import runtime_identifier
        return runtime_identifier(self.public, kind, value, f"{self.world['task_id']}-{kind}-{value}")

    def native(self, name, args):
        with patch.object(self.logic, "now_iso", self.now):
            output = self.backend.call_tool(name, state_manager=self.sm, **args)["content"]
        if output.get("message", "").startswith("INTERNAL_ERROR"):
            raise RuntimeError(output["message"])
        require(output.get("status") == "success", output.get("message", "NATIVE_TOOL_ERROR"))
        return output["data"]

    def blocks(self, page_id):
        return sorted([r for r in self.world["tables"]["page_block"] if r["page_id"] == page_id], key=lambda r: r["position"])

    def content_snapshot(self, page_id):
        return [{k: r[k] for k in ("block_id", "position", "block_type", "content", "checked")} for r in self.blocks(page_id)]

    def requirements(self, page_id):
        page = self.owned("page", page_id)
        return json.loads(self.owned("page_block", page["requirements_block_id"])["content"])

    def contract(self, page_id):
        page = self.owned("page", page_id)
        required = self.requirements(page_id)
        return decode_contract(self.owned("page_block", page["contract_block_id"])["content"],
            capacity_network="capacity_policy" in required, portfolio_network="portfolio_policy" in required)

    def publication(self, identifier, seen=None, verified=None):
        seen = set() if seen is None else seen
        # One validation traversal only: shared DAG ancestors need not be checked
        # once per path. Never retain this cache across business actions.
        verified = set() if verified is None else verified
        require(identifier not in seen, "PUBLICATION_CYCLE")
        seen.add(identifier)
        require(identifier in self.state["publications"], "PUBLICATION_NOT_FOUND")
        row = self.state["publications"][identifier]
        if identifier in verified:
            return deepcopy(row)
        page = self.owned("page", row["page_id"])
        require(page["version"] == row["page_version"] and row["blocks"] == self.content_snapshot(page["page_id"]), "STALE_PUBLICATION")
        require(page["lifecycle_status"] == "approved", "PAGE_NOT_APPROVED")
        review = self.owned("review_request", row["review_id"])
        require(page.get("approved_review_id") == row["review_id"] and review["status"] == "approved", "STALE_PUBLICATION_REVIEW")
        require(not any(t["page_id"] == page["page_id"] and t["status"] in {"open", "reopened"}
                        for t in self.world["tables"]["comment_thread"]), "UNRESOLVED_REVIEW_THREADS")
        for identifier in row["decision_ids"]:
            require(self.owned("decision_record", identifier)["status"] == "accepted", "DECISION_SUPERSEDED")
        for parent in row["contract"]["imports"]:
            upstream = self.publication(parent["publication_id"], set(seen), verified)
            require(upstream["page_id"] == parent["page_id"] and upstream["page_version"] == parent["page_version"], "PARENT_PUBLICATION_IDENTITY")
        verified.add(row["publication_id"])
        return deepcopy(row)

    def checks(self, page_id, *, include_passed_format=True):
        required = self.requirements(page_id)
        try:
            contract = self.contract(page_id)
        except (ValueError, jsonschema.ValidationError, BusinessError):
            return [{"rule": "CONTRACT_FORMAT", "passed": False, "reviewer_role": "both"}]
        # A repaired format objection needs an explicit current passing check.
        checks = [{"rule": "CONTRACT_FORMAT", "passed": True, "reviewer_role": "both"}] if include_passed_format else []
        imports = {r["page_id"]: r for r in contract["imports"]}
        valid_parents = set(imports) == set(required["parent_page_ids"])
        parents = []
        for identifier, binding in imports.items():
            try:
                row = self.publication(binding["publication_id"])
                require(row["page_id"] == identifier and row["page_version"] == binding["page_version"], "PARENT_PUBLICATION_IDENTITY")
                parents.append(row["contract"])
            except BusinessError:
                valid_parents = False
        checks.append({"rule": "CURRENT_PARENT_BINDINGS", "passed": valid_parents, "reviewer_role": "engineering"})
        fields = deepcopy(required["local_fields"])
        compatible = True
        for parent in parents:
            for name, definition in parent["fields"].items():
                if name in fields and fields[name] != definition:
                    compatible = False
                fields[name] = definition
        checks.append({"rule": "FIELD_UNION", "passed": compatible and contract["fields"] == fields, "reviewer_role": "engineering"})
        for limit, role in (("batch_size", "engineering"), ("retention_days", "security")):
            expected = min([required["local_limits"][limit]] + [r["limits"][limit] for r in parents])
            if limit == "batch_size" and "capacity_policy" in required:
                from .capacity_rules import capacity
                if "portfolio_policy" in required:
                    from .portfolio_rules import capacity
                try:
                    expected, allocation = capacity(required, [{"contract": p} for p in parents])
                    allocation_ok = contract["capacity"] == allocation
                    if required.get("portfolio_representation") == "unordered_collections_v2":
                        from .portfolio_rules import portfolio_value
                        allocation_ok = portfolio_value(contract["capacity"]) == portfolio_value(allocation)
                except (KeyError, TypeError, ValueError):
                    expected, allocation_ok = -1, False
                checks.append({"rule": "CAPACITY_ALLOCATION", "passed": valid_parents and allocation_ok, "reviewer_role": "engineering"})
            checks.append({"rule": limit.upper(), "passed": valid_parents and contract["limits"][limit] == expected, "reviewer_role": role})
        return checks

    def invalidate(self, page_id, *, include_self=True):
        affected = {page_id}
        while True:
            expanded = affected | {p["page_id"] for p in self.world["tables"]["page"]
                if set(self.requirements(p["page_id"])["parent_page_ids"]) & affected}
            if expanded == affected:
                break
            affected = expanded
        if not include_self:
            affected.discard(page_id)
        for identifier in affected:
            self.update("page", identifier, {"lifecycle_status": "draft", "updated_at": self.now()})
            for review in list(self.world["tables"]["review_request"]):
                if review["page_id"] == identifier and review["status"] != "canceled":
                    self.update("review_request", review["review_id"], {"prior_status": review["status"], "status": "canceled",
                        "invalidated_reason": "content_or_dependency_changed", "invalidated_at": self.now()})
            for item in self.world["tables"]["database_item"]:
                if item["page_id"] == identifier:
                    self.update("database_item", item["item_id"], {"status": "blocked"})
        self.recount()
        return sorted(affected)

    def recount(self):
        tables = self.world["tables"]
        for thread in tables["comment_thread"]:
            self.update("comment_thread", thread["thread_id"], {"comment_count": sum(c["thread_id"] == thread["thread_id"] for c in tables["comment"])})
        for page in tables["page"]:
            threads = [t for t in tables["comment_thread"] if t["page_id"] == page["page_id"]]
            self.update("page", page["page_id"], {"block_count": len(self.blocks(page["page_id"])), "comment_thread_count": len(threads),
                "unresolved_comment_count": sum(t["comment_count"] for t in threads if t["status"] in {"open", "reopened"})})
        for project in tables["project"]:
            pages = {p["page_id"] for p in tables["page"] if p["project_id"] == project["project_id"]}
            self.update("project", project["project_id"], {"page_count": len(pages),
                "decision_count": sum(d["page_id"] in pages for d in tables["decision_record"]),
                "open_review_count": sum(r["page_id"] in pages and r["status"] in {"requested", "in_review", "changes_requested"} for r in tables["review_request"])})

    def dispatch(self, name, args):
        if name in READS:
            return self.native(name, args)
        for key in ("actor_user_id", "editor_user_id", "requester_user_id", "owner_user_id", "author_user_id"):
            if key in args:
                self.actor(args[key])
        for key, kind in (("page_id", "page"), ("block_id", "page_block"), ("review_id", "review_request"),
                          ("target_page_id", "page"), ("item_id", "database_item"), ("workspace_id", "workspace"), ("thread_id", "comment_thread")):
            if args.get(key):
                self.owned(kind, args[key])
        if name == "read_review":
            return self.owned("review_request", args["review_id"])
        if name == "poll_planning_updates":
            pending = self.state["scheduled_updates"][len(self.state["applied_updates"]):]
            if not pending:
                return {"status": "drained", "events": []}
            try:
                for request in self.public["requests"]:
                    item = self.owned("database_item", request["tracker_item_id"])
                    require(item["status"] == "approved", "INITIAL_DELIVERY_PENDING")
                    row = self.publication(item.get("publication_id"))
                    require(row["page_id"] == request["page_id"] and row["page_version"] == item.get("published_version"), "INITIAL_DELIVERY_PENDING")
            except BusinessError:
                return {"status": "awaiting_initial_delivery", "events": []}
            event = pending[0]
            page = self.owned("page", event["page_id"])
            self.state["revision_history"].setdefault(page["page_id"], []).append({"page_version": page["version"], "blocks": self.content_snapshot(page["page_id"])})
            self.update("page_block", page["requirements_block_id"], {"content": json.dumps(event["requirements"]), "updated_at": self.now()})
            self.update("page", page["page_id"], {"version": page["version"] + 1, "updated_at": self.now()})
            affected = self.invalidate(page["page_id"])
            released = {"event_id": event["event_id"], "page_id": page["page_id"], "reason": event["reason"],
                        "released_at": self.now(), "page_version": page["version"] + 1}
            self.state["applied_updates"].append(released)
            self.add("activity_event", {**released, "workspace_id": page["workspace_id"], "actor_user_id": "environment",
                "entity_type": "page", "entity_id": page["page_id"], "event_type": "requirements.amended", "summary": event["reason"], "created_at": self.now()})
            return {"status": "update_released", "events": [deepcopy(released)], "invalidated_page_ids": affected}
        if name == "read_publication":
            require(args["publication_id"] in self.state["publications"], "PUBLICATION_NOT_FOUND")
            return deepcopy(self.state["publications"][args["publication_id"]])
        if name == "list_publications":
            rows = [r for r in self.state["publications"].values() if not args.get("page_id") or r["page_id"] == args["page_id"]]
            offset, limit = args.get("offset", 0), args.get("limit", 20)
            selected = deepcopy(rows[offset:offset + limit])
            if not args.get("include_contract", True):
                selected = [{key: row[key] for key in ("publication_id", "page_id", "page_version", "review_id", "decision_ids")}
                            for row in selected]
            return {"total": len(rows), "offset": offset, "publications": selected}
        if name == "revise_page_block":
            block = self.owned("page_block", args["block_id"])
            page = self.owned("page", block["page_id"])
            require(block["block_id"] != page["requirements_block_id"], "REQUIREMENTS_ARE_READ_ONLY")
            require("new_content" in args or "checked" in args, "MISSING_EDIT_PAYLOAD")
            if all(args.get(key) is None or args[key] == block[field] for key, field in (("new_content", "content"), ("checked", "checked"))):
                return {"block_id": block["block_id"], "page_version": page["version"], "idempotent": True}
            previous = {"page_version": page["version"], "blocks": self.content_snapshot(page["page_id"])}
            result = self.native(name, args)
            self.state["revision_history"].setdefault(page["page_id"], []).append(previous)
            result["invalidated_pages"] = self.invalidate(page["page_id"])
            return result
        if name == "record_decision":
            require(args["status"] == "proposed", "ACTOR_CANNOT_ACCEPT_DECISION")
            for identifier in args.get("linked_item_ids", []):
                require(self.owned("database_item", identifier)["page_id"] == args["page_id"], "DECISION_ITEM_IDENTITY")
            if args.get("supersedes_decision_id"):
                old = self.owned("decision_record", args["supersedes_decision_id"])
                require(old["page_id"] == args["page_id"] and old["status"] == "accepted", "INVALID_SUPERSESSION")
                require(not any(d["status"] == "proposed" and d.get("supersedes_decision_id") == old["decision_id"]
                    for d in self.world["tables"]["decision_record"]), "CONFLICTING_SUPERSESSION_PROPOSALS")
            return self.native(name, args)
        if name == "request_page_review":
            page = self.owned("page", args["page_id"])
            required = self.requirements(page["page_id"])
            require(set(args["reviewer_user_ids"]) == set(required["reviewer_user_ids"]), "REQUIRED_REVIEWERS_MISMATCH")
            require(args.get("required_signoff_count", len(required["reviewer_user_ids"])) == len(required["reviewer_user_ids"]), "REVIEW_QUORUM_MISMATCH")
            try:
                due = datetime.fromisoformat(args["due_at"].replace("Z", "+00:00"))
                require(due.tzinfo is not None and due > datetime.fromisoformat(self.now().replace("Z", "+00:00")), "INVALID_REVIEW_DUE_AT")
            except ValueError:
                raise BusinessError("INVALID_REVIEW_DUE_AT") from None
            proposed = [d["decision_id"] for d in self.world["tables"]["decision_record"] if d["page_id"] == page["page_id"] and d["status"] == "proposed"]
            require(proposed, "PROPOSED_DECISION_REQUIRED")
            superseded = [self.owned("decision_record", d).get("supersedes_decision_id") for d in proposed]
            superseded = [d for d in superseded if d]
            require(len(set(superseded)) == len(superseded), "CONFLICTING_SUPERSESSION_PROPOSALS")
            result = self.native(name, args)
            self.update("review_request", result["review_id"], {"page_version": page["version"], "content_snapshot": self.content_snapshot(page["page_id"]),
                "decision_ids": proposed, "reviewer_decisions": [], "checks": []})
            return {**result, "page_version": page["version"], "decision_ids": proposed}
        if name == "run_document_review":
            return self.run_review(args["review_id"])
        if name == "approve_review_and_transition_page":
            return self.accept_review(args["review_id"])
        if name == "publish_decision_summary":
            return self.publish(args)
        if name == "synchronize_tracker":
            item = self.owned("database_item", args["item_id"])
            publication = self.publication(args["publication_id"])
            require(item["page_id"] == publication["page_id"], "TRACKER_PUBLICATION_IDENTITY")
            return self.update("database_item", item["item_id"], {"status": "approved", "published_version": publication["page_version"],
                "publication_id": publication["publication_id"], "linked_decision_ids": publication["decision_ids"], "updated_at": self.now()})
        if name == "add_comment":
            for key, kind in (("block_id", "page_block"), ("item_id", "database_item"), ("thread_id", "comment_thread")):
                if args.get(key):
                    require(self.owned(kind, args[key])["page_id"] == args["page_id"], "COMMENT_PAGE_IDENTITY")
            for identifier in args.get("mentioned_user_ids", []):
                self.owned("user", identifier)
            result = self.native(name, args)
            self.recount()
            return result
        if name == "resolve_comment_thread":
            thread = self.owned("comment_thread", args["thread_id"])
            if thread.get("review_rule") and args["resolution_action"] != "reopen":
                passed = {c["rule"]: c["passed"] for c in self.checks(thread["page_id"])}
                require(passed.get(thread["review_rule"], False), "OBJECTION_NOT_RESOLVED")
                require(bool(args.get("resolution_note", "").strip()), "RESOLUTION_NOTE_REQUIRED")
            result = self.native(name, args)
            self.recount()
            return result
        if name == "update_page_status":
            require(args["new_status"] in {"draft", "blocked", "archived"}, "DIRECT_APPROVAL_FORBIDDEN")
            page = self.owned("page", args["page_id"])
            affected = self.invalidate(page["page_id"])
            result = self.update("page", page["page_id"], {"lifecycle_status": args["new_status"], "updated_at": self.now()})
            return {**result, "invalidated_page_ids": affected}
        raise BusinessError("TOOL_NOT_EXPOSED")

    def run_review(self, review_id):
        review = self.owned("review_request", review_id)
        page = self.owned("page", review["page_id"])
        require(review["status"] != "canceled" and review["page_version"] == page["version"] and review["content_snapshot"] == self.content_snapshot(page["page_id"]), "STALE_REVIEW")
        if review["reviewer_decisions"]:
            return review
        # Keep the recorded reviewer rule sets stable; format failures still reach both reviewers.
        checks = self.checks(page["page_id"], include_passed_format=False)
        decisions = []
        for reviewer in review["reviewer_user_ids"]:
            role = self.owned("user", reviewer)["review_role"]
            relevant = [c for c in checks if c["reviewer_role"] in {role, "both"}]
            decision = "approve" if all(c["passed"] for c in relevant) else "request_changes"
            self.native("record_review_signoff", {"review_id": review_id, "reviewer_user_id": reviewer, "decision": decision,
                "notes": "Environment-generated deterministic interface-contract review"})
            decisions.append({"reviewer_user_id": reviewer, "role": role, "decision": decision, "page_version": page["version"], "checks": relevant, "created_at": self.now()})
        for item in checks:
            if not item["passed"]:
                identifier = self.next_id("review-thread")
                self.add("comment_thread", {"thread_id": identifier, "page_id": page["page_id"], "workspace_id": page["workspace_id"],
                    "block_id": page["contract_block_id"], "status": "open", "priority": "high", "review_rule": item["rule"],
                    "review_id": review_id, "created_at": self.now(), "updated_at": self.now(), "last_activity_at": self.now(), "comment_count": 0})
                self.add("comment", {"comment_id": self.next_id("review-comment"), "thread_id": identifier,
                    "workspace_id": page["workspace_id"], "author_user_id": next(r for r in review["reviewer_user_ids"]
                        if item["reviewer_role"] in {"both", self.owned("user", r)["review_role"]}),
                    "body": "Unresolved contract obligation: " + item["rule"], "mentioned_user_ids": [],
                    "created_at": self.now(), "updated_at": self.now()})
        status = "approved" if all(d["decision"] == "approve" for d in decisions) else "changes_requested"
        result = self.update("review_request", review_id, {"status": status, "reviewer_decisions": decisions, "checks": checks,
            "completed_signoff_count": sum(d["decision"] == "approve" for d in decisions)})
        self.recount()
        return result

    def accept_review(self, review_id):
        review = self.owned("review_request", review_id)
        page = self.owned("page", review["page_id"])
        require(review["status"] == "approved" and review["page_version"] == page["version"] and review["content_snapshot"] == self.content_snapshot(page["page_id"]), "CURRENT_PASSING_REVIEW_REQUIRED")
        required = self.requirements(page["page_id"])["reviewer_user_ids"]
        decisions = review["reviewer_decisions"]
        require({r["reviewer_user_id"] for r in decisions} == set(required) and len(decisions) == len(required)
                and all(r["decision"] == "approve" for r in decisions), "REVIEWER_EVIDENCE_REQUIRED")
        require(all(r["passed"] for r in self.checks(page["page_id"])), "CURRENT_CONTRACT_REVIEW_FAILED")
        require(not any(t["page_id"] == page["page_id"] and t["status"] in {"open", "reopened"} for t in self.world["tables"]["comment_thread"]), "UNRESOLVED_REVIEW_THREADS")
        if page.get("approved_review_id") == review_id and page["lifecycle_status"] == "approved":
            return {"page_id": page["page_id"], "page_version": page["version"], "review_id": review_id,
                    "accepted_decision_ids": review["decision_ids"], "idempotent": True}
        for identifier in review["decision_ids"]:
            decision = self.owned("decision_record", identifier)
            require(decision["page_id"] == page["page_id"] and decision["status"] in {"proposed", "accepted"}, "DECISION_NOT_ACCEPTABLE")
            if decision.get("supersedes_decision_id"):
                old = self.owned("decision_record", decision["supersedes_decision_id"])
                require(old["page_id"] == page["page_id"] and old["status"] == "accepted", "SUPERSESSION_SOURCE_CHANGED")
                self.update("decision_record", decision["supersedes_decision_id"], {"status": "superseded", "superseded_by": identifier})
            self.update("decision_record", identifier, {"status": "accepted", "accepted_review_id": review_id,
                "accepted_page_version": page["version"], "decided_at": self.now()})
        self.update("page", page["page_id"], {"lifecycle_status": "approved", "approved_review_id": review_id})
        self.invalidate(page["page_id"], include_self=False)
        return {"page_id": page["page_id"], "page_version": page["version"], "review_id": review_id, "accepted_decision_ids": review["decision_ids"]}

    def publish(self, args):
        require(args["decision_ids"], "DECISION_REQUIRED")
        decisions = [self.owned("decision_record", identifier) for identifier in sorted(args["decision_ids"])]
        page_id = args.get("target_page_id") or decisions[0]["page_id"]
        page = self.owned("page", page_id)
        require(page["workspace_id"] == args["workspace_id"] and page["lifecycle_status"] == "approved", "PAGE_NOT_APPROVED")
        review = self.owned("review_request", page.get("approved_review_id"))
        require(review["status"] == "approved" and review["page_version"] == page["version"], "STALE_REVIEW")
        require(set(args["decision_ids"]) == set(review["decision_ids"]), "REVIEWED_DECISION_SET_MISMATCH")
        require(all(d["page_id"] == page_id and d["status"] == "accepted" and d.get("accepted_review_id") == review["review_id"] for d in decisions), "DECISION_NOT_ACCEPTED")
        require(all(c["passed"] for c in self.checks(page_id)), "CURRENT_CONTRACT_REVIEW_FAILED")
        require(not any(t["page_id"] == page_id and t["status"] in {"open", "reopened"}
                        for t in self.world["tables"]["comment_thread"]), "UNRESOLVED_REVIEW_THREADS")
        contract = self.contract(page_id)
        for existing in self.state["publications"].values():
            if (existing["page_id"] == page_id and existing["page_version"] == page["version"]
                    and existing["review_id"] == review["review_id"]
                    and set(existing["decision_ids"]) == set(args["decision_ids"])):
                return self.publication(existing["publication_id"])
        identifier = self.next_id("publication")
        artifact = {"publication_id": identifier, "page_id": page_id, "page_version": page["version"], "review_id": review["review_id"],
            "decision_ids": sorted(args["decision_ids"]), "contract": contract, "blocks": self.content_snapshot(page_id), "published_at": self.now(),
            "rendered_markdown": "# " + page["title"] + "\n\n" + "\n\n".join(d["decision_title"] + ": " + d["rationale"] for d in decisions)}
        self.state["publications"][identifier] = artifact
        return deepcopy(artifact)

    def validate(self):
        for kind, rows in self.world["tables"].items():
            require(len({r[KEYS[kind]] for r in rows}) == len(rows), "DUPLICATE_ENTITY_ID")
            require(all(r.get("task_id") == self.world["task_id"] for r in rows), "TASK_OWNERSHIP")
        for page in self.world["tables"]["page"]:
            blocks = self.blocks(page["page_id"])
            require(page["block_count"] == len(blocks), "BLOCK_COUNT_MISMATCH")
            require(len({b["position"] for b in blocks}) == len(blocks), "DUPLICATE_BLOCK_POSITION")
            require(type(page["version"]) is int and page["version"] >= 1, "INVALID_CONTENT_VERSION")
            self.owned("page_block", page["requirements_block_id"])
            self.owned("page_block", page["contract_block_id"])
            threads = [t for t in self.world["tables"]["comment_thread"] if t["page_id"] == page["page_id"]]
            require(page["comment_thread_count"] == len(threads) and page["unresolved_comment_count"] == sum(t["comment_count"] for t in threads if t["status"] in {"open", "reopened"}), "COMMENT_COUNT_MISMATCH")
        for thread in self.world["tables"]["comment_thread"]:
            require(thread["comment_count"] == sum(c["thread_id"] == thread["thread_id"] for c in self.world["tables"]["comment"]), "THREAD_COMMENT_COUNT_MISMATCH")
        for review in self.world["tables"]["review_request"]:
            votes = review.get("reviewer_decisions", [])
            if votes:
                require(review["completed_signoff_count"] == sum(v["decision"] == "approve" for v in votes), "REVIEW_COUNTER_MISMATCH")

    def call(self, name, arguments):
        before = deepcopy(self.world)
        try:
            require(name in self.metadata, "TOOL_NOT_EXPOSED")
            try:
                json.dumps(arguments, allow_nan=False)
                jsonschema.validate(arguments, self.metadata[name]["input_schema"])
            except (ValueError, TypeError, jsonschema.ValidationError):
                raise BusinessError("ARGUMENT_SCHEMA") from None
            data = self.dispatch(name, deepcopy(arguments))
            if name not in READS:
                self.validate()
            if self.compact_responses:
                data = self.response_data(name, arguments, data)
            result = {"status": "success", "data": data, "message": ""}
        except BusinessError as error:
            self.world = before
            self.sm.bind(self.world)
            result = {"status": "error", "data": {}, "message": str(error)}
        except Exception:
            self.world = before
            self.sm.bind(self.world)
            raise
        self.record_call(name, arguments, result)
        self.world["sequence"] += 1
        try:
            write_json(self.runtime / "world.json", self.world)
        except Exception:
            self.world = before
            self.sm.bind(self.world)
            raise
        return result

    def response_data(self, name, arguments, data):
        """Project only returned data; immutable internal audit evidence is intact."""
        data = deepcopy(data)
        if name == "revise_page_block":
            block = self.owned("page_block", arguments["block_id"])
            page = self.owned("page", block["page_id"])
            return {"block_id": block["block_id"], "page_id": page["page_id"], "page_version": page["version"],
                    "checked": block["checked"], "idempotent": data.get("idempotent", False),
                    "invalidated_pages": data.get("invalidated_pages", [])}
        if name in {"run_document_review", "read_review"} and not arguments.get("include_snapshot", False):
            data.pop("content_snapshot", None)
        if name in {"publish_decision_summary", "read_publication"} and not arguments.get("include_snapshot", False):
            data.pop("blocks", None)
        if name == "list_publications":
            for row in data["publications"]:
                row.pop("blocks", None)
        return data

    def record_call(self, name, arguments, result):
        pass
