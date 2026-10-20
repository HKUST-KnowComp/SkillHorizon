"""Zero-model public-tool witness; no private files or runtime object access."""

from copy import deepcopy
import json


async def run(call, *, use_script=True, alternative=False, exercise_recovery=False, stop_after_pages=None, record_skills=True):
    public = await call("read_task")
    if public["policy"]["skill_protocol"] == "skill_declaration_v1" and record_skills:
        raise ValueError("Business-only references require record_skills=False")
    for skill in public["skills"]:
        await call("read_skill_file", skill_id=skill["name"])
    editor = public["editor_user_id"]
    completed = 0
    recovery_used = False
    publication_cache = {}

    async def begin(skill, page_id):
        if not record_skills:
            return None
        return (await call("begin_skill", skill_id=skill, page_ids=[page_id]))["run_id"]

    async def finish(run_id, outputs=()):
        if record_skills:
            await call("finish_skill", run_id=run_id, output_ids=list(outputs))

    async def current_publication(page_id):
        if page_id in publication_cache:
            return publication_cache[page_id]
        detail = await call("get_page_detail", page_id=page_id, include_blocks=False, include_comments=False, include_relations=False)
        page = detail["page"]
        if page["lifecycle_status"] != "approved":
            return None
        offset = 0
        while True:
            history = await call("list_publications", page_id=page_id, offset=offset, limit=30, include_contract=False)
            for row in history["publications"]:
                if row["page_version"] == page["version"] and row["review_id"] == page.get("approved_review_id"):
                    publication_cache[page_id] = await call("read_publication", publication_id=row["publication_id"])
                    return publication_cache[page_id]
            offset += len(history["publications"])
            if offset >= history["total"]:
                return None

    while True:
        publication_cache.clear()
        pending = list(public["requests"])
        details = {}
        if alternative:
            pending.reverse()
        while pending:
            progress = False
            for request in pending[:]:
                page_id = request["page_id"]
                if page_id not in details:
                    details[page_id] = await call("get_page_detail", page_id=page_id,
                        include_comments=False, include_relations=False)
                detail = details[page_id]
                page = detail["page"]
                tracker = next(i for i in detail["database_items"] if i["item_id"] == request["tracker_item_id"])
                if page["lifecycle_status"] == "approved" and tracker["status"] == "approved" and tracker.get("published_version") == page["version"]:
                    pending.remove(request)
                    progress = True
                    continue
                requirement = json.loads(next(b["content"] for b in detail["blocks"] if b["block_id"] == page["requirements_block_id"]))
                parents = [await current_publication(p) for p in requirement["parent_page_ids"]]
                if any(p is None for p in parents):
                    continue
                progress = True
                run_id = await begin("trace-planning-dependencies", page_id)
                await finish(run_id, [page_id] + [p["publication_id"] for p in parents])
                run_id = await begin("reconcile-interface-contracts", page_id)
                if use_script:
                    contract = (await call("run_skill_script", skill_id="reconcile-interface-contracts",
                        inputs={"requirements": requirement, "publications": parents}))["contract"]
                else:
                    fields = deepcopy(requirement["local_fields"])
                    for parent in parents:
                        for name, definition in parent["contract"]["fields"].items():
                            if name in fields and fields[name] != definition:
                                raise AssertionError("Incompatible public field definitions")
                            fields[name] = definition
                    contract = {"fields": fields, "limits": {k: min([v] + [p["contract"]["limits"][k] for p in parents])
                        for k, v in requirement["local_limits"].items()}, "imports": [{k: p[k] for k in ("page_id", "page_version", "publication_id")} for p in parents]}
                    if "portfolio_policy" in requirement:
                        from .portfolio_rules import capacity
                        contract["limits"]["batch_size"], contract["capacity"] = capacity(requirement, parents)
                    elif "capacity_policy" in requirement:
                        policy = requirement["capacity_policy"]
                        incoming = sum(next(q["units"] for q in p["contract"]["capacity"]["allocations"]
                            if q["consumer_page_id"] == page_id and q["activation_stage"] == policy["stage"]) for p in parents)
                        batch = min(requirement["local_limits"]["batch_size"], incoming + policy["local_capacity_units"])
                        weights = sum(q["weight"] for q in policy["consumers"])
                        outgoing = [{"consumer_page_id": q["consumer_page_id"], "activation_stage": q["activation_stage"],
                            "units": min(q["max_units"], batch * q["weight"] // weights)} for q in policy["consumers"]]
                        contract["limits"]["batch_size"] = batch
                        contract["capacity"] = {"incoming_units": incoming, "local_units": policy["local_capacity_units"],
                            "allocations": outgoing, "uncommitted_units": batch - sum(q["units"] for q in outgoing)}
                await finish(run_id, [p["publication_id"] for p in parents])
                run_id = await begin("revise-planning-document", page_id)
                proposed_contract = deepcopy(contract)
                recovering = exercise_recovery and not recovery_used
                if recovering:
                    proposed_contract["limits"]["retention_days"] += 1
                    recovery_used = True
                await call("revise_page_block", block_id=page["contract_block_id"], editor_user_id=editor,
                    new_content=json.dumps(proposed_contract, sort_keys=alternative))
                proposed = [d for d in detail["decisions"] if d["status"] == "proposed"]
                if not proposed:
                    accepted = [d for d in detail["decisions"] if d["status"] == "accepted"]
                    extra = {"supersedes_decision_id": accepted[-1]["decision_id"]} if accepted else {}
                    decision = await call("record_decision", page_id=page_id, owner_user_id=editor,
                        decision_title="Adopt reconciled interface", status="proposed", rationale="Integrate declared parent fields and enforce the applicable source limits.",
                        alternatives=["Keep the incompatible prior draft"], impact_score=5, linked_item_ids=[request["tracker_item_id"]], **extra)
                    proposed = [decision]
                await finish(run_id, [d["decision_id"] for d in proposed])
                run_id = await begin("coordinate-document-review", page_id)

                async def review_current():
                    review = await call("request_page_review", page_id=page_id, requester_user_id=editor,
                        reviewer_user_ids=requirement["reviewer_user_ids"], due_at=public["review_due_at"])
                    return await call("run_document_review", review_id=review["review_id"])

                review = await review_current()
                if recovering:
                    if review["status"] != "changes_requested":
                        raise AssertionError("Invalid retention unexpectedly approved")
                    await call("revise_page_block", block_id=page["contract_block_id"], editor_user_id=editor, new_content=json.dumps(contract))
                    detail = await call("get_page_detail", page_id=page_id)
                    for thread in detail["comment_threads"]:
                        if thread["status"] in {"open", "reopened"}:
                            await call("resolve_comment_thread", thread_id=thread["thread_id"], actor_user_id=editor,
                                resolution_action="accepted", resolution_note="Corrected the actual retention value to the declared source cap.")
                    review = await review_current()
                if review["status"] != "approved":
                    raise AssertionError(review)
                accepted = await call("approve_review_and_transition_page", review_id=review["review_id"], actor_user_id=editor)
                await finish(run_id, [review["review_id"]])
                run_id = await begin("publish-reviewed-decision", page_id)
                publication = await call("publish_decision_summary", workspace_id=public["workspace_id"], actor_user_id=editor,
                    target_page_id=page_id, decision_ids=accepted["accepted_decision_ids"])
                publication_cache[page_id] = publication
                await finish(run_id, [publication["publication_id"]])
                run_id = await begin("synchronize-planning-trackers", page_id)
                await call("synchronize_tracker", item_id=request["tracker_item_id"], publication_id=publication["publication_id"])
                await finish(run_id, [request["tracker_item_id"], publication["publication_id"]])
                pending.remove(request)
                completed += 1
                if stop_after_pages is not None and completed >= stop_after_pages:
                    return {"stopped_after_completed_pages": completed}
            if not progress:
                raise AssertionError("No ready document among the public dependencies")
        updates = await call("poll_planning_updates")
        if updates["status"] == "drained":
            return {"completed_page_deliveries": completed}
        if updates["status"] != "update_released":
            raise AssertionError(updates)
