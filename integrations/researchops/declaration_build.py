"""Fresh declaration wrapper; preserve the original calibration business world."""

from copy import deepcopy
from pathlib import Path

from integrations.internal_mcp.io import read_json, write_json
from skillhorizon.construction_provenance import construction_entry, observe_call
from .calibration_build import FORMAT, build as build_calibration
from .declaration_runtime import BOOKKEEPING, PROTOCOL


LOCAL_GOALS = {
    "inspect-library-request": "Understand the specified library request. Understanding is unobserved; reading a request does not by itself prove this cognitive goal.",
    "reconcile-publication-identities": "For each specified acquired work, retain exactly one active canonical representative with authoritative acquisition DOI identity, correct alias resolution and transferred ownership. Either legal representative may be retained; different authoritative works remain distinct.",
    "curate-section-collections": "For the specified section, maintain its collection, reading list and tag with every currently acquired work matching the public metadata criteria exactly once. Reading order is free. Numerical exclusion does not remove a relevant work from a section.",
    "annotate-source-evidence": "Maintain the specified real source annotation with current canonical ownership, exact source version, passage and quote span. Its entire source passage must remain unchanged. This is a local evidence artifact, not a claim that all evidence for a paper or task is complete.",
    "maintain-citation-network": "For the specified citing work, maintain every current source-backed reference context with the correct canonical target and exact real annotation. Retire stale active contexts while preserving history; equal endpoint counts are not enough.",
    "build-versioned-bibliography": "Produce the specified actual bibliography for the explicitly scoped sections, or the public task's full section-work union when no section is scoped, deduplicated once per authoritative work, with unique keys, current catalog and source versions, correct metadata and ordered authors, and matching real BibTeX bytes. A stored artifact from an earlier revision is not automatically current.",
    "assemble-literature-handoff": "Maintain the specified actual review for the current acquired round with complete public section/work coverage, full current results annotations, source-backed citation contexts, exact current bibliography keys, all current calibration ancestors and correct numerical synthesis and Markdown. Rendering success alone does not establish this goal.",
}
SCRIPTS = {"reconcile-publication-identities": "scripts/identity_groups.py",
           "build-versioned-bibliography": "scripts/prepare_keys.py",
           "assemble-literature-handoff": "scripts/synthesize.py"}
GUIDANCE = """

## Controller-owned declarations

This task uses skill_declaration_v1. begin_skill(run_id, skill_id, scope) and
finish_skill(run_id, status, outputs, note), when provided by the controller,
declare work; they do not authorize business operations. status is completed,
blocked or abandoned. completed asserts the local goal, not merely that work
stopped. All business tools and fixed scripts remain usable without declarations.
Call ownership is an optional client skill_run_id envelope, never a business
active-run slot. Old business begin/select/finish/read_skill_run tools are absent.

scope and outputs use actual public {kind,id} references: section, paper,
source_key, annotation, citation, bibliography or review. Explicit outputs must
all be correct and related to the declared target; one correct output cannot
hide a wrong one. Missing or ambiguous targets are unobserved rather than an
implicit global-completion claim. A bibliography uses explicitly scoped sections
or defaults to all public sections; a review always has its whole-round goal.
Existing applicable evidence may be reused.
The first completed cut is fixed; later repairs cannot change that assessment.
Later source updates do not erase historical artifacts or their earlier validity.
Neither understanding nor process adherence follows from a business success.
"""


@construction_entry
def build(source, output, *, seed=7, sections=2, rounds=3, studies_per_section=1,
          alias_copies=1, distractors_per_section=1, background=True,
          scenario="identity_compatibility", dependency_pattern="distributed", runtime_identity="task_local_v1", construction_profile=None):
    if runtime_identity != "task_local_v1":
        raise ValueError("Declaration library requires task_local_v1")
    public = observe_call(build_calibration, source, output, seed=seed, sections=sections, rounds=rounds,
                          studies_per_section=studies_per_section, alias_copies=alias_copies,
                          distractors_per_section=distractors_per_section, background=background, construction_profile=construction_profile,
                          scenario=scenario, dependency_pattern=dependency_pattern, runtime_identity=runtime_identity)
    output = Path(output)
    worlds = {name: read_json(output / name / "world.json") for name in ("checkpoint", "runtime")}
    private = read_json(output / "private/initial_obligations.json")
    if (public != read_json(output / "public/task.json") or private["public"] != public
            or public["format"] != FORMAT or public["policy"]["skill_protocol"] != "observational_v1"
            or worlds["checkpoint"] != worlds["runtime"]
            or private["initial_state"] != worlds["checkpoint"]["library"]
            or private["initial_tables"] != worlds["checkpoint"]["tables"]):
        raise ValueError("Expected the unchanged fresh Calibration V2 construction")
    for world in worlds.values():
        state = world["library"]
        if (world["format"] != FORMAT or world["task_id"] != public["task_id"]
                or type(world.get("sequence")) is not int or world["sequence"] != 0
                or state.get("skill_runs") != {} or "active_skill_run" not in state
                or state["active_skill_run"] is not None):
            raise ValueError("Only fresh unexecuted library worlds can be migrated")
    if len(public["skills"]) != len(LOCAL_GOALS) or {s["name"] for s in public["skills"]} != set(LOCAL_GOALS):
        raise ValueError("The complete seven-skill library is required")
    texts = {}
    for skill in public["skills"]:
        name = skill["name"]
        expected = ["SKILL.md"] + ([SCRIPTS[name]] if name in SCRIPTS else [])
        folder = output / "public/skills" / name
        if sorted(skill["files"]) != sorted(expected) or any(not (folder / f).is_file() for f in expected):
            raise ValueError("The original skill files and three scripts are required")
        path = folder / "SKILL.md"
        texts[path] = path.read_text(encoding="utf-8") + GUIDANCE + "\n### Local completion goal\n\n" + LOCAL_GOALS[name] + "\n"
    public["policy"]["skill_protocol"] = PROTOCOL
    for name, world in worlds.items():
        for key in BOOKKEEPING:
            del world["library"][key]
        write_json(output / name / "world.json", world)
    private["public"], private["initial_state"] = deepcopy(public), deepcopy(worlds["checkpoint"]["library"])
    for path, text in texts.items():
        path.write_text(text, encoding="utf-8")
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", private)
    return public
