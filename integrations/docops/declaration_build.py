"""Fresh C25 portfolio tasks with gateway-owned skill declarations."""

from pathlib import Path

from skillhorizon.construction_provenance import construction_entry, observe_call
from integrations.internal_mcp.io import read_json, write_json
from .portfolio_build import build as build_portfolio

PROTOCOL = "skill_declaration_v1"
DECLARATION_GUIDANCE = """

## Skill declarations

You may declare use of this skill with begin_skill(run_id, skill_id, scope), using
an episode-local label and optional public references such as {"kind":"page","id":"PAGE_ID"}.
Business calls work independently of declarations. Associate individual calls via
the client's skill_run_id envelope, not business arguments or an active skill slot.
finish_skill(run_id, status, outputs, note) records completed, blocked or abandoned.
completed means you believe this skill's local goal has been achieved, not merely
that you stopped working. It does not certify a result or change business state.
Use public reference objects for outputs; neither scope nor outputs supplies hidden
validation or auto-selects inputs. Reference kinds include page, page_block,
review_request, decision_record and publication. Repeated declarations cannot move
the first completion point. An output page_block names a proposal stored in that
block; a source publication alone does not identify the proposed contract.
"""


@construction_entry
def build(source, output, *, seed=7, width=3, depth=4, background=True,
          amendment=None, scenario="expiring_reservations", dependency_pattern="staggered",
          runtime_identity="task_local_v1", construction_profile="legacy"):
    public = observe_call(build_portfolio, source, output, seed=seed, width=width, depth=depth,
        background=background, amendment=amendment, scenario=scenario,
        dependency_pattern=dependency_pattern, runtime_identity=runtime_identity, construction_profile=construction_profile)
    output = Path(output)
    public["policy"]["skill_protocol"] = PROTOCOL
    for directory in ("checkpoint", "runtime"):
        world = read_json(output / directory / "world.json")
        if world["sequence"] != 0 or world["planning"]["skill_runs"] or world["planning"]["active_skill_run"] is not None:
            raise ValueError("Only fresh unexecuted C25 worlds can use the declaration protocol")
        del world["planning"]["skill_runs"]
        del world["planning"]["active_skill_run"]
        write_json(output / directory / "world.json", world)
    for skill in public["skills"]:
        path = output / "public/skills" / skill["name"] / "SKILL.md"
        text = path.read_text()
        text = text.replace("Skill completion is\nonly an observation; it does not approve a page or substitute for business state.",
            "A completed declaration claims the local tracing goal was achieved; it does not approve a page or substitute for business state.")
        path.write_text(text + DECLARATION_GUIDANCE, encoding="utf-8")
    write_json(output / "public/task.json", public)
    private = read_json(output / "private/initial_obligations.json")
    private["public"] = public
    write_json(output / "private/initial_obligations.json", private)
    return public
