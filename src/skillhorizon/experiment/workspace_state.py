"""Standalone stdlib logical workspace trees shared by host and guest collectors.

Only ordinary single-link files and directories are public. Callers must serialize
access and stop all guest writers before snapshotting; this is not a sandbox or
an atomic snapshot against concurrent writers. POSIX attributes are not state.
"""

from contextlib import contextmanager
import base64
import binascii
import json
import os
from pathlib import Path
import stat
import unicodedata


_ROOTS = ("inputs", "work", "outputs")
_CONFIG = ("max_file_bytes", "max_total_bytes", "max_entries", "max_depth", "max_response_chars")
_ERRORS = {
    "ARGUMENT_SCHEMA": "Supply exactly the declared tool fields with valid types.",
    "UNKNOWN_TOOL": "Unknown workspace tool.",
    "PATH": "Use a valid relative workspace path.",
    "UNSAFE_ENTRY": "Links and special files are not permitted.",
    "READ_ONLY": "Inputs and fixed root directories cannot be modified.",
    "NOT_FOUND": "The requested entry or parent directory does not exist.",
    "NOT_FILE": "The operation requires a regular file.",
    "NOT_DIRECTORY": "The operation requires a directory.",
    "EXISTS": "The destination already exists.",
    "NOT_EMPTY": "Only empty directories can be deleted.",
    "MATCH": "Edit requires a nonempty old string with exactly one match.",
    "TEXT": "File content must be valid UTF-8 text.",
    "QUOTA": "The workspace storage or depth quota would be exceeded.",
    "RESPONSE_LIMIT": "The complete response cannot fit the response quota.",
}


def _error(code):
    return {"ok": False, "error": {"code": code, "message": _ERRORS[code]}}


MIN_RESPONSE_CHARS = max(
    len(json.dumps(_error(code), sort_keys=True, separators=(",", ":"), ensure_ascii=True)) for code in _ERRORS
)


class WorkspaceError(ValueError):
    """A deterministic public rejection, also used for invalid initial trees."""

    def __init__(self, code):
        self.code = code
        super().__init__(_ERRORS[code])


class WorkspaceFatalError(RuntimeError):
    """Stop execution: filesystem or journal effects may already be committed."""


def normalize_workspace_config(config):
    if not isinstance(config, dict) or set(config) != set(_CONFIG):
        raise ValueError("Workspace config requires exactly the five quota fields")
    if any(type(config[key]) is not int or config[key] <= 0 for key in _CONFIG):
        raise ValueError("Workspace quotas must be positive integers")
    if config["max_response_chars"] < MIN_RESPONSE_CHARS:
        raise ValueError("Workspace response quota cannot hold the fixed error envelopes")
    return {key: config[key] for key in _CONFIG}


def _parts(path, allow_root=False):
    if allow_root and path == "":
        return ()
    if not isinstance(path, str) or not path:
        raise WorkspaceError("PATH")
    parts = path.split("/")
    if any(part in {"", ".", ".."} or any(
            char in "\\:" or unicodedata.category(char).startswith("C") for char in part) for part in parts):
        raise WorkspaceError("PATH")
    if any(len(part.encode("utf-8")) > 255 for part in parts):
        raise WorkspaceError("PATH")
    if parts[0] not in _ROOTS:
        raise WorkspaceError("PATH")
    return tuple(parts)


def _writable(path):
    parts = _parts(path)
    if parts[0] == "inputs" or len(parts) == 1:
        raise WorkspaceError("READ_ONLY")
    return parts


def _bytes(text):
    if not isinstance(text, str):
        raise WorkspaceError("TEXT")
    try:
        return text.encode("utf-8")
    except UnicodeError:
        raise WorkspaceError("TEXT") from None


def _file(payload):
    return {"type": "file", "bytes": len(payload), "content_base64": base64.b64encode(payload).decode("ascii")}


def _check_quota(tree, config):
    if (len(tree) > config["max_entries"]
            or any(len(path.split("/")) > config["max_depth"] for path in tree)
            or any(row.get("bytes", 0) > config["max_file_bytes"] for row in tree.values())
            or sum(row.get("bytes", 0) for row in tree.values()) > config["max_total_bytes"]):
        raise WorkspaceError("QUOTA")


def _directory(fd, name):
    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode):
        raise WorkspaceError("UNSAFE_ENTRY")
    if not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError("NOT_DIRECTORY")
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        opened = os.fstat(child)
        if not stat.S_ISDIR(opened.st_mode) or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise WorkspaceError("UNSAFE_ENTRY")
        return child
    except BaseException:
        os.close(child)
        raise


@contextmanager
def _root_fd(root):
    root = Path(root)
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise WorkspaceError("UNSAFE_ENTRY")
    if not stat.S_ISDIR(info.st_mode):
        raise WorkspaceError("NOT_DIRECTORY")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode) or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise WorkspaceError("UNSAFE_ENTRY")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _parent_fd(root_fd, path):
    fd = os.dup(root_fd)
    try:
        for part in _parts(path)[:-1]:
            next_fd = _directory(fd, part)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def _scan(root_fd, config):
    tree = {}
    total = 0

    def visit(fd, prefix):
        nonlocal total
        # Bound enumeration as well as file reads, including externally tampered trees.
        with os.scandir(fd) as entries:
            for entry in entries:
                path = prefix + entry.name
                _parts(path)
                if len(tree) >= config["max_entries"] or len(path.split("/")) > config["max_depth"]:
                    raise WorkspaceError("QUOTA")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    tree[path] = {"type": "directory"}
                    child = _directory(fd, entry.name)
                    try:
                        visit(child, path + "/")
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    limit = min(config["max_file_bytes"], config["max_total_bytes"] - total)
                    if info.st_size > limit:
                        raise WorkspaceError("QUOTA")
                    file_fd = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                    with os.fdopen(file_fd, "rb") as handle:
                        opened = os.fstat(handle.fileno())
                        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                                or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)):
                            raise WorkspaceError("UNSAFE_ENTRY")
                        payload = handle.read(limit + 1)
                        finished = os.fstat(handle.fileno())
                        if (finished.st_nlink != 1 or finished.st_size != opened.st_size
                                or finished.st_mtime_ns != opened.st_mtime_ns
                                or finished.st_ctime_ns != opened.st_ctime_ns):
                            raise WorkspaceError("UNSAFE_ENTRY")
                    if len(payload) > limit:
                        raise WorkspaceError("QUOTA")
                    total += len(payload)
                    tree[path] = _file(payload)
                else:
                    raise WorkspaceError("UNSAFE_ENTRY")

    visit(root_fd, "")
    if {path for path in tree if "/" not in path} != set(_ROOTS):
        raise WorkspaceError("PATH")
    if any(tree[name]["type"] != "directory" for name in _ROOTS):
        raise WorkspaceError("NOT_DIRECTORY")
    return {path: tree[path] for path in sorted(tree)}


def workspace_snapshot(root, config):
    """JSON-comparable logical path map, including directories and exact file bytes."""
    config = normalize_workspace_config(config)
    try:
        with _root_fd(root) as fd:
            return _scan(fd, config)
    except OSError:
        raise WorkspaceFatalError("Workspace snapshot I/O failed") from None


def validate_snapshot(tree, config):
    """Return a detached, sorted logical tree after strict schema and quota checks.

    Every parent and all three fixed roots must be explicit directories. File
    lengths are checked against strictly decoded, canonical base64 payloads.
    No metadata, links, implicit parents, or coercions are accepted.
    """
    config = normalize_workspace_config(config)
    if type(tree) is not dict:
        raise WorkspaceError("ARGUMENT_SCHEMA")
    if len(tree) > config["max_entries"]:
        raise WorkspaceError("QUOTA")
    validated = {}
    total = 0
    for path, row in tree.items():
        parts = _parts(path)
        if len(parts) > config["max_depth"]:
            raise WorkspaceError("QUOTA")
        if type(row) is not dict or type(row.get("type")) is not str:
            raise WorkspaceError("ARGUMENT_SCHEMA")
        if row["type"] == "directory":
            if set(row) != {"type"}:
                raise WorkspaceError("ARGUMENT_SCHEMA")
            validated[path] = {"type": "directory"}
        elif row["type"] == "file":
            if (set(row) != {"type", "bytes", "content_base64"}
                    or type(row["bytes"]) is not int or row["bytes"] < 0
                    or type(row["content_base64"]) is not str):
                raise WorkspaceError("ARGUMENT_SCHEMA")
            limit = min(config["max_file_bytes"], config["max_total_bytes"] - total)
            encoded = row["content_base64"]
            # Bound the decoder's allocation before trusting declared lengths.
            if row["bytes"] > limit or len(encoded) > 4 * ((limit + 2) // 3):
                raise WorkspaceError("QUOTA")
            if len(encoded) != 4 * ((row["bytes"] + 2) // 3):
                raise WorkspaceError("ARGUMENT_SCHEMA")
            try:
                payload = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise WorkspaceError("ARGUMENT_SCHEMA") from None
            if len(payload) > limit:
                raise WorkspaceError("QUOTA")
            if len(payload) != row["bytes"] or base64.b64encode(payload).decode("ascii") != encoded:
                raise WorkspaceError("ARGUMENT_SCHEMA")
            total += len(payload)
            validated[path] = _file(payload)
        else:
            raise WorkspaceError("UNSAFE_ENTRY")
    if {path for path in validated if "/" not in path} != set(_ROOTS):
        raise WorkspaceError("PATH")
    for path, row in validated.items():
        if path in _ROOTS and row["type"] != "directory":
            raise WorkspaceError("NOT_DIRECTORY")
        if "/" in path:
            parent = path.rsplit("/", 1)[0]
            if parent not in validated:
                raise WorkspaceError("PATH")
            if validated[parent]["type"] != "directory":
                raise WorkspaceError("NOT_DIRECTORY")
    _check_quota(validated, config)
    return {path: validated[path] for path in sorted(validated)}


def restore_snapshot(root, tree, config):
    """Create a fresh root and return its verified snapshot (not transactional).

    Directories are normalized to 0700 and files to 0600, without saved chmod
    attributes. The caller controls ownership and any guest read-only policy.
    On I/O failure a partial new root may remain; existing roots are never
    overwritten or cleaned. Callers may dispose of their own fresh staging root.
    """
    config = normalize_workspace_config(config)
    validated = validate_snapshot(tree, config)
    root = Path(root)
    if os.path.lexists(root):
        raise ValueError("Workspace root must be fresh")
    try:
        root.mkdir(mode=0o700, parents=True)
        with _root_fd(root) as root_fd:
            os.fchmod(root_fd, 0o700)
            for path, row in validated.items():
                with _parent_fd(root_fd, path) as parent:
                    name = _parts(path)[-1]
                    if row["type"] == "directory":
                        os.mkdir(name, mode=0o700, dir_fd=parent)
                        child = _directory(parent, name)
                        try:
                            os.fchmod(child, 0o700)
                        finally:
                            os.close(child)
                    else:
                        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=parent)
                        with os.fdopen(fd, "wb") as handle:
                            os.fchmod(handle.fileno(), 0o600)
                            handle.write(base64.b64decode(row["content_base64"], validate=True))
            actual = _scan(root_fd, config)
        if actual != validated:
            raise WorkspaceFatalError("Workspace restore verification failed; effects may already be committed")
        return actual
    except (OSError, WorkspaceError):
        raise WorkspaceFatalError("Workspace restore failed; effects may already be committed") from None
