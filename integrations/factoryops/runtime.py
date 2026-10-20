from integrations.internal_mcp.skills import ObservedSkills
from .kernel import Production, require


class SkillProduction(ObservedSkills, Production):
    scope_kind = "order"
    scope_argument = "order_ids"
    require = staticmethod(require)
    script_files = {"schedule-material-bound-run": "scripts/find_interval.py", "disposition-and-recover-lot": "scripts/aggregate_quality.py"}
