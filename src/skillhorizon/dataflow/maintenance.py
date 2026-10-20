"""Established-release maintenance; decisions are data, not an automatic planner."""

from copy import deepcopy
from decimal import Decimal
from typing import Literal
from typing_extensions import TypedDict
from pydantic import ConfigDict

from .runtime import DataflowRuntime, write_table
from .skills import SKILLS
from ..public_contract import Offset, PageLimit, Revision, page, public_progress, completion


PROFILE = "release_maintenance_v2"
Collection = Literal["sources", "artifacts", "active", "changes", "evidence", "components", "policies", "assessments", "decisions", "plans", "consumers"]
HistoryCollection = Literal["sources", "decisions"]


class ObjectFilter(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")
    field: Literal["id", "kind", "region", "period", "source_id", "change_id", "status", "producer_skill", "skill_id"]
    value: str
MAINTENANCE_SKILLS = {
    "review-adjustment": {
        "description": "Assess a requested accounting adjustment against evidence and an applicable standing decision.",
        "inputs": {"change_id": str, "policy_id": str, "supersedes_id": str}, "script": False,
    },
    "apply-adjustment": {
        "description": "Prepare and commit a reviewed adjustment to the established sales workspace.",
        "inputs": {"decision_id": str}, "script": False,
    },
}
INSTRUCTIONS = {
    "review-adjustment": """# Review Adjustment

Inputs: change_id, policy_id, supersedes_id (empty string for a new decision).
Investigate the change, its linked settlement evidence, applicable standing policy,
and source table using search_objects, read_object, read_table and object_history.
Policy age alone does not establish applicability. A newer policy for another
region must not replace an older still-applicable decision.

Business rule: apply only a confirmed settlement-component correction. Each proof
names a distinct ledger component, not an alternative final total for the order.
Its delta is settled_amount_cents minus booked_amount_cents for that component,
not necessarily the amount requested in the ticket. The order total is the sum
of its components; adjustments to distinct components therefore add to that total.
Reject disputed or withdrawn evidence. Preserve all other orders and all published
historical snapshots. Policy scope and adjustment limit constrain later application.

1. compare: supply {delta_cents: integer}. Produces a persistent assessment of
   your proposed delta against the selected source revision. It does not choose
   the correct delta, policy or disposition for you.
2. decide: supply {choice: "apply" or "reject", rationale: string,
   alternatives: [string, ...]}. The decision consumes this run's assessment.
   Explain the evidence and a considered alternative; prose is not keyword-scored.
   A superseding decision must concern the same change. Old records remain readable.

Finish the run. A decision can be consumed by apply-adjustment; it is not itself
an applied change. After a relevant source change, review again against current
data. An unrelated source or run revision does not invalidate the assessment.
""",
    "apply-adjustment": """# Apply Adjustment

Input: decision_id. Read the decision, its assessment and standing policy first.
1. prepare: supply {}. Creates a persistent plan from the selected decision,
   with the source-local revision and policy conditions. It does not modify sales.
2. commit: supply {plan_id: string} from this run's prepare step. An apply
   disposition adjusts the selected ledger component and every duplicate
   representation of its order by the assessed delta; a reject records resolution without changing
   the source. The policy's scope and limit are enforced. Relevant concurrent
   changes reject the commit before any business write. No global revision lock.

Finish the run. Abandoning/revising does not undo a committed adjustment.
To correct a stale plan, review the change again and select that new decision.
Then maintain affected tabular deliverables using the normal installed skills.
Unchanged regional audit objects referenced by consumers must be retained and
reused in the new release review. Historical publication contents are immutable.
""",
}


def install_maintenance_skills(root):
    from pathlib import Path
    from ..skill_packages import skill_header
    for identifier, instructions in INSTRUCTIONS.items():
        folder = Path(root) / identifier
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(skill_header(identifier, MAINTENANCE_SKILLS[identifier]["description"]) + instructions)


class MaintenanceRuntime(DataflowRuntime):
    skills = {**SKILLS, **MAINTENANCE_SKILLS}
    query_tools = [
        ("search_objects", "Search one collection. text is a literal case-insensitive substring over summary fields, not field:value syntax. filters are ANDed exact field/value matches; region/period also match policy scope lists. status means pending/resolved for changes and evidence disposition for evidence; absent fields do not match. Use text='' and filters=[] to list all. Stable ID order; offset>=0, limit 1..200; follow next_offset.",
         {"collection": Collection, "text": str, "filters": list[ObjectFilter], "offset": Offset, "limit": PageLimit}),
        ("read_object", "Read one public object. artifact current means source freshness, not semantic correctness. IDs are collection-specific.", {"collection": Collection, "object_id": str}),
        ("object_history", "Read a paginated history for one source or decision.",
         {"collection": HistoryCollection, "object_id": str, "offset": Offset, "limit": PageLimit}),
        ("read_source_version", "Read one retained source version using table pagination.",
         {"source_id": str, "revision": Revision, "offset": Offset, "limit": PageLimit}),
    ]

    def _collection(self, name):
        if name in {"sources", "artifacts", "active"}:
            return self.project[name]
        self._require(name in {"changes", "evidence", "components", "policies", "assessments", "decisions", "plans", "consumers"},
                      "COLLECTION", "Unknown public collection")
        return self.project["maintenance"][name]

    def _object(self, collection, identifier):
        item = self._collection(collection).get(identifier)
        self._require(item is not None, "UNKNOWN_OBJECT", "Unknown public object")
        return item

    def read_project(self):
        return {"project_id": self.project["project_id"], "goal": self.project["goal"],
                "regions": self.project["regions"], "periods": self.project["periods"],
                "revision": self.project["revision"],
                "progress": self.public_progress(),
                "collections": {name: len(self._collection(name)) for name in
                    ("sources", "artifacts", "active", "changes", "evidence", "components", "policies", "assessments", "decisions", "plans", "consumers")}}

    def public_progress(self):
        identifier = self.project["publication"]
        state = "missing" if identifier is None else "current" if self.current(identifier) else "stale"
        publication = self.project["artifacts"].get(identifier, {})
        review = self.project["artifacts"].get(publication.get("arguments", {}).get("review_id"), {})
        used = review.get("arguments", {}).get("audit_ids", [])
        pending = {"unresolved_changes": sum(c["resolution"] is None for c in self._collection("changes").values()),
                   "unlinked_consumer_audits": sum(c["audit_id"] not in used for c in self._collection("consumers").values())}
        return public_progress(self.project["active"], identifier, state, pending)

    def _page(self, rows, offset, limit):
        return page(rows, offset, limit)

    def search_objects(self, collection, text, filters, offset, limit):
        fields = ("title", "kind", "region", "period", "regions", "periods", "source_id", "change_id", "status", "producer_skill", "skill_id", "work_package")
        rows = []
        for identifier, item in sorted(self._collection(collection).items()):
            summary = {"id": identifier, **{k: item[k] for k in fields if k in item}}
            if collection == "changes":
                summary["status"] = "pending" if item["resolution"] is None else "resolved"
            def matches(rule):
                value = summary.get(rule["field"], summary.get(rule["field"] + "s"))
                return rule["value"] in value if isinstance(value, list) else value == rule["value"]
            if text.casefold() in " ".join(str(v) for v in summary.values()).casefold() and all(matches(rule) for rule in filters):
                rows.append(summary)
        return self._page(rows, offset, limit)

    def read_object(self, collection, object_id):
        item = deepcopy(self._object(collection, object_id))
        if collection == "artifacts":
            item["current"] = self.current(object_id)
        elif collection == "changes":
            item["status"] = "pending" if item["resolution"] is None else "resolved"
        return {"id": object_id, "object": item}

    def object_history(self, collection, object_id, offset, limit):
        self._object(collection, object_id)
        self._require(collection in {"sources", "decisions"}, "HISTORY", "History is available for sources and decisions")
        if collection == "sources":
            rows = self.project["maintenance"]["source_history"][object_id]
        else:
            change = self._object("decisions", object_id)["change_id"]
            rows = [{"id": k, **deepcopy(v)} for k, v in self._collection("decisions").items() if v["change_id"] == change]
        return self._page(deepcopy(rows), offset, limit)

    def read_source_version(self, source_id, revision, offset, limit):
        self._source(source_id)
        self._require(type(revision) is int, "VERSION", "Use an integer source revision")
        versions = self.project["maintenance"]["source_history"][source_id]
        source = next((v for v in versions if v["revision"] == revision), None)
        self._require(source is not None, "VERSION", "Unknown source revision")
        return {"source_id": source_id, "revision": revision, **self._page(self._rows(source), offset, limit)}

    def _record(self, collection, value):
        identifier = f"{collection}-{len(self._collection(collection)) + 1}"
        self._collection(collection)[identifier] = deepcopy(value)
        return identifier

    def _fields(self, args, names):
        self._require(isinstance(args, dict) and set(args) == set(names), "ARGUMENTS", "Supply the documented step fields")

    def execute_step(self, run_id, step_id, arguments):
        run = self._active(run_id)
        if run["skill_id"] not in MAINTENANCE_SKILLS:
            return super().execute_step(run_id, step_id, arguments)
        self._require(run["output"] is None, "STEP_COMPLETE", "Finish or abandon this committed run")
        expected = (("compare", "decide") if run["skill_id"] == "review-adjustment" else ("prepare", "commit"))
        self._require(len(run["steps"]) < 2 and step_id == expected[len(run["steps"])], "STEP_ORDER", "Resume the documented unfinished step")
        selected = run["arguments"]
        if step_id == "compare":
            self._fields(arguments, {"delta_cents"})
            self._require(type(arguments["delta_cents"]) is int, "DELTA", "Use integer cents")
            change = self._object("changes", selected["change_id"])
            policy = self._object("policies", selected["policy_id"])
            source = self._source(change["source_id"])
            evidence = self._object("evidence", change["evidence_id"])
            self._require(change["resolution"] is None, "RESOLVED", "This change is already resolved")
            parent = selected["supersedes_id"]
            if parent:
                previous = self._object("decisions", parent)
                self._require(previous["change_id"] == selected["change_id"] and previous["superseded_by"] is None,
                              "SUPERSEDES", "Supersede a current decision for the same change")
            assessment = {"change_id": selected["change_id"], "source_id": change["source_id"],
                "source_revision": source["revision"], "policy_id": selected["policy_id"], "policy_revision": policy["revision"],
                "evidence_id": change["evidence_id"], "evidence_revision": evidence["revision"],
                "delta_cents": arguments["delta_cents"], "origin": "skill_run"}
            identifier = self._record("assessments", assessment)
            result = {"assessment_id": identifier, **assessment}
        elif step_id == "decide":
            self._fields(arguments, {"choice", "rationale", "alternatives"})
            self._require(self._object("changes", selected["change_id"])["resolution"] is None,
                          "RESOLVED", "This change was resolved while the review was open")
            self._require(arguments["choice"] in {"apply", "reject"} and isinstance(arguments["rationale"], str)
                          and arguments["rationale"].strip() and isinstance(arguments["alternatives"], list)
                          and arguments["alternatives"] and all(isinstance(v, str) and v.strip() for v in arguments["alternatives"]),
                          "DECISION", "Choose a disposition and record a rationale and considered alternatives")
            parent = selected["supersedes_id"]
            if parent:
                self._require(self._object("decisions", parent)["superseded_by"] is None, "SUPERSEDES", "The selected decision was already superseded")
            identifier = self._record("decisions", {**selected, **deepcopy(arguments),
                "assessment_id": run["steps"][0]["assessment_id"], "superseded_by": None, "origin": "skill_run"})
            if parent:
                self._object("decisions", parent)["superseded_by"] = identifier
            result = {"decision_id": identifier}
        elif step_id == "prepare":
            self._fields(arguments, {})
            decision, assessment, change = self._applicable(selected["decision_id"])
            source = self._source(change["source_id"])
            identifier = self._record("plans", {"decision_id": selected["decision_id"],
                "source_id": change["source_id"], "source_revision": source["revision"],
                "delta_cents": assessment["delta_cents"], "choice": decision["choice"], "committed": False})
            result = {"plan_id": identifier, **deepcopy(self._object("plans", identifier))}
        else:
            self._fields(arguments, {"plan_id"})
            self._require(arguments["plan_id"] == run["steps"][0]["plan_id"], "RUN_INPUTS", "Consume this run's prepared plan")
            plan = self._object("plans", arguments["plan_id"])
            decision, assessment, change = self._applicable(plan["decision_id"])
            source = self._source(change["source_id"])
            self._require(not plan["committed"] and source["revision"] == plan["source_revision"], "STALE_PLAN", "Prepared source version changed")
            if decision["choice"] == "apply":
                rows = self._rows(source)
                evidence = self._object("evidence", change["evidence_id"])
                component = self._object("components", evidence["component_id"])
                component_amount = component["amount_cents"] + assessment["delta_cents"]
                self._require(component_amount >= 0, "AMOUNT", "Adjustment would create a negative ledger component")
                matched = [row for row in rows if row["order_id"].strip() == change["order_id"]]
                self._require(matched, "ORDER", "Selected order is absent")
                for row in matched:
                    amount = Decimal(row["amount"]) + Decimal(assessment["delta_cents"]) / 100
                    self._require(amount >= 0, "AMOUNT", "Adjustment would create a negative sale")
                    row["amount"] = format(amount, ".2f")
                version = source["revision"] + 1
                path = f"sources/{change['source_id']}-v{version}.csv"
                write_table(self.root / path, rows, list(rows[0]))
                source.update(revision=version, path=path)
                component["amount_cents"] = component_amount
                self.project["maintenance"]["source_history"][change["source_id"]].append(deepcopy(source))
            plan["committed"] = True
            change["resolution"] = {"decision_id": plan["decision_id"], "plan_id": arguments["plan_id"], "choice": decision["choice"]}
            result = {"change_id": decision["change_id"], "choice": decision["choice"],
                      "source_id": change["source_id"], "source_revision": source["revision"], "plan_id": arguments["plan_id"]}
        run["steps"].append({"step_id": step_id, **deepcopy(result)})
        if step_id in {"decide", "commit"}:
            run["output"] = result
        self.project["revision"] += 1
        self.save()
        return result

    def _applicable(self, decision_id):
        decision = self._object("decisions", decision_id)
        assessment = self._object("assessments", decision["assessment_id"])
        change = self._object("changes", decision["change_id"])
        source = self._source(change["source_id"])
        policy = self._object("policies", assessment["policy_id"])
        evidence = self._object("evidence", change["evidence_id"])
        self._require(change["resolution"] is None, "RESOLVED", "Change has already been resolved")
        self._require(decision["superseded_by"] is None, "SUPERSEDED", "Decision was superseded")
        self._require(source["revision"] == assessment["source_revision"] and policy["revision"] == assessment["policy_revision"]
                      and evidence["revision"] == assessment["evidence_revision"], "STALE_ASSESSMENT", "A selected business premise changed")
        self._require(source["region"] in policy["regions"] and source["period"] in policy["periods"],
                      "POLICY_SCOPE", "Standing decision does not cover this source")
        self._require(decision["choice"] == "reject" or abs(assessment["delta_cents"]) <= policy["max_adjustment_cents"],
                      "POLICY_LIMIT", "Proposed adjustment exceeds the selected standing decision")
        return decision, assessment, change

    def revise_skill(self, run_id, arguments):
        run = self._active(run_id)
        if run["skill_id"] not in MAINTENANCE_SKILLS:
            return super().revise_skill(run_id, arguments)
        self._validate_arguments(run["skill_id"], arguments)
        run.update(arguments=deepcopy(arguments), steps=[], output=None)
        self.project["revision"] += 1
        self.save()
        return {"run_id": run_id, "step_progress_reset": True, "committed_effects_retained": True}

    def run_skill_script(self, run_id):
        self._require(self._active(run_id)["skill_id"] not in MAINTENANCE_SKILLS, "NO_SCRIPT", "This skill is procedure-only")
        return super().run_skill_script(run_id)

    def score(self):
        from .maintenance_verification import verify_maintenance
        result = super().score()
        maintenance = verify_maintenance(self.root)
        result["maintenance"] = maintenance
        result["success"] &= maintenance["passed"]
        result.update(completion(result["success"], self.project["active"], self.project["publication"]))
        return result
