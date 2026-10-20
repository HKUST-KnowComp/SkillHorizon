"""Public observation and invocation contracts shared by all current domains."""

from copy import deepcopy
import inspect
from functools import lru_cache
from typing import Annotated, Any

from pydantic import BeforeValidator, Field, TypeAdapter, ValidationError

from .errors import DomainError


def _public_integer(value):
    if type(value) is float and value.is_integer():
        return int(value)
    if type(value) is not int:
        raise ValueError("Use an integer JSON number, not a string or boolean")
    return value


Offset = Annotated[int, Field(ge=0, description="Zero-based offset; follow next_offset until null."), BeforeValidator(_public_integer)]
PageLimit = Annotated[int, Field(ge=1, le=200, description="Maximum items in this page, from 1 to 200."), BeforeValidator(_public_integer)]
Revision = Annotated[int, Field(ge=1, description="Positive version number obtained from object history."), BeforeValidator(_public_integer)]


@lru_cache(maxsize=64)
def _adapter(annotation):
    return TypeAdapter(annotation)


def _integer_representation(value, schema):
    if schema.get("type") == "integer" and type(value) is float and value.is_integer():
        return int(value)
    return value


def page(rows, offset, limit):
    values = validate_fields({"offset": offset, "limit": limit}, {"offset": Offset, "limit": PageLimit})
    offset, limit = values["offset"], values["limit"]
    return {"items": deepcopy(rows[offset:offset + limit]), "total": len(rows), "offset": offset,
            "next_offset": offset + limit if offset + limit < len(rows) else None}


def validate_fields(arguments, fields):
    if not isinstance(arguments, dict) or set(arguments) != set(fields):
        raise DomainError("ARGUMENT_SCHEMA", "Supply exactly the public tool fields", {"fields": sorted(fields)})
    normalized = deepcopy(arguments)
    for key, annotation in fields.items():
        try:
            adapter = _adapter(annotation)
            normalized[key] = _integer_representation(arguments[key], adapter.json_schema())
            adapter.validate_python(normalized[key], strict=True)
        except ValidationError as exc:
            problems = [{"path": [key, *item["loc"]], "message": item["msg"]}
                        for item in exc.errors(include_url=False, include_input=False)]
            raise DomainError("ARGUMENT_SCHEMA", "Arguments violate the public tool schema", {"issues": problems}) from None
    return normalized


def register_tools(server, runtime, entries):
    """MCP signatures and direct-runtime validation use the same field definitions."""
    for name, description, fields in entries:
        def handler(tool_name):
            def invoke(**kwargs) -> dict[str, Any]:
                return runtime.call(tool_name, kwargs)
            return invoke
        invoke = handler(name)
        invoke.__name__ = name
        invoke.__signature__ = inspect.Signature([
            inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY, annotation=annotation)
            for key, annotation in fields.items()], return_annotation=dict[str, Any])
        server.tool(name=name, description=description)(invoke)


def lifecycle(active, deliverable_id):
    return {"open_run_count": len(active), "terminal_run_ids": sorted(
        key for key, run in active.items()
        if deliverable_id and (run.get("output") or {}).get("artifact_id") == deliverable_id)}


def completion(success, active, deliverable_id):
    state = lifecycle(active, deliverable_id)
    return {"success": bool(success), "lifecycle_complete": not state["open_run_count"],
            "termination_ready": bool(success) and not state["terminal_run_ids"]}


def public_progress(active, deliverable_id, deliverable_state, pending_work):
    state = lifecycle(active, deliverable_id)
    recorded = deliverable_state in {"current", "recorded"} and not any(pending_work.values())
    return {"deliverable": {"id": deliverable_id, "state": deliverable_state},
            "pending_work": pending_work, "lifecycle": state,
            "phase": "handoff_recorded" if recorded and not state["terminal_run_ids"] else "work_pending",
            "verification": "not_evaluated_by_this_public_view"}
