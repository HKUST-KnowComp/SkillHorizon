"""Minimal Agent Skills packages for the public, fixed benchmark library."""
import json
from pathlib import Path
import re

import yaml


def skill_header(name, description):
    return (f"---\nname: {name}\ndescription: {json.dumps(description)}\n"
            "compatibility: Requires the SkillHorizon MCP environment and Python 3.10 or newer.\n---\n\n")


def validate_packages(root):
    entries = []
    for directory in sorted(Path(root).iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError("Skill export must contain only ordinary skill directories")
        text = (directory / "SKILL.md").read_text()
        if not text.startswith("---\n") or "\n---\n" not in text[4:]:
            raise ValueError("SKILL.md requires YAML frontmatter")
        metadata, body = text[4:].split("\n---\n", 1)
        value = yaml.safe_load(metadata)
        name = value.get("name", "")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64 or name != directory.name:
            raise ValueError("Invalid skill name or parent directory")
        description = value.get("description")
        if not isinstance(description, str) or not 1 <= len(description) <= 1024 or not body.strip():
            raise ValueError("Skill needs a nonempty description and instructions")
        if not 1 <= len(value.get("compatibility", "")) <= 500:
            raise ValueError("Invalid compatibility field")
        files = []
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError("Public skill packages do not export symlinks")
            if path.is_file():
                relative = path.relative_to(directory).as_posix()
                if relative != "SKILL.md" and not relative.startswith("scripts/"):
                    raise ValueError("Only instructions and fixed script support files are exported")
                files.append(relative)
        entries.append({"name": name, "files": files, "script_available": (directory / "scripts/execute.py").is_file()})
    if not entries:
        raise ValueError("No skill packages found")
    return {"validation": "focused_agent_skills_format_check_not_external_agent_integration", "skills": entries}


def export_skills(root, *, include_maintenance=False, include_funding=False, include_ops_horizon=False,
                  include_ops_dependencies=False, include_business_depth=False):
    from .dataflow.skills import install_skills as install_data
    from .fieldops.skills import install_skills as install_operations
    root = Path(root)
    if root.exists():
        raise FileExistsError(root)
    root.mkdir(parents=True)
    install_data(root, business_program="credit_sales_v2" if include_business_depth else "funded_sales_v1" if include_funding else None)
    install_operations(root, "fieldops", "pressure_branches_v3" if include_business_depth else "pressure_services_v2" if include_ops_dependencies else "pressure_network_v1" if include_ops_horizon else None)
    install_operations(root, "procureops", "assembly_network_v3" if include_business_depth else "assembly_contracts_v2" if include_ops_dependencies else "staged_contracts_v1" if include_ops_horizon else None)
    if include_maintenance:
        from .dataflow.maintenance import install_maintenance_skills
        install_maintenance_skills(root)
    return validate_packages(root)
