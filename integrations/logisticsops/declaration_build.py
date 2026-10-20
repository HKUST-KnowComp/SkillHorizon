"""Fresh Freight V2 declaration tasks; existing business construction is reused."""

from copy import deepcopy
from pathlib import Path

from integrations.internal_mcp.io import read_json, write_json
from skillhorizon.construction_provenance import construction_entry, observe_call
from .declaration_runtime import BOOKKEEPING, PROTOCOL
from .freight_build import FORMAT, build as build_freight


LOCAL_GOALS = {
    "assess-disruption": "Understand the specified disruption and affected obligations. Reading public facts does not establish understanding; this cognitive goal is unobserved.",
    "plan-reaccommodation": "Choose a feasible connected itinerary. The optional script checks supplied arithmetic and constraints but neither reserves resources nor creates a durable approved plan. Plan understanding and selection are unobserved.",
    "rebook-itinerary": "Maintain the specified original booking on a current eligible same-route flight with an actual correctly owned seat. Preserve its passenger and booking identity; a new booking is not a substitute. An already-valid booking need not be changed.",
    "reconcile-ground-services": "Reconcile the specified booking's original baggage and required services to its current passenger, flight and revision, retaining original identifiers and transfer history. New replacement records do not establish preservation.",
    "settle-travel-account": "Settle the specified booking's actual current fare and accumulated fees, or its recorded cancellation refund entitlement, using the real owned payment ledger and admitted methods. Respect the shared spend ceiling; a proposed charge or one receipt alone is insufficient.",
    "complete-passenger-handoff": "Obtain a current clearance for the specified booking with its actual correct preceding-leg clearance and ground trip, current booking/service snapshot and feasible connection. This is a predeparture record, not evidence of physical flight or global completion.",
    "release-through-baggage": "Maintain a current actual release binding the specified obligation's original bag to its own current origin clearance. A release does not establish delivery through all subsequent hubs.",
    "schedule-cross-cohort-transfer": "Maintain the specified current scheduled trip with exact passenger-group membership, inbound clearances, due cargo, actual incoming freight custody and valid shared vehicle and timing constraints.",
    "forward-through-baggage": "Maintain the specified current freight manifest with actual incoming custody, correct original bags and physical weight, feasible timetable, available eligible service and shared capacity. Canceled or withdrawn-service plans are not current custody.",
}

GUIDANCE = """

## Skill declarations and local completion

This task uses skill_declaration_v1. When a declaration controller is present,
begin_skill(run_id, skill_id, scope) and finish_skill(run_id, status, outputs, note)
are controller operations, not business tools. Status is completed, blocked or
abandoned. completed claims that the local goal has been achieved, not merely
that execution stopped; it does not authorize business work or certify the task.
All business actions remain available without declarations. Associate individual
calls with the client's skill_run_id envelope, never an active business slot.
The old business begin/select/finish/read_skill_run tools are unavailable.

Scope and outputs refer to actual public {kind,id} objects: booking, clearance,
baggage_release, airport_transfer, baggage_forwarding, payment, baggage or
special_service. They locate evidence, not locked inputs or permission. Every
explicit output must be correct and relevant; a correct output cannot hide a
wrong one. Existing applicable records may be reused. The first completed cut
is fixed; later repair cannot retroactively establish that earlier completion.
Operational goals concern current applicability at that cut. Understanding and
process adherence are not established merely by tool calls or business success.
"""


@construction_entry
def build(source, output, *, seed=9, cohorts=3, legs=4, background=True,
          custody_span=3, scenario="standard", dependency_pattern="staggered",
          runtime_identity="task_local_v1"):
    if runtime_identity != "task_local_v1":
        raise ValueError("Declaration Freight requires task_local_v1")
    public = observe_call(build_freight, source, output, seed=seed, cohorts=cohorts,
                          legs=legs, background=background, custody_span=custody_span,
                          scenario=scenario, dependency_pattern=dependency_pattern,
                          runtime_identity=runtime_identity)
    output = Path(output)
    worlds = {name: read_json(output / name / "world.json") for name in ("checkpoint", "runtime")}
    private = read_json(output / "private/initial_obligations.json")
    if (public != read_json(output / "public/task.json") or private["public"] != public
            or public.get("format") != FORMAT
            or public.get("runtime_identity") != "task_local_v1"
            or public.get("policy", {}).get("skill_protocol", "observational_v1") != "observational_v1"):
        raise ValueError("Expected unchanged legacy Freight construction")
    if worlds["checkpoint"] != worlds["runtime"]:
        raise ValueError("Fresh checkpoint and runtime differ")
    for world in worlds.values():
        state = world.get("connection_state", {})
        if (world.get("format") != FORMAT or world.get("task_id") != public["task_id"]
                or type(world.get("sequence")) is not int or world["sequence"] != 0
                or state.get("skill_runs") != {} or "active_skill_run" not in state
                or state["active_skill_run"] is not None
                or type(state.get("next_skill_run")) is not int or state["next_skill_run"] != 1):
            raise ValueError("Only fresh unexecuted Freight worlds can be migrated")
    skills = public["skills"]
    if len(skills) != len(LOCAL_GOALS) or {s["name"] for s in skills} != set(LOCAL_GOALS):
        raise ValueError("Declaration Freight requires all nine public skills")
    texts = {}
    for skill in skills:
        expected = ["SKILL.md"] + (["scripts/check_connections.py"]
                                    if skill["name"] == "plan-reaccommodation" else [])
        if sorted(skill["files"]) != sorted(expected):
            raise ValueError("Unexpected Freight skill library/script contract")
        folder = output / "public/skills" / skill["name"]
        if any(not (folder / name).is_file() for name in expected):
            raise ValueError("Missing Freight public skill file")
        path = folder / "SKILL.md"
        content = path.read_text(encoding="utf-8")
        content = content.replace("Finishing a skill only records the\nagent's declared work; it does not certify the whole travel task.",
                                  "Completing a declared skill asserts its local goal; it does not certify the whole travel task.")
        texts[path] = content + GUIDANCE + "\n### Local goal\n\n" + LOCAL_GOALS[skill["name"]] + "\n"
    public["policy"]["skill_protocol"] = PROTOCOL
    for name, world in worlds.items():
        for key in BOOKKEEPING:
            del world["connection_state"][key]
        write_json(output / name / "world.json", world)
    for path, content in texts.items():
        path.write_text(content, encoding="utf-8")
    private["public"] = deepcopy(public)
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", private)
    return public
