"""Inspectable drafts, constraint checks and submission without resource reservation."""
from copy import deepcopy

from ..errors import DomainError


PLANNING_SKILLS = {"plan-isolation", "plan-purchase"}


def assess(runtime, name, arguments, use_script=False):
    if name == "plan-isolation":
        from .fieldops import _plan, check_reservation
        value = _plan(runtime, arguments)
        check_reservation(runtime, value)
        return value
    from .procureops import _check_purchase
    funding = {key: arguments[key] for key in ("previous_settlement_id", "retention_ids")} if "contract" in runtime.project else {}
    _check_purchase(runtime, arguments["budget_id"], arguments["lines"], **funding)
    priced = runtime.arithmetic(name, {"lines": arguments["lines"], "prices": {
        key: value["unit_price_cents"] for key, value in runtime.project["suppliers"].items()}}, use_script)
    return {**arguments, **priced}


def _draft(runtime, name, identifier):
    draft = runtime.artifact(identifier, "planning_draft")
    runtime.require(draft["data"]["skill_id"] == name, "PLANNING_BINDING", "Draft belongs to another algorithm")
    runtime.validate(name, draft["data"]["arguments"])
    return draft


def business_step(runtime, name, step_id, arguments, use_script=False):
    """One planning implementation; all prerequisites are real business records."""
    runtime.require(name in PLANNING_SKILLS and name in runtime.skills, "UNKNOWN_SKILL", "Unknown planning skill")
    if step_id in {"draft", "revise"}:
        runtime.require(not use_script, "STEP_ORDER", "The script checks a draft; it does not choose one")
        if step_id == "draft":
            selected, parents = arguments, []
        else:
            runtime.require(set(arguments) == {"draft_id", "arguments"}, "RUN_INPUTS", "Supply draft_id and complete replacement arguments")
            _draft(runtime, name, arguments["draft_id"])
            selected = arguments["arguments"]
            parents = [arguments["draft_id"]]
        runtime.validate(name, selected)
        draft = runtime.emit("planning_draft", {"skill_id": name, "arguments": selected}, parents)
        runtime.project["revision"] += 1
        return draft
    if step_id == "check":
        runtime.require(set(arguments) == {"draft_id"}, "RUN_INPUTS", "Supply the actual draft_id")
        draft = _draft(runtime, name, arguments["draft_id"])
        try:
            result = {"feasible": True, "issues": [], "evaluated_plan": assess(runtime, name, draft["data"]["arguments"], use_script)}
        except DomainError as exc:
            result = {"feasible": False, "issues": [exc.to_dict()], "evaluated_plan": None}
        checked = runtime.emit("planning_check", {"draft_id": draft["artifact_id"], **result}, [draft["artifact_id"]])
        runtime.project["revision"] += 1
        return checked
    runtime.require(step_id == "submit" and not use_script and set(arguments) == {"draft_id", "check_id"},
                    "STEP_ORDER", "Submit the actual draft and its completed check")
    draft = _draft(runtime, name, arguments["draft_id"])
    check = runtime.artifact(arguments["check_id"], "planning_check")
    checked = check["data"]
    runtime.require(checked["draft_id"] == draft["artifact_id"] and check["inputs"] == [draft["artifact_id"]],
                    "PLANNING_BINDING", "Check data and dependency must refer to the supplied draft")
    runtime.require(checked["feasible"] is True, "PLAN_INFEASIBLE", "Revise the draft and check it again before submission")
    current = assess(runtime, name, draft["data"]["arguments"])
    runtime.require(current == checked["evaluated_plan"], "STALE_ARTIFACT", "Public conditions changed; check the draft again")
    output = runtime.execute(name, draft["data"]["arguments"])
    artifact = runtime.artifact(output["artifact_id"])
    artifact["inputs"] += [draft["artifact_id"], check["artifact_id"]]
    output["inputs"] = deepcopy(artifact["inputs"])
    return output


def step(runtime, run, step_id, arguments, use_script=False):
    """Legacy run selection/order contract around the shared business steps."""
    from ..experiment.arguments import business_arguments_equal
    state = run.setdefault("planning", {"draft_id": None, "check_id": None})
    runtime.require(run["output"] is None, "STEP_COMPLETE", "This plan was submitted")
    if step_id in {"draft", "revise"}:
        runtime.require(not use_script, "STEP_ORDER", "The script checks a draft; it does not choose one")
        if step_id == "draft":
            runtime.require(state["draft_id"] is None and business_arguments_equal(arguments, run["arguments"]),
                            "RUN_INPUTS", "Draft the selected inputs first")
        else:
            runtime.require(set(arguments) == {"draft_id", "arguments"} and
                            arguments["draft_id"] == state["draft_id"] and state["draft_id"] is not None,
                            "RUN_INPUTS", "Revise this run's current draft with complete replacement arguments")
    elif step_id == "check":
        runtime.require(set(arguments) == {"draft_id"} and arguments["draft_id"] == state["draft_id"] and
                        state["draft_id"] is not None, "RUN_INPUTS", "Check this run's current draft")
    else:
        runtime.require(step_id == "submit" and set(arguments) == {"draft_id", "check_id"} and
                        arguments == state and all(state.values()), "STEP_ORDER", "Submit the current draft and its completed check")
    output = business_step(runtime, run["skill_id"], step_id, arguments, use_script)
    if step_id in {"draft", "revise"}:
        run["arguments"] = deepcopy(output["data"]["arguments"])
        state.update(draft_id=output["artifact_id"], check_id=None)
    elif step_id == "check":
        state["check_id"] = output["artifact_id"]
    else:
        run["output"] = deepcopy(output)
    return output
