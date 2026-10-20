"""Exact mail delivery, versioned content and mailbox-specific authorization."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from string import Template

import jsonschema

from integrations.internal_mcp.io import load_backend, read_json, write_json, controller_source
from integrations.internal_mcp.tables import TableState
from .build import KEYS


READS = {"get_entity", "search_emails", "list_contacts", "list_folders", "list_templates", "list_labels"}


class BusinessError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise BusinessError(code)


def instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise BusinessError("INVALID_TIMESTAMP") from None
    require(parsed.tzinfo is not None, "TIMEZONE_REQUIRED")
    return parsed.astimezone(timezone.utc)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def schema(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required, "additionalProperties": False}


class AuthorizedView:
    """Source tools receive only an authorized view, never a singleton or disk fallback."""

    def __init__(self, mail):
        self.mail = mail

    def get_entity(self, kind, identifier, id_field=None):
        key = KEYS[kind]
        if id_field is not None and id_field != key:
            raise ValueError("Unexpected key")
        row = self.mail.sm.get_entity(kind, identifier, id_field)
        visible = self._visible(kind, [row] if row is not None else [])
        return visible[0] if visible else None

    def list_entities(self, kind):
        return self._visible(kind, self.mail.sm.list_entities(kind))

    def _visible(self, kind, rows):
        mail, actor = self.mail, self.mail.public["actor"]
        if kind == "account":
            public_keys = {"account_id", "email_address", "display_name", "role", "department", "status"}
            return [r if r["account_id"] == actor else {k: v for k, v in r.items() if k in public_keys} for r in rows]
        if kind == "folder":
            return [r for r in rows if r["account_id"] == actor]
        if kind == "email":
            visible = [r for r in rows if mail.mailbox_owner(r) == actor]
            for row in visible:
                if row["sender_id"] != actor:
                    row["bcc_ids"] = []
            return visible
        if kind == "attachment":
            visible = []
            for row in rows:
                email = mail.sm.get_entity("email", row.get("email_id"))
                if email is not None and mail.mailbox_owner(email) == actor:
                    visible.append(row)
            for row in visible:
                row.pop("storage_key", None)
                if "content" not in row:
                    row["content_status"] = "historical_metadata_only"
            return visible
        if kind == "contact":
            return [r for r in rows if r["owner_account_id"] == actor]
        if kind == "template":
            return [r for r in rows if r["created_by"] == actor]
        if kind == "label":
            return rows
        return []


class Correspondence:
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
        self.view = AuthorizedView(self)
        self.metadata = {name: deepcopy(self.backend._TOOL_METADATA[name]) for name in sorted(READS)}
        for entry in self.metadata.values():
            entry["input_schema"]["additionalProperties"] = False
        text = {"type": "string", "minLength": 1, "maxLength": 2000}
        body = {"type": "string", "maxLength": 131072}
        ids = {"type": "array", "items": text, "uniqueItems": True, "maxItems": 64}
        revision = {"type": "integer", "minimum": 1}
        draft_fields = {"recipient_ids": ids, "cc_ids": ids, "bcc_ids": ids, "subject": text, "body": body}
        definitions = {
            "read_correspondence_case": ("Read a project case's declared phases and current actor-visible correspondence identities, not a next action.", schema({"case_id": text})),
            "read_mail_clock": ("Read simulated time and count of outstanding correspondent deliveries.", schema({})),
            "advance_mail_clock": ("Advance time without automatically claiming review or task completion.", schema({"to_time": text})),
            "read_document": ("Read actual actor-accessible versioned document content; no invented historical attachment bytes.", schema({"document_id": text, "revision": revision}, ["document_id"])),
            "create_document": ("Create actual coordinator-authored JSON content. Does not validate its task correctness.", schema({"filename": text, "content": {"type": "object"}})),
            "revise_document": ("Create a new version of coordinator-authored content; old attachments remain immutable.", schema({"document_id": text, "expected_revision": revision, "content": {"type": "object"}})),
            "create_draft": ("Create a coordinator draft; sender identity is fixed by the task, not caller-supplied.", schema(draft_fields, ["recipient_ids", "subject", "body"])),
            "update_draft": ("Edit an unsent draft using optimistic revision binding; every edit increments its revision.", schema({"draft_id": text, "expected_revision": revision, **draft_fields}, ["draft_id", "expected_revision"])),
            "compose_from_template": ("Render a current authorized template without seed fallback; create an ordinary versioned draft.", schema({"template_id": text,
                "values": {"type": "object", "maxProperties": 16, "additionalProperties": {"type": "string", "maxLength": 131072}}, "recipient_ids": ids, "cc_ids": ids, "bcc_ids": ids}, ["template_id", "values", "recipient_ids"])),
            "add_attachment": ("Bind actual document bytes and exact revision to an unsent draft; actor does not declare file size.", schema({"draft_id": text, "expected_revision": revision, "document_id": text, "document_revision": revision})),
            "remove_attachment": ("Detach a draft attachment without erasing a past delivery or document version.", schema({"draft_id": text, "expected_revision": revision, "attachment_id": text})),
            "read_attachment": ("Read an authorized delivered/draft attachment's frozen bytes and source revision.", schema({"attachment_id": text})),
            "send_email": ("Transmit only the exact selected draft revision. Semantic mistakes and wrong known recipients can really be delivered.", schema({"draft_id": text, "expected_revision": revision})),
            "reply_to_email": ("Create a reply draft bound to an observed canonical message; reply-all never includes hidden BCC recipients.", schema({"email_id": text, "body": body, "reply_all": {"type": "boolean"}}, ["email_id", "body"])),
            "forward_email": ("Create a forward draft with independent copies of actual attachments and explicit source-message lineage.", schema({"email_id": text, "recipient_ids": ids, "comment": body, "cc_ids": ids, "bcc_ids": ids}, ["email_id", "recipient_ids", "comment"])),
            "read_mail_thread": ("Read actor-owned copies in an actual canonical thread; other mailbox copies remain private.", schema({"email_id": text})),
            "mark_email_read": ("Mark only an active actor-owned mailbox copy read or unread and derive folder counts.", schema({"email_id": text, "is_read": {"type": "boolean"}})),
            "move_email": ("Move only an actor-owned active mailbox copy to an actor-owned non-draft folder; no deletion or recall.", schema({"email_id": text, "folder_id": text})),
        }
        self.metadata.update({name: {"description": d, "input_schema": s} for name, (d, s) in definitions.items()})
        for name in ("update_draft", "add_attachment", "remove_attachment", "mark_email_read", "move_email"):
            self.metadata[name]["description"] += " Returns a receipt without subject/body; get_entity reads the authorized full email."
        self.validate_state()

    @property
    def state(self):
        return self.world["mail"]

    def now(self):
        return self.world["clock"]

    def next_id(self, kind):
        value = self.state["next_id"]
        self.state["next_id"] += 1
        if self.public.get("runtime_identity") == "task_local_v1":
            return f"{kind}-new-{value}"
        return f"{kind}-{self.world['task_id']}-{value}"

    def mailbox_owner(self, row):
        folder = self.sm.get_entity("folder", row.get("folder_id"))
        return folder["account_id"] if folder else None

    def folder(self, owner, kind):
        rows = [r for r in self.world["tables"]["folder"] if r["account_id"] == owner and r["type"] == kind]
        require(len(rows) == 1, "MAILBOX_FOLDER_UNAVAILABLE")
        return rows[0]["folder_id"]

    def owned(self, kind, identifier):
        if kind == "case":
            require(identifier in self.public["case_ids"], "CASE_NOT_FOUND")
            return {"case_id": identifier}
        if kind == "node":
            require(identifier in self.state["nodes"], "NODE_NOT_FOUND")
            return deepcopy(self.state["nodes"][identifier])
        row = self.sm.get_entity(kind, identifier)
        require(row is not None, kind.upper() + "_NOT_FOUND")
        require(row.get("task_id") == self.world["task_id"], "BACKGROUND_READ_ONLY")
        return row

    def actor_email(self, identifier, *, active=False):
        row = self.view.get_entity("email", identifier)
        require(row is not None, "EMAIL_NOT_VISIBLE")
        if active:
            require(row.get("task_id") == self.world["task_id"], "BACKGROUND_READ_ONLY")
        return row

    def draft(self, identifier, revision):
        row = self.actor_email(identifier, active=True)
        require(row["sender_id"] == self.public["actor"] and row["status"] == "draft", "UNSENT_ACTOR_DRAFT_REQUIRED")
        require(row["revision"] == revision, "DRAFT_REVISION_CONFLICT")
        return row

    def document(self, identifier, revision=None):
        require(identifier in self.state["documents"], "DOCUMENT_NOT_FOUND")
        row = self.state["documents"][identifier]
        require(row["visible_to_actor"], "DOCUMENT_NOT_VISIBLE")
        revision = row["current_revision"] if revision is None else revision
        require(str(revision) in row["versions"], "DOCUMENT_REVISION_NOT_FOUND")
        return {k: deepcopy(v) for k, v in row.items() if k not in {"versions", "visible_to_actor"}} | deepcopy(row["versions"][str(revision)])

    def recipients(self, values):
        groups = [values.get(k, []) for k in ("recipient_ids", "cc_ids", "bcc_ids")]
        flattened = sum(groups, [])
        require(bool(flattened) and len(flattened) == len(set(flattened)), "RECIPIENT_SET_INVALID")
        for identifier in flattened:
            account = self.owned("account", identifier)
            require(account["status"] == "active", "RECIPIENT_NOT_ACTIVE")
        return flattened

    def event(self, kind, data):
        record = {"event_id": self.next_id("event"), "type": kind, "sequence": self.world["sequence"], "occurred_at": self.now(), "data": deepcopy(data)}
        self.state["events"].append(record)
        return record

    def create_draft(self, values, *, reply_to=None, forward_of=None, thread_id=None):
        values = {**values, "cc_ids": values.get("cc_ids", []), "bcc_ids": values.get("bcc_ids", [])}
        require(0 < len(values["subject"]) <= 2000 and len(values["body"]) <= 131072, "RENDERED_MESSAGE_TOO_LARGE")
        self.recipients(values)
        identifier = self.next_id("email")
        row = self.sm.add_entity("email", {"email_id": identifier, "sender_id": self.public["actor"],
            **{k: deepcopy(values[k]) for k in ("recipient_ids", "cc_ids", "bcc_ids", "subject", "body")},
            "status": "draft", "is_read": True, "is_starred": False, "priority": "normal", "folder_id": self.folder(self.public["actor"], "drafts"),
            "label_ids": [], "attachment_ids": [], "reply_to_email_id": None, "reply_to_message_id": reply_to, "forward_of_message_id": forward_of,
            "message_id": None, "thread_id": thread_id, "template_id": values.get("template_id"), "sent_at": None,
            "revision": 1, "created_at": self.now(), "updated_at": self.now()})
        self.event("draft.created", row)
        return row

    def make_attachment(self, email_id, snapshot, *, source_attachment_id=None):
        identifier = self.next_id("attachment")
        row = self.sm.add_entity("attachment", {"attachment_id": identifier, "email_id": email_id, "filename": snapshot["filename"],
            "display_name": snapshot["filename"], "file_type": "application/json", "size_bytes": len(encoded(snapshot["content"]).encode()),
            "upload_status": "uploaded", "download_count": 0, "last_downloaded_at": None, "created_at": self.now(),
            "document_id": snapshot["document_id"], "document_revision": snapshot.get("document_revision", snapshot.get("revision")),
            "content": deepcopy(snapshot["content"]), "allowed_recipient_ids": deepcopy(snapshot["allowed_recipient_ids"]),
            "source_attachment_id": source_attachment_id})
        return row

    def freeze_attachments(self, row):
        return [self.owned("attachment", identifier) for identifier in row["attachment_ids"]]

    def send_draft(self, row):
        self.recipients(row)
        require(sum(m["sender_id"] == self.public["actor"] for m in self.state["messages"].values()) < self.public["policy"]["maximum_sent_messages"], "MESSAGE_QUOTA")
        return self.deliver(row["sender_id"], row["recipient_ids"], row["cc_ids"], row["bcc_ids"], row["subject"], row["body"],
            self.freeze_attachments(row), draft=row, reply_to=row["reply_to_message_id"], forward_of=row["forward_of_message_id"], thread_id=row["thread_id"])

    def deliver(self, sender, to, cc, bcc, subject, body, attachments, *, draft=None, reply_to=None, forward_of=None, thread_id=None):
        identifier = self.next_id("message")
        thread_id = thread_id or identifier
        envelope = {"message_id": identifier, "thread_id": thread_id, "sender_id": sender, "recipient_ids": deepcopy(to), "cc_ids": deepcopy(cc),
            "bcc_ids": deepcopy(bcc), "subject": subject, "body": body, "reply_to_message_id": reply_to, "forward_of_message_id": forward_of,
            "sent_at": self.now(), "attachments": deepcopy(attachments), "draft_id": draft["email_id"] if draft else None,
            "draft_revision": draft["revision"] if draft else None, "copy_ids": []}
        for owner in [sender] + to + cc + bcc:
            sent_copy = not envelope["copy_ids"]
            copy_id = draft["email_id"] if sent_copy and draft else self.next_id("email")
            row = {"email_id": copy_id, "sender_id": sender, "recipient_ids": deepcopy(to), "cc_ids": deepcopy(cc),
                "bcc_ids": deepcopy(bcc) if sent_copy else [], "subject": subject, "body": body, "status": "sent" if sent_copy else "received",
                "is_read": sent_copy, "is_starred": False, "priority": "normal", "folder_id": self.folder(owner, "sent" if sent_copy else "inbox"),
                "label_ids": [], "attachment_ids": [], "reply_to_email_id": None, "reply_to_message_id": reply_to,
                "forward_of_message_id": forward_of, "message_id": identifier, "thread_id": thread_id, "template_id": draft.get("template_id") if draft else None,
                "sent_at": self.now(), "revision": draft["revision"] if draft else 1,
                "created_at": draft["created_at"] if sent_copy and draft else self.now(), "updated_at": self.now()}
            if sent_copy and draft:
                row["attachment_ids"] = deepcopy(draft["attachment_ids"])
                self.sm.update_entity("email", copy_id, row)
            else:
                self.sm.add_entity("email", row)
                row["attachment_ids"] = [self.make_attachment(copy_id, a, source_attachment_id=a.get("attachment_id"))["attachment_id"] for a in attachments]
                self.sm.update_entity("email", copy_id, {"attachment_ids": row["attachment_ids"]})
            envelope["copy_ids"].append(copy_id)
        self.state["messages"][identifier] = envelope
        self.event("mail.sent", {"message": envelope, "copies": [self.owned("email", i) for i in envelope["copy_ids"]]})
        if sender == self.public["actor"]:
            due = (instant(self.now()) + timedelta(seconds=self.public["policy"]["reply_delay_seconds"])).isoformat().replace("+00:00", "Z")
            self.state["pending"].extend({"message_id": identifier, "recipient_id": i, "due_at": due} for i in to + cc + bcc)
        return {"message_id": identifier, "email_id": envelope["copy_ids"][0], "thread_id": thread_id, "sent_at": self.now(),
            "draft_revision": envelope["draft_revision"], "delivered_to": to + cc + bcc}

    def native(self, name, args):
        if "account_id" in args:
            require(args["account_id"] == self.public["actor"], "MAILBOX_NOT_AUTHORIZED")
        result = self.backend.call_tool(name, state_manager=self.view, **args)["content"]
        if result.get("message", "").startswith("TOOL_EXECUTION_ERROR"):
            raise RuntimeError(result["message"])
        require(result["status"] == "success", result.get("message", "NATIVE_TOOL_ERROR"))
        return result["data"]

    def dispatch(self, name, args):
        if name in READS:
            return self.native(name, args)
        if name == "read_correspondence_case":
            self.owned("case", args["case_id"])
            nodes = [deepcopy(n) for n in self.state["nodes"].values() if n["case_id"] == args["case_id"]]
            return {"case_id": args["case_id"], "nodes": nodes}
        if name == "read_mail_clock":
            return {"now": self.now(), "pending_delivery_count": len(self.state["pending"])}
        if name == "advance_mail_clock":
            target = instant(args["to_time"])
            require(instant(self.now()) <= target <= instant(self.public["origin"]) + timedelta(seconds=self.public["policy"]["maximum_clock_seconds"]), "CLOCK_BOUND")
            self.world["clock"] = target.isoformat().replace("+00:00", "Z")
            self.event("clock.advanced", {"now": self.now()})
            return {"now": self.now()}
        if name == "read_document":
            return self.document(args["document_id"], args.get("revision"))
        if name in {"create_document", "revise_document"}:
            require(len(encoded(args["content"]).encode()) <= 131072, "DOCUMENT_TOO_LARGE")
            if name == "create_document":
                identifier = self.next_id("document")
                row = {"document_id": identifier, "owner_account_id": self.public["actor"], "filename": args["filename"], "current_revision": 1,
                    "allowed_recipient_ids": list(self.public["project_staff_ids"]), "visible_to_actor": True, "authorship": "actor", "versions": {}}
                self.state["documents"][identifier] = row
            else:
                self.document(args["document_id"])
                row = self.state["documents"][args["document_id"]]
                require(row["owner_account_id"] == self.public["actor"] and row["authorship"] == "actor", "ENVIRONMENT_DOCUMENT_IMMUTABLE")
                require(row["current_revision"] == args["expected_revision"], "DOCUMENT_REVISION_CONFLICT")
                row["current_revision"] += 1
            version = {"revision": row["current_revision"], "content": deepcopy(args["content"]), "created_at": self.now()}
            row["versions"][str(row["current_revision"])] = version
            self.event("document.authored", {"document_id": row["document_id"], "version": version, "filename": row["filename"]})
            return self.document(row["document_id"])
        if name == "create_draft":
            return self.create_draft(args)
        if name == "update_draft":
            row = self.draft(args["draft_id"], args["expected_revision"])
            values = {k: args[k] for k in ("recipient_ids", "cc_ids", "bcc_ids", "subject", "body") if k in args}
            require(bool(values), "MISSING_MUTATION")
            self.recipients({**row, **values})
            updated = self.sm.update_entity("email", row["email_id"], {**values, "revision": row["revision"] + 1, "updated_at": self.now()})
            self.event("draft.updated", updated)
            return updated
        if name == "compose_from_template":
            template = self.view.get_entity("template", args["template_id"])
            require(template is not None and template["status"] == "active", "TEMPLATE_NOT_AVAILABLE")
            patterns = [Template(template[k]) for k in ("subject_pattern", "body_pattern")]
            require(all(p.is_valid() for p in patterns), "TEMPLATE_SYNTAX_UNSUPPORTED")
            expected = set().union(*(p.get_identifiers() for p in patterns))
            require(set(template["placeholders"]) == expected, "TEMPLATE_SYNTAX_UNSUPPORTED")
            require(set(args["values"]) == expected, "TEMPLATE_PLACEHOLDER_MISMATCH")
            subject, body = (p.substitute(args["values"]) for p in patterns)
            require(0 < len(subject) <= 2000 and len(body) <= 131072, "RENDERED_MESSAGE_TOO_LARGE")
            return self.create_draft({**args, "subject": subject, "body": body})
        if name in {"add_attachment", "remove_attachment"}:
            row = self.draft(args["draft_id"], args["expected_revision"])
            if name == "add_attachment":
                snapshot = self.document(args["document_id"], args["document_revision"])
                existing = self.freeze_attachments(row)
                require(not any(a["document_id"] == snapshot["document_id"] and a["document_revision"] == snapshot["revision"] for a in existing), "DUPLICATE_ATTACHMENT_BINDING")
                attachment = self.make_attachment(row["email_id"], snapshot)
                attachment_ids = row["attachment_ids"] + [attachment["attachment_id"]]
            else:
                require(args["attachment_id"] in row["attachment_ids"], "ATTACHMENT_NOT_ON_DRAFT")
                attachment_ids = [i for i in row["attachment_ids"] if i != args["attachment_id"]]
            require(len(attachment_ids) <= 64, "ATTACHMENT_LIMIT")
            updated = self.sm.update_entity("email", row["email_id"], {"attachment_ids": attachment_ids, "revision": row["revision"] + 1, "updated_at": self.now()})
            self.event("draft.attachments_changed", {"draft": updated, "attachments": self.freeze_attachments(updated)})
            return updated
        if name == "read_attachment":
            row = self.view.get_entity("attachment", args["attachment_id"])
            require(row is not None, "ATTACHMENT_NOT_VISIBLE")
            require("content" in row, "HISTORICAL_ATTACHMENT_BYTES_UNAVAILABLE")
            return row
        if name == "send_email":
            return self.send_draft(self.draft(args["draft_id"], args["expected_revision"]))
        if name in {"reply_to_email", "forward_email"}:
            original = self.actor_email(args["email_id"], active=True)
            require(original.get("message_id") is not None and original["status"] != "draft", "DELIVERED_MESSAGE_REQUIRED")
            if name == "reply_to_email":
                visible = [original["sender_id"]] + (original["recipient_ids"] + original["cc_ids"] if args.get("reply_all", False) else [])
                to = list(dict.fromkeys(i for i in visible if i != self.public["actor"]))
                return self.create_draft({"recipient_ids": to, "subject": "Re: " + original["subject"], "body": args["body"]},
                    reply_to=original["message_id"], thread_id=original["thread_id"])
            draft = self.create_draft({**args, "subject": "Fwd: " + original["subject"], "body": args["comment"] + "\n\n" + original["body"]},
                forward_of=original["message_id"])
            attachments = [self.make_attachment(draft["email_id"], a, source_attachment_id=a["attachment_id"])["attachment_id"] for a in self.freeze_attachments(original)]
            if attachments:
                draft = self.sm.update_entity("email", draft["email_id"], {"attachment_ids": attachments, "revision": draft["revision"] + 1})
                self.event("draft.attachments_changed", {"draft": draft, "attachments": self.freeze_attachments(draft)})
            return draft
        if name == "read_mail_thread":
            row = self.actor_email(args["email_id"], active=True)
            require(row.get("thread_id") is not None, "UNSENT_THREAD")
            return {"thread_id": row["thread_id"], "emails": [r for r in self.view.list_entities("email") if r.get("thread_id") == row["thread_id"]]}
        if name in {"mark_email_read", "move_email"}:
            row = self.actor_email(args["email_id"], active=True)
            if name == "mark_email_read":
                values = {"is_read": args["is_read"]}
            else:
                folder = self.view.get_entity("folder", args["folder_id"])
                require(folder is not None and folder["type"] in {"inbox", "sent", "archive", "custom"}, "FOLDER_NOT_AUTHORIZED")
                require(row["status"] != "draft", "DRAFT_MOVE_FORBIDDEN")
                values = {"folder_id": folder["folder_id"]}
            updated = self.sm.update_entity("email", row["email_id"], {**values, "updated_at": self.now()})
            self.event("mailbox.updated", {"email_id": row["email_id"], "values": values})
            return updated
        raise BusinessError("TOOL_NOT_IMPLEMENTED")

    def recount(self):
        emails = self.sm.list_entities("email")
        attachments = {r["attachment_id"]: r for r in self.sm.list_entities("attachment")}
        for account in self.world["tables"]["account"]:
            folder_ids = {r["folder_id"] for r in self.world["tables"]["folder"] if r["account_id"] == account["account_id"]}
            owned = [r for r in emails if r.get("folder_id") in folder_ids]
            size = sum(len((r.get("subject", "") + r.get("body", "")).encode()) + sum(max(0, int(attachments.get(i, {}).get("size_bytes", 0))) for i in r.get("attachment_ids", [])) for r in owned)
            require(size <= account["storage_limit_bytes"], "MAILBOX_STORAGE_EXCEEDED")
            self.sm.update_entity("account", account["account_id"], {"storage_used_bytes": size, "storage_used_mb": size / 1048576})
            for folder in self.world["tables"]["folder"]:
                if folder["folder_id"] in folder_ids:
                    selected = [r for r in owned if r["folder_id"] == folder["folder_id"]]
                    self.sm.update_entity("folder", folder["folder_id"], {"email_count": len(selected), "unread_count": sum(not r.get("is_read", False) for r in selected)})

    def validate_state(self):
        for kind, rows in self.world["tables"].items():
            require(len({r[KEYS[kind]] for r in rows}) == len(rows), "ENTITY_IDENTITY")
            require(all(r.get("task_id") == self.world["task_id"] for r in rows), "ENTITY_OWNERSHIP")
        for row in self.world["tables"]["email"]:
            require(self.mailbox_owner(row) is not None, "MAILBOX_OWNER_MISSING")
            for identifier in row["attachment_ids"]:
                attachment = self.owned("attachment", identifier)
                require(attachment["email_id"] == row["email_id"], "ATTACHMENT_COPY_IDENTITY")
                require(attachment["size_bytes"] == len(encoded(attachment["content"]).encode()), "ATTACHMENT_ACTUAL_SIZE")
            if row["status"] == "received":
                require(row["bcc_ids"] == [], "RECIPIENT_BCC_PRIVACY")

    def record_call(self, name, arguments, result):
        pass

    def result_data(self, name, data):
        if name in {"update_draft", "add_attachment", "remove_attachment", "mark_email_read", "move_email"}:
            # Keep addressing and revision bindings; the authorized read returns the full copy.
            return {k: deepcopy(v) for k, v in data.items() if k not in {"body", "subject"}}
        return data

    def call(self, name, arguments):
        before = deepcopy(self.world)
        try:
            require(name in self.metadata, "TOOL_NOT_EXPOSED")
            try:
                encoded(arguments)
                jsonschema.validate(arguments, self.metadata[name]["input_schema"])
            except (ValueError, TypeError, jsonschema.ValidationError):
                raise BusinessError("ARGUMENT_SCHEMA") from None
            data = self.dispatch(name, deepcopy(arguments))
            if name not in READS:
                self.recount()
                self.validate_state()
            result = {"status": "success", "data": self.result_data(name, data), "message": ""}
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
