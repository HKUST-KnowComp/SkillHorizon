"""One frozen C25 cell: read-only inspection, then separately reviewed probes.

Run this file directly with python -B. --inspect prints JSON and never imports
the runtime/evaluator. Execution requires that saved inspection and source-v2.
No builder, witness, provider, or transport qualification is performed here.
"""

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import signal
import sys
import time


CELL = "C25-expiring_amendments-s7"
FROZEN = Path("/workspace/runs/skillhorizon-harbor-mainline-20260917/core7-enrichment-20260917-v1")
FORMAT = "skillhorizon.c25.portfolio_network.v2"
MIB = 1024 ** 2
PARAMETERS = dict(seed=7, width=3, depth=4, background=True, amendment=None,
                  scenario="expiring_amendments", dependency_pattern="staggered", runtime_identity="task_local_v1")


class CheckError(ValueError):
    def __init__(self, code, status="evidence_gap"):
        super().__init__(code)
        self.status = status


def need(condition, code, status="evidence_gap"):
    if not condition:
        raise CheckError(code, status)


def regular(path):
    path = Path(path).absolute()
    need(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_input")
    need(path.is_file(), "missing_regular_file")
    return path


class Inputs:
    """Only selected non-secret files; two domains together read at most 1 GiB."""
    def __init__(self):
        self.bytes = 0
        self.manifest = {}

    def read(self, path, *, json_file=False):
        path = regular(path)
        size = path.stat().st_size
        need(not json_file or size <= 256 * MIB, "single_json_budget", "budget_blocked")
        need(self.bytes + size <= 512 * MIB, "read_budget", "budget_blocked")
        data = path.read_bytes()
        self.bytes += len(data)
        need(len(data) == size and self.bytes <= 512 * MIB, "input_changed_or_read_budget", "budget_blocked")
        fingerprint = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if str(path) in self.manifest:
            need(self.manifest[str(path)] == fingerprint, "original_changed")
        self.manifest[str(path)] = fingerprint
        return json.loads(data) if json_file else data

    def unchanged(self):
        for path in tuple(self.manifest):
            self.read(path)


def tree_files(root):
    root = Path(root)
    need(root.is_dir() and not any(p.is_symlink() for p in (root, *root.parents)), "unsafe_tree")
    files = []
    for path in root.rglob("*"):
        need(not path.is_symlink(), "symlink_tree")
        if path.is_file():
            need(path.name != ".env" and not path.name.startswith(".env."), "unexpected_secret_file")
            files.append(path)
        else:
            need(path.is_dir(), "nonregular_tree")
        need(len(files) <= 4096, "file_count_budget", "budget_blocked")
    return sorted(files)


def read_inputs(task, reader):
    task = Path(task).absolute()
    root = task.parents[3]
    need(task == root / "cells" / CELL / "construction/task", "wrong_cell_path")
    load = lambda p: reader.read(p, json_file=True)
    plan = load(root / "PLAN.json")
    measured = load(task.parent / "MEASUREMENTS.json")
    rows = [r for r in plan["rows"] if r["id"] == CELL]
    need(len(rows) == 1, "plan_cell_identity")
    profile = rows[0]["profile"]
    need(measured["environment"] == "C25" and measured["construction_profile"] == profile, "profile_measurement_mismatch")
    need(profile["environment_id"] == "C25" and profile["environment_format"] == FORMAT
         and profile["skill_protocol"] == "observational_v1" and profile["parameters"] == PARAMETERS,
         "wrong_full_profile")
    need(profile["expected_effective"] == {**PARAMETERS, "amendment": True}, "effective_profile_mismatch")
    public = load(task / "public/task.json")
    world = load(task / "runtime/world.json")
    initial = load(task / "checkpoint/world.json")
    private = load(task / "private/initial_obligations.json")
    background = load(task / "background.json")
    controller = load(task / "controller.json")
    need(public == private["public"], "public_frozen_mismatch")
    need(public["format"] == world["format"] == initial["format"] == FORMAT
         and public["task_id"] == world["task_id"] == initial["task_id"], "task_identity")
    need(public["scenario"]["family"] == PARAMETERS["scenario"] and public["scenario"]["seed"] == 7
         and public["scenario"]["world_size"] == {"width": 3, "depth": 4}
         and public["runtime_identity"] == "task_local_v1"
         and public["policy"]["skill_protocol"] == "observational_v1"
         and public["planned_change_windows"] == 2 and any(background.values()), "public_specification")
    need(all(initial["construction"].get(k) == v for k, v in
             dict(seed=7, width=3, depth=4, dependency_pattern="staggered").items()), "construction_specification")
    verification = load(task / "DIRECT_VERIFICATION.json")
    evaluation = load(task / "EVALUATION.json")
    need(verification["accepted"] and verification["replay_verified"] and verification["provider_calls"] == 0
         and evaluation["accepted"], "saved_baseline_not_passed")
    backend = Path(controller["source"])
    backend = (backend if backend.is_absolute() else task / backend).resolve()
    for relative in ("mcp_servers/server.py", "mcp_servers/tool_impl.py"):
        reader.read(backend / relative)
    # Retain exactly the completion files; no archives, provider logs, or rebuild.
    for path in tree_files(task / "runtime"):
        if str(path) not in reader.manifest:
            reader.read(path)
    return dict(task=task, root=root, public=public, world=world, private=private,
                profile=profile, backend=backend)


def page_data(world):
    pages = {p["page_id"]: p for p in world["tables"]["page"]}
    blocks = {b["block_id"]: b for b in world["tables"]["page_block"]}
    requirements = {pid: json.loads(blocks[p["requirements_block_id"]]["content"]) for pid, p in pages.items()}
    contracts = {pid: json.loads(blocks[p["contract_block_id"]]["content"]) for pid, p in pages.items()}
    return pages, blocks, requirements, contracts


def closure(requirements, roots):
    seen = set(roots)
    while True:
        expanded = seen | {p for p, r in requirements.items() if set(r["parent_page_ids"]) & seen}
        if seen == expanded:
            return seen
        seen = expanded


def conserved(contract, requirement):
    cap = contract["capacity"]
    portfolio = {s["origin_page_id"]: s for s in cap["portfolio"]}
    need(len(portfolio) == len(cap["portfolio"]), "duplicate_portfolio_origin")
    totals = Counter()
    consumers = {c["consumer_page_id"]: c for c in requirement["capacity_policy"]["consumers"]}
    quotas = cap["allocations"]
    need(len(quotas) == len(consumers) and {q["consumer_page_id"] for q in quotas} == set(consumers), "quota_set")
    for rows in [cap["uncommitted_sources"], *(q["sources"] for q in quotas)]:
        need(len({s["origin_page_id"] for s in rows}) == len(rows), "duplicate_bucket_origin")
        for source in rows:
            origin = source["origin_page_id"]
            need(type(source["units"]) is int and source["units"] > 0, "nonpositive_source")
            need(origin in portfolio and {k: v for k, v in source.items() if k != "units"}
                 == {k: v for k, v in portfolio[origin].items() if k != "units"}, "source_rights_changed")
            totals[origin] += source["units"]
    need(totals == Counter({k: s["units"] for k, s in portfolio.items()}), "origin_conservation")
    for quota in quotas:
        consumer = consumers[quota["consumer_page_id"]]
        need(type(quota["units"]) is int and 0 <= quota["units"] <= consumer["max_units"]
             and quota["units"] == sum(s["units"] for s in quota["sources"])
             and quota["activation_stage"] == consumer["activation_stage"], "quota_conservation")
    need(cap["uncommitted_units"] == sum(s["units"] for s in cap["uncommitted_sources"])
         and sum(q["units"] for q in quotas) + cap["uncommitted_units"] == contract["limits"]["batch_size"],
         "total_conservation")


def transfer(contract, requirement, selected):
    """Reallocate one real unit; never edit rights or fabricate capacity."""
    result = deepcopy(contract)
    conserved(result, requirement)
    cap = result["capacity"]
    quotas = {q["consumer_page_id"]: q for q in cap["allocations"]}
    target = quotas[selected["consumer_page_id"]]
    consumer = next(c for c in requirement["capacity_policy"]["consumers"] if c["consumer_page_id"] == selected["consumer_page_id"])
    source = next(s for s in cap["portfolio"] if s["origin_page_id"] == selected["origin_page_id"])
    expired = source["expires_stage"] < consumer["activation_stage"]
    allowed = consumer["purpose"] in source["allowed_purposes"]
    need((expired and allowed) if selected["kind"] == "expired" else (not expired and not allowed), "target_condition_changed")
    need(not any(s["origin_page_id"] == source["origin_page_id"] for s in target["sources"]), "target_already_allocated")

    def move(rows, origin, delta):
        row = next((s for s in rows if s["origin_page_id"] == origin), None)
        if row is None:
            need(delta > 0, "missing_donor")
            row = {**next(s for s in cap["portfolio"] if s["origin_page_id"] == origin), "units": 0}
            rows.append(row)
        row["units"] += delta
        need(row["units"] >= 0, "donor_underflow")
        if row["units"] == 0:
            rows.remove(row)

    donor = cap["uncommitted_sources"] if selected["donor"] == "uncommitted" else quotas[selected["donor"]]["sources"]
    move(donor, source["origin_page_id"], -1)
    move(target["sources"], source["origin_page_id"], 1)
    if selected["method"] == "swap":
        need(selected["donor"] == "uncommitted", "invalid_swap_bucket")
        legal = next(s for s in target["sources"] if s["origin_page_id"] == selected["legal_origin"])
        need(legal["expires_stage"] >= consumer["activation_stage"] and consumer["purpose"] in legal["allowed_purposes"], "swap_donor_not_legal")
        move(target["sources"], legal["origin_page_id"], -1)
        move(donor, legal["origin_page_id"], 1)
    else:
        need(selected["method"] == "transfer", "unknown_transfer_method")
        target["units"] += 1
        if selected["donor"] == "uncommitted":
            cap["uncommitted_units"] -= 1
        else:
            quotas[selected["donor"]]["units"] -= 1
    conserved(result, requirement)
    return result


def candidate_priority(selection):
    return (0 if selection["method"] == "swap" else 1 if selection["donor"] == "uncommitted" else 2,
            selection["page_id"], selection["consumer_page_id"], selection["origin_page_id"])


def candidates(context):
    world = context["world"]
    pages, _, requirements, contracts = page_data(world)
    affected = closure(requirements, [u["page_id"] for u in world["planning"]["scheduled_updates"]])
    independent = sorted(set(pages) - affected)
    need(independent, "no_independent_track", "coverage_missing")
    found = {"expired": [], "purpose": []}
    for pid in sorted(affected):
        if closure(requirements, [pid]) & set(independent):
            continue
        contract, requirement = contracts[pid], requirements[pid]
        conserved(contract, requirement)
        publication = next((p for p in world["planning"]["publications"].values()
                            if p["page_id"] == pid and p["page_version"] == pages[pid]["version"]
                            and p["review_id"] == pages[pid].get("approved_review_id")), None)
        if not publication or not publication["decision_ids"]:
            continue
        for consumer in requirement["capacity_policy"]["consumers"]:
            quota = next(q for q in contract["capacity"]["allocations"] if q["consumer_page_id"] == consumer["consumer_page_id"])
            for source in sorted(contract["capacity"]["portfolio"], key=lambda s: s["origin_page_id"]):
                expired, allowed = source["expires_stage"] < consumer["activation_stage"], consumer["purpose"] in source["allowed_purposes"]
                if (expired and not allowed) or (not expired and allowed):
                    continue
                kind = "expired" if expired else "purpose"
                residual = any(s["origin_page_id"] == source["origin_page_id"] for s in contract["capacity"]["uncommitted_sources"])
                legal = next((s for s in quota["sources"] if s["expires_stage"] >= consumer["activation_stage"]
                              and consumer["purpose"] in s["allowed_purposes"]), None)
                donor = "uncommitted" if residual else next((q["consumer_page_id"] for q in contract["capacity"]["allocations"]
                    if q is not quota and any(s["origin_page_id"] == source["origin_page_id"] for s in q["sources"])), None)
                if donor is None or not (residual and legal) and quota["units"] >= consumer["max_units"]:
                    continue
                selection = dict(kind=kind, page_id=pid, consumer_page_id=consumer["consumer_page_id"],
                    origin_page_id=source["origin_page_id"], donor=donor, method="swap" if residual and legal else "transfer",
                    legal_origin=legal["origin_page_id"] if residual and legal else None,
                    decision_id=sorted(publication["decision_ids"])[0], independent=independent,
                    invalidated_pages=sorted(closure(requirements, [pid])))
                transfer(contract, requirement, selection)
                found[kind].append(selection)
    for kind in found:
        found[kind] = sorted(found[kind], key=candidate_priority)[:32]
    return found


def inspection(task):
    reader = Inputs()
    context = read_inputs(task, reader)
    found = candidates(context)
    reader.unchanged()
    missing = [k for k, rows in found.items() if not rows]
    return dict(revision="c25_exact_v1", status="coverage_missing" if missing else "ready", cell=CELL,
                task=str(context["task"]), source_v2=str(context["root"] / "source-v2"),
                controller_source=str(context["backend"]), profile=context["profile"], candidates=found,
                selected={k: rows[0] for k, rows in found.items() if rows}, missing=missing,
                inputs=reader.manifest, bytes_read=reader.bytes, runtime_calls=0, evaluator_calls=0, provider_calls=0)


def size(root):
    return sum(p.stat().st_size for p in tree_files(root))


class Probe:
    def __init__(self, output):
        self.output = output
        self.calls = 0
        self.evaluations = 0

    def guard(self):
        need(size(self.output) <= 256 * MIB, "output_space_budget", "budget_blocked")

    def call(self, env, arm, name, args):
        self.guard()
        need(self.calls < 40, "call_budget", "budget_blocked")
        self.calls += 1
        with (self.output / "calls.jsonl").open("a") as handle:
            handle.write(json.dumps(dict(phase="before", arm=arm, call=self.calls, name=name, arguments=args)) + "\n")
        result = env.call(name, args)
        with (self.output / "calls.jsonl").open("a") as handle:
            handle.write(json.dumps(dict(phase="after", arm=arm, call=self.calls, response=result)) + "\n")
        self.guard()
        need(result.get("status") == "success", "public_call_failed:" + name, "failed")
        return result["data"]

    def evaluate(self, evaluator, task, runtime, label):
        need(self.evaluations < 5, "evaluation_budget", "budget_blocked")
        self.evaluations += 1
        result = evaluator(task, runtime)
        (self.output / (label + "-evaluation.json")).write_text(json.dumps(result, indent=2) + "\n")
        self.guard()
        return result


def review_assertion(review, negative):
    failed = {r["rule"] for r in review["checks"] if not r["passed"]}
    need(failed == ({"CAPACITY_ALLOCATION"} if negative else set()), "nonspecific_review_failure", "failed")
    need(review["status"] == ("changes_requested" if negative else "approved"), "review_outcome", "failed")
    expected = {"CURRENT_PARENT_BINDINGS", "FIELD_UNION", "CAPACITY_ALLOCATION", "BATCH_SIZE", "RETENTION_DAYS"}
    need({r["rule"] for r in review["checks"]} == expected, "review_rule_set", "failed")
    need({r["role"] for r in review["reviewer_decisions"]} == {"engineering", "security"}, "reviewer_roles", "failed")
    for vote in review["reviewer_decisions"]:
        need(vote["decision"] == ("request_changes" if negative and vote["role"] == "engineering" else "approve"), "reviewer_vote", "failed")


def outcome_assertion(result, selection, negative):
    need(result["global_errors"] == [], "global_integrity_error", "failed")
    page = next(p for p in result["pages"] if p["page_id"] == selection["page_id"])
    errors = set(page["errors"])
    if negative:
        need(result["accepted"] is False and "EXACT_CONSUMER_CAPACITY_ALLOCATION" in errors, "missing_target_failure", "failed")
        forbidden = {"SHARED_CAPACITY_CONSERVATION", "CONTRACT_SHAPE", "LIMITS", "FIELD_UNION", "PARENT_SET",
                     "PARENT_VERSION_IDENTITY", "MISSING_OR_MALFORMED_EVIDENCE"}
        need(not errors & forbidden, "confounded_target_failure", "failed")
    else:
        need(page["accepted"] is True and not errors, "positive_target_failed", "failed")
    need(all(p["accepted"] for p in result["pages"] if p["page_id"] in selection["independent"]), "independent_track_failed", "failed")


def run_arm(context, selection, negative, env, probe, evaluator, arm):
    before = deepcopy(env.world)
    pages, blocks, requirements, contracts = page_data(before)
    pid = selection["page_id"]
    page, required, good = pages[pid], requirements[pid], contracts[pid]
    need(page["lifecycle_status"] == "approved" and not any(t["page_id"] == pid and t["status"] in {"open", "reopened"}
         for t in before["tables"]["comment_thread"]), "target_not_ready")
    need(not any(d["page_id"] == pid and d["status"] == "proposed" for d in before["tables"]["decision_record"]), "existing_proposal")
    decision = next(d for d in before["tables"]["decision_record"] if d["decision_id"] == selection["decision_id"])
    need(decision["page_id"] == pid and decision["status"] == "accepted", "supersession_not_current")
    payload = transfer(good, required, selection) if negative else good
    raw = blocks[page["contract_block_id"]]["content"]
    text = json.dumps(payload) + "\n"
    if text == raw:
        text += "\n"
    need(json.loads(text) == payload and text != raw, "revision_payload")
    public = context["public"]
    editor = public["editor_user_id"]
    request = next(r for r in public["requests"] if r["page_id"] == pid)
    now = datetime.fromisoformat(before["clock_origin"].replace("Z", "+00:00")) + timedelta(seconds=before["sequence"] + 40)
    need(datetime.fromisoformat(public["review_due_at"].replace("Z", "+00:00")) > now, "review_due_at_expired")
    call = lambda name, args: probe.call(env, arm, name, args)
    revision = call("revise_page_block", dict(block_id=page["contract_block_id"], editor_user_id=editor, new_content=text))
    current = next(p for p in env.world["tables"]["page"] if p["page_id"] == pid)
    need(not revision.get("idempotent") and current["version"] == page["version"] + 1
         and sorted(revision["invalidated_pages"]) == selection["invalidated_pages"], "real_revision_required", "failed")
    history = env.state["revision_history"][pid]
    old_history = before["planning"]["revision_history"].get(pid, [])
    need(history[:-1] == old_history and len(history) == len(old_history) + 1, "revision_history_append", "failed")
    call("record_decision", dict(page_id=pid, owner_user_id=editor, decision_title="Recheck origin allocations", status="proposed",
        rationale="Submit origin allocation contract for deterministic review.", alternatives=["Keep previous allocation"],
        impact_score=5, linked_item_ids=[request["tracker_item_id"]], supersedes_decision_id=selection["decision_id"]))
    review = call("request_page_review", dict(page_id=pid, requester_user_id=editor,
        reviewer_user_ids=required["reviewer_user_ids"], due_at=public["review_due_at"]))
    call("run_document_review", {"review_id": review["review_id"]})
    review = call("read_review", {"review_id": review["review_id"]})
    review_assertion(review, negative)
    need(review["page_version"] == current["version"] and review["content_snapshot"] == env.content_snapshot(pid), "review_binding", "failed")
    if not negative:
        accepted = call("approve_review_and_transition_page", dict(review_id=review["review_id"], actor_user_id=editor))
        publication = call("publish_decision_summary", dict(workspace_id=public["workspace_id"], actor_user_id=editor,
            target_page_id=pid, decision_ids=accepted["accepted_decision_ids"]))
        call("synchronize_tracker", dict(item_id=request["tracker_item_id"], publication_id=publication["publication_id"]))
    result = probe.evaluate(evaluator, context["task"], env.runtime, arm)
    outcome_assertion(result, selection, negative)
    after_pages, _, _, after_contracts = page_data(env.world)
    for identifier, publication in before["planning"]["publications"].items():
        need(env.state["publications"].get(identifier) == publication, "old_publication_changed", "failed")
    for independent in selection["independent"]:
        need(after_pages[independent] == pages[independent] and after_contracts[independent] == contracts[independent]
             and [p for p in env.state["publications"].values() if p["page_id"] == independent]
             == [p for p in before["planning"]["publications"].values() if p["page_id"] == independent], "independent_track_changed", "failed")
    return dict(status="passed", review=review, evaluation=result, selected=selection)


def execute(task, inspected, output):
    reader = Inputs()
    context = read_inputs(task, reader)
    need(context["root"] == FROZEN, "execution_requires_original_cpu8_root")
    lock = reader.read(inspected, json_file=True)
    need(lock["revision"] == "c25_exact_v1" and lock["status"] == "ready" and lock["task"] == str(context["task"])
         and lock["profile"] == context["profile"] and lock["controller_source"] == str(context["backend"]), "inspection_lock_identity")
    current_manifest = {k: v for k, v in reader.manifest.items() if k != str(Path(inspected).absolute())}
    need(current_manifest == lock["inputs"] and set(lock["selected"]) == {"expired", "purpose"}, "inspection_inputs_changed")
    output = Path(output).absolute()
    need(not output.exists() and not output.is_relative_to(FROZEN) and not FROZEN.is_relative_to(output)
         and not output.is_relative_to(context["backend"]) and not any(p.is_symlink() for p in output.parents), "unsafe_output")
    need(shutil.disk_usage(output.parent).free >= 2 * 1024 ** 3, "free_space", "budget_blocked")
    need(size(context["task"] / "runtime") * 4 <= 240 * MIB, "copy_budget", "budget_blocked")
    baseline = FROZEN / "source-v2"
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(baseline), str(baseline / "src")]
    runtime = importlib.import_module("integrations.docops.runtime")
    evaluation = importlib.import_module("integrations.docops.evaluate")
    kernel = importlib.import_module("integrations.docops.kernel")
    for module in (runtime, evaluation, kernel):
        need(Path(module.__file__).resolve().is_relative_to(baseline), "not_source_v2_run_as_direct_script")
    output.mkdir()
    probe = Probe(output)
    report = dict(status="running", cell=CELL, provider_calls=0, arms={}, loaded_modules={m.__name__: m.__file__ for m in (runtime, evaluation, kernel)},
                  controller_source=str(context["backend"]), scope="exact_cell_public_runtime_not_transport")
    started = time.monotonic()
    try:
        baseline_result = probe.evaluate(evaluation.evaluate, context["task"], context["task"] / "runtime", "baseline")
        need(baseline_result["accepted"], "baseline_not_accepted", "failed")
        for kind in ("expired", "purpose"):
            selection = lock["selected"][kind]
            need(selection in lock["candidates"][kind] and selection["kind"] == kind, "unlocked_selection")
            for negative in (False, True):
                arm = kind + ("-negative" if negative else "-positive")
                destination = output / arm
                shutil.copytree(context["task"] / "runtime", destination)
                env = runtime.SkillPlanning(context["task"], destination)
                need(env.world == context["world"] and env.source == context["backend"], "copy_or_backend_mismatch")
                report["arms"][arm] = run_arm(context, selection, negative, env, probe, evaluation.evaluate, arm)
        report["status"] = "passed"
    except BaseException as error:
        report.update(status=getattr(error, "status", "incomplete" if isinstance(error, TimeoutError) else "failed"), failure=type(error).__name__, detail=str(error))
        raise
    finally:
        try:
            reader.unchanged()
            report["originals_unchanged"] = True
        except Exception:
            report.update(status="failed", originals_unchanged=False)
            raise
        finally:
            report.update(calls=probe.calls, evaluations=probe.evaluations, elapsed_seconds=round(time.monotonic() - started, 3))
            (output / "REPORT.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--inspection", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.inspect == bool(args.inspection) or (not args.inspect and args.output is None) or args.inspect and args.output:
        parser.error("use --inspect alone, or --inspection REPORT --output FRESH_DIRECTORY")
    def timeout(*_):
        raise TimeoutError("wall_clock_budget")
    previous = signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, 30 if args.inspect else 120)
    try:
        report = inspection(args.task) if args.inspect else execute(args.task, args.inspection, args.output)
    except (CheckError, OSError, KeyError, TypeError, StopIteration, TimeoutError, json.JSONDecodeError) as error:
        report = dict(status=getattr(error, "status", "incomplete" if isinstance(error, TimeoutError) else "evidence_gap"),
                      error=str(error) if isinstance(error, (CheckError, TimeoutError)) else type(error).__name__, provider_calls=0)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    text = json.dumps(report, indent=2)
    if len(text.encode()) > MIB:
        text = json.dumps(dict(status="budget_blocked", error="summary_budget"))
        report["status"] = "budget_blocked"
    print(text)
    return 0 if report["status"] in {"ready", "passed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
