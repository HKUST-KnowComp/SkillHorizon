"""Optional public delivery checks over controller-captured, stopped workspaces.

The caller must stop the agent and MCP before capture/business verification and
keep them stopped through scoring. Persist snapshots outside agent_workspace.
This module neither persists evidence nor independently verifies business state.
"""

import base64
import binascii
import json
from pathlib import Path

from ..fieldops import FORMAT as OPERATIONS_FORMAT, PROCURE_INTERFACE_REVISION
from ..util import load_json
from .workspace import normalize_workspace_config, workspace_snapshot


CONTRACT = "procureops_handoff_v1"
HANDOFF_PATH = "outputs/handoff.json"


def normalize_delivery_contract(value, task=None, workspace=None):
    """Validate an optional contract; with task, require compatible file support.

Without task this validates the identifier only (and quotas if supplied).
With task, accept only current Operations ProcureOps tasks and require quotas.
Invalid configuration raises ValueError before any agent is started.
"""
    if value is None:
        return None
    if type(value) is not str or value != CONTRACT:
        raise ValueError("Unknown delivery contract")
    if workspace is not None:
        normalize_workspace_config(workspace)
    if task is not None:
        config = task.get("config") if isinstance(task, dict) else None
        if (not isinstance(config, dict)
                or task.get("format") != OPERATIONS_FORMAT
                or config.get("family") not in ("joint_procurement", "split_fulfillment")
                or task.get("observation_protocol") != PROCURE_INTERFACE_REVISION):
            raise ValueError("Delivery contract requires a current ProcureOps task")
        if workspace is None:
            raise ValueError("Delivery contract requires an enabled workspace")
    return value


def public_delivery_instruction(contract):
    """Return answer-free public requirements, or no additional instruction."""
    if normalize_delivery_contract(contract) is None:
        return ""
    return (
        "Delivery contract procureops_handoff_v1: before finishing, save "
        "outputs/handoff.json using the workspace tools. It must be a UTF-8 JSON "
        "object with exactly four keys: task_id (the current task ID, a string), "
        "acceptance_id (a string), receipt_ids (an array of unique strings), and "
        "delivered (an object mapping request ID strings to nonnegative integer "
        "quantities; booleans and floating-point quantities are not allowed). "
        "Do not include extra or duplicate JSON keys at any nesting level. "
        "Use the actual business records from this episode: acceptance_id must "
        "equal the current acceptance pointer when the episode stops, and "
        "receipt_ids and delivered must match that acceptance artifact exactly "
        "(receipt order does not matter). Inspect read_project and read_artifact "
        "as needed. Calling accept-deliveries again creates a new acceptance, "
        "so update this file; an older still-readable acceptance is not current. "
        "Other files and work notes are not graded by this delivery contract. "
        "The original business goal remains required."
    )


def collect_delivery_snapshot(bundle, config):
    from .managed_session import require_world_quiescent
    require_world_quiescent(bundle)
    """Capture B's bounded exact-byte tree after shutdown; never write evidence.

    Returns {valid: bool, tree: dict | None, errors: list[str]}. Capture failures
    are infrastructure uncertainty, not evidence of a bad handoff. Error codes
    deliberately omit exception messages and host paths.
    """
    try:
        from .execution_bundle import ensure_execution_known
        ensure_execution_known(bundle)
        tree = workspace_snapshot(Path(bundle) / "agent_workspace", config)
    except Exception:
        return {"valid": False, "tree": None, "errors": ["SNAPSHOT_UNAVAILABLE"]}
    return {"valid": True, "tree": tree, "errors": []}


def _result(success, *errors, applicable=True):
    return {"applicable": applicable, "valid": success is not None,
            "task_success": success, "errors": list(errors)}


class _HandoffError(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _HandoffError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _invalid_constant(value):
    raise _HandoffError("INVALID_JSON")


def _string(value):
    if type(value) is not str:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _quantities(value):
    return (isinstance(value, dict) and all(_string(key) and type(quantity) is int
            and quantity >= 0 for key, quantity in value.items()))


def _receipts(value):
    return (isinstance(value, list) and all(_string(item) for item in value)
            and len(value) == len(set(value)))


def _read_handoff(snapshot):
    # An invalid capture envelope is not a malformed agent-authored document.
    if (not isinstance(snapshot, dict) or snapshot.get("valid") is not True
            or snapshot.get("errors") != [] or not isinstance(snapshot.get("tree"), dict)):
        raise RuntimeError("Invalid snapshot envelope")
    tree = snapshot["tree"]
    if any(tree.get(root) != {"type": "directory"} for root in ("inputs", "work", "outputs")):
        raise RuntimeError("Incomplete snapshot tree")
    if HANDOFF_PATH not in tree:
        raise _HandoffError("MISSING_HANDOFF")
    row = tree[HANDOFF_PATH]
    if row == {"type": "directory"}:
        raise _HandoffError("HANDOFF_NOT_FILE")
    if (not isinstance(row, dict) or set(row) != {"type", "bytes", "content_base64"}
            or row["type"] != "file" or type(row["bytes"]) is not int or row["bytes"] < 0
            or type(row["content_base64"]) is not str):
        raise RuntimeError("Invalid captured file entry")
    try:
        payload = base64.b64decode(row["content_base64"], validate=True)
    except (ValueError, binascii.Error):
        raise RuntimeError("Invalid captured bytes") from None
    if len(payload) != row["bytes"]:
        raise RuntimeError("Captured byte count mismatch")
    try:
        text = payload.decode("utf-8")
    except UnicodeError:
        raise _HandoffError("INVALID_UTF8") from None
    try:
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except _HandoffError:
        raise
    except (ValueError, RecursionError):
        raise _HandoffError("INVALID_JSON") from None
    if (not isinstance(value, dict)
            or set(value) != {"task_id", "acceptance_id", "receipt_ids", "delivered"}
            or not _string(value["task_id"]) or not _string(value["acceptance_id"])
            or not _receipts(value["receipt_ids"]) or not _quantities(value["delivered"])):
        raise _HandoffError("HANDOFF_SCHEMA")
    return value


def _current_acceptance(state, task_id):
    """Extract the current record from the actual OperationsRuntime.project shape."""
    if (not isinstance(state, dict) or not _string(task_id)
            or state.get("project_id") != task_id or state.get("domain") != "procureops"
            or "acceptance" not in state or not isinstance(state.get("artifacts"), dict)):
        raise ValueError("Unverified task/state identity or shape")
    identifier = state["acceptance"]
    if identifier is None:
        return None
    if not _string(identifier) or not identifier:
        raise ValueError("Invalid acceptance pointer")
    artifact = state["artifacts"].get(identifier)
    if (not isinstance(artifact, dict) or artifact.get("artifact_id") != identifier
            or artifact.get("kind") != "acceptance" or not isinstance(artifact.get("data"), dict)):
        raise ValueError("Invalid current acceptance artifact")
    data = artifact["data"]
    if not _receipts(data.get("receipt_ids")) or not _quantities(data.get("delivered")):
        raise ValueError("Invalid acceptance data")
    return {"task_id": task_id, "acceptance_id": identifier,
            "receipt_ids": data["receipt_ids"], "delivered": data["delivered"]}


def score_delivery(contract, snapshot, task_id, authoritative_state):
    """Score captured bytes against an independently verified project dictionary.

    None state means unknown, not no acceptance. A verified project whose
    acceptance is None has a known failed delivery even if capture failed.
    Known failures have valid=True;
    infrastructure/unverified states have valid=False and task_success=None.
    No contract returns applicable=False, valid=True, task_success=None.
    """
    if normalize_delivery_contract(contract) is None:
        return {"applicable": False, "valid": True, "task_success": None, "errors": []}
    if authoritative_state is None:
        return _result(None, "AUTHORITATIVE_STATE_UNVERIFIED")
    try:
        expected = _current_acceptance(authoritative_state, task_id)
    except Exception:
        return _result(None, "AUTHORITATIVE_STATE_INVALID")
    if expected is None:
        return _result(False, "NO_CURRENT_ACCEPTANCE")
    try:
        actual = _read_handoff(snapshot)
    except _HandoffError as error:
        return _result(False, str(error))
    except Exception:
        return _result(None, "SNAPSHOT_OR_SCORING_UNAVAILABLE")
    errors = []
    for key in ("task_id", "acceptance_id", "delivered"):
        if actual[key] != expected[key]:
            errors.append(key.upper() + "_MISMATCH")
    if set(actual["receipt_ids"]) != set(expected["receipt_ids"]):
        errors.append("RECEIPT_IDS_MISMATCH")
    return _result(not errors, *errors)


def score_bundle_delivery(bundle, contract, snapshot, business_verified: bool):
    if contract is not None:
        from .managed_session import require_world_quiescent
        require_world_quiescent(bundle)
    """Read original business runtime only after independent state verification.

    business_verified is a verification gate, NOT the business success value.
    In particular, pass True for a verified unsuccessful business outcome. This
    helper does not replay, call tools, repair the runtime, or use a reference.
    """
    if normalize_delivery_contract(contract) is None:
        return score_delivery(None, snapshot, None, None)
    if business_verified is not True:
        return score_delivery(contract, snapshot, None, None)
    try:
        bundle = Path(bundle)
        task = load_json(bundle / "private_task.json")
        if task.get("format") != OPERATIONS_FORMAT:
            raise ValueError("Unsupported delivery runtime")
        state = load_json(bundle / "runtime/project.json")
        task_id = task["task_id"]
    except Exception:
        return _result(None, "AUTHORITATIVE_STATE_UNAVAILABLE")
    return score_delivery(contract, snapshot, task_id, state)


def _truth(component):
    if (isinstance(component, dict) and component.get("valid") is True
            and type(component.get("task_success")) is bool):
        return component["task_success"]
    return None


def overall_result(environment, delivery):
    """Three-valued conjunction; N/A does not participate in required delivery."""
    values = [_truth(environment)]
    if not isinstance(delivery, dict) or delivery.get("applicable") is not False:
        values.append(_truth(delivery))
    success = False if False in values else (None if None in values else True)
    return {"valid": success is not None, "task_success": success}
