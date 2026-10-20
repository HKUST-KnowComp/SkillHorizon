"""Small durable records and portable path validation; no credential discovery."""
import json
import os
from pathlib import Path, PurePosixPath


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))


def read(path):
    return decode(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def inside(root, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError("Expected a bundle-relative path")
    part = PurePosixPath(relative)
    if part.is_absolute() or ".." in part.parts or "\\" in relative or ":" in relative:
        raise ValueError("Path escapes bundle")
    root = Path(root).resolve()
    path = root.joinpath(*part.parts)
    if any(p.is_symlink() for p in (path, *path.parents) if p != root.parent):
        raise ValueError("Symlink inputs are not supported")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Path escapes bundle")
    return path
