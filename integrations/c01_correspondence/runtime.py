from integrations.internal_mcp.skills import ObservedSkills, skill_receipt
from .counterparts import ProjectCorrespondents
from .kernel import Correspondence, require


class SkillCorrespondence(ObservedSkills, ProjectCorrespondents, Correspondence):
    scope_kind = "case"
    scope_argument = "case_ids"
    require = staticmethod(require)
    script_files = {"prepare-review-request": "scripts/render_template.py", "assemble-dispatch-package": "scripts/assemble_package.py"}

    def result_data(self, name, data):
        return skill_receipt(name, super().result_data(name, data))
