"""Public message positions and current work, not inferred attention or opaque-token estimates."""
from copy import deepcopy
from functools import lru_cache
import json

from ..util import canonical_json


@lru_cache(maxsize=1)
def _encoding():
    import tiktoken
    return tiktoken.get_encoding("o200k_base")


@lru_cache(maxsize=4096)
def _tokens(text):
    encoding = _encoding()
    return len(encoding.encode(text)) if encoding else None


def request_observation(payload, public_project, state):
    encoding = _encoding()
    tokens = lambda value: _tokens(canonical_json(value))
    offset = tokens(payload["tools"])
    spans, exposures, known = [], {}, set()
    opaque_count = 0
    for index, original in enumerate(payload["input"]):
        message = deepcopy(original)
        if message.get("type") == "reasoning":
            opaque_count += int(bool(message.pop("encrypted_content", None)))
            message.pop("id", None)
        size = tokens(message)
        end = offset + size if offset is not None else None
        span = {"message_index": index, "local_token_start": offset, "local_token_end": end,
                "type": message.get("type", message.get("role"))}
        spans.append(span)

        def observe(value):
            if not isinstance(value, dict):
                return
            # Layered workspace objects have stable IDs without tabular artifact wrappers.
            if isinstance(value.get("id"), str):
                known.add(value["id"])
                if isinstance(value.get("object"), dict):
                    detail = value["object"]
                    # Tabular object details describe lineage, not table contents.
                    if "row_count" not in detail:
                        exposures[value["id"]] = {**span, "coverage": "full"}
            for key in ("assessment_id", "decision_id", "plan_id", "change_id"):
                if isinstance(value.get(key), str):
                    known.add(value[key])
                    if key in {"assessment_id", "plan_id"} and "source_revision" in value:
                        exposures[value[key]] = {**span, "coverage": "full"}
            identifier = value.get("artifact_id") or value.get("table_id") or value.get("source_id")
            if isinstance(identifier, str):
                known.add(identifier)
                if "data" in value or "rows" in value:
                    if "rows" not in value or value["rows"] or value.get("row_count", 0) == 0:
                        partial = bool(value.get("rows_truncated")) or value.get("next_offset") is not None
                        if "rows" in value:
                            partial |= value.get("offset", 0) > 0 or len(value["rows"]) < value.get("row_count", len(value["rows"]))
                        exposures[identifier] = {**span, "coverage": "partial" if partial else "full"}
            for key in ("project", "output", "object"):
                observe(value.get(key))
            for entry in value.get("items", []):
                observe(entry)
            for key in ("artifacts", "sources", "active", "active_skill_runs"):
                container = value.get(key, {})
                for entry in container.values() if isinstance(container, dict) else container:
                    observe(entry)

        if message.get("type") == "function_call_output":
            try:
                observe(json.loads(message["output"]))
            except (ValueError, TypeError):
                pass
        elif message.get("role") == "user" and isinstance(message.get("content"), str):
            for prefix in ("ACTIVE PROJECT. ", "PROJECT STATE SNAPSHOT. "):
                if message["content"].startswith(prefix):
                    observe(json.loads(message["content"][len(prefix):]))
        offset = end
    return {"local_public_input_tokens": offset, "local_tokenizer": "o200k_base" if encoding else None,
            "token_basis": "sum of serialized public message and tool-schema components; excludes encrypted reasoning payload and opaque reasoning IDs; not provider tokens",
            "opaque_reasoning_items_not_tokenized": opaque_count, "message_spans": spans,
            "known_public_ids": sorted(known), "latest_content_exposures": exposures,
            "business_state": {"remaining_case_deliverables": state["total_cases"] - state["completed_cases"],
                "final_acceptance_current": state["success"],
                "open_skill_runs": public_project.get("collections", {}).get("active",
                    len(public_project.get("active", public_project.get("active_skill_runs", {})))),
                "notice_count": public_project.get("collections", {}).get("notices", len(public_project.get("notices", []))),
                "project_revision": public_project.get("revision"),
                "source_revisions": ({key: value["revision"] for key, value in public_project["sources"].items()}
                                     if "sources" in public_project else None if "collections" in public_project else {})},
            "business_state_note": "Current unmet case deliverables, not remaining minimal skills; may increase after changes. This local measurement is not sent to the provider."}


def binding_positions(observation, calls):
    known = set(observation["known_public_ids"])
    bindings = []

    def visit(value, path, call):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, f"{path}.{key}" if path else key, call)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]", call)
        elif isinstance(value, str) and value in known:
            exposure = observation["latest_content_exposures"].get(value)
            length = observation["local_public_input_tokens"]
            end = exposure["local_token_end"] if exposure else None
            bindings.append({"tool": call["name"], "call_id": call["call_id"], "argument": path, "public_id": value,
                "latest_content_message": exposure, "estimated_gap_public_tokens": length - end if end is not None else None,
                "estimated_relative_message_end": end / length if end is not None and length else None})

    for call in calls:
        visit(call["arguments"], "", call)
    return {"bindings": bindings, "position_unit": "local_public_message_span_not_value_span_or_attention",
            "note": "Only previously observed public IDs are linked. Normal rereads refresh exposure; scripts may bind inputs internally. No position-caused failure label is inferred."}
