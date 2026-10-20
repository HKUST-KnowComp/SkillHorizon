"""Fresh Reach V2 construction with controller-owned skill declarations.

The legacy builder saves its initial state before this migration. This module
does not yet bind the stdio server or framework to the declaration runtime.
"""

from copy import deepcopy
from pathlib import Path

from skillhorizon.construction_provenance import construction_entry, observe_call
from integrations.internal_mcp.io import read_json, write_json
from .declaration_runtime import PROTOCOL
from .reach_build import FORMAT, build as build_reach


LOCAL_GOALS = {
    "inspect-campaign-brief": (
        "Understand the intended brief, dependencies, constraints and public evidence. "
        "Reading a file does not prove understanding; that cognitive goal is unobserved."
    ),
    "adapt-campaign-content": (
        "Prepare a specified post revision with the required facts, disclosure, format, "
        "eligible assets, measured learning, consenting audience and inherited CTAs. "
        "A deployment must retain the actual winner's content; preparation is not approval."
    ),
    "coordinate-content-approval": (
        "Obtain an actual environment approval for the specified current post revision "
        "from the required reviewer. A request alone, canceled review or old snapshot "
        "does not meet this goal; later edits and rights changes can invalidate approval."
    ),
    "schedule-cross-platform-release": (
        "Release a specified post through a real due publication with current approval, "
        "legal window and account spacing, and complete exposure and reach bindings. "
        "A proposed schedule or reservation alone is not a completed release. The "
        "optional allocation script does not reserve slots or publish anything."
    ),
    "analyze-campaign-experiment": (
        "Complete a specified brief's real experiment using every required variant's "
        "actual observation after its measurement window. Select clicks/impressions "
        "with zero impressions scored as zero and the existing post-ID tie rule. "
        "The comparison script is arithmetic, not evidence of a completed experiment."
    ),
    "propagate-campaign-learning": (
        "Bind and incorporate all required real parent reports into a specified target "
        "post, including the measured parent selection, audience intersection and CTAs. "
        "Parent ties retain the existing report-ID ordering. Reading a source report "
        "does not establish that a target incorporated it or that future waves finished."
    ),
    "reconcile-audience-exposure": (
        "For a specified due post, bind all actual positive contributing publications "
        "and exact per-segment decayed loads, or identify its immutable release-time "
        "exposure evidence. An older binding remains usable when its version, sources "
        "and loads still match; a changed clock alone does not require replacement."
    ),
    "reconcile-converted-audience": (
        "For a specified due post, bind every contributing observed conversion snapshot "
        "and the exact cooldown-filtered cohort union and eligible sets, or identify "
        "its immutable release-time reach evidence. Zero reach is legal. New observations "
        "and cooldown expiry can change current applicability without rewriting history."
    ),
}

DECLARATION_GUIDANCE = """

## Skill declarations

This task uses skill_declaration_v1. When a declaration controller is present,
begin_skill(run_id, skill_id, scope) and finish_skill(run_id, status, outputs, note)
are controller operations, not tools on the business runtime. Status is completed,
blocked or abandoned. The old business begin/select/finish/read_skill_run tools
are unavailable. All business actions remain usable without any declaration.
Associate calls through the client's skill_run_id envelope, never a business
argument or an active skill slot. Do not invent business IDs or substitute a
declaration for approval, publication, observation or any source binding.

completed claims that this skill's local goal has been achieved; it is not a
certificate, a business authorization, or merely a signal that work stopped.
Scope and outputs locate public evidence, not execution locks or automatic input
selection. Use actual response IDs and typed references: content_post,
approval_request, publication, engagement_snapshot, ab_test, campaign_report or
campaign_brief. Bindings without their own ID are located by content_post; script
calculations are not business artifacts. Explicit outputs must each be relevant
and correct; another correct output cannot hide a wrong one. Applicable existing
evidence may be reused without manufacturing a new artifact. First completion
cannot be moved by repeating declarations. Process adherence and understanding
are not established merely by calls or outcomes.
"""


@construction_entry
def build(source, output, *, seed=6, campaigns=3, waves=4, background=True,
          scenario="standard", dependency_pattern="staggered",
          runtime_identity="task_local_v1"):
    if runtime_identity != "task_local_v1":
        raise ValueError("Declaration Reach requires task_local_v1")
    public = observe_call(build_reach, source, output, seed=seed, campaigns=campaigns,
                          waves=waves, background=background, scenario=scenario,
                          dependency_pattern=dependency_pattern,
                          runtime_identity=runtime_identity)
    output = Path(output)
    worlds = {name: read_json(output / name / "world.json")
              for name in ("checkpoint", "runtime")}
    private = read_json(output / "private/initial_obligations.json")
    if (public != read_json(output / "public/task.json") or private["public"] != public
            or public.get("format") != FORMAT
            or public.get("runtime_identity") != "task_local_v1"
            or public.get("policy", {}).get("skill_protocol") != "observational_v1"):
        raise ValueError("Expected an unmodified legacy Reach construction")
    for world in worlds.values():
        social = world.get("social", {})
        if (world.get("format") != FORMAT or world.get("task_id") != public["task_id"]
                or type(world.get("sequence")) is not int or world["sequence"] != 0
                or social.get("skill_runs") != {} or "active_skill_run" not in social
                or social["active_skill_run"] is not None):
            raise ValueError("Only fresh unexecuted Reach worlds can be migrated")
    if worlds["checkpoint"] != worlds["runtime"]:
        raise ValueError("Fresh checkpoint and runtime must agree")
    names = [skill["name"] for skill in public["skills"]]
    if len(names) != len(LOCAL_GOALS) or set(names) != set(LOCAL_GOALS):
        raise ValueError("Declaration Reach requires the complete eight-skill library")
    documents = {}
    for skill in public["skills"]:
        if "SKILL.md" not in skill["files"]:
            raise ValueError("Missing public skill instructions")
        path = output / "public/skills" / skill["name"] / "SKILL.md"
        documents[path] = (path.read_text(encoding="utf-8") + DECLARATION_GUIDANCE
                           + "\n### Local completion goal\n\n" + LOCAL_GOALS[skill["name"]] + "\n")

    # Validate both worlds and the complete library before the first migration write.
    public["policy"]["skill_protocol"] = PROTOCOL
    for name, world in worlds.items():
        del world["social"]["skill_runs"]
        del world["social"]["active_skill_run"]
        write_json(output / name / "world.json", world)
    for path, content in documents.items():
        path.write_text(content, encoding="utf-8")
    write_json(output / "public/task.json", public)
    private["public"] = deepcopy(public)
    write_json(output / "private/initial_obligations.json", private)
    return public
