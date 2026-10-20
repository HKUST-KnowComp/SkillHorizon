"""Fixed multi-seed slots and deterministic, resumable pilot selection."""

from copy import deepcopy
import math

from skillhorizon.util import canonical_json
from .history_bands import BANDS, interval
from .history_coverage import DOMAINS

ENVIRONMENTS = ("DataOps", *DOMAINS)
PILOT_SEED = 20260915
SEEDS = (20260916, 20260917, 20260918)
ATTEMPTS_PER_DOMAIN = 16
REVISION = "history_grid_v2"
SEARCH_REVISION = "history_grid_search_v4"
GATES = ("construction", "business", "dependencies", "reference_history", "sdk_replay", "resources", "diversity")
CALIBRATION_GATES = ("construction", "business", "reference_history", "sdk_replay", "resources")


def slots():
    return [{"id": f"{domain}-{band}K-{seed}", "environment": domain, "band": band, "seed": seed}
            for domain in ENVIRONMENTS for band in BANDS for seed in SEEDS]


def protocol():
    return {"revision": REVISION, "scope": "construction_development_not_model_behavior",
        "pilot_seed": PILOT_SEED, "seeds": list(SEEDS), "slots": slots(),
        "bands": {str(b): list(interval(b)) for b in BANDS}, "gates": list(GATES),
        "calibration_gates": list(CALIBRATION_GATES), "selection_scope": "length_candidate_not_final_qualification",
        "calibration_attempts_per_domain": ATTEMPTS_PER_DOMAIN,
        "calibration_attempts_total": len(ENVIRONMENTS)*ATTEMPTS_PER_DOMAIN,
        "provider_calls": 0, "profiles_frozen": False,
        "resources": {"parallel_domains": 4, "parallel_candidates_per_domain": 1,
            "candidate_seconds": 3600, "cleanup": "TERM about 0.25s then KILL about 5s; actual elapsed recorded",
            "candidate_disk_bytes_including_tmp_and_log": 8*1024**3,
            "log_bytes": 256*1024**2, "suite_disk_bytes": 160*1024**3,
            "candidate_descendant_rss_bytes": 16*1024**3, "sampling_seconds": 2,
            "monitoring": "sampled stop budgets, not hard cgroup limits",
            "admission": "reserve full candidate disk budget centrally before dispatch"},
        "selection": "first eligible; nearest measured target then lowest rank at selection time; never revise a selected cell",
        "rank_interpolation": "heuristic only, not proof of unobserved monotonicity or reachability",
        "retry": "no automatic repeat, no seed replacement, all starts consume attempts",
        "revalidation": "domain profile/reference change: all 4 bands x 3 seeds; common measurement/replay change: all 120; new reviewed budget",
        "completion": "120 unique fixed slots all qualified; terminal records alone do not imply completion"}


def new_search(candidates, *, environment, plan_id, environment_format=None):
    if (not candidates or any(not isinstance(c, dict) for c in candidates)
            or len({canonical_json(c) for c in candidates}) != len(candidates)):
        raise ValueError("A nonempty, unique ordered scale-vector list is required")
    from .catalog import resolve_profile
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise ValueError("An approved plan identity is required")
    for candidate in candidates:
        profile = resolve_profile(environment, seed=PILOT_SEED, overrides=candidate,
                                  environment_format=environment_format)
        environment_format = profile["environment_format"]
    return {"revision": SEARCH_REVISION, "environment": environment, "pilot_seed": PILOT_SEED,
            "environment_format": environment_format,
            "plan_id": plan_id, "candidates": deepcopy(candidates), "events": [],
            "attempts": [], "selections": {}}


def validate_search(state, *, approved=None, committed=None):
    """Replay decisions; the runner supplies the external plan and durable prefix.

    A standalone snapshot cannot prove it has not been rolled back. This function
    deliberately keeps that persistence obligation explicit rather than signing
    a self-reported snapshot.
    """
    if (state.get("revision") != SEARCH_REVISION
            or not isinstance(state.get("environment_format"), str) or not state["environment_format"]):
        raise ValueError("Search requires the current revision and explicit environment format")
    fresh = new_search(state["candidates"], environment=state["environment"], plan_id=state["plan_id"],
                       environment_format=state["environment_format"])
    identity = {k: fresh[k] for k in ("revision", "environment", "environment_format", "pilot_seed", "plan_id", "candidates")}
    if approved is not None and identity != {k: approved[k] for k in identity}:
        raise ValueError("Search differs from the approved candidate plan")
    if committed is not None:
        validate_search(committed, approved=approved)
        if (identity != {k: committed[k] for k in identity}
                or state["events"][:len(committed["events"])] != committed["events"]):
            raise ValueError("Search rewrites or rolls back the committed prefix")
    for event in state["events"]:
        if event.get("kind") == "advance":
            actual = _advance(fresh)
            if actual != event.get("result") or actual["kind"] in {"await_terminal", "finished"}:
                raise ValueError("Invalid persisted pilot decision")
        elif event.get("kind") == "finish":
            _finish(fresh, event["rank"], reference_tokens=event["reference_tokens"],
                    gates=event["gates"], failure=event["failure"])
        else:
            raise ValueError("Unknown search event")
        fresh["events"].append(deepcopy(event))
    if fresh != state:
        raise ValueError("Pilot snapshot differs from its replayed event history")


def next_action(state):
    """Persist the returned state before dispatching a candidate process."""
    validate_search(state)
    result = _advance(state)
    if result["kind"] not in {"await_terminal", "finished"}:
        state["events"].append({"kind": "advance", "result": deepcopy(result)})
    return result


def _advance(state):
    if any(r["status"] == "running" for r in state["attempts"]):
        return {"kind": "await_terminal"}
    measured = [r for r in state["attempts"] if r["status"] == "measured"]
    for band in BANDS:
        if str(band) in state["selections"]:
            continue
        low, high = interval(band)
        hits = [r for r in measured if low <= r["reference_tokens"] <= high]
        if hits:
            best = min(hits, key=lambda r: (abs(r["reference_tokens"]-band*1024), r["rank"]))
            selection = {"status": "selected", "rank": best["rank"], "after_attempts": len(state["attempts"])}
            state["selections"][str(band)] = selection
            return {"kind": "selected", "band": band, **selection}
        available = [r for r in range(len(state["candidates"])) if r not in {a["rank"] for a in state["attempts"]}]
        if len(state["attempts"]) >= ATTEMPTS_PER_DOMAIN or not available:
            state["selections"][str(band)] = {"status": "not_found", "reason": "attempt_budget_or_candidate_list_exhausted"}
            return {"kind": "not_found", "band": band}
        ordered = sorted(measured, key=lambda r: r["rank"])
        monotonic = all(a["reference_tokens"] <= b["reference_tokens"] for a, b in zip(ordered, ordered[1:]))
        proposed = available[0]
        if measured and monotonic:
            below = [r["rank"] for r in measured if r["reference_tokens"] < band*1024]
            above = [r["rank"] for r in measured if r["reference_tokens"] >= band*1024]
            if below and above:
                proposed = (max(below)+min(above))//2
            else:
                last = measured[-1]
                proposed = math.ceil((last["rank"]+1)*band*1024/last["reference_tokens"])-1
            proposed = max(0, min(len(state["candidates"])-1, proposed))
        rank = min(available, key=lambda r: (abs(r-proposed), r))
        state["attempts"].append({"rank": rank, "target_band": band, "status": "running"})
        return {"kind": "start", "rank": rank, "target_band": band, "parameters": deepcopy(state["candidates"][rank])}
    return {"kind": "finished"}


def finish_attempt(state, rank, *, reference_tokens=None, gates=None, failure=None):
    validate_search(state)
    _finish(state, rank, reference_tokens=reference_tokens, gates=gates, failure=failure)
    state["events"].append({"kind": "finish", "rank": rank, "reference_tokens": reference_tokens,
                            "gates": deepcopy(gates), "failure": failure})


def _finish(state, rank, *, reference_tokens, gates, failure):
    row = next((r for r in state["attempts"] if r["rank"] == rank), None)
    if type(rank) is not int or row is None or row["status"] != "running":
        raise ValueError("A terminal pilot attempt cannot be replaced")
    required = set(CALIBRATION_GATES)
    if gates is not None and not isinstance(gates, dict):
        raise ValueError("Qualification gates must be an object")
    passed = failure is None and gates is not None and all(gates.get(g) is True for g in required)
    if passed and (type(reference_tokens) is not int or reference_tokens <= 0):
        raise ValueError("A measured pilot requires a positive exact history length")
    row.update(status="measured" if passed else "failed", reference_tokens=reference_tokens if passed else None,
               gates=deepcopy(gates), failure=failure if failure else None if passed else "qualification_failed")


def coverage(rows, *, frozen_cells):
    expected = {s["id"]: s for s in slots()}
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("Duplicate cell-seed results")
    qualified = 0
    for row in rows:
        slot = expected.get(row["id"])
        if slot is None or any(row.get(k) != v for k, v in slot.items()):
            raise ValueError("Result does not belong to its frozen slot")
        low, high = interval(slot["band"])
        valid = (row.get("status") == "qualified" and type(row.get("reference_tokens")) is int
                 and low <= row["reference_tokens"] <= high and all(row.get("gates", {}).get(g) is True for g in GATES))
        if row.get("status") == "qualified" and not valid:
            raise ValueError("A claimed qualified slot is out of band or lacks a gate")
        if valid:
            binding = frozen_cells.get(f"{slot['environment']}-{slot['band']}K")
            required = {"parameters", "construction_version", "reference_policy", "public_contract"}
            if (not isinstance(binding, dict) or not required <= binding.keys()
                    or any(not binding[k] for k in required) or "seed" in binding["parameters"]
                    or row.get("binding") != binding):
                raise ValueError("Qualified slot differs from its frozen cell and reference binding")
        qualified += valid
    terminal = sum(r.get("status") in {"qualified", "failed", "out_of_band"} for r in rows)
    return {"slots": len(expected), "terminal": terminal, "qualified": qualified,
            "complete": qualified == len(expected), "provider_calls": 0}
