"""Opt-in native Harbor agents with a private, authoritative business service."""

PROFILE = "native_external_v1"
HARBOR_COMMIT = "cfc54c995e90cd438deb862189a58053b9d89fd3"
AGENTS = ("terminus-2", "codex", "claude-code")
ENVIRONMENTS = ("FinanceOps", "FieldOps", "SocialOps", "LogisticsOps",
                "ResearchOps", "DocOps", "FactoryOps")


def prepare(bundle, output, *, agent, model, max_calls, agent_version=None):
    from .export import prepare as implementation
    return implementation(bundle, output, agent=agent, model=model,
                          max_calls=max_calls, agent_version=agent_version)


def run(prepared, *, credential_env, api_base=None):
    from .runner import run as implementation
    return implementation(prepared, credential_env=credential_env, api_base=api_base)
