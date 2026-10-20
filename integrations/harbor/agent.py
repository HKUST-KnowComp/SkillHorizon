"""The upstream Terminus-2 with controller metadata and read-only observation."""

from harbor.agents.terminus_2.terminus_2 import Terminus2, Terminus2Options

from .observation import ObservationMixin


class SkillHorizonTerminusOptions(Terminus2Options):
    origin_manifest: str | None = None
    observe: bool = True


class SkillHorizonTerminus(ObservationMixin, Terminus2):
    options_model = SkillHorizonTerminusOptions


def preflight_config(config):
    from harbor.agents.factory import AgentFactory
    if AgentFactory.get_agent_class_from_config(config) is not SkillHorizonTerminus:
        raise ValueError("Native observed agent class differs")
    AgentFactory.run_preflight(config)
    return {"status": "passed", "import_path": "integrations.harbor.agent:SkillHorizonTerminus",
            "options": dict(config.kwargs)}
