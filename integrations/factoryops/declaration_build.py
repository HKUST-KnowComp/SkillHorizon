"""Fresh declaration wrapper; preserve the original coupled production world."""

from copy import deepcopy
from pathlib import Path

from integrations.internal_mcp.io import read_json, write_json
from skillhorizon.construction_provenance import construction_entry, observe_call
from .coupled_build import FORMAT, build as build_coupled
from .declaration_runtime import BOOKKEEPING, PROTOCOL


LOCAL_GOALS = {
    "inspect-production-order": "Understand the specified production order. Understanding is unobserved; inspecting an order alone does not establish this cognitive goal.",
    "prepare-program-revision": "Maintain the specified actual program revision for its intended work order, cell, tool and mandatory profile, with correct ordered waypoints and public speed, coordinate and payload constraints. Preserve released history. A structurally prepared program is not proof of later product quality or calibration.",
    "calibrate-and-validate-program": "Obtain a current published release for the specified program revision, bound to an actual passing snapshot validation and real same-revision tool calibration measurement with correct compensation. Flags alone do not establish this goal; tool drift prevents future reuse of an old release.",
    "schedule-material-bound-run": "Maintain the specified valid queued plan with its current release, exact real BOM inputs, coupled setup time and feasible shared equipment and maintenance interval. For an already executed run, use its actual historical scheduling and execution snapshot, not later equipment state. A canceled plan is not usable scheduling evidence.",
    "execute-and-observe-batch": "Execute the specified real run through its processing end and obtain every actual output-unit and mandatory-metric observation with matching run, slot, parent material, release and profile. Measured defects do not negate execution; disposition belongs to a later goal. A clock advance or invented measurement is insufficient.",
    "disposition-and-recover-lot": "Correctly partition the specified run's actual unit-level QC. Where units are quarantined, complete necessary bounded rework using those original units and retain their replacement-slot lineage, failed history and accepted siblings. Partition alone does not complete required recovery. Later legitimate consumption of accepted output does not undo recovery.",
    "reconcile-production-handoff": "Freeze the actual handoff with exact terminal output slots and quantities, their true producing dispositions, no remaining active attempts, handled declared updates and full material conservation. A workflow marker or matching total count does not establish completion.",
}
SCRIPTS = {"schedule-material-bound-run": "scripts/find_interval.py",
           "disposition-and-recover-lot": "scripts/aggregate_quality.py"}
GUIDANCE = """

## Controller-owned declarations

This task uses skill_declaration_v1. begin_skill(run_id, skill_id, scope) and
finish_skill(run_id, status, outputs, note), when supplied by the controller,
declare work without authorizing business operations. status is completed,
blocked or abandoned. completed asserts the local goal, not merely a stopped
attempt. Business tools and fixed scripts do not require a declaration.
Call attribution uses an optional client skill_run_id envelope. There is no
business active-run slot or old begin/select/finish/read_skill_run tool.

scope and outputs name actual public {kind,id} objects: order, program,
validation, calibration, release, run, lot, unit, quality_check, disposition
or dispatch. A dispatch uses the public task_id as its identifier. Every
explicit output must be correct and relevant; a correct output cannot hide
a wrong one. Missing or ambiguous targets are unobserved, not an implicit
global claim. Existing applicable outputs may be reused. A skill run_id is
not a manufacturing run_id; use the correct kind and real business identity.
The first completed cut is fixed. Later repairs cannot backdate completion;
later tool drift does not invalidate an earlier completed physical attempt.
Process adherence and understanding are not inferred from business success.
"""


@construction_entry
def build(source, output, *, seed=7, levels=3, terminal_quantity=1, background=True,
          scenario="equipment_windows", dependency_pattern="distributed", runtime_identity="task_local_v1"):
    if runtime_identity != "task_local_v1":
        raise ValueError("Declaration production requires task_local_v1")
    public = observe_call(build_coupled, source, output, seed=seed, levels=levels,
                          terminal_quantity=terminal_quantity, background=background, scenario=scenario,
                          dependency_pattern=dependency_pattern, runtime_identity=runtime_identity)
    output = Path(output)
    worlds = {name: read_json(output / name / "world.json") for name in ("checkpoint", "runtime")}
    private = read_json(output / "private/initial_obligations.json")
    if (public != read_json(output / "public/task.json") or private["public"] != public
            or public["format"] != FORMAT or public["policy"]["skill_protocol"] != "observational_v1"
            or worlds["checkpoint"] != worlds["runtime"]
            or private["initial_state"] != worlds["checkpoint"]["production"]
            or private["initial_tables"] != worlds["checkpoint"]["tables"]):
        raise ValueError("Expected the unchanged fresh Coupled V2 construction")
    for world in worlds.values():
        state = world["production"]
        if (world["format"] != FORMAT or world["task_id"] != public["task_id"]
                or type(world.get("sequence")) is not int or world["sequence"] != 0
                or state.get("skill_runs") != {} or "active_skill_run" not in state
                or state["active_skill_run"] is not None):
            raise ValueError("Only fresh unexecuted production worlds can be migrated")
    if len(public["skills"]) != len(LOCAL_GOALS) or {s["name"] for s in public["skills"]} != set(LOCAL_GOALS):
        raise ValueError("The complete seven-skill library is required")
    texts = {}
    for skill in public["skills"]:
        name = skill["name"]
        expected = ["SKILL.md"] + ([SCRIPTS[name]] if name in SCRIPTS else [])
        folder = output / "public/skills" / name
        if sorted(skill["files"]) != sorted(expected) or any(not (folder / f).is_file() for f in expected):
            raise ValueError("The original skill files and two scripts are required")
        path = folder / "SKILL.md"
        texts[path] = path.read_text(encoding="utf-8") + GUIDANCE + "\n### Local completion goal\n\n" + LOCAL_GOALS[name] + "\n"
    public["policy"]["skill_protocol"] = PROTOCOL
    for name, world in worlds.items():
        for key in BOOKKEEPING:
            del world["production"][key]
        write_json(output / name / "world.json", world)
    private["public"], private["initial_state"] = deepcopy(public), deepcopy(worlds["checkpoint"]["production"])
    for path, text in texts.items():
        path.write_text(text, encoding="utf-8")
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", private)
    return public
