"""Explicit launch only. Real Harbor agents retain their upstream native loops."""
import asyncio
from contextlib import suppress
from importlib.metadata import distribution
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from . import HARBOR_COMMIT
from .common import decode, read, write
from .config import model_ids
from .provider import relay, validate_base

ROOT = Path(__file__).resolve().parents[2]


async def execute(prepared):
    if os.environ.get("SKILLHORIZON_NATIVE_WORKER") != "relay_v1":
        raise ValueError("Use the explicit run command, not the internal worker")
    from harbor.models.trial.config import TrialConfig
    from harbor.trial.trial import Trial
    root = Path(prepared).resolve()
    config = read(root / "job.json")
    record = read(root / "NATIVE.json")
    actor = config["agents"][0]
    if actor["name"] == "terminus-2":
        key = "ANTHROPIC_BASE_URL" if record["model"].startswith("anthropic/") else "OPENAI_BASE_URL"
        actor["kwargs"]["api_base"] = os.environ[key]
    trial = await Trial.create(TrialConfig.model_validate({
        "task": {"path": str(root / "task")}, "trials_dir": str(root / "jobs"),
        "trial_name": "native", "agent": actor, "environment": config["environment"],
        "verifier": config["verifier"]}))
    native_run = trial.agent.run
    terminal = {"reason": None, "freeze": "not_requested"}
    native_stop = trial.agent_environment.stop

    async def settled_stop(*args, **kwargs):
        # Harbor's delete=False still runs compose down. Preserve a worker whose
        # freeze acknowledgement is unknown rather than killing it at finalize.
        if terminal["freeze"] in {"pending", "failed"}:
            return
        await native_stop(*args, **kwargs)

    trial.agent_environment.stop = settled_stop

    async def monitor(environment):
        while True:
            result = await environment.service_exec(
                "python -m integrations.native_agents.service status", service="business", timeout_sec=15)
            if result.return_code != 0:
                raise RuntimeError("Native service status unavailable")
            state = decode(result.stdout)
            if state.get("phase") in {"exhausted", "uncertain"}:
                terminal["reason"] = state["phase"]
                return
            if state.get("phase") != "ready":
                raise RuntimeError("Unexpected native service state")
            await asyncio.sleep(2)

    async def supervised(instruction, environment, context):
        actor_task = asyncio.create_task(native_run(instruction, environment, context))
        watcher = asyncio.create_task(monitor(environment))

        async def freeze_and_stop():
            terminal["freeze"] = "pending"
            try:
                result = await environment.service_exec(
                    "python -m integrations.native_agents.service freeze", service="business", timeout_sec=600)
                state = decode(result.stdout) if result.return_code == 0 else {}
                if (state.get("closing") is not True or state.get("uncertain") is not False
                        or "inflight" not in state or state["inflight"] is not None
                        or state.get("phase") not in {"frozen", "sealed", "scored"}):
                    raise RuntimeError("Business admission did not freeze and settle; cannot verify")
                terminal["freeze"] = "settled"
            finally:
                if terminal["freeze"] != "settled":
                    terminal["freeze"] = "failed"
                    trial.config.verifier.disable = True
                for pending in (watcher, actor_task):
                    pending.cancel()
                await asyncio.gather(watcher, actor_task, return_exceptions=True)
                # Main contains only the native agent. No business process is killed.
                if record["agent"] in {"codex", "claude-code"}:
                    command = "pkill -TERM -f '(^|/)(codex|claude)( |$)'"
                    with suppress(Exception):
                        await environment.exec(command=command, timeout_sec=10)

        try:
            done, _ = await asyncio.wait((actor_task, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                await watcher
                if terminal["reason"] == "uncertain":
                    raise RuntimeError("Uncertain business execution; no replay")
            else:
                await actor_task
                terminal["reason"] = "native_agent_return"
        finally:
            original = sys.exception()
            cancellation = original if isinstance(original, asyncio.CancelledError) else None
            cleanup = asyncio.create_task(freeze_and_stop())
            # Even repeated timeout/cancel signals must not bypass private freeze.
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError as error:
                    if cancellation is None:
                        cancellation = error
                except BaseException:
                    break
            try:
                cleanup.result()
            except BaseException as error:
                if cancellation is not None:
                    raise cancellation from error
                raise
            if cancellation is not None:
                raise cancellation

    trial.agent.run = supervised
    result = await trial.run()
    write(root / "RUN.json", {"profile": record["profile"], "agent": record["agent"],
          "agent_version": record["agent_version"], "harbor_commit": HARBOR_COMMIT,
          "terminal": terminal["reason"], "has_exception": result.exception_info is not None,
          "freeze": terminal["freeze"], "environment_retained": terminal["freeze"] == "failed",
          "provider_behavior_verified": False,
          "reward": result.verifier_result.rewards if result.verifier_result else None})
    return 1 if (terminal["freeze"] != "settled" or result.exception_info is not None
                 or result.verifier_result is None) else 0


def docker_endpoint():
    # Resolve only the endpoint; never copy Docker config, auth or TLS material.
    context = os.environ.get("DOCKER_CONTEXT")
    endpoint = None if context else os.environ.get("DOCKER_HOST")
    if not endpoint:
        command = ["docker", "context", "inspect"]
        if context:
            command.append(context)
        command += ["--format", "{{.Endpoints.docker.Host}}"]
        discovery = {name: os.environ[name] for name in ("PATH", "HOME", "DOCKER_CONFIG")
                     if name in os.environ}
        result = subprocess.run(command, env=discovery, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError("Cannot resolve local Docker endpoint")
        endpoint = result.stdout.strip()
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "unix" or parsed.netloc or not parsed.path.startswith("/")
            or parsed.query or parsed.fragment or any(ord(c) < 32 for c in endpoint)):
        raise ValueError("Native profile supports only a local unix Docker endpoint, not remote/TLS contexts")
    return "unix://" + str(Path(parsed.path).resolve())


def run(prepared, *, credential_env, api_base=None):
    root = Path(prepared).resolve()
    record = read(root / "NATIVE.json")
    if record.get("harbor_commit") != HARBOR_COMMIT:
        raise ValueError("Prepared profile uses a different Harbor revision")
    installed = distribution("harbor")
    direct = decode(installed.read_text("direct_url.json") or "{}")
    expected_url = "https://github.com/harbor-framework/harbor/archive/" + HARBOR_COMMIT + ".tar.gz"
    if direct.get("url") != expected_url:
        raise ValueError("Installed Harbor is not the pinned archive; use native requirements.txt")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", credential_env):
        raise ValueError("credential_env must name exactly one environment variable")
    if any((root / name).exists() for name in ("STARTED.json", "jobs", "RUN.json")):
        raise ValueError("Native trials are single-use; no implicit retries or resume")
    model, provider_model = model_ids(record["agent"], record["model"])
    actors = read(root / "job.json")["agents"]
    if (record["model"] != model or record.get("provider_model") != provider_model
            or len(actors) != 1 or actors[0]["name"] != record["agent"]
            or actors[0]["model_name"] != model):
        raise ValueError("Prepared model mapping is inconsistent; prepare a fresh profile")
    endpoint = docker_endpoint()
    anthropic = record["agent"] == "claude-code" or (
        record["agent"] == "terminus-2" and record["model"].startswith("anthropic/"))
    protocol = "anthropic" if anthropic else "openai"
    base = api_base or ("https://api.anthropic.com" if anthropic else "https://api.openai.com/v1")
    validate_base(base)
    with tempfile.TemporaryDirectory(prefix="native-home-", dir=root) as home:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home,
               "DOCKER_HOST": endpoint, "DOCKER_CONFIG": str(Path(home) / ".docker"),
               "PYTHONPATH": os.pathsep.join((str(ROOT / "src"), str(ROOT))),
               "PYTHONDONTWRITEBYTECODE": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
               "SKILLHORIZON_NATIVE_WORKER": "relay_v1"}
        # Probe and worker share the exact Docker endpoint and empty config/home.
        probe = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                               env=env, capture_output=True, text=True, timeout=15)
        if probe.returncode or not probe.stdout.strip() or probe.stdout.strip() in {"<nil>", "null"}:
            raise RuntimeError("Docker daemon unavailable; no model request or credential read")
        key = os.environ.get(credential_env)
        if not key:
            raise ValueError("The explicitly named credential variable is unset")
        write(root / "STARTED.json", {"profile": record["profile"], "provider_protocol": protocol,
                                  "provider_base": base, "agent": record["agent"],
                                  "model": model, "provider_model": provider_model,
                                  "installed_harbor_version": installed.version,
                                  "installed_harbor_commit": HARBOR_COMMIT})
        with relay(base, key, provider_model, protocol) as (port, token, traffic):
            host = "127.0.0.1" if record["agent"] == "terminus-2" else "host.docker.internal"
            prefix = "ANTHROPIC" if anthropic else "OPENAI"
            env[prefix + "_API_KEY"] = token
            env[prefix + "_BASE_URL"] = f"http://{host}:{port}" + ("" if anthropic else "/v1")
            try:
                result = subprocess.run([sys.executable, "-B", "-m", "integrations.native_agents",
                                         "_worker", str(root)], cwd=root, env=env)
                return result.returncode
            finally:
                write(root / "PROVIDER.json", {"native_protocol": protocol, **traffic,
                                               "usage": "See native agent trajectory; no inferred usage"})
