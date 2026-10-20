"""Portable Harbor job configuration. Provider auth is never serialized here."""
import re
from . import AGENTS, HARBOR_COMMIT, PROFILE


def model_ids(agent, model):
    if agent not in AGENTS:
        raise ValueError("Unsupported native agent")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", model):
        raise ValueError("Explicit model ID required")
    if agent == "terminus-2":
        if not model.startswith(("openai/", "anthropic/")):
            raise ValueError("Terminus native profile requires openai/ or anthropic/ model routing")
        native = model.split("/", 1)[1]
        if not native:
            raise ValueError("Explicit provider model ID required")
        return model, native
    prefix = "openai/" if agent == "codex" else "anthropic/"
    native = model.removeprefix(prefix)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", native):
        raise ValueError("Native CLI requires a raw model ID or one matching provider prefix; nested paths are unsupported")
    return native, native


def agent_config(agent, model, agent_version=None):
    model, _ = model_ids(agent, model)
    if agent == "terminus-2":
        if agent_version not in (None, HARBOR_COMMIT):
            raise ValueError("Terminus-2 version is the pinned Harbor commit")
        options = {}
    else:
        if not isinstance(agent_version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", agent_version):
            raise ValueError("Codex and Claude Code require an explicit CLI semantic version")
        options = {"version": agent_version}
        if agent == "codex":
            options["web_search"] = "disabled"
    return {"name": agent, "model_name": model, "kwargs": options}


def job_config(agent, model, agent_version=None):
    return {"jobs_dir": "jobs", "n_attempts": 1, "n_concurrent_trials": 1,
            "retry": {"max_retries": 0},
            "agents": [agent_config(agent, model, agent_version)],
            "tasks": [{"path": "task"}],
            "environment": {"type": "docker", "delete": False},
            "verifier": {"import_path": "integrations.native_agents.verifier:NativeVerifier"}}


def metadata(agent, model, B, agent_version):
    model, provider_model = model_ids(agent, model)
    return dict(profile=PROFILE, agent=agent, model=model, provider_model=provider_model, B=B,
                harbor_commit=HARBOR_COMMIT,
                agent_version=HARBOR_COMMIT if agent == "terminus-2" else agent_version,
                business_budget="server_admission_attempts", declarations_consume_B=False,
                final_policy="native_agent_end", context_management="native_agent_default",
                cumulative_time_limit=None, provider_verified=False)
