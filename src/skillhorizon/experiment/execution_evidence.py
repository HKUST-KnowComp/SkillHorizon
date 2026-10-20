"""Controller-only dispatch evidence; never synthesize missing actor observations."""

from ..util import load_json, load_jsonl


def read_business_evidence(bundle, run):
    """Read parent/child facts without a world, file consumer, or dispatcher.

    A pending file commit does not erase confirmed child observations. This
    projection never authorizes replay, and never turns program output into a
    model observation. Partial evidence is retained but cannot renew a budget.
    """
    from pathlib import Path
    from ..util import canonical_json
    from .business_binding import business_binding
    from .execution_replay import read_execution_records
    from .ptc_config import ptc_storage_bounds
    from .ptc_budget import validate_child_events, validate_grant

    bundle, run = Path(bundle), Path(run)
    keys = ("nested_requests_admitted", "nested_dispatch_intents", "nested_business_dispatched",
            "nested_dispatch_unknown", "nested_rejections", "bridge_exhaustion_events", "bridge_bytes")
    counts = {key: 0 for key in keys}
    try:
        events = dispatch_semantics(load_jsonl(run / "dispatch_events.jsonl"))
        dispatched = [row for row in events if row["event"] == "dispatch"]
        names = {tool["name"] for tool in load_json(bundle / "private_task.json")["tools"]}
        direct = [{**row, "audience": "model", "parent_id": None, "child_seq": None}
                  for row in dispatched if row["name"] in names]
        ptc = business_binding(bundle)["ptc"]
    except (ValueError, KeyError, TypeError, OSError, AttributeError, RecursionError):
        return {"accounting_known": False, "top_level_tool_calls": None,
                **{key: None for key in keys}, "charged_tool_calls": None,
                "business_intents": [], "parents": [],
                "errors": ["native_evidence_invalid_or_unreadable"],
                "scope": "Unreadable evidence; not model exposure or replay authority"}
    result = {"accounting_known": True, "top_level_tool_calls": len(dispatched), **counts,
              "charged_tool_calls": len(dispatched), "business_intents": direct,
              "parents": [], "errors": [], "scope": "Recorded business intentions and program observations; not model exposure or replay authority"}
    if ptc is None:
        return result
    try:
        manifest = load_json(bundle / "manifest.json")
        from .managed_session import read_private, CONTROL as BUSINESS_CONTROL
        control = read_private(bundle / BUSINESS_CONTROL)
        if any(type(row["tool_index"]) is not int or row["tool_index"] != index
               for index, row in enumerate(dispatched, 1)):
            raise ValueError("Native dispatch indices differ")
        bounds = ptc_storage_bounds(ptc, manifest["workspace"], manifest["python_execution"], manifest["execution_max_actions"])
        journal = bundle / "workspace_actions.jsonl"
        rows = read_execution_records(journal, max_bytes=bounds["journal_bytes"], max_record_bytes=bounds["record_bytes"],
                                      max_actions=manifest["execution_max_actions"]) if journal.exists() else []
        receipt_path = journal.with_name(journal.name + ".execution_receipt")
        receipt = None
        if receipt_path.exists():
            receipts = read_execution_records(receipt_path, max_bytes=bounds["receipt_bytes"],
                                              max_record_bytes=bounds["receipt_bytes"], max_actions=1)
            if len(receipts) != 1:
                raise ValueError("Missing current execution receipt")
            receipt = receipts[0]
        parents = {}
        for index, row in enumerate(rows, 1):
            if row["name"] == "ptc_execute":
                data = row["execution"]["ptc"]
                if data["parent"]["workspace_index"] != index:
                    raise ValueError("PTC file position differs")
                parent_id = data["parent"]["grant"]["parent_id"]
                if parent_id in parents:
                    raise ValueError("Duplicate PTC parent")
                parents[parent_id] = (row, True)
        if receipt is not None and (receipt.get("action") or {}).get("name") == "ptc_execute":
            row = receipt["action"]
            data = row["execution"]["ptc"]
            parent_id = data["parent"]["grant"]["parent_id"]
            index = data["parent"]["workspace_index"]
            if receipt["usage"]["actions"] != index:
                raise ValueError("PTC receipt position differs")
            if parent_id in parents:
                existing = parents[parent_id][0]
                original = existing["execution"]["ptc"]
                shorter, longer = sorted((original["events"], data["events"]), key=len)
                if (original["parent"] != data["parent"] or canonical_json(shorter) != canonical_json(longer[:len(shorter)])
                        or canonical_json(existing["arguments"]) != canonical_json(row["arguments"])
                        or len(data["events"]) > len(original["events"])):
                    raise ValueError("PTC receipt conflicts with committed journal")
            elif index == len(rows) + 1:
                parents[parent_id] = (row, False)
            else:
                raise ValueError("PTC receipt is not the current uncommitted parent")
            if receipt["usage"].get("ptc_journal_unknown") is True:
                result["errors"].append("ptc_receipt_persistence_unknown")
        by_id = {row["call_id"]: row for row in dispatched}
        expected = {row["call_id"] for row in dispatched if row["name"] == "ptc_execute"}
        if set(parents) != expected:
            result["errors"].append("ptc_parent_evidence_incomplete")
        if expected and receipt is None:
            result["errors"].append("ptc_current_receipt_missing")
        prior = 0
        for parent_id, (row, committed) in sorted(parents.items(), key=lambda item: item[1][0]["execution"]["ptc"]["parent"]["grant"]["top_level_index"]):
            data = row["execution"]["ptc"]
            parent = data["parent"]
            grant = validate_grant(parent["grant"], ptc)
            native = by_id[parent_id]
            if (set(parent) != {"grant", "turn", "actor_turn", "workspace_index"}
                    or any(type(parent[key]) is not int or not 1 <= parent[key] <= 100000
                           for key in ("turn", "actor_turn", "workspace_index"))
                    or any(grant[key] != control[key] for key in ("bundle_id", "episode_id"))
                    or native["name"] != "ptc_execute" or native["tool_index"] != grant["top_level_index"]
                    or grant["prior_nested"] != prior or grant["max_tool_calls"] != manifest["execution_max_actions"]
                    or canonical_json(native["arguments"]) != canonical_json(row["arguments"])
                    or any(native[key] != parent[key] for key in ("turn", "actor_turn"))):
                raise ValueError("PTC journal differs from the native parent")
            account = validate_child_events(grant, ptc, data["events"], require_terminal=committed)
            for key in keys:
                counts[key] += account[key]
            prior = counts["nested_requests_admitted"]
            result["parents"].append({"parent_id": parent_id, "committed": committed, "accounting": account})
            request = None
            pending = None
            for event in data["events"]:
                if event["event"] == "admit":
                    request = event["request"]
                elif event["event"] == "dispatch_intent":
                    pending = {**native, "name": request["name"], "arguments": request["arguments"],
                               "tool_kind": "business", "parent_tool_kind": native.get("tool_kind"),
                               "parent_id": parent_id, "child_seq": event["seq"], "audience": "program",
                               "observation_state": "unknown", "output": None}
                    result["business_intents"].append(pending)
                elif event["event"] == "result":
                    pending.update(observation_state="confirmed", output=event["result"])
        result.update(counts)
        result["business_intents"].sort(key=lambda row: (row["tool_index"], row["child_seq"] or 0))
        result["accounting_known"] = not result["errors"]
    except (ValueError, KeyError, TypeError, OSError, AttributeError, RecursionError):
        result.update(counts)
        result["accounting_known"] = False
        result["errors"].append("ptc_evidence_invalid_or_unreadable")
    result["charged_tool_calls"] = len(dispatched) + counts["nested_requests_admitted"] if result["accounting_known"] else None
    if result["charged_tool_calls"] is not None and result["charged_tool_calls"] > manifest["execution_max_actions"]:
        result["accounting_known"] = False
        result["charged_tool_calls"] = None
        result["errors"].append("charged_tool_budget_exceeded")
    return result


def business_observations(bundle, run, direct):
    """Separate completed business observations from their model/program audience."""
    from .business_binding import business_binding
    if business_binding(bundle)["ptc"] is None:
        return direct
    evidence = read_business_evidence(bundle, run)
    if not evidence["accounting_known"]:
        raise ValueError("Cannot diagnose unknown PTC business evidence")
    by_id = {row["call_id"]: row for row in direct}
    rows = []
    for index, intention in enumerate(evidence["business_intents"], 1):
        if intention["audience"] == "program":
            if intention["observation_state"] != "confirmed":
                raise ValueError("Program business observation is unknown")
            row = dict(intention)
        else:
            row = by_id.get(intention["call_id"])
            if row is None or any(row[key] != intention[key] for key in ("name", "arguments", "turn", "tool_index")):
                raise ValueError("Direct business observation differs from its dispatch")
            row = {**row, "audience": "model", "parent_id": None, "child_seq": None}
        rows.append({**row, "business_index": index})
    return rows


def dispatch_semantics(events):
    """Validate local timing/order, then omit only non-deterministic timestamps."""
    active, seen, projected = None, set(), []
    previous = -1
    for event in events:
        stamp = event["monotonic_ns"]
        if type(stamp) is not int or stamp < previous:
            raise ValueError("Dispatch clock went backwards")
        previous = stamp
        identity = event["call_id"]
        if event["event"] == "dispatch":
            if active is not None or identity in seen:
                raise ValueError("Overlapping or duplicate serial dispatch")
            active = event
            seen.add(identity)
        elif event["event"] in {"return", "error"}:
            if active is None or any(event[key] != active[key] for key in
                    ("call_id", "name", "arguments", "tool_index", "actor_turn", "turn")):
                raise ValueError("Tool return without matching dispatch")
            active = None
        else:
            raise ValueError("Unknown dispatch event")
        projected.append({k: v for k, v in event.items() if k != "monotonic_ns"})
    return projected


def reconcile_execution(bundle, run, report):
    from .managed_session import require_world_quiescent
    require_world_quiescent(bundle)
    from .workspace import workspace_tools
    # Business identity is controller-owned and independent of file capability health.
    names = {tool["name"] for tool in load_json(bundle / "private_task.json")["tools"]}
    kinds = {**{tool["name"]: "workspace" for tool in workspace_tools()},
             "python_execute": "execution", "ptc_execute": "execution",
             **{name: "business" for name in names}}
    events = dispatch_semantics(load_jsonl(run / "dispatch_events.jsonl"))
    dispatched = [e for e in events if e["event"] == "dispatch"]
    returned = {e["call_id"] for e in events if e["event"] == "return"}
    observation_errors = []
    try:
        tools = load_jsonl(run / "tools.jsonl")
        for row in tools:
            if (not isinstance(row.get("call_id"), str) or not isinstance(row.get("name"), str)
                    or not isinstance(row.get("arguments"), dict) or not isinstance(row.get("output"), dict)):
                raise ValueError("Malformed recorded observation")
    except (ValueError, OSError):
        tools = []
        observation_errors.append("observation_log_unreadable")
    initial = load_jsonl(bundle / "checkpoint/model_skill_actions.jsonl")
    actions = load_jsonl(bundle / "runtime/model_skill_actions.jsonl")
    errors = []
    for event in dispatched:
        if event.get("tool_kind", "business") != kinds.get(event["name"]):
            observation_errors.append("untrusted_dispatch_category")
    from .business_binding import business_binding
    ptc = business_binding(bundle)["ptc"]
    program_errors = []
    if ptc is not None:
        evidence = read_business_evidence(bundle, run)
        business = evidence["business_intents"]
        if not evidence["accounting_known"]:
            errors.append("ptc_parent_child_evidence_unknown")
    else:
        business = [event for event in dispatched if kinds.get(event["name"]) == "business"]
    if actions[:len(initial)] != initial:
        errors.append("checkpoint_prefix_changed")
    actions = actions[len(initial):]
    if len(dispatched) != report["attempted_tool_calls"]:
        errors.append("dispatch_count_mismatch")
    if [d["tool_index"] for d in dispatched] != list(range(1, len(dispatched) + 1)):
        errors.append("dispatch_index_mismatch")
    if len(tools) != report["executed_tool_calls"]:
        observation_errors.append("executed_observation_count_mismatch")
    if len(actions) != len(business) or any(
            (d["name"], d["arguments"]) != (a["skill_id"], a["arguments"])
            for d, a in zip(business, actions)):
        errors.append("dispatch_effect_association_unknown")
    for index, event in enumerate(business):
        if event.get("parent_id") is not None and event.get("observation_state") == "confirmed":
            if index >= len(actions) or event["output"] != actions[index]["result"]:
                program_errors.append("program_observation_differs_from_effect")
    if len(tools) != len(returned) or {t["call_id"] for t in tools} != returned:
        observation_errors.append("returned_observation_log_mismatch")
    indices = {d["call_id"]: i for i, d in enumerate(business)}
    for tool in tools:
        if tool.get("tool_kind", "business") != kinds.get(tool["name"]):
            observation_errors.append("untrusted_observation_category")
        if kinds.get(tool["name"]) != "business":
            continue
        i = indices.get(tool["call_id"], len(actions))
        if i >= len(actions) or (tool["name"], tool["arguments"], tool["output"]) != (
                actions[i]["skill_id"], actions[i]["arguments"], actions[i]["result"]):
            observation_errors.append("returned_observation_differs_from_effect")
            break
    missing = [d["call_id"] for d in dispatched if d["call_id"] not in returned]
    complete = not missing and len(tools) == len(returned) and {t["call_id"] for t in tools} == returned
    return {"effect_association_valid": not errors, "effect_association_errors": errors,
            "observation_errors": observation_errors,
            "actor_observations_complete": complete,
            "actor_observations_consistent": not observation_errors,
            "replay_eligible": not errors and complete and not observation_errors and not program_errors,
            **({"program_observation_errors": program_errors} if ptc is not None else {}),
            "dispatched_without_observation": missing,
            "not_dispatched_call_ids": [key for key in report["pending_tool_call_ids"] if key not in {d["call_id"] for d in dispatched}],
            "scope": "Serial recorded dispatches matched to independently replayed business actions; missing returns are not actor observations."}
