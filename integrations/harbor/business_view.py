"""Byte-preserving business projections for the existing isolated producer."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat

from .producer import validate_producer


SUFFIXES = frozenset({".py", ".json", ".md", ".txt"})
SENSITIVE_NAMES = ("credential", "secret", "password", "token_cache", "id_rsa")
SECRET_LITERAL = re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def full_inventory(root):
    """Include every ordinary task file and empty directory, including caches."""
    root = Path(root)
    if not root.is_absolute() or root != root.resolve() or not root.is_dir():
        raise ValueError("Ordinary absolute task required")
    files, directories = {}, []
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(mode):
            directories.append(relative)
        elif stat.S_ISREG(mode):
            data = path.read_bytes()
            files[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        else:
            raise ValueError("Task copies reject links and special files")
    return {"files": files, "directories": directories}


def copy_task(source, destination):
    source, destination = Path(source), Path(destination)
    if (not destination.is_absolute() or destination != destination.resolve()
            or destination.is_relative_to(source.resolve()) or source.resolve().is_relative_to(destination)):
        raise ValueError("Task copies must be disjoint unaliased absolute paths")
    if destination.exists():
        raise FileExistsError(destination)
    original = full_inventory(source)
    destination.mkdir(parents=True, exist_ok=False)
    for relative in original["directories"]:
        (destination / relative).mkdir()
    for relative, entry in original["files"].items():
        path = source / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError("Task source changed during copy")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError("Task source changed during copy")
        target = destination / relative
        with target.open("xb") as stream:
            stream.write(data)
        shutil.copymode(path, target)
    if full_inventory(source) != original or full_inventory(destination) != original:
        raise ValueError("Task source or copy changed")
    return {"source": str(source), "copy": str(destination), "inventory": original, "provider_calls": 0}


def inventory(source):
    source = Path(source)
    if not source.is_absolute() or source != source.resolve() or not source.is_dir():
        raise ValueError("Ordinary absolute source required")
    files, exclusions = {}, {}

    def visit(path):
        relative = path.relative_to(source).as_posix()
        if relative == "integrations/harbor" or path.name == "__pycache__" or path.name.startswith("."):
            exclusions[relative] = "excluded_subtree_or_hidden_entry"
            return
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError("Source projection rejects links and special files")
        if stat.S_ISDIR(mode):
            for child in sorted(path.iterdir()):
                visit(child)
            return
        if any(word in path.name.lower() for word in SENSITIVE_NAMES):
            raise ValueError("Credential filename forbidden")
        if path.suffix not in SUFFIXES:
            exclusions[relative] = "unsupported_suffix"
            return
        data = path.read_bytes()
        if SECRET_LITERAL.search(data):
            raise ValueError("Credential-like source content forbidden")
        files[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    for name in ("src", "integrations"):
        path = source / name
        if not path.is_dir():
            raise ValueError("Business source subtree missing")
        visit(path)
    if not files:
        raise ValueError("Empty source projection")
    return {"files": files, "exclusions": exclusions}


def create_business_view(source, destination):
    source, destination = Path(source), Path(destination)
    if (not source.is_absolute() or source != source.resolve() or not destination.is_absolute()
            or destination != destination.resolve() or destination.is_relative_to(source)
            or source.is_relative_to(destination)):
        raise ValueError("Source and destination must be disjoint unaliased absolute paths")
    if destination.exists():
        raise FileExistsError(destination)
    original = inventory(source)
    destination.mkdir(parents=True, exist_ok=False)
    for relative, entry in original["files"].items():
        path = source / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError("Source changed during projection")
        data = path.read_bytes()
        if SECRET_LITERAL.search(data) or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError("Source changed during projection")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
    current = inventory(source)
    copied = inventory(destination)
    if current != original or copied["files"] != original["files"] or copied["exclusions"]:
        raise ValueError("Source or copied projection differs")
    validate_producer(destination)
    return {"version": 1, "source": str(source), "view": str(destination),
            **original, "files_count": len(original["files"]),
            "bytes": sum(entry["bytes"] for entry in original["files"].values()),
            "old_qualification_inherited": False, "provider_calls": 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    if args.receipt.exists() or args.receipt.is_symlink():
        raise FileExistsError(args.receipt)
    for root in (args.source, args.destination):
        if args.receipt.resolve().is_relative_to(root.resolve()):
            raise ValueError("Receipt must be outside source and view")
    result = create_business_view(args.source, args.destination)
    with args.receipt.open("x") as stream:
        json.dump(result, stream, sort_keys=True)


if __name__ == "__main__":
    main()
