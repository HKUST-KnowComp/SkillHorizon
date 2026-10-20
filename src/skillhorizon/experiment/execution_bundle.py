"""Private production wiring for bounded Python effects, never a model tool API."""

import os
import json
import math
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
import uuid

from ..util import canonical_json, load_json
from .execution_workspace import ExecutionWorkspace
from .execution_replay import read_execution_records
from .python_execution import normalize_python_execution
from .workspace import WorkspaceFatalError
from .workspace_state import workspace_snapshot


JOURNAL = "workspace_actions.jsonl"
CONTROL = "execution_control.json"
RECORDS = "execution_replay.jsonl"
STATE = "execution_state.json"
RELEASE = "execution_release.json"


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="ascii") as handle:
            handle.write(canonical_json(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def execution_config(bundle):
    bundle = Path(bundle)
    manifest = load_json(bundle / "manifest.json")
    public = load_json(bundle / "public/task.json")
    config = manifest.get("python_execution")
    if canonical_json(public.get("python_execution")) != canonical_json(config):
        raise ValueError("Public Python execution configuration differs")
    if config is not None:
        if manifest.get("workspace") is None:
            raise ValueError("Python execution requires workspace")
        config = normalize_python_execution(config, manifest["workspace"])
        limit = manifest.get("execution_max_actions")
        if (type(limit) is not int or not 1 <= limit <= 100000
                or type(public.get("execution_max_actions")) is not int or public["execution_max_actions"] != limit):
            raise ValueError("Python execution requires a frozen controller action budget")
    elif manifest.get("execution_max_actions") is not None or public.get("execution_max_actions") is not None:
        raise ValueError("Undeclared execution action budget")
    return config


def ensure_execution_known(bundle):
    bundle = Path(bundle)
    # The pending marker wins even if configuration or the last receipt is lost.
    if os.path.lexists(bundle / (JOURNAL + ".execution_pending")):
        raise WorkspaceFatalError("Execution file state is unknown; continuation and delivery are forbidden")


def failure_execution_accounting(bundle):
    """Keep last confirmed process resources without treating old files as current."""
    result = {"state": "unknown", "physical_executions": None, "backend_invocations": None,
              "unknown_execution_attempts": None, "replayable": False}
    try:
        limits = execution_limits(bundle)
        path = Path(bundle) / (JOURNAL + ".execution_receipt")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            import stat
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limits.receipt_bound:
                return result
            payload = handle.read(limits.receipt_bound + 1)
        if len(payload) > limits.receipt_bound:
            return result
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("Duplicate receipt key")
                value[key] = item
            return value
        receipt = json.loads(payload, object_pairs_hook=unique)
        usage = receipt["usage"]
        keys = ("physical_executions", "backend_invocations", "unknown_execution_attempts",
                "calls", "actions", "records", "wall_seconds", "truncated_streams", "captured_bytes")
        if (any(type(usage.get(k)) is not int or not 0 <= usage[k] <= limits.max_actions
                for k in ("physical_executions", "backend_invocations", "unknown_execution_attempts", "calls", "actions", "records"))
                or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("truncated_streams", "captured_bytes"))
                or type(usage.get("wall_seconds")) not in (int, float)
                or not math.isfinite(usage["wall_seconds"]) or usage["wall_seconds"] < 0
                or usage["calls"] > limits.config["max_calls"]
                or usage["physical_executions"] + usage["unknown_execution_attempts"] != usage["backend_invocations"]
                or usage["backend_invocations"] > usage["calls"] or usage["records"] > usage["actions"]
                or usage["truncated_streams"] > 2 * usage["calls"]
                or usage["captured_bytes"] > usage["calls"] * limits.files.config["max_total_bytes"]):
            return result
        result.update({k: usage[k] for k in keys})
        result["scope"] = "Last controller receipt only; pending effects remain unknown and non-replayable"
    except Exception:
        pass
    return result


def execution_limits(bundle):
    bundle = Path(bundle)
    config = execution_config(bundle)
    if config is None:
        return None
    manifest = load_json(bundle / "manifest.json")
    with TemporaryDirectory(prefix="execution-envelope-") as temporary:
        # Reuse the accepted consumer's exact bounds; never derive or substitute
        # a different total-tool limit while loading a historical episode.
        return ExecutionWorkspace(bundle / "workspace_initial", Path(temporary) / JOURNAL,
            manifest["workspace"], config, max_actions=manifest["execution_max_actions"], ptc=manifest.get("ptc"))


def preflight_execution(bundle, max_actions=None, *, check_backend=True):
    ensure_execution_known(bundle)
    limits = execution_limits(bundle)
    if limits is None:
        return
    if max_actions is not None and (type(max_actions) is not int or limits.max_actions != max_actions):
        raise ValueError("Frozen execution max_actions differs from actual tool budget")
    if check_backend:
        limits.backend.check_available()


def execution_records(bundle):
    ensure_execution_known(bundle)
    limits = execution_limits(bundle)
    if limits is None:
        raise ValueError("Execution records require an explicit declaration")
    path = Path(bundle) / JOURNAL
    if not path.exists():
        # A zero-action episode has no journal, but must have a server receipt.
        state = load_json(Path(bundle) / STATE)
        if any(type(state["usage"].get(k)) is not int or state["usage"][k] != 0 for k in ("actions", "records")):
            raise ValueError("Missing execution action journal")
        records = []
    else:
        records = read_execution_records(path, max_bytes=limits.journal_bound,
            max_record_bytes=limits.record_bound, max_actions=limits.max_actions)
    tree = validate_ordered_records(bundle, records, limits)
    if tree != workspace_snapshot(Path(bundle) / "agent_workspace", limits.files.config):
        raise ValueError("Execution recorded tree differs from the current workspace")
    ensure_execution_known(bundle)
    return records


def validate_ordered_records(bundle, records, limits):
    # Decoding is not effect authority. Validate the entire sequence, including
    # an unconsumed private replay tail, without ever invoking the backend.
    with TemporaryDirectory(prefix="execution-record-validation-") as temporary:
        root = Path(temporary)
        shutil.copytree(Path(bundle) / "workspace_initial", root / "workspace")
        runtime = ExecutionWorkspace(root / "workspace", root / JOURNAL,
            limits.files.config, limits.config, max_actions=limits.max_actions,
            records=records, replay_mode="full", ptc=limits.ptc)
        for row in records:
            runtime.call(row["name"], row["arguments"])
        runtime.finish_replay()
        return workspace_snapshot(root / "workspace", limits.files.config)


def private_replay_records(bundle, limits):
    control = load_json(Path(bundle) / CONTROL)
    if set(control) != {"mode"} or control["mode"] not in {"full", "prefix"}:
        raise ValueError("Invalid private replay mode")
    records = read_execution_records(Path(bundle) / RECORDS, max_bytes=limits.journal_bound,
        max_record_bytes=limits.record_bound, max_actions=limits.max_actions)
    validate_ordered_records(bundle, records, limits)
    return control["mode"], records


def prepare_execution_replay(bundle, source, mode):
    if mode not in {"full", "prefix"}:
        raise ValueError("Explicit replay mode required")
    bundle, source = Path(bundle), Path(source)
    from .business_binding import business_binding
    binding = business_binding(bundle)
    if binding != business_binding(source):
        raise ValueError("Business replay declaration differs")
    if binding["business_transport"] == "managed_worker":
        from .managed_session import require_world_quiescent, read_private, CONTROL as BUSINESS_CONTROL
        require_world_quiescent(source)
        original = read_private(source / BUSINESS_CONTROL)
        target = bundle / "business_replay.json"
        if target.exists():
            raise ValueError("Business replay identity must start fresh")
        atomic_json(target, {"bundle_id": original["bundle_id"], "episode_id": original["episode_id"],
                            "source_invocation_id": original["invocation_id"], "mode": mode})
    if execution_config(bundle) is None:
        return
    if execution_config(bundle) != execution_config(source):
        raise ValueError("Execution replay declaration differs")
    execution_accounting(source)
    records = execution_records(source)
    if any((bundle / name).exists() for name in (CONTROL, RECORDS, STATE, RELEASE, JOURNAL, "agent_workspace")):
        raise ValueError("Execution replay must start fresh")
    with (bundle / RECORDS).open("x", encoding="ascii") as handle:
        for row in records:
            handle.write(canonical_json(row) + "\n")
    atomic_json(bundle / CONTROL, {"mode": mode})


def finish_execution_replay(bundle):
    if execution_config(bundle) is None:
        return
    ensure_execution_known(bundle)
    execution_accounting(bundle)
    state = load_json(Path(bundle) / STATE)
    if state["mode"] not in {"full", "prefix"} or state["replay_complete"] is not True:
        raise ValueError("Execution replay boundary is incomplete or unlocked")
    from .business_binding import business_binding
    if business_binding(bundle)["ptc"] is not None and state.get("business_reconstruction_complete") is not True:
        raise ValueError("PTC business reconstruction has not been completed by the controller")


def authorize_execution_live(bundle):
    """Called by the private controller only after C validation and admission."""
    if execution_config(bundle) is None:
        return
    finish_execution_replay(bundle)
    if load_json(Path(bundle) / STATE)["mode"] != "prefix":
        raise ValueError("Full replay cannot be released")
    atomic_json(Path(bundle) / RELEASE, {"authorize_live": True})


class ProductionExecution:
    def __init__(self, bundle, root, workspace, execution, *, business=None):
        self.bundle = Path(bundle)
        self.business = business
        from .business_binding import business_binding
        self.ptc = business_binding(bundle)["ptc"]
        if self.ptc is not None and business is None:
            raise ValueError("PTC production requires the existing managed business client")
        self.business_parents = self.business_children = 0
        if any((self.bundle / name).exists() for name in (STATE, RELEASE)):
            raise ValueError("Execution controller state must start fresh")
        if (self.bundle / CONTROL).exists() != (self.bundle / RECORDS).exists():
            raise ValueError("Private replay declaration and records must both be present")
        manifest = load_json(self.bundle / "manifest.json")
        records = mode = None
        if (self.bundle / CONTROL).exists():
            limits = execution_limits(bundle)
            mode, records = private_replay_records(bundle, limits)
        self.runtime = ExecutionWorkspace(root, self.bundle / JOURNAL, workspace, execution,
            max_actions=manifest["execution_max_actions"], records=records, replay_mode=mode, ptc=self.ptc)
        if mode is None:
            self.runtime.backend.check_available()
        self.publish()

    def publish(self):
        runtime = self.runtime
        complete = runtime.mode is not None and runtime.index == len(runtime.records)
        if complete:
            runtime.finish_replay()
        try:
            state = {"mode": runtime.mode, "replay_complete": complete, "usage": runtime.usage}
            if self.ptc is not None:
                expected = sum(row["name"] == "ptc_execute" for row in (runtime.records or []))
                state.update(business_reconstructed_parents=self.business_parents,
                             business_reconstructed_children=self.business_children,
                             business_reconstruction_complete=bool(complete and self.business_parents == expected))
            atomic_json(self.bundle / STATE, state)
        except Exception:
            runtime.failed = True
            runtime._mark_pending()
            raise WorkspaceFatalError("Execution controller state persistence is unknown") from None

    def call(self, name, arguments, *, ptc_parent=None):
        ensure_execution_known(self.bundle)
        if (self.bundle / RELEASE).exists() and self.runtime.mode is not None:
            release = load_json(self.bundle / RELEASE)
            if set(release) != {"authorize_live"} or release["authorize_live"] is not True:
                raise ValueError("Invalid controller release")
            self.runtime.authorize_live()
        if name == "ptc_execute":
            result = self._ptc(arguments, ptc_parent)
        else:
            if ptc_parent is not None:
                raise ValueError("Unexpected private PTC context")
            result = self.runtime.call(name, arguments)
        self.publish()
        # MCP also exposes a JSON text projection. Keep its object order stable
        # across live results and canonical-journal decoding during replay.
        return json.loads(canonical_json(result))

    def _ptc(self, arguments, parent):
        from .managed_session import read_private, matched, PARENT, STATE as BUSINESS_STATE
        from .ptc_budget import GrantStore, make_grant
        if self.ptc is None or self.business is None or type(parent) is not dict:
            raise ValueError("Missing managed PTC execution context")
        current = matched(read_private(self.bundle / PARENT), self.business.control)
        state = matched(read_private(self.bundle / BUSINESS_STATE), self.business.control)
        if (current != parent or state.get("phase") != "active" or state.get("active_call_id") != parent["call_id"]
                or parent["name"] != "ptc_execute" or canonical_json(parent["arguments"]) != canonical_json(arguments)
                or parent["workspace_index"] != self.runtime.usage["actions"] + 1):
            raise ValueError("PTC context differs from the actual native invocation")
        if self.runtime.mode is not None:
            return self._reconstruct_ptc(arguments, parent)
        if parent["reconstruct"]:
            raise ValueError("Reconstruction context cannot execute new Python")
        expected = make_grant(bundle_id=self.business.control["bundle_id"], episode_id=self.business.control["episode_id"],
            parent_id=parent["call_id"], top_level_index=parent["tool_index"],
            prior_nested=self.runtime.usage["nested_requests_admitted"],
            max_tool_calls=self.runtime.max_actions, config=self.ptc, deadline_monotonic=parent["deadline_monotonic"])
        if parent["grant"] != expected:
            raise ValueError("Private grant differs from native position or actual remaining budget")
        grant = GrantStore(self.bundle / "ptc_grants", self.ptc).consume(expected)
        context = {"grant": grant, **{key: parent[key] for key in ("turn", "actor_turn", "workspace_index")}}
        backend = _ProductionPTCBackend(self.runtime.config, self.runtime.files.config, self.ptc, self.business)
        return self.runtime.call("ptc_execute", arguments, ptc_parent=context, ptc_backend=backend)

    def _reconstruct_ptc(self, arguments, parent):
        """Only this fresh-worker production path has business replay authority."""
        if not parent["reconstruct"] or not (self.bundle / "business_replay.json").exists():
            raise ValueError("Historical PTC requires explicit fresh-world authority")
        runtime = self.runtime
        if runtime.index >= len(runtime.records):
            raise ValueError("No historical PTC remains")
        record = runtime.validate_record(runtime.records[runtime.index])
        if record["name"] != "ptc_execute" or canonical_json(record["arguments"]) != canonical_json(arguments):
            raise ValueError("Historical PTC arguments differ")
        historical = record["execution"]["ptc"]["parent"]
        if (historical["grant"]["parent_id"] != parent["call_id"]
                or any(historical["grant"][key] != self.business.control[key] for key in ("bundle_id", "episode_id"))
                or historical["grant"]["top_level_index"] != parent["tool_index"]
                or any(historical[key] != parent[key] for key in ("turn", "actor_turn", "workspace_index"))):
            raise ValueError("Historical PTC native parent binding differs")
        request = None
        try:
            for event in record["execution"]["ptc"]["events"]:
                if event["event"] == "admit":
                    request = event["request"]
                elif event["event"] == "result":
                    actual = self.business.client.call(request["name"], request["arguments"],
                                                       deadline=parent["deadline_monotonic"] - 15)
                    if (self.business.client.last_reply.dispatched is not True
                            or canonical_json(actual) != canonical_json(event["result"])):
                        raise ValueError("Reconstructed business result differs from the original observation")
                    self.business_children += 1
            output = runtime.apply_validated_parent_files(record)
            self.business_parents += 1
            return output
        except BaseException:
            runtime.failed = True
            if not os.path.lexists(runtime.pending):
                runtime._mark_pending()
            raise


class _ProductionPTCBackend:
    def __init__(self, execution, workspace, ptc, business):
        self.execution, self.workspace, self.ptc, self.business = execution, workspace, ptc, business

    def execute(self, before, code, *, budget, deadline):
        from .ptc_bridge import PTCBridge
        from .ptc_execution import DockerPTC
        bridge = PTCBridge(budget=budget, tools=self.business.client.tools, client=self.business.client)
        return DockerPTC(self.execution, self.workspace, self.ptc).execute(before, code, bridge=bridge, deadline=deadline)


class ExecutionGuardedBusiness:
    def __init__(self, bundle, business):
        self.bundle, self.business = bundle, business
        if hasattr(business, "metadata"):
            self.metadata = business.metadata

    def call(self, name, arguments):
        ensure_execution_known(self.bundle)
        return self.business.call(name, arguments)


def logical_execution_usage(records, ptc=None):
    rows = [r for r in records if r["name"] in {"python_execute", "ptc_execute"}]
    started = [r for r in rows if r["execution"]["started"]]
    result = {"tool_calls": len(rows), "calls": len(started), "public_rejections": len(rows) - len(started),
        "wall_seconds": sum(r["output"]["data"]["wall_seconds"] for r in started),
        "truncated_streams": sum(r["output"]["data"][key + "_truncated"] for r in started for key in ("stdout", "stderr")),
        "captured_bytes": sum(node.get("bytes", 0) for r in started for node in r["execution"]["after"].values())}
    if ptc is not None:
        from .ptc_budget import validate_child_events
        parents = [r["execution"]["ptc"] for r in rows if r["name"] == "ptc_execute"]
        counts = [validate_child_events(row["parent"]["grant"], ptc, row["events"]) for row in parents]
        result["ptc_calls"] = sum(r["name"] == "ptc_execute" for r in started)
        for key in ("nested_requests_admitted", "nested_dispatch_intents", "nested_business_dispatched",
                    "nested_dispatch_unknown", "nested_rejections", "bridge_exhaustion_events", "bridge_bytes"):
            result[key] = sum(row[key] for row in counts)
    return result


def execution_accounting(bundle):
    """Logical usage is reconstructed; physical usage belongs to this invocation."""
    bundle = Path(bundle)
    if execution_config(bundle) is None:
        return {}
    ensure_execution_known(bundle)
    records = execution_records(bundle)
    from .business_binding import business_binding
    ptc = business_binding(bundle)["ptc"]
    logical = logical_execution_usage(records, ptc)
    state = load_json(bundle / STATE)
    usage = state["usage"]
    expected = {"actions": len(records), "records": len(records),
        **{key: logical[key] for key in ("calls", "wall_seconds", "truncated_streams", "captured_bytes")}}
    if ptc is not None:
        expected.update({key: value for key, value in logical.items()
                         if key not in {"tool_calls", "public_rejections"}})
    if (type(usage.get("wall_seconds")) not in (int, float)
            or not math.isfinite(usage["wall_seconds"]) or usage["wall_seconds"] < 0):
        raise ValueError("Execution wall-clock accounting must be finite and nonnegative")
    if any((type(usage.get(k)) not in (int, float) if k == "wall_seconds" else type(usage.get(k)) is not int)
           or usage.get(k) != v for k, v in expected.items()):
        raise ValueError("Execution accounting differs from trusted ordered effects")
    historical = 0
    prefix = []
    if (bundle / CONTROL).exists() != (bundle / RECORDS).exists():
        raise ValueError("Private replay declaration and records must both be present")
    if (bundle / CONTROL).exists():
        limits = execution_limits(bundle)
        mode, replay = private_replay_records(bundle, limits)
        prefix = replay[:min(len(records), len(replay))]
        if canonical_json(records[:len(prefix)]) != canonical_json(prefix):
            raise ValueError("Execution prefix differs from private replay records")
        if state["mode"] == mode:
            if len(records) > len(replay) or state["replay_complete"] is not (len(records) == len(replay)):
                raise ValueError("Execution replay completion differs from the full private sequence")
        elif state["mode"] is None and mode == "prefix":
            release = load_json(bundle / RELEASE)
            if (set(release) != {"authorize_live"} or release["authorize_live"] is not True
                    or len(records) < len(replay) or state["replay_complete"] is not False):
                raise ValueError("Live execution lacks a complete explicitly released prefix")
        else:
            raise ValueError("Execution replay mode differs from the controller boundary")
        historical = logical_execution_usage(prefix)["calls"]
    elif state["mode"] is not None or state["replay_complete"] is not False:
        raise ValueError("Undeclared execution replay state")
    reconstructed = {}
    if ptc is not None:
        historical_ptc = [row for row in prefix if row["name"] == "ptc_execute"]
        reconstructed = {
            "business_reconstructed_parents": len(historical_ptc),
            "business_reconstructed_children": sum(
                event["event"] == "result" for row in historical_ptc for event in row["execution"]["ptc"]["events"])}
        if (any(type(state.get(key)) is not int or state[key] != value for key, value in reconstructed.items())
                or state.get("business_reconstruction_complete") is not state["replay_complete"]):
            raise ValueError("Business reconstruction differs from the consumed historical prefix")
    physical = logical["calls"] - historical
    if any(type(usage.get(k)) is not int or usage[k] != v for k, v in {
            "physical_executions": physical, "backend_invocations": physical, "unknown_execution_attempts": 0}.items()):
        raise ValueError("Physical execution accounting differs from replay history")
    return {"execution_usage": logical, "execution_runtime": {"state": "known",
        "physical_executions": physical, "backend_invocations": physical, "unknown_execution_attempts": 0,
        **reconstructed}}
