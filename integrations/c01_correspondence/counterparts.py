"""Bounded synthetic correspondents consume actual delivered mail, not actor claims."""

from copy import deepcopy
import json

from .kernel import BusinessError, encoded, instant, require, schema


def payload(body):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    try:
        value = json.loads(body, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        raise BusinessError("BODY_MUST_BE_PUBLIC_JSON_PACKET") from None
    require(type(value) is dict, "BODY_MUST_BE_PUBLIC_JSON_PACKET")
    return value


def exact_keys(value, names):
    require(set(value) == set(names), "PACKET_FIELD_SET")


class ProjectCorrespondents:
    def __init__(self, task, runtime):
        super().__init__(task, runtime)
        self.metadata["poll_mailbox"] = {"description": "Deliver due deterministic counterpart replies and declared briefing amendments into the actor inbox. Returns new mailbox identities, not task answers.",
            "input_schema": schema({})}

    def dispatch(self, name, args):
        if name == "poll_mailbox":
            return self.poll()
        return super().dispatch(name, args)

    def attachment_set(self, message, expected):
        actual = message["attachments"]
        require(len(actual) == len(expected), "ATTACHMENT_SET_MISMATCH")
        for document_id, revision in expected:
            matches = [a for a in actual if a["document_id"] == document_id and a["document_revision"] == revision]
            require(len(matches) == 1, "ATTACHMENT_REVISION_BINDING")
            doc = self.state["documents"][document_id]
            require(matches[0]["content"] == doc["versions"][str(revision)]["content"], "ATTACHMENT_CONTENT_BINDING")

    def parent_dispatches(self, node, identifiers):
        require(type(identifiers) is list and all(type(i) is str for i in identifiers) and len(identifiers) == len(set(identifiers)), "PARENT_MESSAGE_IDENTITIES")
        require(len(identifiers) == len(node["parent_node_ids"]), "PARENT_DISPATCH_SET")
        rows = []
        for identifier in identifiers:
            require(identifier in self.state["acknowledgements"], "PARENT_DISPATCH_NOT_ACKNOWLEDGED")
            row = self.state["acknowledgements"][identifier]
            parent = self.state["nodes"][row["node_id"]]
            require(row["brief_revision"] == self.state["documents"][parent["brief_document_id"]]["current_revision"], "STALE_PARENT_DISPATCH")
            rows.append(row)
        require({r["node_id"] for r in rows} == set(node["parent_node_ids"]), "PARENT_NODE_IDENTITY_JOIN")
        return rows

    def new_environment_document(self, owner, filename, content):
        identifier = self.next_id("document")
        row = {"document_id": identifier, "owner_account_id": owner, "filename": filename, "current_revision": 1,
            "allowed_recipient_ids": list(self.public["project_staff_ids"]), "visible_to_actor": True, "authorship": "environment",
            "versions": {"1": {"revision": 1, "content": deepcopy(content), "created_at": self.now()}}}
        self.state["documents"][identifier] = row
        self.event("document.correspondent_created", row)
        return self.document(identifier)

    def reply(self, sender, original, body, documents=()):
        return self.deliver(sender, [self.public["actor"]], [], [], "Re: " + original["subject"], encoded(body), documents,
            reply_to=original["message_id"], thread_id=original["thread_id"])

    def review_request(self, recipient, message, request):
        exact_keys(request, ("kind", "node_id", "role", "brief_revision", "parent_dispatch_ids"))
        require(type(request["node_id"]) is str and request["node_id"] in self.state["nodes"], "UNKNOWN_PROJECT_PHASE")
        node = self.state["nodes"][request["node_id"]]
        require(request["role"] in ("finance", "operations"), "UNKNOWN_REVIEW_ROLE")
        require(recipient == node["reviewer_ids"][request["role"]], "RECIPIENT_ROLE_MISMATCH")
        require(message["recipient_ids"] == [recipient] and not message["cc_ids"] and not message["bcc_ids"], "REVIEW_ENVELOPE_SCOPE")
        require(type(request["brief_revision"]) is int, "BRIEF_REVISION_TYPE")
        brief = self.document(node["brief_document_id"])
        require(request["brief_revision"] == brief["revision"], "BRIEF_REVISION_STALE")
        require(instant(message["sent_at"]) <= instant(node["deadline"]), "REQUEST_AFTER_DEADLINE")
        parents = self.parent_dispatches(node, request["parent_dispatch_ids"])
        required = [(brief["document_id"], brief["revision"])] + [(p["document_id"], p["document_revision"]) for p in parents]
        self.attachment_set(message, required)
        items = brief["content"]["line_items"]
        content = {"kind": "phase_review", "node_id": node["node_id"], "role": request["role"], "brief_document_id": brief["document_id"],
            "brief_revision": brief["revision"], "parent_dispatch_ids": sorted(request["parent_dispatch_ids"]), "request_message_id": message["message_id"]}
        if request["role"] == "finance":
            content["budget_cents"] = sum(i["quantity"] * i["unit_cost_cents"] for i in items)
            if "funding_network" in self.public:
                from .funding_rules import funding
                try:
                    values = funding(node, brief, parents)
                except (KeyError, ValueError):
                    raise BusinessError("FUNDING_ORIGIN_OR_RESERVE_SHORTFALL") from None
                content.update(local_grant_cents=values["local_grant_cents"], incoming_funding_cents=values["incoming_cents"])
        else:
            content["duration_days"] = sum(i["work_days"] for i in items)
            content["start_day"] = max((p["finish_day"] for p in parents), default=0)
        document = self.new_environment_document(recipient, node["node_id"] + "-" + request["role"] + "-review.json", content)
        sent = self.reply(recipient, message, {"kind": "review_reply", "outcome": "reviewed", "node_id": node["node_id"],
            "review_document_id": document["document_id"], "document_revision": 1}, [document])
        review = {**content, "reply_message_id": sent["message_id"], "document_id": document["document_id"], "document_revision": 1,
            "reviewer_id": recipient, "issued_at": self.now()}
        self.state["reviews"][sent["message_id"]] = review
        self.event("review.issued", review)
        return sent

    def dispatch_request(self, recipient, message, request):
        exact_keys(request, ("kind", "node_id", "package_document_id", "package_revision"))
        require(type(request["node_id"]) is str and request["node_id"] in self.state["nodes"], "UNKNOWN_PROJECT_PHASE")
        node = self.state["nodes"][request["node_id"]]
        require(recipient == node["owner_account_id"], "RECIPIENT_ROLE_MISMATCH")
        require(message["recipient_ids"] == [recipient] and message["cc_ids"] == [] and message["bcc_ids"] == [node["audit_account_id"]], "DISPATCH_RECIPIENT_AND_BCC_POLICY")
        require(instant(message["sent_at"]) <= instant(node["deadline"]) and instant(self.now()) <= instant(node["deadline"]), "DISPATCH_ACK_DEADLINE")
        require(type(request["package_revision"]) is int and type(request["package_document_id"]) is str, "PACKAGE_IDENTITY_TYPE")
        package = self.document(request["package_document_id"], request["package_revision"])
        require(package["authorship"] == "actor", "COORDINATOR_PACKAGE_REQUIRED")
        content = package["content"]
        exact_keys(content, ("kind", "node_id", "brief_document_id", "brief_revision", "parent_dispatch_ids", "finance_reply_message_id",
            "operations_reply_message_id", "budget_cents", "start_day", "finish_day", "line_items") + (("funding",) if "funding_network" in self.public else ()))
        require(content["kind"] == "phase_dispatch" and content["node_id"] == node["node_id"], "PACKAGE_PHASE_IDENTITY")
        brief = self.document(node["brief_document_id"])
        require(content["brief_document_id"] == brief["document_id"] and type(content["brief_revision"]) is int and content["brief_revision"] == brief["revision"], "BRIEF_REVISION_STALE")
        if node["node_id"] == self.public["policy"]["declared_amendment_node_id"]:
            require(self.state["applied_amendment"] is not None, "DECLARED_AMENDMENT_PENDING")
        parents = self.parent_dispatches(node, content["parent_dispatch_ids"])
        reviews = {}
        for role in ("finance", "operations"):
            identifier = content[role + "_reply_message_id"]
            require(type(identifier) is str and identifier in self.state["reviews"], "REVIEW_REPLY_NOT_ISSUED")
            review = self.state["reviews"][identifier]
            require(review["role"] == role and review["node_id"] == node["node_id"] and review["brief_revision"] == brief["revision"], "REVIEW_ROLE_PHASE_REVISION_JOIN")
            require(set(review["parent_dispatch_ids"]) == set(content["parent_dispatch_ids"]), "REVIEW_PARENT_JOIN")
            reviews[role] = review
        require(all(type(content[k]) is int for k in ("budget_cents", "start_day", "finish_day")), "PACKAGE_NUMERIC_TYPES")
        require(content["budget_cents"] == reviews["finance"]["budget_cents"], "PACKAGE_BUDGET_JOIN")
        start = reviews["operations"]["start_day"]
        if "liquidity_network" in self.public:
            from .funding_rules import funding
            start = funding(node, brief, parents)["funded_start_day"]
        require(content["start_day"] == start and content["finish_day"] == content["start_day"] + reviews["operations"]["duration_days"], "PACKAGE_SCHEDULE_JOIN")
        require(encoded(content["line_items"]) == encoded(brief["content"]["line_items"]), "PACKAGE_LINE_ITEM_CONTENT")
        if "funding_network" in self.public:
            from .funding_rules import funding
            try:
                values = funding(node, brief, parents)
            except (KeyError, ValueError):
                raise BusinessError("FUNDING_ORIGIN_OR_RESERVE_SHORTFALL") from None
            require(encoded(content["funding"]) == encoded(values), "PACKAGE_EARMARKED_FUNDING_AND_CONSERVATION")
        expected = [(package["document_id"], package["revision"]), (brief["document_id"], brief["revision"])]
        expected += [(r["document_id"], r["document_revision"]) for r in reviews.values()]
        expected += [(p["document_id"], p["document_revision"]) for p in parents]
        self.attachment_set(message, expected)
        response = {"kind": "dispatch_acknowledgement", "outcome": "accepted", "node_id": node["node_id"], "dispatch_message_id": message["message_id"],
            "document_id": package["document_id"], "document_revision": package["revision"], "brief_revision": brief["revision"],
            "budget_cents": content["budget_cents"], "start_day": content["start_day"], "finish_day": content["finish_day"], "acknowledged_at": self.now()}
        if "funding_network" in self.public:
            response["funding"] = deepcopy(content["funding"])
        sent = self.reply(recipient, message, response)
        response["acknowledgement_message_id"] = sent["message_id"]
        self.state["acknowledgements"][message["message_id"]] = response
        self.event("dispatch.acknowledged", response)
        return sent

    def apply_amendment(self):
        if self.state["applied_amendment"] is not None:
            return None
        update = self.state["amendment"]
        roles = {r["role"] for r in self.state["reviews"].values() if r["node_id"] == update["node_id"] and r["brief_revision"] == 1}
        if roles != {"finance", "operations"}:
            return None
        document = self.state["documents"][update["document_id"]]
        content = deepcopy(document["versions"]["1"]["content"])
        content["revision"] = 2
        content["line_items"][0].update(unit_cost_cents=update["new_unit_cost_cents"], work_days=update["new_work_days"])
        if "funding_policy_updates" in update:
            content["funding_policy"].update(deepcopy(update["funding_policy_updates"]))
        document["versions"]["2"] = {"revision": 2, "content": content, "created_at": self.now()}
        document["current_revision"] = 2
        changed = {"node_id": update["node_id"], "document_id": document["document_id"], "previous_revision": 1, "current_revision": 2, "released_at": self.now()}
        self.state["applied_amendment"] = changed
        self.event("brief.amended", {"update": changed, "version": document["versions"]["2"]})
        node = self.state["nodes"][update["node_id"]]
        return self.deliver(node["owner_account_id"], [self.public["actor"]], [], [], "Brief amendment: " + node["node_id"],
            encoded({"kind": "brief_amendment", **changed}), [self.document(document["document_id"], 2)])

    def poll(self):
        due = [p for p in self.state["pending"] if instant(p["due_at"]) <= instant(self.now())]
        self.state["pending"] = [p for p in self.state["pending"] if instant(p["due_at"]) > instant(self.now())]
        delivered = []
        reviewer_ids = {i for n in self.state["nodes"].values() for i in n["reviewer_ids"].values()}
        owner_ids = {n["owner_account_id"] for n in self.state["nodes"].values()}
        for pending in due:
            identity = [pending["message_id"], pending["recipient_id"]]
            require(identity not in self.state["processed_deliveries"], "DELIVERY_ALREADY_PROCESSED")
            recipient = pending["recipient_id"]
            message = self.state["messages"][pending["message_id"]]
            response = None
            if recipient in reviewer_ids | owner_ids:
                try:
                    request = payload(message["body"])
                    if request.get("kind") == "review_request":
                        response = self.review_request(recipient, message, request)
                    elif request.get("kind") == "dispatch":
                        response = self.dispatch_request(recipient, message, request)
                    else:
                        raise BusinessError("UNKNOWN_CORRESPONDENCE_PACKET")
                except BusinessError as error:
                    response = self.reply(recipient, message, {"kind": "clarification", "outcome": "needs_revision", "request_message_id": message["message_id"], "reason": str(error)})
                if response:
                    delivered.append(response)
                amendment = self.apply_amendment()
                if amendment:
                    delivered.append(amendment)
            self.state["processed_deliveries"].append(identity)
            self.event("delivery.processed", {**pending, "reply_message_id": response["message_id"] if response else None})
        output = []
        for sent in delivered:
            message = self.state["messages"][sent["message_id"]]
            actor_copy = next(self.owned("email", i) for i in message["copy_ids"] if self.mailbox_owner(self.owned("email", i)) == self.public["actor"])
            output.append({"email_id": actor_copy["email_id"], "message_id": message["message_id"], "sender_id": message["sender_id"], "subject": message["subject"]})
        return {"now": self.now(), "new_inbox_messages": output, "pending_delivery_count": len(self.state["pending"])}
