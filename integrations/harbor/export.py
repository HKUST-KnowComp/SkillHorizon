"""Export full-online tasks with separate public and private Docker build contexts."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil

import yaml

from skillhorizon.experiment.internal_mcp import FORMAT, ROOT
from skillhorizon.util import load_json
from . import ADAPTER_REVISION, HARBOR_COMMIT
from .backend import require_full_online, write_record
from . import declarations


BUSINESS_REQUIREMENTS = """mcp==2.0.0
pydantic==2.13.4
jsonschema==4.26.0
PyYAML==6.0.3
tiktoken==0.14.0
typing-extensions==4.16.0
pybtex==0.26.1
"""

MAIN_DOCKERFILE = """FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends bash tmux asciinema procps ca-certificates && rm -rf /var/lib/apt/lists/*
RUN printf '%s\\n' 'export PS1="root@skillhorizon:\\w# "' >> /root/.bashrc
COPY client.py /opt/skillhorizon/client.py
COPY skills /skills
ENV PYTHONPATH=/opt/skillhorizon SKILLHORIZON_URL=http://business:8765
WORKDIR /work
CMD ["sleep", "infinity"]
"""

BUSINESS_DOCKERFILE = """FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends bash && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY code /code
COPY task /task
ENV PYTHONPATH=/code/src:/code PYTHONDONTWRITEBYTECODE=1
WORKDIR /code
"""


def check_tree(root):
    root = Path(root)
    if root.is_symlink() or any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("Export does not follow symlinks")


def copy_file(source, destination):
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Expected an ordinary source file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def export_skills(task_root, task, destination):
    if task["format"] == FORMAT:
        public = task_root / "checkpoint/environment/public"
        candidates = load_json(public / "task.json")["skills"]
        source = public / "skills"
    else:
        source = task_root / "checkpoint/skills"
        candidates = [{"name": name, "files": [p.relative_to(source / name).as_posix()
                        for p in sorted((source / name).rglob("*"))
                        if p.is_file() and "__pycache__" not in p.parts]}
                      for name in task["skill_ids"]]
    check_tree(source)
    if declarations.enabled(task):
        names = [skill["name"] for skill in candidates]
        actual = {path.parent.name for path in source.glob("*/SKILL.md")}
        if len(names) != len(set(names)) or set(names) != set(task["skill_ids"]) or set(names) != actual:
            raise ValueError("Declaration skill_ids must match the complete public skill library")
    seen, transformed = set(), []
    for skill in candidates:
        name = skill["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or name in seen:
            raise ValueError("Invalid or duplicate public skill name")
        seen.add(name)
        if "SKILL.md" not in skill["files"]:
            raise ValueError("Public skill lacks SKILL.md")
        for filename in skill["files"]:
            relative = PurePosixPath(filename)
            if relative.is_absolute() or ".." in relative.parts or "\\" in filename:
                raise ValueError("Skill file path escapes its package")
            if filename != "SKILL.md" and (not filename.startswith("scripts/") or relative.suffix not in {".py", ".json", ".md", ".txt"}):
                raise ValueError("Unexpected public skill support file")
            copy_file(source / name / filename, destination / name / filename)
        entry = destination / name / "SKILL.md"
        body = entry.read_text(encoding="utf-8")
        if body.startswith("---\n"):
            parts = body[4:].split("\n---", 1)
            metadata = yaml.safe_load(parts[0]) if len(parts) == 2 else None
            if (not isinstance(metadata, dict) or metadata.get("name") != name
                    or not isinstance(metadata.get("description"), str) or not metadata["description"].strip()):
                raise ValueError("Existing frontmatter is malformed; fix source explicitly")
        else:
            # A generic public name is sufficient; do not infer a preferred plan from the oracle.
            description = skill.get("description") or "Public workflow: " + name.replace("-", " ")
            entry.write_text("---\n" + yaml.safe_dump({"name": name, "description": description}, sort_keys=False)
                             + "---\n\n" + body, encoding="utf-8")
            transformed.append(name)
    if not seen:
        raise ValueError("No public skill library")
    return {"candidate_names": sorted(seen), "frontmatter_added": transformed}


def copy_private_source(destination, *, task_root=None):
    business_root = ROOT.resolve()
    adapter = Path(__file__).resolve().parent
    selected = [(business_root / relative, relative, "business") for relative in (
        "src/skillhorizon", "integrations/internal_mcp", "integrations/c01_correspondence",
        "integrations/socialops", "integrations/logisticsops", "integrations/researchops",
        "integrations/docops", "integrations/c47_finops", "integrations/factoryops")]
    selected.append((adapter, "integrations/harbor", "adapter"))
    count = 0
    origins = {}
    for directory, relative_root, owner in selected:
        if not directory.is_dir():
            raise ValueError("Missing declared private source directory")
        check_tree(directory)
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix not in {".py", ".json", ".md", ".txt"}:
                continue
            if path.name.startswith(("test_", ".")):
                continue
            data = path.read_bytes()
            if re.search(rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,}|-----BEGIN (?:OPENSSH|RSA|EC|PRIVATE) .*KEY-----", data):
                raise ValueError("Credential-like literal in selected source; export stopped")
            relative = Path(relative_root) / path.relative_to(directory)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            origins[relative.as_posix()] = {"owner": owner, "source": str(path),
                                           "sha256": hashlib.sha256(data).hexdigest()}
            count += 1
    task_python = {}
    if task_root is not None:
        checkpoint = Path(task_root) / "checkpoint"
        for path in sorted(checkpoint.rglob("*.py")):
            if "__pycache__" not in path.parts:
                task_python[path.relative_to(checkpoint).as_posix()] = {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "source": str(path)}
    write_record(destination / "CODE_ORIGINS.json", {"version": 1,
        "business_source_root": str(business_root), "adapter_source_root": str(adapter.parents[1]),
        "files": origins, "task_python_files": task_python})
    return count


def export_task(task_root, output, *, max_calls, include_oracle=False, oracle_actions=None,
                require_reference_contract=False):
    task_root, output = Path(task_root).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(task_root) or task_root.is_relative_to(output):
        raise ValueError("Export requires a fresh, nonoverlapping destination")
    raw_task = load_json(task_root / "task.json")
    reference_metadata = ()
    if raw_task.get("format") == FORMAT and raw_task.get("skill_protocol") == declarations.PROTOCOL:
        reference_metadata = ("REFERENCE_ORIGIN.json", "REFERENCE_CONTRACT.json")
        for name in reference_metadata:
            path = task_root / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("Internal declaration reference metadata must be ordinary files")
    check_tree(task_root / "checkpoint")
    task = require_full_online(task_root)
    declaration_enabled = declarations.enabled(task)
    if oracle_actions is not None and not include_oracle:
        raise ValueError("Oracle actions require an explicit oracle export")
    selected_oracle = None
    if require_reference_contract and not (task_root / "REFERENCE_CONTRACT.json").is_file():
        raise ValueError("Frozen-source export requires a reference contract")
    if include_oracle:
        from integrations.benchmark_suite.reference_policy import reference_actions, bound_reference
        from skillhorizon.util import sha256_value
        selected_oracle = reference_actions(task_root) if oracle_actions is None else oracle_actions
        if not isinstance(selected_oracle, list) or not selected_oracle:
            raise ValueError("Canonical oracle actions are missing")
        if require_reference_contract or (task_root / "REFERENCE_CONTRACT.json").exists():
            if sha256_value(selected_oracle) != bound_reference(task_root)["actions_digest"]:
                raise ValueError("Oracle differs from the bound canonical reference")
    if type(max_calls) is not int or max_calls < 1:
        raise ValueError("Explicit positive business-call budget required")
    output.mkdir(parents=True)
    public = output / "environment/public"
    private = output / "environment/business"
    public.mkdir(parents=True)
    private.mkdir(parents=True)
    library = export_skills(task_root, task, public / "skills")
    copy_file(Path(__file__).with_name("client.py"), public / "client.py")
    main_dockerfile = MAIN_DOCKERFILE
    if declaration_enabled:
        main_dockerfile += "ENV SKILLHORIZON_PROTOCOL=" + declarations.PROTOCOL + "\n"
    (public / "Dockerfile").write_text(main_dockerfile)
    # Harbor validates this path; Compose explicitly overrides its build context.
    (output / "environment/Dockerfile").write_text(main_dockerfile)
    (private / "Dockerfile").write_text(BUSINESS_DOCKERFILE)
    (private / "requirements.txt").write_text(BUSINESS_REQUIREMENTS)
    source_files = copy_private_source(private / "code", task_root=task_root)
    for filename in ("task.json", "BUILD.json", *reference_metadata):
        copy_file(task_root / filename, private / "task" / filename)
    shutil.copytree(task_root / "checkpoint", private / "task/checkpoint")
    command = ["python", "-m", "integrations.harbor.backend", "serve", "/task", "/state", "--max-calls", str(max_calls),
               "--require-code-origins"]
    compose = {"services": {
        "main": {"build": {"context": "${CONTEXT_DIR}/public", "dockerfile": "Dockerfile"},
                 "networks": ["business_net"], "depends_on": {"business": {"condition": "service_healthy"}},
                 "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"]},
        "business": {"build": {"context": "${CONTEXT_DIR}/business", "dockerfile": "Dockerfile"},
                     "command": command, "networks": ["business_net"],
                     "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                     "healthcheck": {"test": ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2).read()"],
                                     "interval": "2s", "timeout": "3s", "retries": 30}}},
        "networks": {"business_net": {"internal": True}}}
    (output / "environment/docker-compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False))
    instructions = "\n\n".join(message["content"] for message in task["messages"])
    instructions += ("\n\n## Terminal interface\nThe entire public skill library is in /skills. "
        "Use python /opt/skillhorizon/client.py list to discover public tools, schema NAME to inspect a tool, "
        "and call NAME 'JSON' (or @FILE or - for stdin) to invoke it. Python programs can import Client from client. "
        "Work files may be saved in /work. Tool calls operate on the persistent business service; "
        "reading or running a local skill script is not itself a business-state commit. "
        "The gateway never retries writes. After a transport disconnect, execution may already have occurred; "
        "inspect public state rather than blindly repeat the operation.\n")
    if declaration_enabled:
        instructions += ("\n## Skill declarations\n"
            "begin_skill declares use of a public skill with your own run_id and optional scope references. "
            "finish_skill closes that declaration as completed, blocked or abandoned. completed means you "
            "claim the local goal is achieved; it is not business certification. Declarations neither bind "
            "inputs nor authorize operations. For business calls, associate a run using --skill-run-id LABEL "
            "or Client.call(..., skill_run_id=LABEL); do not add this metadata to business arguments. "
            "Exact declaration repeats preserve the first receipt; changed content with the same run_id "
            "conflicts. Use a new run_id for new work. DeclarationUncertain means only the declaration "
            "receipt is unknown, not that a business action executed.\n")
    (output / "instruction.md").write_text(instructions, encoding="utf-8")
    (output / "tests").mkdir()
    (output / "tests/test.sh").write_text(
        '#!/bin/bash\nprintf "%s\\n" "Use the SkillHorizon host verifier; default script scoring is disabled." >&2\nexit 64\n')
    (output / "tests/test.sh").chmod(0o755)
    (output / "task.toml").write_text(
        'schema_version = "1.4"\n[metadata]\nadapter = ' + json.dumps(ADAPTER_REVISION) +
        '\ntrack = "full_online"\n[agent]\ntimeout_sec = 900\n[verifier]\ntimeout_sec = 600\n'
        '[environment]\nbuild_timeout_sec = 1200\ncpus = 2\nmemory_mb = 4096\nskills_dir = "/skills"\n')
    if include_oracle:
        solution = output / "solution"
        solution.mkdir()
        write_record(solution / "actions.json", [{"name": a["skill_id"], "arguments": a["arguments"]} for a in selected_oracle])
        (solution / "solve.sh").write_text("#!/bin/bash\nset -euo pipefail\npython - <<'PY'\nimport json\nfrom client import Client\nc = Client()\nfor a in json.load(open('/solution/actions.json')):\n    c.call(a['name'], a['arguments'])\nPY\n")
        (solution / "solve.sh").chmod(0o755)
    preserved_reference_files = []
    for filename in ("REFERENCE_CONTRACT.json", "REFERENCE_ORIGIN.json"):
        if (task_root / filename).is_file():
            copy_file(task_root / filename, output / "reference_metadata" / filename)
            preserved_reference_files.append(filename)
    manifest = {"adapter_revision": ADAPTER_REVISION, "harbor_commit": HARBOR_COMMIT,
                "task_id": task["task_id"], "source_task": str(task_root), "track": "full_online", "max_calls": max_calls,
                "source_files": source_files, "skills": library, "oracle_in_controller_only_solution": include_oracle,
                "code_origins_version": 1,
                "business_source_root": str(ROOT.resolve()), "adapter_source_root": str(Path(__file__).resolve().parents[2]),
                "oracle_actions_digest": sha256_value(selected_oracle) if include_oracle else None,
                "reference_length": "unchanged_source_protocol; not_remeasured_by_export",
                "preserved_reference_files": preserved_reference_files,
                "provider_calls": 0, "status": "exported_not_runtime_qualified"}
    if declaration_enabled:
        manifest["skill_protocol"] = declarations.PROTOCOL
    write_record(output / "migration.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--include-oracle", action="store_true")
    args = parser.parse_args()
    print(json.dumps(export_task(args.task, args.output, max_calls=args.max_calls, include_oracle=args.include_oracle)))


if __name__ == "__main__":
    main()
