"""Observed business-skill lifecycle, distinct from generic MCP tool counts."""


def skill_execution_measurements(task, actions, records):
    skills = set(task["skill_ids"])
    active, runs, per_skill, attributed = {}, [], {}, []
    measurements, checks, history = {}, {}, {}
    unattributed_failures = 0

    def stats(name):
        return per_skill.setdefault(name, {key: 0 for key in (
            "selection_attempts", "started_runs", "completed_runs", "abandoned_runs",
            "manual_step_calls", "script_calls", "instruction_reads", "failed_calls")})

    def initial_action(action):
        args, output = action["arguments"], action["result"]
        if not output.get("ok"):
            return
        if action["skill_id"] == "begin_skill":
            history[output["run_id"]] = args["skill_id"]
            active[output["run_id"]] = {"run_id": output["run_id"], "skill_id": args["skill_id"],
                "origin": "reference_prefix", "start_turn": None, "end_turn": None, "status": "open"}
        elif action["skill_id"] in {"finish_skill", "abandon_skill"}:
            active.pop(args["run_id"], None)

    for action in actions[:task["checkpoint_action_count"]]:
        initial_action(action)
    runs.extend(active.values())
    unknown_selections = 0
    for record in records:
        name, output = record["name"], record["output"]
        args = record["arguments"] if isinstance(record["arguments"], dict) else {}
        successful = record["executed"] and output.get("ok") is True
        computation = name in skills or name in {"execute_step", "run_skill_script"}
        if successful and computation and output.get("kind") == "test" and isinstance(output.get("data", {}).get("passed"), bool):
            measurements[output["artifact_id"]] = output["data"]["passed"]
        if successful and computation and output.get("kind") == "planning_check":
            checks[output["artifact_id"]] = output["data"]["feasible"]
        run_id = args.get("run_id")
        lifecycle = name in {"execute_step", "run_skill_script", "finish_skill", "abandon_skill", "revise_skill"}
        current = active.get(run_id) if lifecycle and isinstance(run_id, str) else None
        historical_skill = history.get(run_id) if lifecycle and isinstance(run_id, str) else None
        skill = name if name in skills else args.get("skill_id") if name in {"begin_skill", "read_skill_file"} else current["skill_id"] if current else historical_skill
        skill = skill if isinstance(skill, str) else None
        attribution = ("active_run" if current else "closed_run" if historical_skill else "unknown_run") if lifecycle else (
            "explicit_skill" if skill in skills else "unknown_skill" if name in {"begin_skill", "read_skill_file"} else "non_skill_tool")
        phase = {"begin_skill": "selection", "execute_step": "manual_step", "run_skill_script": "script",
                 "finish_skill": "finish", "abandon_skill": "abandon", "revise_skill": "revision", "read_skill_file": "instruction_read"}.get(name, "macro" if name in skills else "observation")
        attributed.append({"turn": record["turn"], "tool": name, "skill_id": skill if skill in skills else None,
                           "requested_skill_id": args.get("skill_id"), "run_id": run_id,
                           "attribution": attribution, "phase": phase,
                           "executed": record["executed"], "operation_succeeded": successful,
                           "business_measurement_passed": output.get("data", {}).get("passed") if output.get("kind") == "test" else None,
                           "draft_feasible": output.get("data", {}).get("feasible") if output.get("kind") == "planning_check" else None,
                           "error_code": output.get("error", {}).get("code")})
        if name == "begin_skill" and skill not in skills:
            unknown_selections += 1
        if skill not in skills:
            unattributed_failures += int(not successful)
            continue
        counts = stats(skill)
        counts["failed_calls"] += int(not successful)
        if phase in {"selection", "macro"}:
            counts["selection_attempts"] += 1
        if phase == "manual_step":
            counts["manual_step_calls"] += 1
        elif phase == "script":
            counts["script_calls"] += 1
        elif phase == "instruction_read":
            counts["instruction_reads"] += 1
        if successful and phase in {"selection", "macro"}:
            counts["started_runs"] += 1
            current = {"run_id": output.get("run_id"), "skill_id": skill, "origin": "evaluated_policy",
                       "start_turn": record["turn"], "end_turn": None, "status": "open"}
            runs.append(current)
            if phase == "selection":
                active[current["run_id"]] = current
                history[current["run_id"]] = skill
        if successful and phase in {"finish", "macro", "abandon"}:
            status = "abandoned" if phase == "abandon" else "completed"
            counts[status + "_runs"] += 1
            if current:
                current.update(status=status, end_turn=record["turn"])
            if phase != "macro":
                active.pop(args["run_id"], None)
    failed = sum(not r["operation_succeeded"] for r in attributed)
    assigned_failed = sum(s["failed_calls"] for s in per_skill.values())
    if assigned_failed + unattributed_failures != failed or len(attributed) != len(records):
        raise ValueError("Skill call accounting does not conserve observed calls")
    return {"measurement_version": 2, "per_skill": per_skill, "runs": runs, "calls": attributed,
            "call_accounting": {"observed_calls": len(records), "failed_calls": failed,
                                "skill_attributed_failed_calls": assigned_failed,
                                "unattributed_failed_calls": unattributed_failures,
                                "closed_run_calls": sum(r["attribution"] == "closed_run" for r in attributed),
                                "unknown_run_calls": sum(r["attribution"] == "unknown_run" for r in attributed),
                                "conserved": True},
            "unknown_skill_selection_attempts": unknown_selections,
            "business_measurements": {"reports": len(measurements), "passed": sum(measurements.values()),
                                      "failed": sum(not v for v in measurements.values())},
            "planning_checks": {"reports": len(checks), "feasible": sum(checks.values()),
                                "infeasible": sum(not v for v in checks.values())},
            "open_runs_at_stop": len(active),
            "note": "Selection acceptance checks the public interface, not expert-next-skill correctness. A completed run is not necessarily a current valid deliverable. Script calls delegate the documented computation."}
