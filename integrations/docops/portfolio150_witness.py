"""Efficient public DocOps reference with selective cache invalidation."""

import json

REVISION = "public_document_cache_v2"


async def run(call):
    task = await call("read_task")
    for skill in task["skills"]:
        await call("read_skill_file", skill_id=skill["name"])
    requests = {r["page_id"]: r for r in task["requests"]}
    details, publications, accepted_decisions = {}, {}, {}
    pending = set(requests)
    editor = task["editor_user_id"]
    deliveries = 0
    while True:
        while pending:
            ready = sorted((pid for pid in pending if all(p in publications
                for p in requests[pid]["parent_page_ids"])),
                key=lambda pid: (requests[pid]["stage"], pid))
            if not ready:
                raise ValueError("No ready document in public dependency directory")
            pid = ready[0]
            request = requests[pid]
            if pid not in details:
                details[pid] = await call("get_page_detail", page_id=pid,
                    include_comments=False, include_relations=False)
            detail = details[pid]
            page = detail["page"]
            req = json.loads(next(b["content"] for b in detail["blocks"]
                                 if b["block_id"] == page["requirements_block_id"]))
            # The script needs the child's quota and inherited schema, not the
            # parent's duplicated page blocks, prose or other consumers' quotas.
            parents = [{**{k: publications[p][k] for k in ("page_id", "page_version", "publication_id")},
                "contract": {"fields": publications[p]["contract"]["fields"],
                    "limits": publications[p]["contract"]["limits"], "capacity": {
                        "allocations": [q for q in publications[p]["contract"]["capacity"]["allocations"]
                                        if q["consumer_page_id"] == pid]}}}
                for p in req["parent_page_ids"]]
            contract = (await call("run_skill_script", skill_id="reconcile-interface-contracts",
                inputs={"requirements": req, "publications": parents}))["contract"]
            await call("revise_page_block", block_id=page["contract_block_id"],
                editor_user_id=editor, new_content=json.dumps(contract))
            extra = {"supersedes_decision_id": accepted_decisions[pid]} if pid in accepted_decisions else {}
            decision = await call("record_decision", page_id=pid, owner_user_id=editor,
                decision_title="Adopt reconciled interface", status="proposed",
                rationale="Preserve actual source rights and current parent allocations.",
                alternatives=["Retain an outdated contract"], impact_score=5,
                linked_item_ids=[request["tracker_item_id"]], **extra)
            review = await call("request_page_review", page_id=pid, requester_user_id=editor,
                reviewer_user_ids=req["reviewer_user_ids"], due_at=task["review_due_at"])
            review = await call("run_document_review", review_id=review["review_id"])
            if review["status"] != "approved":
                raise ValueError(f"Public reference review failed: {review}")
            acceptance = await call("approve_review_and_transition_page", review_id=review["review_id"], actor_user_id=editor)
            publication = await call("publish_decision_summary", workspace_id=task["workspace_id"],
                actor_user_id=editor, target_page_id=pid, decision_ids=acceptance["accepted_decision_ids"])
            await call("synchronize_tracker", item_id=request["tracker_item_id"],
                publication_id=publication["publication_id"])
            publications[pid] = publication
            accepted_decisions[pid] = decision["decision_id"]
            pending.remove(pid)
            deliveries += 1
        update = await call("poll_planning_updates")
        if update["status"] == "drained":
            return {"completed_deliveries": deliveries}
        if update["status"] != "update_released":
            raise ValueError(f"Unexpected planning update status: {update['status']}")
        # Public notification is the validity evidence; unaffected records remain.
        pending = set(update["invalidated_page_ids"])
        for pid in pending:
            publications.pop(pid, None)
        for event in update["events"]:
            details.pop(event["page_id"], None)
