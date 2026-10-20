"""Explicit PTC declarations and pre-dispatch storage envelopes.

This module grants no execution capability. Production export and both MCP
entries must bind the normalized declaration before enabling a bridge.
"""

from .python_execution import frame_bound, normalize_python_execution
from .workspace_state import normalize_workspace_config


PTC_PROTOCOL = "ptc_v1"
EPISODE_STORAGE_CEILING = 4 * 1024**3
CHILD_METADATA_BYTES = 8192
TERMINAL_RESERVE_BYTES = 16384
_RANGES = {
    "max_children_per_program": (1, 64),
    "max_children_per_episode": (1, 4096),
    "max_request_bytes": (1024, 1024**2),
    "max_response_bytes": (1024, 4 * 1024**2),
    "max_bridge_bytes": (2048, 64 * 1024**2),
    "max_json_depth": (4, 32),
    "max_json_nodes": (64, 65536),
}


def normalize_business_transport(value):
    if value not in ("inline", "managed_worker") or type(value) is not str:
        raise ValueError("Declare inline or managed_worker business transport")
    return value


def normalize_ptc(config, *, workspace=None, execution=None, observation_mode=None,
                  business_transport="inline", max_actions=None):
    normalize_business_transport(business_transport)
    if config is None:
        return None
    config = normalize_ptc_resources(config)
    if workspace is None or execution is None or observation_mode != "full":
        raise ValueError("PTC requires workspace, Python execution, and full observations")
    if business_transport != "managed_worker":
        raise ValueError("PTC requires managed_worker business transport")
    workspace = normalize_workspace_config(workspace)
    execution = normalize_python_execution(execution, workspace)
    if type(max_actions) is not int or not 1 <= max_actions <= 100000:
        raise ValueError("PTC requires a frozen total tool-call budget")
    bounds = ptc_storage_bounds(config, workspace, execution, max_actions)
    if bounds["journal_bytes"] + bounds["receipt_bytes"] > EPISODE_STORAGE_CEILING:
        raise ValueError("PTC action journal and receipt exceed the episode storage ceiling")
    return config


def normalize_ptc_resources(config):
    """Validate resources only; not authorization to enable a production tool."""
    if type(config) is not dict or set(config) != set(_RANGES):
        raise ValueError("PTC requires the exact declared resource fields")
    for key, (low, high) in _RANGES.items():
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError("PTC resource outside supported bounded range: " + key)
    if config["max_children_per_episode"] < config["max_children_per_program"]:
        raise ValueError("PTC program budget exceeds the episode child budget")
    if config["max_bridge_bytes"] < config["max_request_bytes"] + config["max_response_bytes"]:
        raise ValueError("PTC bridge cannot hold one declared request and response")
    return dict(config)


def ptc_storage_bounds(config, workspace, execution, max_actions):
    # Child JSON is stored as JSON, not as a quoted second serialization. Wire
    # maxima include framing; reserving them whole is a conservative bound.
    path_bytes = workspace["max_depth"] * 256
    argument = 6 * (2 * workspace["max_file_bytes"] + 2 * path_bytes
                    + execution["max_code_bytes"]) + 4096
    output = max(6 * workspace["max_response_chars"], 12 * execution["max_stream_bytes"] + 4096)
    action = argument + output + 1024
    snapshots = 2 * frame_bound(workspace, execution)
    child = config["max_request_bytes"] + config["max_response_bytes"] + CHILD_METADATA_BYTES
    programs = min(max_actions, execution["max_calls"])
    children = min(max_actions, config["max_children_per_episode"],
                   programs * config["max_children_per_program"])
    record = action + snapshots + config["max_children_per_program"] * child + TERMINAL_RESERVE_BYTES
    journal = (max_actions * action + programs * (snapshots + TERMINAL_RESERVE_BYTES)
               + children * child)
    # A pending receipt can preserve the entire last admitted parent, including
    # its known children, without using bridge traffic's terminal allowance.
    receipt = record + TERMINAL_RESERVE_BYTES
    return {"child_bytes": child, "record_bytes": record,
            "journal_bytes": journal, "receipt_bytes": receipt,
            "terminal_reserve_bytes": TERMINAL_RESERVE_BYTES}


def ptc_tools():
    return [{"type": "function", "name": "ptc_execute", "strict": False,
             "description": "Run isolated Python with synchronous access to declared business tools. "
                            "Use from skillhorizon_tools import call. Business changes and files can "
                            "persist after a program error; calls consume the shared tool budget.",
             "parameters": {"type": "object", "properties": {"code": {"type": "string"}},
                            "required": ["code"], "additionalProperties": False}}]
