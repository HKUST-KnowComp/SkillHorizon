"""Export submission bundles without building references or contacting models."""
import ast
import importlib.util
from pathlib import Path
import re
import shutil

import yaml

from . import ENVIRONMENTS
from .common import inside, read, write
from .config import job_config, metadata

ROOT = Path(__file__).resolve().parents[2]
SUFFIXES = {".py", ".json", ".jsonl", ".md", ".txt", ".yaml", ".yml", ".csv"}
DOMAINS = ("socialops", "logisticsops", "researchops", "docops", "factoryops")
EXCLUDED = {"__pycache__", ".git", ".env", "reference", "oracle", "solution", "runtime"}
BUSINESS_REQUIREMENTS = """mcp==2.0.0
pydantic==2.13.5
jsonschema==4.26.0
PyYAML==6.0.3
tiktoken==0.14.0
typing-extensions==4.16.0
pybtex==0.26.1
"""
MAIN_DOCKERFILE = """FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 python-is-python3 bash curl ca-certificates tmux asciinema procps git && rm -rf /var/lib/apt/lists/*
COPY client.py /opt/skillhorizon/client.py
COPY skills /skills
COPY instruction.md /work/instruction.md
ENV PYTHONPATH=/opt/skillhorizon SKILLHORIZON_URL=http://business:8765
WORKDIR /work
CMD ["sleep", "infinity"]
"""
BUSINESS_DOCKERFILE = """FROM python:3.12-slim
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY code /code
COPY input /input
ENV PYTHONPATH=/code/src:/code PYTHONDONTWRITEBYTECODE=1
WORKDIR /code
CMD ["python", "-m", "integrations.native_agents.service", "serve"]
"""


def copy_file(source, target):
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Expected an ordinary input file")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def copy_tree(source, target, *, docs_only=False):
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Expected an ordinary input directory")
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(p in EXCLUDED or p.startswith(".") for p in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("Symlinks are not exported")
        if path.is_file() and (path.name == "SKILL.md" if docs_only else path.suffix in SUFFIXES):
            copy_file(path, target / relative)


def source_closure(destination):
    # Resolve local imports statically. No application modules or credentials run.
    index = {}
    for folder in (ROOT / "src/skillhorizon", ROOT / "integrations", ROOT / "scripts"):
        base = ROOT / "src" if folder.name == "skillhorizon" else ROOT
        for path in folder.rglob("*.py"):
            if any(p.startswith(".") or p == "__pycache__" for p in path.relative_to(folder).parts):
                continue
            parts = list(path.relative_to(base).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            index[".".join(parts)] = path
    pending = ["integrations.native_agents.service", "integrations.inspect_agent.environments"]
    pending += [n for n, p in index.items()
                if any(n.startswith("integrations." + d + ".") for d in DOMAINS)
                and not p.name.startswith(("test_", "qualify", "finalize")) and "checks" not in p.parts]
    chosen = {}
    while pending:
        name = pending.pop()
        if name in chosen or name not in index:
            continue
        path = index[name]
        chosen[name] = path
        parts = name.split(".")
        pending.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                pending.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                target = node.module or ""
                if node.level:
                    target = importlib.util.resolve_name("." * node.level + target, package)
                pending.append(target)
                pending.extend(target + "." + a.name for a in node.names)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in index:
                pending.append(node.value)
    for path in set(chosen.values()):
        copy_file(path, destination / path.relative_to(ROOT))
    for folder in {p.parent for p in chosen.values()}:
        for resource in folder.glob("*.json"):
            if not resource.name.startswith("."):
                copy_file(resource, destination / resource.relative_to(ROOT))
    for domain in DOMAINS:
        for folder in (ROOT / "integrations" / domain).glob("*skills"):
            copy_tree(folder, destination / folder.relative_to(ROOT))


def bundle_inputs(bundle, manifest):
    environment = manifest.get("environment", manifest.get("env"))
    if environment not in ENVIRONMENTS:
        raise ValueError("Unknown submission environment")
    task = inside(bundle, manifest["task"])
    public = inside(bundle, manifest["public"])
    if environment == "FieldOps" and (task / "environment/business/task").is_dir():
        task = inside(task, "environment/business/task")
    return environment, task, public


def task_inputs(environment, source, destination):
    destination.mkdir(parents=True)
    if environment == "FinanceOps":
        if read(source / "task.json").get("format") != "skillhorizon.dataflow_v6.tasks.v1":
            raise ValueError("Expected native FinanceOps V6")
        copy_file(source / "task.json", destination / "task.json")
        copy_tree(source / "initial", destination / "initial")
        return source / "initial/skills"
    if environment == "FieldOps":
        spec = read(source / "task.json")
        if spec.get("checkpoint_action_count") != 0:
            raise ValueError("Native profile requires an unexecuted checkpoint")
        copy_file(source / "task.json", destination / "task.json")
        copy_file(source / "BUILD.json", destination / "BUILD.json")
        copy_tree(source / "checkpoint", destination / "checkpoint")
        return source / "checkpoint/skills"
    names = ["PUBLIC_TOOLS.json", "mcp.json"]
    if environment != "LogisticsOps" or (source / "background.json").exists():
        names.append("background.json")
    for name in names:
        copy_file(source / name, destination / name)
    for name in ("checkpoint", "public", "private"):
        copy_tree(source / name, destination / name)
    controller = read(source / "controller.json")
    backend = Path(controller["source"])
    backend = backend if backend.is_absolute() else source / backend
    # Only the declared backend's code/data are needed, never its logs or .env.
    copy_tree(backend / "mcp_servers", destination / "backend/mcp_servers")
    if (backend / "data").is_dir():
        copy_tree(backend / "data", destination / "backend/data")
    controller["source"] = "backend"
    write(destination / "controller.json", controller)
    return source / "public/skills"


def instruction(public, B):
    messages = public.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("Expected public task messages")
    content = []
    for message in messages:
        if message.get("role") != "user" or not isinstance(message.get("content"), str):
            raise ValueError("Native bundles must provide user goal messages, not controller prompts")
        text = message["content"]
        if re.search(r"(?:compaction at|estimated 200000|type.{0,6}:\s*.actions|submit final again)", text, re.I):
            raise ValueError("Controlled harness instructions are not native public goals")
        content.append(text)
    return "\n\n".join(content) + f"""

## Native execution contract
Use your native terminal and context-management workflow. Public skill documents
are in /skills. The persistent business service is the only authority for task
state; editing local files does not commit business changes. Work files belong
in /work. Skill scripts run through the public business tools, not local copies.
Import Client from client, or use:

python /opt/skillhorizon/client.py list
python /opt/skillhorizon/client.py schema TOOL
python /opt/skillhorizon/client.py call TOOL 'JSON'

Every business query, script invocation or other attempted call costs one of
B={B}, including unknown-tool and schema rejections. begin_skill and finish_skill
are free declarations, not business certification. Associate business calls with
--skill-run-id when appropriate. Tool catalogue/schema discovery and local file
access do not spend B. B is shared by all your terminal processes. At exhaustion
no further calls are admitted and the harness stops the agent.

Make one business call at a time and inspect its result before deciding what to
do next. A transport failure can mean the operation already happened: never
automatically retry writes. An uncertain operation closes execution for review.
Finish through your native agent when you decide to submit the current state.
There is no controlled-JSON final envelope or additional final confirmation.
Hidden evaluation runs only after execution is sealed. Local output, exit status
and completion claims do not establish business acceptance.
"""


def prepare(bundle, output, *, agent, model, max_calls, agent_version=None):
    if type(max_calls) is not int or max_calls <= 0:
        raise ValueError("Positive integer max_calls required")
    config = job_config(agent, model, agent_version)
    bundle, output = Path(bundle).resolve(), Path(output).absolute()
    if output.exists() or output.resolve().is_relative_to(bundle) or bundle.is_relative_to(output.resolve()):
        raise ValueError("Use a fresh, nonoverlapping output directory")
    environment, source, public_path = bundle_inputs(bundle, read(bundle / "manifest.json"))
    public = read(public_path)
    text = instruction(public, max_calls)
    output.mkdir(parents=True)
    task = output / "task"
    main, business = task / "environment/public", task / "environment/business"
    main.mkdir(parents=True)
    business.mkdir(parents=True)
    skills = task_inputs(environment, source, business / "input/task")
    copy_tree(skills, main / "skills", docs_only=True)
    if not list((main / "skills").glob("*/SKILL.md")):
        raise ValueError("Public skill documents are required")
    copy_file(Path(__file__).with_name("client.py"), main / "client.py")
    source_closure(business / "code")
    write(business / "input/public.json", public)
    write(business / "input/service.json", {"environment": environment, "B": max_calls})
    (business / "requirements.txt").write_text(BUSINESS_REQUIREMENTS)
    main_dockerfile = MAIN_DOCKERFILE
    if agent != "terminus-2":
        package = "@openai/codex" if agent == "codex" else "@anthropic-ai/claude-code"
        # Build with egress; the runtime's dropped capabilities need no installer.
        main_dockerfile += f"RUN npm install -g {package}@{agent_version}\n"
    (main / "Dockerfile").write_text(main_dockerfile)
    (business / "Dockerfile").write_text(BUSINESS_DOCKERFILE)
    (task / "environment/Dockerfile").write_text(main_dockerfile)
    (main / "instruction.md").write_text(text)
    (task / "instruction.md").write_text(text)
    (task / "task.toml").write_text('schema_version = "1.4"\n[metadata]\nprofile = "native_external_v1"\n'
        '[agent]\n[verifier]\ntimeout_sec = 660\n[environment]\nbuild_timeout_sec = 1200\n'
        'cpus = 2\nmemory_mb = 4096\nskills_dir = "/skills"\n')
    compose = {"services": {
        "main": {"build": {"context": "${CONTEXT_DIR}/public"},
                 "networks": ["business_net", "agent_egress"],
                 "extra_hosts": ["host.docker.internal:host-gateway"],
                 "depends_on": {"business": {"condition": "service_healthy"}},
                 "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"]},
        "business": {"build": {"context": "${CONTEXT_DIR}/business"},
                     "networks": ["business_net"], "cap_drop": ["ALL"],
                     "security_opt": ["no-new-privileges:true"],
                     "healthcheck": {"test": ["CMD", "python", "-c",
                         "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=2).read()"],
                         "interval": "2s", "timeout": "3s", "retries": 30}}},
        "networks": {"business_net": {"internal": True}, "agent_egress": {}}}
    (task / "environment/docker-compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False))
    (task / "tests").mkdir()
    (task / "tests/test.sh").write_text('#!/bin/sh\necho "Use the native host verifier" >&2\nexit 64\n')
    (task / "tests/test.sh").chmod(0o755)
    write(output / "job.json", config)
    record = metadata(agent, model, max_calls, agent_version)
    record.update(environment=environment, status="prepared_not_executed", provider_calls=0)
    write(output / "NATIVE.json", record)
    return record
