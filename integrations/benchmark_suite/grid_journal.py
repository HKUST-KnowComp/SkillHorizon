"""External anchor-first journal and bounded permits for synthetic calibration."""

from contextlib import contextmanager
from copy import deepcopy
import fcntl
from pathlib import Path

from skillhorizon.util import load_json, sha256_value, canonical_json
from .history_grid import ENVIRONMENTS, new_search, validate_search
from .history_grid_probe import exclusive_json

REVISION = "history_grid_calibration_v1"
PERMIT = "SKILLHORIZON_CONSTRUCTION_PERMIT"
CANDIDATE = "SKILLHORIZON_CONSTRUCTION_CANDIDATE"
START = "SKILLHORIZON_CONSTRUCTION_START"
START_DIGEST = "SKILLHORIZON_CONSTRUCTION_START_DIGEST"


def read(path):
    path = Path(path)
    if path != path.resolve() or not path.is_file():
        raise ValueError("Journal path missing or redirected")
    return load_json(path)


def present_domains(plan):
    domains = plan.get("present_domains")
    manifest = plan["candidates"]
    if (not isinstance(domains, list) or not domains
            or domains != [d for d in ENVIRONMENTS if d in domains]
            or domains != manifest.get("present_domains")
            or set(plan["profiles"]) != set(domains) or set(manifest["counts"]) != set(domains)):
        raise ValueError("Plan participation differs from its explicit candidate pool")
    entries = manifest["entries"]
    if {e["environment"] for e in entries.values()} != set(domains):
        raise ValueError("Candidate belongs to an absent domain")
    for domain in domains:
        rows = [e for e in entries.values() if e["environment"] == domain]
        program = plan["profiles"][domain].get("environment_format")
        if (not isinstance(program, str) or not program
                or any(e["profile"].get("environment_format") != program for e in rows)):
            raise ValueError("A domain search requires one explicit environment format")
        if (not rows or type(manifest["counts"][domain]) is not int
                or not 1 <= manifest["counts"][domain] <= 16 or len(rows) != manifest["counts"][domain]
                or any(type(e["rank"]) is not int for e in rows)
                or [e["rank"] for e in rows] != list(range(len(rows)))
                or any(e["id"] != f"{domain}-r{e['rank']:02d}" or entries.get(e["id"]) != e for e in rows)
                or plan["profiles"][domain] != rows[0]["profile"]):
            raise ValueError("Candidate ranks, IDs or domain context differ")
    return domains


def require_states(plan, states):
    if not isinstance(states, dict) or set(states) != set(present_domains(plan)):
        raise ValueError("State map must contain exactly the approved participating domains")


def require_counts(plan, counts):
    domains = present_domains(plan)
    if (not isinstance(counts, dict) or set(counts) != set(ENVIRONMENTS)
            or any(type(value) is not int or value < 0 for value in counts.values())
            or any(counts[d] != 0 for d in ENVIRONMENTS if d not in domains)):
        raise ValueError("START counts require all ten domains and strict nonnegative integers")


def event_domain(plan, payload):
    domain, kind = payload["environment"], payload["kind"]
    if domain not in present_domains(plan) or kind not in {"start", "finish", "selection"}:
        raise ValueError("Event targets an absent domain or unknown transition")
    if kind in {"start", "finish"}:
        entry = plan["candidates"]["entries"].get(payload["candidate"])
        if entry is None or entry["environment"] != domain:
            raise ValueError("Event candidate does not belong to its participating domain")
    return domain


def initial_states(plan):
    entries = plan["candidates"]["entries"]
    return {d: new_search([e["profile"]["overrides"] for e in entries.values()
                          if e["environment"] == d], environment=d, plan_id=sha256_value(plan),
                         environment_format=plan["profiles"][d]["environment_format"])
            for d in present_domains(plan)}


def prefix(plan, events):
    counts = events[-1]["starts"] if events else {d: 0 for d in ENVIRONMENTS}
    require_counts(plan, counts)
    return {"seq": len(events), "digest": sha256_value(events[-1]) if events else None,
            "starts": counts}


def load_events(plan, *, replay=True):
    present_domains(plan)
    anchor, mirror = Path(plan["control"]) / "events", Path(plan["root"]) / "events"
    names = sorted(p.name for p in anchor.iterdir())
    if names != sorted(p.name for p in mirror.iterdir()) or names != [f"{i:06d}.json" for i in range(1, len(names)+1)]:
        raise ValueError("External journal and local mirror have a gap or rollback")
    events, states = [], initial_states(plan) if replay else {}
    for name in names:
        event = read(anchor / name)
        event_domain(plan, event["payload"])
        require_counts(plan, event["starts"])
        if type(event["seq"]) is not int or event["seq"] <= 0:
            raise ValueError("Event sequence must be a positive integer")
        if canonical_json(event) != canonical_json(read(mirror / name)):
            raise ValueError("External journal and local mirror differ")
        if replay:
            validate_event(plan, events, states, event)
            states[event["payload"]["environment"]] = event["payload"]["state"]
        else:
            prior = prefix(plan, events)
            counts = dict(prior["starts"])
            if event["payload"]["kind"] == "start":
                counts[event["payload"]["environment"]] += 1
            if (event["seq"] != prior["seq"]+1 or event["previous"] != prior["digest"]
                    or event["plan_digest"] != sha256_value(plan) or event["starts"] != counts):
                raise ValueError("Journal chain differs")
        events.append(event)
        validate_permit(plan, event["payload"]["permit"], events)
    return events, states


def validate_event(plan, events, states, event):
    require_states(plan, states)
    require_counts(plan, event["starts"])
    if type(event["seq"]) is not int or event["seq"] <= 0:
        raise ValueError("Event sequence must be a positive integer")
    previous = prefix(plan, events)
    payload = event["payload"]
    domain, state = event_domain(plan, payload), payload["state"]
    old = states[domain]
    validate_search(state, approved=initial_states(plan)[domain], committed=old)
    if len(state["events"]) != len(old["events"])+1:
        raise ValueError("One journal event must commit exactly one search transition")
    last = state["events"][-1]
    kind = payload["kind"]
    if kind == "start":
        action = last.get("result", {})
        entry = plan["candidates"]["entries"][payload["candidate"]]
        if (last["kind"] != "advance" or action.get("kind") != "start"
                or entry["environment"] != domain or entry["rank"] != action["rank"]
                or entry["profile"]["overrides"] != action["parameters"]):
            raise ValueError("Anchored START differs from selected candidate")
        if payload["argv"] != worker_argv(plan, payload["candidate"], payload["permit"]):
            raise ValueError("Anchored worker argv differs")
    elif kind == "finish":
        if last["kind"] != "finish":
            raise ValueError("Terminal is not a finish transition")
        entry = plan["candidates"]["entries"][payload["candidate"]]
        starts = [e["payload"] for e in events if e["payload"].get("candidate") == payload["candidate"]
                  and e["payload"]["kind"] == "start"]
        if (len(starts) != 1 or entry["environment"] != domain or entry["rank"] != last["rank"]
                or starts[0]["permit"] != payload["permit"] or starts[0]["permit_digest"] != payload["permit_digest"]):
            raise ValueError("Terminal candidate or permit differs from START")
        receipt = payload["receipt"]
        if any(last[k] != receipt[k] for k in ("reference_tokens", "gates", "failure")):
            raise ValueError("Terminal receipt differs from search outcome")
    elif kind == "selection":
        if last["kind"] != "advance" or last["result"]["kind"] not in {"selected", "not_found"}:
            raise ValueError("Invalid selection transition")
    else:
        raise ValueError("Unknown journal event")
    domains = present_domains(plan)
    counts = {d: len((state if d == domain else states[d])["attempts"]) if d in domains else 0 for d in ENVIRONMENTS}
    if (event["seq"] != previous["seq"]+1 or event["previous"] != previous["digest"]
            or event["plan_digest"] != sha256_value(plan) or event["starts"] != counts):
        raise ValueError("Journal chain or cumulative START counts differ")


def commit(plan, events, states, payload):
    require_states(plan, states)
    event_domain(plan, payload)
    domains = present_domains(plan)
    prior = prefix(plan, events)
    event = {"seq": prior["seq"]+1, "previous": prior["digest"], "plan_digest": sha256_value(plan),
             "starts": {d: len((payload["state"] if d == payload["environment"] else states[d])["attempts"]) if d in domains else 0
                        for d in ENVIRONMENTS}, "payload": deepcopy(payload)}
    validate_event(plan, events, states, event)
    validate_permit(plan, payload["permit"], [*events, event])
    filename = f"{event['seq']:06d}.json"
    exclusive_json(Path(plan["control"]) / "events" / filename, event)
    exclusive_json(Path(plan["root"]) / "events" / filename, event)
    events.append(event)
    states[payload["environment"]] = deepcopy(payload["state"])
    return event


def worker_argv(plan, candidate, permit):
    return [*plan["worker_command"], str(Path(plan["root"]) / candidate), "--candidate", candidate,
            "--plan", str(Path(plan["root"]) / "PLAN.json"), "--permit", str(permit)]


def validate_permit(plan, path, events):
    present_domains(plan)
    path = Path(path)
    permit = read(path)
    if (path.parent != Path(plan["control"]) / "permits" or permit.get("approved") is not True
            or permit.get("plan_digest") != sha256_value(plan)):
        raise ValueError("Independent execution permit differs")
    base = permit["prefix"]
    require_counts(plan, base["starts"])
    seq = base["seq"]
    allowed, maximum = permit["allowed_candidates"], permit["max_additional_starts"]
    if (type(seq) is not int or not 0 <= seq <= len(events) or prefix(plan, events[:seq]) != base
            or not isinstance(allowed, list) or not allowed or len(set(allowed)) != len(allowed)
            or any(c not in plan["candidates"]["entries"] for c in allowed)
            or type(maximum) is not int or not 0 < maximum <= len(allowed)):
        raise ValueError("Execution permit prefix or allowance invalid")
    seen = set()
    for event in events[seq:]:
        payload = event["payload"]
        event_domain(plan, payload)
        if payload["permit"] != str(path) or payload["permit_digest"] != sha256_value(permit):
            raise ValueError("Old permit cannot reenter a later continuation")
        if payload["kind"] == "start":
            candidate = payload["candidate"]
            if candidate not in allowed or candidate in seen:
                raise ValueError("Permit contains an unauthorized or repeated START")
            seen.add(candidate)
    if len(seen) > maximum:
        raise ValueError("Execution permit START allowance exceeded")
    return permit, seen


def authorized_start(plan, permit_path, candidate, sequence, digest, *, full=True):
    events, states = load_events(plan, replay=full)
    permit, _ = validate_permit(plan, permit_path, events)
    if type(sequence) is not int or not 1 <= sequence <= len(events):
        raise ValueError("Invalid anchored START sequence")
    event = events[sequence-1]
    p = event["payload"]
    entry = plan["candidates"]["entries"].get(candidate)
    if (sha256_value(event) != digest or p["kind"] != "start" or p["candidate"] != candidate
            or p["permit"] != str(permit_path) or candidate not in permit["allowed_candidates"]
            or event != events[-1] or entry is None or entry["environment"] != p["environment"]
            or p["argv"] != worker_argv(plan, candidate, permit_path)):
        raise ValueError("Candidate is not the active permitted anchored START")
    return p


def require_terminal(states):
    if any(a["status"] == "running" for s in states.values() for a in s["attempts"]):
        raise ValueError("Unfinished START requires reviewed cleanup reconciliation; no automatic retry")


def require_clean_resources(events):
    for event in events:
        p = event["payload"]
        if p["kind"] == "finish":
            r = p["receipt"]["resources"]
            if (r["quiescent"] is not True or r.get("monitor_error_class") is not None
                    or r["failure"] in {"total_storage_limit", "resource_monitor_failed"}):
                raise ValueError("Prior resource uncertainty requires reviewed reconciliation")


@contextmanager
def controller_lock(plan):
    control = Path(plan["control"])
    if control != control.resolve():
        raise ValueError("Control path redirected")
    with (control / "LOCK").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
