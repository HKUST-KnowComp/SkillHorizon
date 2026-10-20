"""Common offline measurements; workflow claims are never business certificates."""

from collections import Counter


REVISION = "ten_environment_measurements_v2"
MANAGEMENT_TOOLS = {"begin_skill", "select_skill_run", "finish_skill", "abandon_skill", "read_skill_run", "revise_skill"}
DISCOVERY_TOOLS = {"read_task", "read_skill_file", "read_public_document", "list_skills", "search_skills"}


def operation_succeeded(output, protocol):
    if protocol == "observational_v1":
        return output.get("status") == "success"
    if protocol in {"procedural", "macro"}:
        return output.get("ok") is True
    raise ValueError("Unknown skill measurement protocol")


def measure_episode(task, initial, records, effects, attempts, stop_reason):
    """Consume replay-verified logs only; prefix state seeds attribution, not credit."""
    if not effects["valid"]:
        raise ValueError("Common measurements require valid execution evidence")
    if len(initial) != task["checkpoint_action_count"]:
        raise ValueError("Checkpoint action boundary differs from the task")
    protocol = task.get("skill_protocol", task["config"].get("execution_mode", "procedural"))
    if protocol not in {"observational_v1", "procedural", "macro"}:
        raise ValueError("Unknown skill measurement protocol")
    skills = set(task.get("skill_ids", []))
    runs, active, events = {}, None, []

    def process(name, args, output, *, prefix, index, turn):
        nonlocal active
        ok = operation_succeeded(output, protocol)
        data = output.get("data", {}) if protocol == "observational_v1" else output
        run_id = args.get("run_id")
        before = runs.get(run_id or (active if protocol == "observational_v1" else None))
        skill = args.get("skill_id") if name in {"begin_skill", "read_skill_file", "run_skill_script"} else None
        skill = skill or (before["skill_id"] if before else name if name in skills else None)
        if ok and name == "begin_skill":
            run_id = data["run_id"]
            if run_id in runs:
                raise ValueError("Duplicate skill run in replayed history")
            runs[run_id] = {"run_id": run_id, "skill_id": args["skill_id"],
                "origin": "reference_prefix" if prefix else "actor", "status": "open",
                "started_tool_index": None if prefix else index, "closed_tool_index": None}
            active = run_id
        elif ok and name == "select_skill_run":
            if run_id not in runs or runs[run_id]["status"] != "open":
                raise ValueError("Selected run absent from replayed history")
            active = run_id
        elif ok and name in {"finish_skill", "abandon_skill"}:
            if run_id not in runs or runs[run_id]["status"] != "open":
                raise ValueError("Closed run absent from replayed history")
            runs[run_id].update(status="abandoned" if name == "abandon_skill" else
                "claimed_complete" if protocol == "observational_v1" else "execution_closed",
                closed_tool_index=None if prefix else index)
            if active == run_id:
                active = None
        if not prefix:
            events.append({"tool_index": index, "turn": turn, "tool": name,
                "skill_id": skill, "run_id": run_id or (before["run_id"] if before else None),
                "operation_succeeded": ok, "selection_attempt": name == "begin_skill" or name in skills,
                "script_attempt": name == "run_skill_script",
                "error_code": None if ok else output.get("message") if protocol == "observational_v1"
                    else output.get("error", {}).get("code"),
                "skill_selection_correct": None, "business_effect_correct": None})

    for row in initial:
        process(row["skill_id"], row["arguments"], row["result"], prefix=True, index=None, turn=None)
    prefix_open = sum(r["status"] == "open" for r in runs.values())
    for index, row in enumerate(records, 1):
        process(row["name"], row["arguments"], row["output"], prefix=False, index=row.get("tool_index", index), turn=row["turn"])
        events[-1]["business_index"] = index
        if "audience" in row:
            events[-1].update(audience=row["audience"], parent_id=row.get("parent_id"), child_seq=row.get("child_seq"))
    actor_runs = [r for r in runs.values() if r["origin"] == "actor"]
    selections = [r for r in events if r["selection_attempt"]]
    rejections = [r for r in attempts if not r["accepted"]]
    return {"revision": REVISION, "visibility": "offline_only_never_actor_feedback",
        "skill_protocol": protocol,
        "skill_observability": skill_observability(records, events, protocol, runs),
        "observation_scope": {"checkpoint_actions": len(initial), "actor_tool_calls": len(records),
            "prefix_open_runs_at_entry": prefix_open,
            "prefix_runs_closed_by_actor": sum(r["origin"] == "reference_prefix" and r["closed_tool_index"] is not None for r in runs.values()),
            "full_online": not initial and not any(m.get("type") in {"function_call", "function_call_output"} for m in task["messages"]),
            "business_audiences": dict(Counter(r.get("audience", "model") for r in records)),
            "audience_note": "Actor-origin business actions may execute inside a program; program observations are not model-visible documentation or results."},
        "selection": {"attempts": len(selections), "interface_accepted": sum(r["operation_succeeded"] for r in selections),
            "independently_scored_choices": 0, "correct_choices": None, "accuracy": None,
            "note": "Interface acceptance and declared skill identity do not establish choice utility or optimality."},
        "execution": {"tool_calls": len(events), "successful_tool_calls": sum(r["operation_succeeded"] for r in events),
            "tool_errors": sum(not r["operation_succeeded"] for r in events),
            "script_calls": sum(r["script_attempt"] for r in events),
            "successful_script_calls": sum(r["script_attempt"] and r["operation_succeeded"] for r in events)},
        "lifecycle": {"actor_started_runs": len(actor_runs),
            "actor_claimed_complete_runs": sum(r["status"] == "claimed_complete" for r in actor_runs),
            "actor_execution_closed_runs": sum(r["status"] == "execution_closed" for r in actor_runs),
            "actor_abandoned_runs": sum(r["status"] == "abandoned" for r in actor_runs),
            "open_runs_at_stop": sum(r["status"] == "open" for r in runs.values()),
            "verified_skill_success_rate": None},
        "business": {"effects_valid": True, "success_at_stop": effects["task_success"],
            "natural_final": stop_reason == "sdk_final_output",
            "premature_final": stop_reason == "sdk_final_output" and not effects["task_success"],
            "stop_reason": stop_reason, "per_effect_correctness": "see_domain_diagnostics_not_inferred_from_tool_acceptance"},
        "infrastructure": {"trajectory_requests": len(attempts), "accepted_actor_responses": sum(r["accepted"] and r["role"] == "actor" for r in attempts),
            "accepted_compactions": sum(r["accepted"] and r["role"] == "compaction" for r in attempts),
            "rejected_requests": len(rejections), "rejected_statuses": dict(Counter(r["status"] for r in rejections)),
            "known_reported_tokens": sum(r["reported_tokens"] or 0 for r in attempts),
            "rejected_known_reported_tokens": sum(r["reported_tokens"] or 0 for r in rejections),
            "unknown_usage_requests": sum(r["reported_tokens"] is None for r in attempts),
            "scope": "Trajectory only, admission separate; resumed cumulative logs overlap their parent."},
        "runs": list(runs.values()), "calls": events}


def skill_observability(records, events, protocol, runs):
    """Orthogonal observations, not inferred mental skill use or routing accuracy."""
    def successful(name):
        return [r for r in records if r["name"] == name and operation_succeeded(r["output"], protocol)]

    reads = successful("read_skill_file")
    documents = [r for r in reads if r["arguments"].get("path", "SKILL.md") == "SKILL.md"]
    scripts = [e for e in events if e["script_attempt"] and e["operation_succeeded"]]
    domain = [e for e in events if e["tool"] not in MANAGEMENT_TOOLS | DISCOVERY_TOOLS | {"run_skill_script"}]
    attributed = sum(e["run_id"] in runs for e in domain)
    return {"scope": "actor_suffix_only",
        "documentation": {"successful_reads": len(documents),
            "model_observed_reads": sum(r.get("audience", "model") == "model" for r in documents),
            "program_observed_reads": sum(r.get("audience") == "program" for r in documents),
            "unique_skill_ids": sorted({r["arguments"]["skill_id"] for r in documents}),
            "other_skill_file_reads": len(reads) - len(documents), "instruction_following_established": None},
        "scripts": {"successful_calls": len(scripts),
            "unique_skill_ids": sorted({r["skill_id"] for r in scripts if r["skill_id"] is not None}),
            "unattributed_successful_calls": sum(r["skill_id"] is None for r in scripts),
            "business_correctness_established": None},
        "explicit_runs": {"successful_starts": len(successful("begin_skill")),
            "recorded_domain_calls": attributed, "unrecorded_domain_calls": len(domain) - attributed,
            "domain_call_recording_fraction": attributed / len(domain) if domain else None,
            "semantic_attribution_established": None},
        "note": "Reading documentation, executing a script and recording a run are distinct observations. No run does not imply no skill use. Run association is declared context, not proof of correct selection, cooperation or business effects. No mandatory bookkeeping or evaluator feedback was added."}
