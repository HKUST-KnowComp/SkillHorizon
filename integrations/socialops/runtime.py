from integrations.internal_mcp.skills import ObservedSkills
from .kernel import Social, require


class SkillSocial(ObservedSkills, Social):
    scope_kind = "content_post"
    scope_argument = "post_ids"
    require = staticmethod(require)
    script_files = {"schedule-cross-platform-release": "scripts/allocate_slots.py", "analyze-campaign-experiment": "scripts/compare_rates.py"}
