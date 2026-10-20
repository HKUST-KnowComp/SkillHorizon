"""Episode-local instruction reads for public scripted reference policies."""


class ImmutableSkillDocuments:
    def __init__(self):
        self.read_skills = set()

    async def read(self, call, skill_id):
        if skill_id in self.read_skills:
            return
        result = await call("read_skill_file", skill_id=skill_id)
        if (not isinstance(result, dict) or result.get("skill_id") != skill_id
                or result.get("path") != "SKILL.md"
                or not isinstance(result.get("content"), str) or not result["content"].strip()):
            raise ValueError("Missing public skill document")
        self.read_skills.add(skill_id)
