"""Public, uncompressed interaction length, independent of billing and context size."""

from collections import Counter
from copy import deepcopy
from functools import lru_cache
from importlib.metadata import version
import hashlib
import json
from pathlib import Path

import tiktoken

from skillhorizon.util import append_jsonl, write_json

REVISION = "public_event_history_v2"
K = 1024


def serialize(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


@lru_cache(maxsize=1)
def encoding():
    return tiktoken.get_encoding("o200k_base")


def tokens(value):
    return len(encoding().encode(serialize(value), disallowed_special=()))


def projection(message):
    """Only provider metadata is removed; semantic IDs and argument strings survive."""
    result = {k: deepcopy(v) for k, v in message.items()
              if k not in {"id", "status", "phase", "encrypted_content"}}
    if result.get("type") == "message":
        result.pop("type")
    if result.get("type") == "reasoning" and not (result.get("content") or result.get("summary")):
        return None
    # SDK supplies absent empty annotations; these carry no public information.
    if isinstance(result.get("content"), list):
        for part in result["content"]:
            if isinstance(part, dict) and part.get("annotations") == []:
                part.pop("annotations")
    return result


def contract():
    return {"revision": REVISION, "tokenizer": "o200k_base", "tiktoken_version": version("tiktoken"),
            "K": K, "serialization": "sorted compact UTF-8 JSON; each projected event separately",
            "special_tokens": "ordinary text", "schema": "count once separately",
            "not_measured": ["opaque reasoning", "repeated request prefixes", "unsurfaced program internals"]}


def reference_history(rows):
    """A reference is a validated full-online reference-origin SDK trajectory."""
    report = measure_ledger(rows)
    if report["status"] != "available" or not report["fixture"] or report["track"] != "full_online":
        raise ValueError("Reference requires a complete full-online reference ledger")
    if rows[-1]["stop"] != "sdk_final_output" or rows[-1]["pending"]:
        raise ValueError("Reference execution did not finish")
    total = report["cumulative_raw_tokens"]
    return {**report, "reference_tokens": total, "reference_K": total / K,
            "tool_calls": sum(r["kind"] == "admission" and (r["message"] or {}).get("type") == "function_call" for r in rows),
            "policy_efficiency_verified": False,
            "scope": "Executed reference, not shortest solution or calibrated task band"}


def load_reference(root):
    if read_measurement(root)["status"] != "available":
        raise ValueError("Reference ledger or required ancestry is unavailable")
    return reference_history(strict_rows(Path(root) / "PUBLIC_HISTORY.jsonl"))


class HistoryLedger:
    """Append-only admissions, exposure and compaction provenance for new runs.

    SDK-visible admissions and internal protocol feedback have separate cursors.
    Repeated content is never deduplicated. Instrumentation errors invalidate the
    measurement, not the business trajectory.
    """

    def __init__(self, root, initial, tools, *, checkpoint_actions=None, fixture=False, boundary_verified=True):
        self.path = Path(root) / "PUBLIC_HISTORY.jsonl"
        self.rows, self.events, self.sdk_ids, self.active = [], {}, [], []
        self.synced = 0
        self.issues = []
        self.fixture = fixture
        self.write("header", contract=contract(), tools=tools, checkpoint_actions=checkpoint_actions,
                   fixture=fixture, boundary_verified=boundary_verified)
        has_prefix = any(m.get("type") in {"function_call", "function_call_output"} for m in initial)
        self.track = ("full_online" if checkpoint_actions == 0 and not has_prefix else
                      "reference_prefix_continuation" if checkpoint_actions and has_prefix else "unknown")
        if self.track == "unknown":
            self.issues.append("unverified_checkpoint_boundary")
        for m in initial:
            origin = "initial" if m.get("role") in {"system", "developer", "user"} else "checkpoint_prefix"
            self.admit([m], origin, attempt=0, sdk=True)

    def write(self, kind, **data):
        row = {"seq": len(self.rows), "kind": kind, **data}
        append_jsonl(self.path, row)
        self.rows.append(row)
        return row["seq"]

    def admit(self, messages, origin, *, attempt, sdk=True, activate=False):
        ids = []
        for index, message in enumerate(messages):
            projected = projection(message)
            excluded = self.fixture and origin == "actor" and message.get("type") != "function_call"
            eid = self.write("admission", message=projected, origin=origin, attempt=attempt,
                             item_index=index, call_id=message.get("call_id"),
                             counted=projected is not None and not excluded,
                             sdk=sdk, activate=activate,
                             source="fixture" if excluded else origin)
            self.events[eid] = projected
            ids.append(eid)
        if sdk:
            self.sdk_ids.extend(ids)
        if activate:
            self.active.extend(ids)
        return ids

    def sync(self, items):
        expected = [self.events[i] for i in self.sdk_ids]
        observed = [projection(m) for m in items]
        if expected != observed:
            self.issues.append("sdk_admission_projection_mismatch")
            return
        self.active.extend(self.sdk_ids[self.synced:])
        self.write("sync", event_ids=self.sdk_ids[self.synced:])
        self.synced = len(self.sdk_ids)

    def expose(self, items, attempt, role):
        if [self.events[i] for i in self.active] != [projection(m) for m in items]:
            self.issues.append("active_history_projection_mismatch")
            return
        self.write("exposure", event_ids=list(self.active), attempt=attempt, role=role)

    def compact(self, before, after, plan, attempt):
        if [self.events[i] for i in self.active] != [projection(m) for m in before]:
            self.issues.append("compaction_input_projection_mismatch")
            return
        remaining = list(self.active)
        anchors = []
        for m in plan["anchors"]:
            matches = [i for i in remaining if self.events[i] == projection(m)]
            if not matches:
                raise ValueError("Compaction anchor missing")
            anchors.append(matches[0])
            remaining.remove(matches[0])
        n = plan["source_message_count"]
        replaced, tail = remaining[:n], remaining[n:]
        if [self.events[i] for i in tail] != [projection(m) for m in plan["tail"]]:
            raise ValueError("Compaction tail mismatch")
        message = projection(after[len(anchors)])
        sid = self.write("summary", message=message, attempt=attempt)
        self.events[sid] = message
        self.active = anchors + [sid] + tail
        self.write("compaction", attempt=attempt, replaced=replaced, retained=anchors + tail,
                   prior_summaries=[i for i in replaced if self.rows[i]["kind"] == "summary"], inserted=sid,
                   active_after=list(self.active), anchors=anchors, tail=tail)

    def close(self, *, stop, pending, uncertain_calls=(), provenance=None):
        self.write("closure", stop=stop, pending=list(pending), uncertain_calls=list(uncertain_calls),
                   issues=list(self.issues), track=self.track,
                   provenance=provenance or {"mode": "root"})
        result = measure_ledger(self.rows)
        write_json(self.path.parent / "HISTORY_MEASUREMENT.json", result)
        return result


def _measure_ledger(rows):
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("Ledger must be a list of objects")
    reasons = []
    if not rows or rows[0].get("kind") != "header":
        reasons.append("missing_header")
    if not rows or rows[-1].get("kind") != "closure":
        reasons.append("missing_closure")
    if any(row.get("seq") != index for index, row in enumerate(rows)):
        reasons.append("discontinuous_ledger")
    header = rows[0] if rows else {}
    closure = rows[-1] if rows and rows[-1].get("kind") == "closure" else {}
    provenance = closure.get("provenance", {})
    if provenance.get("mode") not in {"root", "resume", "replay"}:
        reasons.append("missing_lineage_provenance")
    elif provenance["mode"] != "root" and (not provenance.get("path") or not provenance.get("digest")
            or type(provenance.get("inherited_records")) is not int):
        reasons.append("incomplete_lineage_provenance")
    if header.get("contract") != contract():
        reasons.append("incompatible_tokenizer_contract")
    if not header.get("boundary_verified"):
        reasons.append("unverified_checkpoint_boundary")
    reasons.extend(closure.get("issues", []))
    if closure.get("uncertain_calls"):
        reasons.append("unresolved_tool_dispatch")
    counts = Counter()
    calls, results = set(), set()
    events, active, pending_sdk = {}, [], []
    dispositions, decisions, actor_items = {}, {}, Counter()
    call_owners, generations, outcomes, applications = {}, {}, {}, {}
    initial_calls = 0
    known_kinds = {"header", "admission", "sync", "exposure", "disposition", "decision", "summary_generation", "summary_outcome", "summary", "compaction", "closure"}
    for row in rows:
        kind = row.get("kind")
        if kind not in known_kinds:
            reasons.append("unknown_record_kind")
        if kind in {"header", "closure"}:
            if (kind == "header" and row["seq"] != 0 or kind == "closure" and row["seq"] != len(rows)-1):
                reasons.append("misplaced_header_or_closure")
        if kind == "disposition":
            if row["attempt"] in dispositions:
                reasons.append("duplicate_disposition")
            dispositions[row["attempt"]] = row
        elif kind == "decision":
            d = dispositions.get(row["attempt"], {})
            if d.get("provider_accepted") is not True or d.get("role") != "actor" or row["attempt"] in decisions:
                reasons.append("invalid_actor_decision")
            decisions[row["attempt"]] = row
        elif kind == "sync":
            if row["event_ids"] != pending_sdk:
                reasons.append("invalid_sdk_sync")
            active.extend(row["event_ids"])
            pending_sdk = []
        elif kind == "exposure":
            if row["event_ids"] != active or any(i not in events for i in row["event_ids"]):
                reasons.append("invalid_exposure")
        elif kind == "summary_generation":
            d = dispositions.get(row["attempt"], {})
            if d.get("provider_accepted") is not True or d.get("role") != "compaction":
                reasons.append("invalid_summary_generation")
            if row["attempt"] in generations:
                reasons.append("duplicate_summary_generation")
            generations[row["attempt"]] = row
        elif kind == "summary_outcome":
            if row["attempt"] not in generations or row["attempt"] in outcomes:
                reasons.append("invalid_summary_outcome")
            outcomes[row["attempt"]] = row["applied"]
        elif kind == "summary":
            if not any(r["kind"] == "summary_generation" and r["attempt"] == row["attempt"] for r in rows[:row["seq"]]):
                reasons.append("missing_summary_generation")
            events[row["seq"]] = row["message"]
        elif kind == "compaction":
            replaced, retained = row["replaced"], row["retained"]
            if [i for i in active if i not in row["anchors"]] != replaced + row["tail"]:
                reasons.append("invalid_compaction_order")
            if (len(set(replaced + retained)) != len(replaced + retained)
                    or set(replaced + retained) != set(active)
                    or row["inserted"] not in events
                    or rows[row["inserted"]]["kind"] != "summary"
                    or row["prior_summaries"] != [i for i in replaced if rows[i]["kind"] == "summary"]):
                reasons.append("invalid_compaction_partition")
            if any(i >= row["seq"] or i not in events for i in replaced + retained):
                reasons.append("invalid_compaction_reference")
            active = list(row["active_after"])
            if (active != row["anchors"] + [row["inserted"]] + row["tail"]
                    or retained != row["anchors"] + row["tail"]):
                reasons.append("invalid_compaction_output")
            inserted = rows[row["inserted"]]
            generation = generations.get(row["attempt"], {})
            from .context import SUMMARY_PREFIX
            text = "\n".join(c["text"] for m in generation.get("output", []) if m and m.get("role") == "assistant"
                             for c in m.get("content", []) if isinstance(c, dict) and c.get("type") == "output_text").strip()
            if (inserted.get("attempt") != row["attempt"] or inserted["message"] != {"role": "user", "content": SUMMARY_PREFIX + text}
                    or row["attempt"] in applications):
                reasons.append("invalid_summary_application")
            applications[row["attempt"]] = row["inserted"]
        if kind != "admission":
            continue
        m = row["message"] or {}
        origin, attempt = row["origin"], row["attempt"]
        if origin not in {"initial", "checkpoint_prefix", "actor", "tool_result", "protocol_feedback"}:
            reasons.append("invalid_origin")
        if origin in {"initial", "checkpoint_prefix"}:
            if attempt != 0 or dispositions:
                reasons.append("invalid_initial_membership")
            expected_origin = "initial" if m.get("role") in {"system", "developer", "user"} else "checkpoint_prefix"
            if origin != expected_origin:
                reasons.append("misclassified_initial_message")
        elif origin == "actor":
            decision = decisions.get(attempt, {})
            if decision.get("admitted") is not True:
                reasons.append("unapproved_actor_admission")
            if row["item_index"] != actor_items[attempt]:
                reasons.append("invalid_actor_item_order")
            internal = decision.get("reason") == "protocol_feedback"
            if row["sdk"] == internal or row["activate"] != internal:
                reasons.append("invalid_actor_projection_route")
            actor_items[attempt] += 1
        elif origin in {"tool_result", "protocol_feedback"}:
            decision = decisions.get(attempt, {})
            internal = origin == "protocol_feedback"
            if (m.get("type") != "function_call_output" or call_owners.get(m.get("call_id")) != attempt
                    or not decision.get("admitted") or (decision.get("reason") == "protocol_feedback") != internal
                    or row["sdk"] == internal or row["activate"] != internal):
                reasons.append("invalid_result_attribution")
        events[row["seq"]] = row["message"]
        if row["sdk"]:
            pending_sdk.append(row["seq"])
        if row["activate"]:
            active.append(row["seq"])
        countable = row["message"] is not None and not (header.get("fixture") and origin == "actor" and m.get("type") != "function_call")
        if row["counted"] != countable:
            reasons.append("invalid_countability")
        cid = m.get("call_id")
        if m.get("type") == "function_call":
            if not cid or cid in calls:
                reasons.append("duplicate_or_missing_call_id")
            calls.add(cid)
            call_owners[cid] = attempt
            initial_calls += int(attempt == 0)
        elif m.get("type") == "function_call_output":
            if cid not in calls or cid in results:
                reasons.append("unpaired_or_duplicate_result")
            results.add(cid)
        if countable:
            counts[row["origin"]] += tokens(m)
    for attempt, decision in decisions.items():
        if actor_items[attempt] != (decision["item_count"] if decision["admitted"] else 0):
            reasons.append("incomplete_actor_admission")
    for attempt, d in dispositions.items():
        if d["provider_accepted"] and d["role"] == "actor" and attempt not in decisions:
            reasons.append("missing_admission_decision")
        if d["provider_accepted"] and d["role"] == "compaction" and attempt not in generations:
            reasons.append("missing_summary_generation")
    if set(generations) != set(outcomes) or {a for a, applied in outcomes.items() if applied} != set(applications):
        reasons.append("incomplete_summary_outcome")
    if {r["seq"] for r in rows if r["kind"] == "summary"} != set(applications.values()):
        reasons.append("orphan_summary")
    if initial_calls != header.get("checkpoint_actions"):
        reasons.append("checkpoint_history_count_mismatch")
    track = "full_online" if initial_calls == 0 else "reference_prefix_continuation"
    if closure.get("track") != track:
        reasons.append("invalid_track")
    if calls - results != set(closure.get("pending", [])):
        reasons.append("unexplained_missing_results")
    schema = tokens(header.get("tools", []))
    valid = not reasons
    total = sum(counts.values())
    return {**contract(), "status": "available" if valid else "unavailable",
            "reasons": sorted(set(reasons)), "track": track,
            "message_tokens": total if valid else None, "schema_once_tokens": schema if valid else None,
            "cumulative_raw_tokens": total + schema if valid else None,
            "actor_suffix_tokens": sum(v for k, v in counts.items() if k not in {"initial", "checkpoint_prefix"}) if valid else None,
            "components": dict(counts) if valid else None,
            "applied_summary_tokens": sum(tokens(rows[i]["message"]) for i in applications.values()),
            "generated_summary_tokens": sum(tokens(r["output"]) for r in rows if r.get("kind") == "summary_generation"),
            "pending_calls": closure.get("pending", []), "fixture": header.get("fixture"),
            "business_outcome": "independent; measurement unavailability is not task failure"}


def measure_ledger(rows):
    try:
        return _measure_ledger(rows)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return {"revision": REVISION, "status": "unavailable", "reasons": ["malformed_public_history_ledger"],
                "cumulative_raw_tokens": None}


def strict_rows(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result
    return [json.loads(line, object_pairs_hook=pairs) for line in Path(path).read_text().splitlines()]


def read_measurement(root, _seen=()):
    path = Path(root) / "PUBLIC_HISTORY.jsonl"
    if not path.is_file():
        return {"revision": REVISION, "status": "unavailable", "reasons": ["missing_public_history_ledger"],
                "cumulative_raw_tokens": None}
    try:
        if path.resolve() in _seen:
            raise ValueError("Lineage cycle")
        rows = strict_rows(path)
        report = measure_ledger(rows)
        if report["status"] != "available":
            return report
        source = rows[-1]["provenance"]
        if source["mode"] != "root":
            parent = Path(source["path"])
            if (parent.name != "PUBLIC_HISTORY.jsonl" or hashlib.sha256(parent.read_bytes()).hexdigest() != source["digest"]
                    or read_measurement(parent.parent, (*_seen, path.resolve()))["status"] != "available"):
                raise ValueError("Invalid source history")
            inherited = strict_rows(parent)[:-1]
            if source["inherited_records"] != len(inherited) or rows[:len(inherited)] != inherited:
                raise ValueError("Lineage overlap mismatch")
        return report
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return {"revision": REVISION, "status": "unavailable", "reasons": ["malformed_public_history_ledger"],
                "cumulative_raw_tokens": None}
