from integrations.internal_mcp.skills import ObservedSkills
from .kernel import FinOps, require


class SkillFinOps(ObservedSkills, FinOps):
    scope_kind = "resource"
    scope_argument = "resource_ids"
    require = staticmethod(require)
    script_files = {"repair-cost-allocation": "scripts/apportion_costs.py", "plan-commitment-portfolio": "scripts/price_commitments.py"}
