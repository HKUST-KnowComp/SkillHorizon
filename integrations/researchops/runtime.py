from integrations.internal_mcp.skills import ObservedSkills, skill_receipt
from .kernel import Library, require


class SkillLibrary(ObservedSkills, Library):
    def __init__(self, task, runtime):
        super().__init__(task, runtime)
        if "synthesis_policy" in self.public:
            self.script_files = {**self.script_files, "assemble-literature-handoff": "scripts/synthesize.py"}

    scope_kind = "section"
    scope_argument = "section_ids"
    require = staticmethod(require)
    script_files = {"reconcile-publication-identities": "scripts/identity_groups.py",
        "build-versioned-bibliography": "scripts/prepare_keys.py"}

    def result_data(self, name, data):
        return skill_receipt(name, super().result_data(name, data))
