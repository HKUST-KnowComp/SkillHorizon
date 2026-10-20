"""Independent raw-state grading; never invokes runtime checks or actor tools."""

from copy import deepcopy
import json
from pathlib import Path

from integrations.internal_mcp.io import read_json


def evaluate(task, runtime):
    task, runtime = Path(task), Path(runtime)
    spec = read_json(task / "private/initial_obligations.json")
    def equal_capacity(actual, expected):
        if spec["public"].get("policy", {}).get("portfolio_representation") == "unordered_collections_v2":
            from .portfolio_evaluation import equivalent_capacity
            return equivalent_capacity(actual, expected)
        return actual == expected
    world = read_json(runtime / "world.json")
    tables, state = world["tables"], world["planning"]
    pages = {p["page_id"]: p for p in tables["page"]}
    blocks = {b["block_id"]: b for b in tables["page_block"]}
    reviews = {r["review_id"]: r for r in tables["review_request"]}
    decisions = {d["decision_id"]: d for d in tables["decision_record"]}
    trackers = {i["item_id"]: i for i in tables["database_item"]}
    users = {u["user_id"]: u for u in tables["user"]}
    initial_pages = {p["page_id"]: p for p in spec["initial_tables"]["page"]}
    initial_blocks = {b["block_id"]: b for b in spec["initial_tables"]["page_block"]}
    expected_requirements = {pid: json.loads(initial_blocks[p["requirements_block_id"]]["content"]) for pid, p in initial_pages.items()}
    updates = spec["scheduled_updates"]
    applied_ids = [u.get("event_id") for u in state["applied_updates"]]
    planned_ids = [u["event_id"] for u in updates]
    global_errors = []
    keys = {"workspace": "workspace_id", "user": "user_id", "product_area": "area_id", "project": "project_id", "database_view": "view_id",
        "page": "page_id", "page_block": "block_id", "database_item": "item_id", "review_request": "review_id", "comment_thread": "thread_id",
        "comment": "comment_id", "decision_record": "decision_id", "stakeholder": "stakeholder_id", "relation": "relation_id", "activity_event": "event_id"}
    for kind, rows in tables.items():
        if len({r.get(keys[kind]) for r in rows}) != len(rows) or any(r.get("task_id") != world["task_id"] for r in rows):
            global_errors.append("ENTITY_IDENTITY_OR_OWNERSHIP")
    if world["task_id"] != spec["public"]["task_id"]:
        global_errors.append("TASK_IDENTITY")
    if world.get("format") != spec["public"]["format"] or read_json(task / "public/task.json")["format"] != spec["public"]["format"]:
        global_errors.append("ENVIRONMENT_FORMAT")
    if applied_ids != planned_ids:
        global_errors.append("CHANGE_WINDOWS_NOT_COMPLETED")
    if set(pages) != set(initial_pages):
        global_errors.append("PAGE_SET_CHANGED")
    for update in updates:
        if update["event_id"] in applied_ids:
            expected_requirements[update["page_id"]] = update["requirements"]
            matching = [e for e in tables["activity_event"] if e["event_id"] == update["event_id"]]
            if len(matching) != 1 or matching[0].get("actor_user_id") != "environment" or matching[0].get("entity_id") != update["page_id"]:
                global_errors.append("AMENDMENT_EVENT_EVIDENCE")

    expected = {}

    def expected_contract(page_id, trail=()):
        if page_id in expected:
            return expected[page_id]
        if page_id in trail:
            raise ValueError("Requirement cycle")
        requirement = expected_requirements[page_id]
        fields = deepcopy(requirement["local_fields"])
        parents = [expected_contract(p, trail + (page_id,)) for p in requirement["parent_page_ids"]]
        for parent in parents:
            for key, value in parent["fields"].items():
                if key in fields and fields[key] != value:
                    raise ValueError("Conflicting source obligations")
                fields[key] = value
        expected[page_id] = {"fields": fields, "limits": {key: min([value] + [p["limits"][key] for p in parents])
            for key, value in requirement["local_limits"].items()}}
        if "portfolio_policy" in requirement:
            from .portfolio_evaluation import expected_capacity
            total, allocation = expected_capacity(requirement, parents)
            expected[page_id]["limits"]["batch_size"] = total
            expected[page_id]["capacity"] = allocation
        elif "capacity_policy" in requirement:
            policy = requirement["capacity_policy"]
            incoming = 0
            for parent_id in requirement["parent_page_ids"]:
                producer = expected_contract(parent_id, trail + (page_id,))
                assignments = {r["consumer_page_id"]: r for r in producer["capacity"]["allocations"]}
                quota = assignments[page_id]
                if quota["activation_stage"] != policy["stage"]:
                    raise ValueError("Wrong quota activation stage")
                incoming += quota["units"]
            total = min(incoming + policy["local_capacity_units"], requirement["local_limits"]["batch_size"])
            denominator = sum(c["weight"] for c in policy["consumers"])
            allocations = []
            for consumer in policy["consumers"]:
                quotient, _ = divmod(total * consumer["weight"], denominator)
                allocations.append({"consumer_page_id": consumer["consumer_page_id"], "activation_stage": consumer["activation_stage"], "units": min(quotient, consumer["max_units"])})
            expected[page_id]["limits"]["batch_size"] = total
            expected[page_id]["capacity"] = {"incoming_units": incoming, "local_units": policy["local_capacity_units"],
                "allocations": allocations, "uncommitted_units": total - sum(c["units"] for c in allocations)}
        return expected[page_id]

    outcomes = []
    for request in spec["public"]["requests"]:
        page_id = request["page_id"]
        errors = []

        def check(condition, code):
            if not condition:
                errors.append(code)

        try:
            page, initial = pages[page_id], initial_pages[page_id]
            requirement = expected_requirements[page_id]
            for key in ("workspace_id", "project_id", "area_id", "owner_user_id", "requirements_block_id", "contract_block_id"):
                check(page[key] == initial[key], "PAGE_IDENTITY_" + key.upper())
            check(json.loads(blocks[initial["requirements_block_id"]]["content"]) == requirement, "REQUIREMENTS_CHANGED")
            contract = json.loads(blocks[initial["contract_block_id"]]["content"])
            ideal = expected_contract(page_id)
            check(set(contract) == {"fields", "limits", "imports"} | ({"capacity"} if "capacity_policy" in requirement else set()), "CONTRACT_SHAPE")
            check(contract["fields"] == ideal["fields"], "FIELD_UNION")
            check(contract["limits"] == ideal["limits"], "LIMITS")
            if "capacity_policy" in requirement:
                check(equal_capacity(contract.get("capacity"), ideal["capacity"]), "EXACT_CONSUMER_CAPACITY_ALLOCATION")
                capacity = contract["capacity"]
                check(sum(c["units"] for c in capacity["allocations"]) + capacity["uncommitted_units"] == contract["limits"]["batch_size"], "SHARED_CAPACITY_CONSERVATION")
            bindings = {p["page_id"]: p for p in contract["imports"]}
            check(len(bindings) == len(contract["imports"]) and set(bindings) == set(requirement["parent_page_ids"]), "PARENT_SET")
            for parent_id, binding in bindings.items():
                parent = state["publications"][binding["publication_id"]]
                check(parent["page_id"] == parent_id and parent["page_version"] == binding["page_version"] == pages[parent_id]["version"], "PARENT_VERSION_IDENTITY")
                check(parent["review_id"] == pages[parent_id].get("approved_review_id") and reviews[parent["review_id"]]["status"] == "approved", "PARENT_REVIEW")
                check(pages[parent_id]["lifecycle_status"] == "approved", "PARENT_NOT_APPROVED")
                check(not any(t["page_id"] == parent_id and t["status"] in {"open", "reopened"} for t in tables["comment_thread"]), "PARENT_OBJECTION")
                parent_ideal = expected_contract(parent_id)
                check(parent["contract"]["fields"] == parent_ideal["fields"] and parent["contract"]["limits"] == parent_ideal["limits"], "PARENT_CONTENT")
                if "capacity" in parent_ideal:
                    check(equal_capacity(parent["contract"].get("capacity"), parent_ideal["capacity"]), "PARENT_CAPACITY_RESERVATIONS")
            snapshot = [{k: b[k] for k in ("block_id", "position", "block_type", "content", "checked")}
                        for b in sorted((b for b in tables["page_block"] if b["page_id"] == page_id), key=lambda b: b["position"])]
            check(page["block_count"] == len(snapshot), "BLOCK_COUNT")
            check(len({b["position"] for b in snapshot}) == len(snapshot), "BLOCK_POSITIONS")
            check(page["lifecycle_status"] == "approved", "PAGE_NOT_APPROVED")
            review = reviews[page["approved_review_id"]]
            check(review["page_id"] == page_id and review["page_version"] == page["version"] and review["content_snapshot"] == snapshot, "REVIEW_CONTENT_VERSION")
            required = requirement["reviewer_user_ids"]
            votes = review["reviewer_decisions"]
            check(review["status"] == "approved" and review["required_signoff_count"] == len(required) == review["completed_signoff_count"], "REVIEW_STATUS_QUORUM")
            check(len(votes) == len(required) and {v["reviewer_user_id"] for v in votes} == set(required), "INDIVIDUAL_REVIEWERS")
            for vote in votes:
                role = users[vote["reviewer_user_id"]]["review_role"]
                needed = {"CURRENT_PARENT_BINDINGS", "FIELD_UNION", "BATCH_SIZE"} if role == "engineering" else {"RETENTION_DAYS"}
                if role == "engineering" and "capacity_policy" in requirement:
                    needed.add("CAPACITY_ALLOCATION")
                check(vote["role"] == role and vote["decision"] == "approve" and vote["page_version"] == page["version"], "INDIVIDUAL_REVIEW_OUTCOME")
                check({c["rule"] for c in vote["checks"]} == needed and all(c["passed"] is True for c in vote["checks"]), "INDIVIDUAL_REVIEW_CHECKS")
                signoffs = [e for e in tables["activity_event"] if e.get("entity_id") == review["review_id"] and
                            e.get("event_type") == "review.signoff_recorded" and e.get("actor_user_id") == vote["reviewer_user_id"]]
                check(len(signoffs) == 1 and signoffs[0]["created_at"] == vote["created_at"], "REVIEW_SIGNOFF_EVENT")
            check(set(review.get("signed_reviewer_user_ids", [])) == set(required), "SIGNED_REVIEWER_IDS")
            threads = [t for t in tables["comment_thread"] if t["page_id"] == page_id]
            check(page["comment_thread_count"] == len(threads), "THREAD_COUNT")
            check(all(t["comment_count"] == sum(c["thread_id"] == t["thread_id"] for c in tables["comment"]) for t in threads), "COMMENT_COUNT")
            check(not any(t["page_id"] == page_id and t["status"] in {"open", "reopened"} for t in tables["comment_thread"]), "OPEN_OBJECTIONS")
            check(bool(review["decision_ids"]) and len(set(review["decision_ids"])) == len(review["decision_ids"]), "REVIEW_DECISION_SET")
            for identifier in review["decision_ids"]:
                decision = decisions[identifier]
                check(decision["page_id"] == page_id and decision["status"] == "accepted" and decision.get("accepted_review_id") == review["review_id"]
                    and decision.get("accepted_page_version") == page["version"], "ACCEPTED_DECISION_LINEAGE")
                if decision.get("supersedes_decision_id"):
                    old = decisions[decision["supersedes_decision_id"]]
                    check(old["page_id"] == page_id and old["status"] == "superseded" and old.get("superseded_by") == identifier, "DECISION_SUPERSESSION")
            tracker = trackers[request["tracker_item_id"]]
            publication = state["publications"][tracker["publication_id"]]
            check(tracker["page_id"] == page_id and tracker["status"] == "approved" and tracker["published_version"] == page["version"], "TRACKER_IDENTITY_VERSION")
            check(publication["page_id"] == page_id and publication["page_version"] == page["version"] and publication["review_id"] == review["review_id"], "PUBLICATION_IDENTITY_VERSION")
            check(publication["blocks"] == snapshot and publication["contract"] == contract, "PUBLICATION_CONTENT")
            check(set(publication["decision_ids"]) == set(review["decision_ids"]) == set(tracker["linked_decision_ids"]), "TRACKER_DECISION_SET")
            rendered = "# " + page["title"] + "\n\n" + "\n\n".join(decisions[d]["decision_title"] + ": " + decisions[d]["rationale"] for d in publication["decision_ids"])
            check(publication["rendered_markdown"] == rendered, "PUBLISHED_SUMMARY_CONTENT")
        except (KeyError, TypeError, ValueError):
            errors.append("MISSING_OR_MALFORMED_EVIDENCE")
        outcomes.append({"page_id": page_id, "accepted": not errors, "errors": sorted(set(errors))})
    metrics = {}
    if all("capacity_policy" in r for r in expected_requirements.values()):
        from collections import Counter
        levels, fanout, ages, origin_joins = {}, Counter(), Counter(), []
        for page_id, requirement in expected_requirements.items():
            parents = requirement["parent_page_ids"]
            levels[page_id] = 1 + max([levels[p] for p in parents], default=0)
            positive_origins = set()
            for parent in parents:
                producer = expected_contract(parent)
                quota = next(r for r in producer["capacity"]["allocations"] if r["consumer_page_id"] == page_id)
                if quota["units"] > 0:
                    origin = expected_requirements[parent]["capacity_policy"]["stage"]
                    ages[requirement["capacity_policy"]["stage"] - origin] += 1
                    fanout[parent] += 1
                    positive_origins.add(origin)
            origin_joins.append(len(positive_origins))
        metrics = {"recorded_contract_depth": max(levels.values()), "positive_quota_age_histogram": dict(sorted(ages.items())),
            "positive_quota_edges": sum(ages.values()), "multi_origin_stage_joins": sum(n > 1 for n in origin_joins),
            "max_origins_joined": max(origin_joins), "max_shared_consumer_fanout": max(fanout.values(), default=0),
            "reference_task_count": len(outcomes), "minimal_horizon_proven": False,
            "source_accounting_not_proof_of_completed_trajectory": True}
        if spec["public"].get("policy", {}).get("instance_revision") == "docops_portfolio150_v2":
            metrics["max_distinct_immediate_parent_stages"] = metrics.pop("max_origins_joined")
            metrics["multiple_immediate_parent_stage_joins"] = metrics.pop("multi_origin_stage_joins")
    if all("portfolio_policy" in r for r in expected_requirements.values()):
        from .portfolio_evaluation import metrics as portfolio_metrics
        metrics["origin_portfolios"] = portfolio_metrics(expected, expected_requirements)
    return {"task_id": world["task_id"], "accepted": not global_errors and all(r["accepted"] for r in outcomes),
            "completed_pages": sum(r["accepted"] for r in outcomes), "required_pages": len(outcomes),
            "global_errors": sorted(set(global_errors)), "pages": outcomes, "provider_calls": 0,
            "evidence_kind": "zero_model_business_state_not_model_behavior", **({"dependency_metrics": metrics} if metrics else {})}
