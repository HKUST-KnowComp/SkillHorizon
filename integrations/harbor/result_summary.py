"""Join controller observations with native results, without exposing messages."""

import json
from pathlib import Path

from .claim_measurement import summary as claims_summary


def summarize_trial(result_path, *, max_turns):
    try:
        return _summarize_trial(result_path, max_turns=max_turns)
    except Exception as error:
        return {"observation_valid": False, "observation_error": type(error).__name__,
                "business_score_source": "independent verifier", "provider_usage_source": "controller ledger"}


def _summarize_trial(result_path, *, max_turns):
    result_path = Path(result_path)
    value = json.loads(result_path.read_text())
    metadata = (value.get("agent_result") or {}).get("metadata") or {}
    events_path = result_path.parent / "controller_observation/events.jsonl"
    events = []
    error = None
    if events_path.exists():
        try:
            events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
        except (OSError, ValueError):
            error = "observation_unreadable"
    else:
        error = "observation_missing"
    known_peaks = [event["active_content_tokens"] for event in events
                   if event.get("kind") == "actor_query_start"
                   and type(event.get("active_content_tokens")) is int]
    episodes = metadata.get("n_episodes")
    observer = metadata.get("skillhorizon_observation") or {}
    return {"claims": claims_summary(result_path.parent / "business_private"), "native_episodes": episodes,
        "native_turn_limit_reached": episodes >= max_turns if type(episodes) is int else None,
        "native_summarization_counter": metadata.get("summarization_count"),
        "completed_compact_events": sum(event.get("kind") == "compact_end"
            and event.get("subagent_trajectories") == 3 for event in events) if error is None else None,
        "native_counter_meaning": "incremented before summary attempt; not a successful-compact count",
        "observation_valid": error is None and observer.get("observation_failed") is False,
        "observation_error": error,
        "peak_actor_content_tokens_estimated": max(known_peaks) if known_peaks else None,
        "terminal_truncation_events": sum(event.get("kind") == "terminal_output"
            and event.get("truncated") is True for event in events) if error is None else None,
        "command_batches": sum(event.get("kind") == "command_batch_start" for event in events) if error is None else None,
        "script_internal_reads": "unobserved",
        "business_score_source": "independent verifier",
        "provider_usage_source": "controller ledger",
        "length_measure": "active content estimate, not reference L_ref or provider billing"}
