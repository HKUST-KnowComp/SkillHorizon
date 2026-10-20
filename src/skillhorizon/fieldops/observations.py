"""Bounded public Operations inventory; no future-event or evaluator access."""
from copy import deepcopy
from typing import Literal
from typing_extensions import TypedDict
from pydantic import ConfigDict
from ..public_contract import Offset, PageLimit, page, validate_fields
from .documents import DOCUMENTS, public_document

Collection = Literal["artifacts", "active", "devices", "windows", "crews", "parts", "isolations",
                     "requests", "suppliers", "orders", "notices", "services"]


class ObjectFilter(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")
    field: Literal["id", "kind", "status", "skill_id", "stage", "device_id", "supplier_id", "request_id"]
    value: str


QUERY_TOOLS = [
    ("search_objects", "Search public resource and artifact summaries, not artifact contents. Exact AND filters, literal case-insensitive text; text='' and filters=[] lists records. Stable ID order, limit 1..200; follow next_offset.",
     {"collection": Collection, "text": str, "filters": list[ObjectFilter], "offset": Offset, "limit": PageLimit}),
    ("read_object", "Read one public resource or committed business record, including old records. Active outputs are receipts; read_artifact retrieves their contents. Unavailable collections are rejected.",
     {"collection": Collection, "object_id": str}),
]


def receipt(output):
    return {key: deepcopy(output[key]) for key in ("artifact_id", "kind", "notices") if key in output}


class OperationsObservations:
    @property
    def query_tools(self):
        documents = [("read_public_document", "Read a public contract document listed by read_project. "
                       "Returns rules and terms, not a score or task diagnosis.", {"document_id": str})]
        return QUERY_TOOLS + (documents if self.project["domain"] == "procureops" else [])

    def read_public_document(self, document_id):
        self.require(self.project["domain"] == "procureops" and document_id in DOCUMENTS,
                     "DOCUMENT_NOT_EXPOSED", "Use a document_id listed by read_project; filesystem paths are not accepted")
        return public_document(self.project, document_id)

    def public_notices(self, notices):
        return [{k: deepcopy(v) for k, v in n.items() if k != "invalidated_artifacts"} for n in notices]

    def collection(self, name):
        if name == "notices":
            return {f"notice-{i+1}": n for i, n in enumerate(self.public_notices(self.project["notices"]))}
        self.require(name in self.project and isinstance(self.project[name], dict), "COLLECTION", "Collection unavailable in this domain")
        return self.project[name]

    def search_objects(self, collection, text, filters, offset, limit):
        validate_fields(dict(collection=collection, text=text, filters=filters, offset=offset, limit=limit), self.query_tools[0][2])
        rows = []
        for key, value in sorted(self.collection(collection).items()):
            item = {"id": key, **{k: deepcopy(value[k]) for k in
                    ("kind", "status", "skill_id", "revision", "quantity", "stage") if k in value}}
            if collection == "artifacts":
                item.update({k: value["data"][k] for k in ("device_id", "supplier_id", "request_id", "stage") if k in value["data"]})
            if collection == "active":
                item["status"] = "committed" if value["output"] else "pending"
            if collection == "notices":
                item["kind"] = value["type"]
            if text.casefold() in " ".join(map(str, item.values())).casefold() and all(
                    str(item.get(f["field"], "")) == f["value"] for f in filters):
                rows.append(item)
        return page(rows, offset, limit)

    def read_object(self, collection, object_id):
        validate_fields(dict(collection=collection, object_id=object_id), self.query_tools[1][2])
        value = deepcopy(self.collection(collection).get(object_id))
        self.require(value is not None, "UNKNOWN_OBJECT", "Unknown public object")
        if collection == "active" and value["output"]:
            value["output"] = receipt(value["output"])
        return {"id": object_id, "object": value}

    def read_project(self):
        p = self.project
        collections = [name for name in Collection.__args__ if name == "notices" or name in p]
        return {"project_id": p["project_id"], "domain": p["domain"], "family": p["family"], "goal": p["goal"],
                "revision": p["revision"], "business_program": p.get("business_program"),
                "collections": {name: len(self.collection(name)) for name in collections},
                "rules": {key: deepcopy(p[key]) for key in
                          ("minimum_upstream_pressure", "network", "total_budget_cents", "budget_id", "contract",
                           "service_rules", "assembly_rules") if key in p},
                "progress": self.public_progress(),
                **({"public_documents": [{"document_id": key, "title": title} for key, title in DOCUMENTS.items()],
                    "public_interface_revision": p["public_interface_revision"]} if p["domain"] == "procureops" else {})}
