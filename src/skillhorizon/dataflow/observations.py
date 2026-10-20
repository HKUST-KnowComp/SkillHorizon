"""Bounded public discovery; no evaluator or scheduled-event access."""

from copy import deepcopy
from typing import Literal
from typing_extensions import TypedDict
from pydantic import ConfigDict

from ..public_contract import Offset, PageLimit, page, validate_fields


Collection = Literal["sources", "artifacts", "active", "work_packages", "notices"]


class ObjectFilter(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")
    field: Literal["id", "kind", "region", "period", "source_id", "status", "producer_skill", "skill_id"]
    value: str


QUERY_TOOLS = [
    ("search_objects", "Search public sources, artifacts, active runs, work_packages or delivered notices. Literal case-insensitive text over summary fields; filters are exact AND matches. Use text='' and filters=[] to list. Artifact status is current/stale (source freshness, not correctness); active status is pending/committed. Stable ID order. Follow next_offset; limit 1..200.",
     {"collection": Collection, "text": str, "filters": list[ObjectFilter], "offset": Offset, "limit": PageLimit}),
    ("read_object", "Read one public object's metadata and dependencies. Active run outputs are receipts, not table rows. Use read_table for contents. Old artifacts remain accessible; current is source freshness, not correctness.",
     {"collection": Collection, "object_id": str}),
]


def artifact_receipt(runtime, output):
    if not isinstance(output, dict) or "artifact_id" not in output:
        return deepcopy(output)
    return {**{key: deepcopy(output[key]) for key in
               ("artifact_id", "kind", "region", "period", "row_count", "notices") if key in output},
            "current": runtime.current(output["artifact_id"])}


class PublicObservations:
    query_tools = QUERY_TOOLS

    def _public_collection(self, name):
        if name in {"sources", "artifacts", "active"}:
            return self.project[name]
        if name == "work_packages":
            return {item["id"]: item for item in self.project[name]}
        self._require(name == "notices", "COLLECTION", "Unknown public collection")
        return {f"notice-{i + 1}": item for i, item in
                enumerate(self._public_notices(self.project["notices"]))}

    def _public_summary(self, collection, identifier, item):
        result = {"id": identifier, **{key: deepcopy(item[key]) for key in
                  ("kind", "region", "period", "source_id", "revision", "row_count", "producer_skill", "skill_id")
                  if key in item}}
        if collection == "artifacts":
            result["status"] = "current" if self.current(identifier) else "stale"
        elif collection == "active":
            result["status"] = "committed" if item["output"] is not None else "pending"
            result["output_id"] = (item["output"] or {}).get("artifact_id")
        elif collection == "notices":
            result.update(kind=item["type"], region=item.get("effective_region"), period=item.get("effective_period"))
        return result

    def search_objects(self, collection, text, filters, offset, limit):
        validate_fields(dict(collection=collection, text=text, filters=filters, offset=offset, limit=limit), QUERY_TOOLS[0][2])
        rows = []
        for identifier, item in sorted(self._public_collection(collection).items()):
            summary = self._public_summary(collection, identifier, item)
            if (text.casefold() in " ".join(str(v) for v in summary.values()).casefold()
                    and all(summary.get(rule["field"]) == rule["value"] for rule in filters)):
                rows.append(summary)
        return page(rows, offset, limit)

    def read_object(self, collection, object_id):
        validate_fields(dict(collection=collection, object_id=object_id), QUERY_TOOLS[1][2])
        item = deepcopy(self._public_collection(collection).get(object_id))
        self._require(item is not None, "UNKNOWN_OBJECT", "Unknown public object")
        if collection == "artifacts":
            item["current"] = self.current(object_id)
        elif collection == "active":
            item["output"] = artifact_receipt(self, item["output"])
        return {"id": object_id, "object": item}

    def read_project(self):
        return {"project_id": self.project["project_id"], "goal": self.project["goal"],
                "revision": self.project["revision"], "regions": self.project["regions"],
                "periods": self.project["periods"], "progress": self.public_progress(),
                "collections": {key: len(self._public_collection(key)) for key in
                                ("sources", "artifacts", "active", "work_packages", "notices")},
                **({"business_program": self.project["business_program"], "funding": deepcopy(self.project["funding"])}
                   if self.project.get("business_program") else {})}
