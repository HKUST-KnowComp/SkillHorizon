"""Shared logical filesystem; controller-owned roots and journals are never public.

Calls must be serialized by the controller, including across runtime instances.
The three fixed directories count toward entry/depth quotas (root depth is zero).
"""

from collections.abc import Mapping
import base64
import os
from pathlib import Path
import shutil
import stat
import sys
from typing import Annotated
import uuid

from pydantic import Field, TypeAdapter

from ..errors import DomainError
from ..public_contract import Offset, validate_fields
from ..util import canonical_json
from .mcp_tools import canonicalize_mcp_schema, strict_compatible_mcp_schema


# Keep historical import names bound to the single stdlib implementation.
from .workspace_state import (
    MIN_RESPONSE_CHARS, WorkspaceError, WorkspaceFatalError,
    _CONFIG, _ERRORS, _ROOTS, _bytes, _check_quota, _directory, _error,
    _file, _parent_fd, _parts, _root_fd, _scan, _writable,
    normalize_workspace_config, restore_snapshot, validate_snapshot, workspace_snapshot,
)


Limit = Annotated[int, Field(ge=1, description="Maximum entries or Unicode characters to return.")]


def workspace_entries():
    """The sole field definitions for direct calls, MCP and provider schemas."""
    return [
        ("workspace_list", "List sorted direct children; path '' lists the fixed roots. Offsets count entries.",
         {"path": str, "offset": Offset, "limit": Limit}),
        ("workspace_read", "Read UTF-8 text; offsets and limits count Unicode characters. Follow next_offset.",
         {"path": str, "offset": Offset, "limit": Limit}),
        ("workspace_write", "Atomically create or replace a UTF-8 file in work or outputs; parent must exist.",
         {"path": str, "content": str}),
        ("workspace_edit", "Replace exactly one occurrence of a nonempty old string in a writable UTF-8 file.",
         {"path": str, "old": str, "new": str}),
        ("workspace_mkdir", "Create one writable directory; parent must exist.", {"path": str}),
        ("workspace_move", "Move a regular writable file; destination must not exist and parent must exist.",
         {"source": str, "destination": str}),
        ("workspace_delete", "Delete a writable regular file or empty directory; never recursively.", {"path": str}),
    ]


def workspace_tools():
    result = []
    for name, description, fields in workspace_entries():
        schema = canonicalize_mcp_schema({
            "type": "object", "properties": {key: TypeAdapter(value).json_schema() for key, value in fields.items()},
            "required": list(fields),
        })
        result.append({"type": "function", "name": name, "description": description,
                       "parameters": schema, "strict": strict_compatible_mcp_schema(schema)})
    return result


def initialize_workspace(root, public_inputs, config):
    """Create a fresh root from public text only; input keys are relative to inputs/."""
    config = normalize_workspace_config(config)
    if not isinstance(public_inputs, Mapping):
        raise ValueError("Public inputs must be a relative filename to text mapping")
    tree = {name: {"type": "directory"} for name in _ROOTS}
    for name, text in public_inputs.items():
        if not isinstance(name, str):
            raise WorkspaceError("PATH")
        path = "inputs/" + name
        parts = _parts(path)
        payload = _bytes(text)
        for end in range(1, len(parts)):
            parent = "/".join(parts[:end])
            if parent in tree and tree[parent]["type"] != "directory":
                raise WorkspaceError("EXISTS")
            tree[parent] = {"type": "directory"}
        if path in tree:
            raise WorkspaceError("EXISTS")
        tree[path] = _file(payload)
        _check_quota(tree, config)
    _check_quota(tree, config)
    root = Path(root)
    if os.path.lexists(root):
        raise ValueError("Workspace root must be fresh")
    created = False
    try:
        root.mkdir(parents=True)
        created = True
        for path, row in sorted(tree.items()):
            target = root / path
            if row["type"] == "directory":
                target.mkdir()
            else:
                target.write_bytes(base64.b64decode(row["content_base64"]))
        return workspace_snapshot(root, config)
    except OSError:
        raise WorkspaceFatalError("Workspace initialization I/O failed") from None
    finally:
        # Only remove our new tree when construction did not complete successfully.
        if created and sys.exc_info()[0] is not None:
            try:
                shutil.rmtree(root)
            except OSError:
                raise WorkspaceFatalError("Workspace initialization cleanup failed") from None


def _atomic_write(parent_fd, name, payload):
    temporary = ".workspace-" + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


class WorkspaceRuntime:
    def __init__(self, root, journal, config):
        self.config = normalize_workspace_config(config)
        workspace_snapshot(root, self.config)
        self.root = Path(root).absolute()
        journal = Path(journal).absolute()
        if journal.is_symlink():
            raise ValueError("Workspace journal must not be a link")
        # Controller paths may use OS aliases such as /tmp -> /private/tmp.
        self.journal = journal.parent.resolve() / journal.name
        resolved_root, resolved_journal = self.root.resolve(), self.journal.resolve()
        if resolved_journal.is_relative_to(resolved_root) or resolved_root.is_relative_to(resolved_journal):
            raise ValueError("Workspace journal must be controller-only and outside the workspace")
        self._failed = False
        self._fields = {name: fields for name, _, fields in workspace_entries()}
        self._check_journal()

    def _check_journal(self):
        for path in (self.journal, *self.journal.parents):
            if path.is_symlink():
                raise ValueError("Workspace journal must not traverse links")
        if self.journal.exists():
            info = self.journal.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Workspace journal must be a regular unlinked file")

    def _append(self, name, arguments, output, *, execution=None):
        self._check_journal()
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        record = {"name": name, "arguments": arguments, "output": output}
        if execution is not None:
            record["execution"] = execution
        payload = canonical_json(record) + "\n"
        fd = os.open(self.journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Invalid journal entry")
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    def _fits(self, output):
        return len(canonical_json(output)) <= self.config["max_response_chars"]

    def _success(self, data):
        output = {"ok": True, "data": data}
        if not self._fits(output):
            raise WorkspaceError("RESPONSE_LIMIT")
        return output

    def _page(self, values, offset, limit, key):
        available = max(0, len(values) - offset)
        size = min(limit, available)

        def envelope(count):
            end = offset + count
            return {"ok": True, "data": {key: values[offset:end], "offset": offset, "total": len(values),
                    "next_offset": end if end < len(values) else None, "truncated": end < len(values)}}

        output = envelope(size)
        if self._fits(output):
            return output
        # The full-page null next_offset is checked above; partial pages grow monotonically.
        low, high = 1, size - 1
        best = None
        while low <= high:
            middle = (low + high) // 2
            candidate = envelope(middle)
            if self._fits(candidate):
                best, low = candidate, middle + 1
            else:
                high = middle - 1
        if best is None:
            raise WorkspaceError("RESPONSE_LIMIT")
        return best

    def call(self, name, args):
        if self._failed:
            raise WorkspaceFatalError("Workspace runtime stopped after a fatal failure")
        try:
            if not isinstance(name, str) or name not in self._fields:
                raise WorkspaceError("UNKNOWN_TOOL")
            try:
                arguments = validate_fields(args, self._fields[name])
            except DomainError:
                raise WorkspaceError("ARGUMENT_SCHEMA") from None
            with _root_fd(self.root) as fd:
                tree = _scan(fd, self.config)
                output = self._execute(fd, tree, name, arguments)
        except WorkspaceError as exc:
            output = _error(exc.code)
        except OSError:
            self._failed = True
            raise WorkspaceFatalError("Workspace I/O failed; effects may already be committed") from None
        try:
            # Preserve dispatched arguments, not coercions or host-dependent diagnostics.
            self._append(name, args, output)
        except Exception:
            self._failed = True
            raise WorkspaceFatalError("Workspace journal failed; effects may already be committed") from None
        return output

    def _execute(self, fd, tree, name, args):
        path = args.get("path", args.get("source"))
        parts = _parts(path, allow_root=name == "workspace_list")
        if name == "workspace_list":
            if path:
                self._require(tree, path, "directory")
            prefix = path + "/" if path else ""
            rows = [{"name": item[len(prefix):], "type": row["type"]} for item, row in tree.items()
                    if item.startswith(prefix) and "/" not in item[len(prefix):]]
            return self._page(rows, args["offset"], args["limit"], "items")
        if name == "workspace_read":
            return self._page(self._text(tree, path), args["offset"], args["limit"], "content")
        _writable(path)
        parent = "/".join(parts[:-1])
        self._require(tree, parent, "directory")
        proposed = dict(tree)
        data = {"path": path}
        if name in {"workspace_write", "workspace_edit"}:
            if path in tree:
                self._require(tree, path, "file")
            if name == "workspace_edit":
                text = self._text(tree, path)
                old = args["old"]
                first = text.find(old)
                if not old or first < 0 or text.find(old, first + 1) >= 0:
                    raise WorkspaceError("MATCH")
                content = text[:first] + args["new"] + text[first + len(old):]
            else:
                content = args["content"]
            payload = _bytes(content)
            proposed[path] = _file(payload)
            data["bytes"] = len(payload)
        elif name == "workspace_mkdir":
            if path in tree:
                raise WorkspaceError("EXISTS")
            proposed[path] = {"type": "directory"}
        elif name == "workspace_move":
            self._require(tree, path, "file")
            destination = args["destination"]
            target_parts = _writable(destination)
            if destination in tree:
                raise WorkspaceError("EXISTS")
            self._require(tree, "/".join(target_parts[:-1]), "directory")
            proposed[destination] = proposed.pop(path)
            data = {"source": path, "destination": destination}
        elif name == "workspace_delete":
            self._require(tree, path)
            if any(item.startswith(path + "/") for item in tree):
                raise WorkspaceError("NOT_EMPTY")
            del proposed[path]
        _check_quota(proposed, self.config)
        output = self._success(data)
        with _parent_fd(fd, path) as parent_fd:
            if name in {"workspace_write", "workspace_edit"}:
                _atomic_write(parent_fd, parts[-1], payload)
            elif name == "workspace_mkdir":
                os.mkdir(parts[-1], dir_fd=parent_fd)
            elif name == "workspace_delete":
                operation = os.rmdir if tree[path]["type"] == "directory" else os.unlink
                operation(parts[-1], dir_fd=parent_fd)
            elif name == "workspace_move":
                with _parent_fd(fd, destination) as destination_fd:
                    # link+unlink is a portable no-clobber move for same-filesystem regular files.
                    # If unlink fails after link, propagate fatal without claiming rollback.
                    os.link(parts[-1], target_parts[-1], src_dir_fd=parent_fd,
                            dst_dir_fd=destination_fd, follow_symlinks=False)
                    os.unlink(parts[-1], dir_fd=parent_fd)
        return output

    @staticmethod
    def _require(tree, path, kind=None):
        if path not in tree:
            raise WorkspaceError("NOT_FOUND")
        if kind and tree[path]["type"] != kind:
            raise WorkspaceError("NOT_FILE" if kind == "file" else "NOT_DIRECTORY")

    def _text(self, tree, path):
        self._require(tree, path, "file")
        try:
            return base64.b64decode(tree[path]["content_base64"]).decode("utf-8")
        except UnicodeError:
            raise WorkspaceError("TEXT") from None
