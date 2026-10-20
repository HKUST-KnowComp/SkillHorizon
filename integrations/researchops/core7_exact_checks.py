"""Frozen C11 final-compose probes; run this FILE with python -B, not -m.

--inspect TASK reads selected originals only and prints a bounded JSON lock.
TASK --inspection LOCK --output FRESH executes only after separate code review.
Run from an original source-v2 COPY plus this checker: selected implementation
files must match source-v2. No builder, witness, provider or remote operations.
Missing evidence/candidates stop the run; fixtures are never a fallback.
Inspection: 30s, 512 MiB reads, 1 MiB stdout. Execution: 180s, 560 public
calls, five evaluations, 512 MiB output checks (not a hard disk quota).
"""

import argparse
from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import time


CELL = "C11-ambiguous_withdrawal-s7"
FROZEN = Path("/workspace/runs/skillhorizon-harbor-mainline-20260917/core7-enrichment-20260917-v1")
FORMAT = "skillhorizon.c11.calibration_network.v2"
REVISION = "c11_exact_v1"
MIB = 1024 ** 2
PARAMETERS = dict(seed=7, sections=2, rounds=3, studies_per_section=1,
                  alias_copies=1, distractors_per_section=1, background=True,
                  scenario="ambiguous_withdrawal", dependency_pattern="distributed",
                  runtime_identity="task_local_v1")
# Only the domain's execution dependencies, not a repository-wide hash tree.
CODE_FILES = (
    "integrations/researchops/__init__.py", "integrations/researchops/runtime.py",
    "integrations/researchops/kernel.py", "integrations/researchops/build.py",
    "integrations/researchops/evaluate.py", "integrations/researchops/synthesis_contract.py",
    "integrations/researchops/synthesis_evaluate.py", "integrations/internal_mcp/__init__.py",
    "integrations/internal_mcp/io.py", "integrations/internal_mcp/skills.py",
    "integrations/internal_mcp/tables.py", "integrations/internal_mcp/topology.py",
    "src/skillhorizon/__init__.py", "src/skillhorizon/host_runtime.py",
    "src/skillhorizon/util.py", "src/skillhorizon/experiment/__init__.py",
    "src/skillhorizon/experiment/construction_envelope.py",
)
SEMANTIC_ERRORS = {"positive": set(),
    "withdrawn": {"INDEPENDENT_SECTION_WEIGHTING_UNITS_AND_CALIBRATION"},
    "old_ancestor": {"CURRENT_ANCESTOR_CALIBRATION_EVIDENCE"}}
CONTRIBUTION_KEYS = {"source_key", "annotation_id", "calibration_source_key",
    "calibration_annotation_id", "sample_size", "calibrated_points",
    "effective_calibration", "calibration_chain"}


class CheckError(ValueError):
    def __init__(self, code, status="evidence_gap"):
        super().__init__(code)
        self.status = status


def need(condition, code, status="evidence_gap"):
    if not condition:
        raise CheckError(code, status)


def fingerprint(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def regular(path):
    path = Path(path).absolute()
    need(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_input")
    need(path.is_file(), "missing_file:" + str(path))
    need(path.name != ".env" and not path.name.startswith(".env."), "secret_file")
    return path


def relative_file(root, relative):
    value = Path(relative)
    need(not value.is_absolute() and value.parts and ".." not in value.parts, "unsafe_relative_file")
    return regular(Path(root) / value)


def files(root):
    root = Path(root)
    need(root.is_dir() and not any(p.is_symlink() for p in (root, *root.parents)), "unsafe_tree")
    result = []
    for index, path in enumerate(root.rglob("*")):
        need(index < 4096, "file_count_budget", "budget_blocked")
        need(not path.is_symlink(), "symlink_tree")
        if path.is_file():
            result.append(regular(path))
        else:
            need(path.is_dir(), "nonregular_tree")
    return sorted(result)


class Inputs:
    def __init__(self, deadline=None):
        self.bytes = 0
        self.manifest = {}
        self.deadline = deadline

    def read(self, path, *, json_file=False):
        need(self.deadline is None or time.monotonic() < self.deadline, "wall_clock_budget", "incomplete")
        path = regular(path)
        size = path.stat().st_size
        need(not json_file or size <= 256 * MIB, "single_json_budget", "budget_blocked")
        need(self.bytes + size <= 512 * MIB, "input_read_budget", "budget_blocked")
        with path.open("rb") as handle:
            data = handle.read(size + 1)
        self.bytes += len(data)
        need(len(data) == size, "input_changed_during_read")
        value = fingerprint(data)
        if str(path) in self.manifest:
            need(self.manifest[str(path)] == value, "original_changed:" + str(path))
        self.manifest[str(path)] = value
        return json.loads(data) if json_file else data

    def unchanged(self):
        for path in tuple(self.manifest):
            self.read(path)


def results(version):
    passages = [p for p in version["passages"] if p["passage_id"] == "results"]
    need(len(passages) == 1, "results_passage_not_unique")
    return passages[0]["text"]


def artifact_arguments(row):
    """Remove runtime-added item snapshots, retaining every submitted binding."""
    return {"bibliography_artifact_id": row["bibliography_artifact_id"],
        "sections": [{**{k: s[k] for k in ("section_id", "title", "synthesis")},
            "items": [{k: item[k] for k in ("paper_id", "annotation_id", "citation_ids", "citation_key")}
                      for item in s["items"]]} for s in row["sections"]],
        "cross_section_synthesis": row["cross_section_synthesis"]}


def final_cut(trace, initial, completed):
    need(isinstance(trace, list) and trace, "missing_trace")
    need(all(set(e) == {"name", "arguments", "output"} and isinstance(e["arguments"], dict)
             and e["output"].get("status") == "success" for e in trace), "non_success_original_trace")
    compose = [i for i, e in enumerate(trace) if e["name"] == "compose_literature_review"]
    need(compose, "no_compose", "coverage_missing")
    k = compose[-1]
    suffix = trace[k + 1:]
    need(k <= 532 and len(suffix) + 2 <= 8 and k + 3 * (len(suffix) + 2) <= 560,
         "replay_call_budget", "budget_blocked")
    need(all(e["name"] in {"finish_skill", "read_library_workspace", "read_library_artifact", "read_skill_run"}
             for e in suffix), "unsupported_original_suffix")
    need(completed["sequence"] == initial["sequence"] + len(trace), "trace_completed_sequence")
    state = completed["library"]
    final_id = trace[k]["output"]["data"]["artifact_id"]
    need(state["round"] == 2 and state["artifacts"][final_id]["round"] == 2, "not_final_round")
    for i in compose:
        event = trace[i]
        row = state["artifacts"][event["output"]["data"]["artifact_id"]]
        need(row["kind"] == "review" and artifact_arguments(row)
             == {key: value for key, value in event["arguments"].items() if key != "include_content"},
             "trace_artifact_arguments")
        matched = [e for e in state["events"] if e["type"] == "artifact.created"
                   and e["data"]["artifact_id"] == row["artifact_id"]]
        need(len(matched) == 1 and matched[0]["data"] == row
             and matched[0]["sequence"] == initial["sequence"] + i, "trace_artifact_event")
    need(all(e["sequence"] < initial["sequence"] + k or
             e["sequence"] == initial["sequence"] + k and e["type"] == "artifact.created"
             and e["data"]["artifact_id"] == final_id for e in state["events"]), "business_events_after_cut")
    for round_index in (0, 1):
        committed = state["committed_reviews"][str(round_index)]
        need(any(trace[i]["output"]["data"]["artifact_id"] == committed for i in compose[:-1]),
             "unlinked_committed_review")
    return {"k": k, "trace_length": len(trace), "suffix_names": [e["name"] for e in suffix],
            "artifact_id": final_id, "calls_upper_bound": k + 3 * (len(suffix) + 2)}


def select_candidates(trace, world, k):
    """Read-only deterministic selection; execution rechecks against the prefix."""
    state = world["library"]
    versions = {key: source["versions"][str(state["source_current"][key])]
                for key, source in state["sources"].items()}
    values = {key: json.loads(results(value)) for key, value in versions.items()}
    annotations = {a["annotation_id"]: a for a in world["tables"]["annotation"]}
    papers = {p["paper_id"]: p for p in world["tables"]["paper"]}
    creations = {e["data"]["annotation_id"]: e for e in state["events"] if e["type"] == "annotation.created"}
    call_indices = {}
    for i, event in enumerate(trace[:k]):
        if event["name"] == "add_annotation":
            row = event["output"]["data"]
            identifier = row["annotation_id"]
            need(identifier not in call_indices and identifier in creations, "annotation_creation_identity")
            creation = creations[identifier]["data"]
            need(type(row.get("current_for_source")) is bool
                 and {key: value for key, value in row.items() if key != "current_for_source"}
                 == {key: value for key, value in creation.items() if key != "source_passage_text"},
                 "annotation_trace_creation_mismatch")
            call_indices[identifier] = i

    def quote(identifier, key, *, current=False):
        need(identifier in call_indices and identifier in annotations, "missing_real_annotation")
        row = annotations[identifier]
        creation = creations[identifier]["data"]
        need({k: v for k, v in row.items() if k not in {"paper_id", "updated_at"}}
             == {k: v for k, v in creation.items() if k not in {"paper_id", "updated_at"}},
             "annotation_changed_after_creation")
        need(row["source_key"] == key and row["passage_id"] == "results"
             and papers[row["paper_id"]]["source_key"] == key and papers[row["paper_id"]]["status"] == "active",
             "annotation_ownership")
        original = state["sources"][key]["versions"][str(row["source_revision"])]
        text = results(original)
        need(row["content"] == row["source_passage_text"] == text and row["start"] == 0
             and row["end"] == len(text), "annotation_not_full_real_quote")
        if current:
            need(text == results(versions[key]), "current_annotation_not_current")
        return row

    def ancestor_path(start, target, visiting=()):
        need(start not in visiting and len(visiting) < 256, "cyclic_or_oversized_calibration_dag")
        value = values[start]
        need(value["role"] == "calibration", "non_calibration_parent")
        parents = [p["source_key"] for p in value["parent_calibrations"]]
        need(len(set(parents)) == len(parents), "duplicate_calibration_parent")
        paths = []
        for parent in parents:
            path = ancestor_path(parent, target, (*visiting, start))
            if parent == target:
                paths.append([start, parent])
            elif path:
                paths.append([start, *path])
        return paths[0] if paths else None

    args = trace[k]["arguments"]
    selected = {}
    for si, section in enumerate(args["sections"]):
        synthesis = section["synthesis"]
        keys = {c["source_key"] for c in synthesis["contributions"]}
        for exclusion in synthesis["exclusions"]:
            key = exclusion["source_key"]
            if exclusion["reason"] != "retracted_study" or key in keys:
                continue
            need(values[key]["role"] == "study" and values[key].get("evidence_status") == "retracted",
                 "withdrawal_not_in_current_source")
            for hi, event in enumerate(trace[:k]):
                if event["name"] != "compose_literature_review" or event["output"]["data"]["round"] >= 2:
                    continue
                for old_section in event["arguments"]["sections"]:
                    if old_section["section_id"] != section["section_id"]:
                        continue
                    for contribution in old_section["synthesis"]["contributions"]:
                        if contribution["source_key"] != key or "withdrawn" in selected:
                            continue
                        need(set(contribution) == CONTRIBUTION_KEYS, "historical_contribution_schema")
                        bindings = [(key, contribution["annotation_id"]),
                            (contribution["calibration_source_key"], contribution["calibration_annotation_id"]),
                            *((b["source_key"], b["annotation_id"]) for b in contribution["calibration_chain"])]
                        for source, identifier in bindings:
                            quote(identifier, source)
                            need(call_indices[identifier] < hi, "contribution_evidence_after_use")
                        selected["withdrawn"] = {"section_index": si, "section_id": section["section_id"],
                            "historical_compose_index": hi, "source_revision": state["source_current"][key],
                            "contribution": deepcopy(contribution)}
        for ci, contribution in enumerate(synthesis["contributions"]):
            study = values[contribution["source_key"]]
            need(study["role"] == "study" and study.get("evidence_status", "active") == "active"
                 and study["calibration_doi"] == contribution["calibration_source_key"], "inactive_or_wrong_anchor")
            for bi, binding in enumerate(contribution["calibration_chain"]):
                key, identifier = binding["source_key"], binding["annotation_id"]
                if key == contribution["calibration_source_key"]:
                    continue
                path = ancestor_path(contribution["calibration_source_key"], key)
                need(path, "not_strict_ancestor")
                current = quote(identifier, key, current=True)
                for old_id in sorted(call_indices, key=call_indices.get):
                    old = annotations[old_id]
                    if ("old_ancestor" in selected or old["source_key"] != key or old["passage_id"] != "results"
                            or old["content"] == results(versions[key]) or call_indices[old_id] >= call_indices[identifier]
                            or old["source_revision"] >= current["source_revision"]):
                        continue
                    quote(old_id, key)
                    selected["old_ancestor"] = {"section_index": si, "contribution_index": ci,
                        "binding_index": bi, "source_key": key, "ancestor_path": path,
                        "current_annotation_id": identifier, "old_annotation_id": old_id,
                        "current_creation": deepcopy(creations[identifier]), "old_creation": deepcopy(creations[old_id]),
                        "source_revision": state["source_current"][key]}
    return selected


def mutate(args, arm, selected):
    result = deepcopy(args)
    if arm == "withdrawn":
        choice = selected[arm]
        synthesis = result["sections"][choice["section_index"]]["synthesis"]
        need(choice["contribution"]["source_key"] not in {c["source_key"] for c in synthesis["contributions"]},
             "withdrawn_already_present")
        synthesis["contributions"].append(deepcopy(choice["contribution"]))
    elif arm == "old_ancestor":
        choice = selected[arm]
        contribution = result["sections"][choice["section_index"]]["synthesis"]["contributions"][choice["contribution_index"]]
        binding = contribution["calibration_chain"][choice["binding_index"]]
        need(binding == {"source_key": choice["source_key"], "annotation_id": choice["current_annotation_id"]}
             and binding["source_key"] != contribution["calibration_source_key"], "ancestor_binding_changed")
        binding["annotation_id"] = choice["old_annotation_id"]
    else:
        need(arm == "positive", "unknown_arm")
    return result


def read_inputs(task, reader):
    task = Path(task).absolute()
    need(len(task.parents) >= 4, "wrong_task_path")
    root = task.parents[3]
    need(task == root / "cells" / CELL / "construction/task", "wrong_cell_path")
    load = lambda p: reader.read(p, json_file=True)
    rows = [r for r in load(root / "PLAN.json")["rows"] if r["id"] == CELL]
    need(len(rows) == 1, "plan_cell_identity")
    profile = rows[0]["profile"]
    measured = load(task.parent / "MEASUREMENTS.json")
    need(profile["environment_id"] == measured["environment"] == "C11"
         and measured["construction_profile"] == profile and profile["environment_format"] == FORMAT
         and profile["skill_protocol"] == measured["skill_protocol"] == "observational_v1"
         and profile["parameters"] == profile["expected_effective"] == PARAMETERS, "full_profile_mismatch")
    public = load(task / "public/task.json")
    private = load(task / "private/initial_obligations.json")
    initial = load(task / "checkpoint/world.json")
    world = load(task / "runtime/world.json")
    trace = load(task / "trace.json")
    controller = load(task / "controller.json")
    background = load(task / "background.json")
    build = load(task / "BUILD.json")["construction_provenance"]
    requested = build["calls"][0]
    need(requested["phase"] == "requested" and requested["returned"] is True
         and requested["callable"] == "integrations.researchops.calibration_build.build"
         and {k: v for k, v in requested["arguments"].items() if k not in {"source", "output"}} == PARAMETERS,
         "construction_provenance_mismatch")
    need(public == private["public"] and initial["library"] == private["initial_state"]
         and initial["tables"] == private["initial_tables"], "private_checkpoint_mismatch")
    need(public["format"] == initial["format"] == world["format"] == FORMAT
         and public["task_id"] == initial["task_id"] == world["task_id"], "task_identity")
    need(public["scenario"]["family"] == PARAMETERS["scenario"] and public["scenario"]["construction_seed"] == 7
         and public["scenario"]["world_size"] == {k: PARAMETERS[k] for k in
             ("sections", "rounds", "studies_per_section", "alias_copies", "distractors_per_section", "background")}
         and public["runtime_identity"] == "task_local_v1" and public["rounds"] == 3
         and public["dependency_construction"]["pattern"] == "distributed"
         and public["dependency_construction"]["seed"] == 7 and any(background.values())
         and "calibration_graph" in public["synthesis_policy"], "public_specification")
    verified, evaluated = load(task / "DIRECT_VERIFICATION.json"), load(task / "EVALUATION.json")
    need(verified["accepted"] is True and verified["replay_verified"] is True and verified["provider_calls"] == 0
         and verified["calls"] == len(trace) and evaluated["accepted"] is True, "saved_baseline_not_passed")
    backend = Path(controller["source"])
    backend = (backend if backend.is_absolute() else task / backend).absolute()
    need(not any(p.is_symlink() for p in (backend, *backend.parents)), "backend_symlink")
    backend = backend.resolve()
    for relative in CODE_FILES:
        reader.read(root / "source-v2" / relative)
    reader.read(backend / "tool_classification.json", json_file=True)
    backend_files = [p for p in files(backend / "mcp_servers") if p.suffix == ".py"]
    need(len(backend_files) <= 128 and backend / "mcp_servers/server.py" in backend_files, "backend_code_inventory")
    for path in backend_files:
        reader.read(path)
    support = [task / "controller.json", task / "background.json",
               *files(task / "public"), *files(task / "private")]
    for path in support:
        if str(path) not in reader.manifest:
            reader.read(path, json_file=path.suffix == ".json")
    for directory, snapshot in (("checkpoint", initial), ("runtime", world)):
        for path in files(task / directory):
            if str(path) not in reader.manifest:
                reader.read(path, json_file=path.suffix == ".json")
        for source in snapshot["library"]["sources"].values():
            for version in source["versions"].values():
                path = relative_file(task / directory, version["path"])
                need(reader.manifest[str(path)] == fingerprint("\n\n".join(p["text"] for p in version["passages"]).encode()),
                     "source_file_bytes")
        for artifact in snapshot["library"]["artifacts"].values():
            path = relative_file(task / directory, artifact["path"])
            need(reader.manifest[str(path)] == fingerprint(artifact["content"].encode()), "artifact_file_bytes")
    return dict(task=task, root=root, profile=profile, public=public, initial=initial, world=world,
                trace=trace, backend=backend, support=support)


def inspection(task, *, deadline=None):
    reader = Inputs(deadline)
    context = read_inputs(task, reader)
    cut = final_cut(context["trace"], context["initial"], context["world"])
    selected = select_candidates(context["trace"], context["world"], cut["k"])
    reader.unchanged()
    missing = sorted({"withdrawn", "old_ancestor"} - selected.keys())
    copy_bytes = sum(v["bytes"] for p, v in reader.manifest.items()
                     if Path(p).is_relative_to(context["task"] / "runtime")) * 4
    copy_bytes += sum(reader.manifest[str(p)]["bytes"] for p in context["support"])
    need(copy_bytes <= 480 * MIB, "copy_upper_bound", "budget_blocked")
    return dict(revision=REVISION, status="coverage_missing" if missing else "ready", cell=CELL,
        task=str(context["task"]), source_v2=str(context["root"] / "source-v2"),
        controller_source=str(context["backend"]), profile=context["profile"], cut=cut,
        selected=selected, missing=missing, inputs=reader.manifest, bytes_read=reader.bytes,
        copy_upper_bound_bytes=copy_bytes, runtime_calls=0, evaluator_calls=0, provider_calls=0)


def save(path, value):
    with Path(path).open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


class Probe:
    def __init__(self, output, deadline):
        self.output, self.deadline = Path(output), deadline
        self.calls = self.evaluations = 0
        self.timings = {"copy": 0.0, "replay": 0.0, "evaluation": 0.0}

    def guard(self):
        need(time.monotonic() < self.deadline, "wall_clock_budget", "incomplete")
        need(sum(p.stat().st_size for p in files(self.output)) <= 512 * MIB, "output_space_budget", "budget_blocked")

    def log(self, row):
        with (self.output / "calls.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def call(self, env, arm, name, args, expected=None):
        self.guard()
        need(self.calls < 560, "call_budget", "budget_blocked")
        self.calls += 1
        self.log(dict(phase="before", call=self.calls, arm=arm, name=name, arguments=args))
        try:
            response = env.call(name, deepcopy(args))
        except BaseException as error:
            self.log(dict(phase="exception", call=self.calls, arm=arm, error=type(error).__name__))
            raise
        # Persist even unsuccessful/mismatched responses BEFORE checking anything.
        self.log(dict(phase="after", call=self.calls, arm=arm, response=response))
        self.guard()
        need(response.get("status") == "success", "public_call_failed:" + name, "failed")
        need(expected is None or response == expected, "response_replay_mismatch:" + name, "failed")
        return response["data"]

    def evaluate(self, evaluator, task, runtime, label):
        self.guard()
        need(self.evaluations < 5, "evaluation_budget", "budget_blocked")
        self.evaluations += 1
        start = time.monotonic()
        result = evaluator(task, runtime)
        self.timings["evaluation"] += time.monotonic() - start
        save(self.output / (label + "-evaluation.json"), result)
        self.guard()
        return result

    def copy(self, source, destination):
        self.guard()
        start = time.monotonic()
        original = files(source)
        shutil.copytree(source, destination, copy_function=shutil.copyfile)
        need([p.relative_to(source) for p in original] == [p.relative_to(destination) for p in files(destination)],
             "copy_inventory")
        for path in original:
            target = destination / path.relative_to(source)
            need(not os.path.samefile(path, target) and path.read_bytes() == target.read_bytes(), "copy_not_independent")
        self.timings["copy"] += time.monotonic() - start
        self.guard()


def outcome_assertion(result, arm, artifact_id):
    need(result.get("integrity_valid") is True and result.get("integrity_errors") == [], "integrity_failed", "failed")
    rounds = result["rounds"]
    need(len(rounds) == 3 and [r["round"] for r in rounds] == [0, 1, 2], "evaluation_round_identity", "failed")
    need(all(r["accepted"] is True and r["errors"] == [] for r in rounds[:2]), "historical_round_failed", "failed")
    need(rounds[2]["artifact_id"] == artifact_id and set(rounds[2]["errors"]) == SEMANTIC_ERRORS[arm],
         "nonexact_target_errors", "failed")
    need(result["accepted"] is (arm == "positive") and rounds[2]["accepted"] is (arm == "positive"),
         "wrong_acceptance", "failed")
    need(set(result["errors"]) == SEMANTIC_ERRORS[arm], "extra_global_errors", "failed")


def preservation(before, after, runtime):
    old, new = before["library"], after["library"]
    need(new["events"][:len(old["events"])] == old["events"], "historical_events_changed", "failed")
    need(new["sources"] == old["sources"] and before["tables"] == after["tables"], "business_bindings_changed", "failed")
    mutable = {"artifacts", "events", "skill_runs", "active_skill_run", "next_id"}
    need({k: v for k, v in new.items() if k not in mutable} == {k: v for k, v in old.items() if k not in mutable},
         "library_bindings_changed", "failed")
    need(set(new["artifacts"]) - set(old["artifacts"]) and len(new["artifacts"]) == len(old["artifacts"]) + 1,
         "extra_new_artifacts", "failed")
    for key, artifact in old["artifacts"].items():
        need(new["artifacts"].get(key) == artifact, "historical_artifact_changed", "failed")
    for source in old["sources"].values():
        for version in source["versions"].values():
            need(relative_file(runtime, version["path"]).read_bytes()
                 == "\n\n".join(p["text"] for p in version["passages"]).encode(), "historical_source_bytes", "failed")
    for artifact in old["artifacts"].values():
        need(relative_file(runtime, artifact["path"]).read_bytes() == artifact["content"].encode(),
             "historical_artifact_bytes", "failed")


def run_branches(context, lock, task, runtime_type, evaluator, probe, report):
    baseline = probe.evaluate(evaluator, task, context["task"] / "runtime", "baseline")
    outcome_assertion(baseline, "positive", lock["cut"]["artifact_id"])
    prefix = probe.output / "prefix"
    probe.copy(context["task"] / "checkpoint", prefix)
    env = runtime_type(task, prefix)
    need(env.source == context["backend"] and env.world == context["initial"], "prefix_initialization")
    k, trace = lock["cut"]["k"], context["trace"]
    start = time.monotonic()
    for event in trace[:k]:
        probe.call(env, "prefix", event["name"], event["arguments"], expected=event["output"])
    probe.timings["replay"] = time.monotonic() - start
    before = deepcopy(env.world)
    prefix_result = probe.evaluate(evaluator, task, prefix, "prefix")
    need(prefix_result.get("integrity_valid") is True and prefix_result.get("integrity_errors") == [], "prefix_integrity", "failed")
    need(select_candidates(trace, before, k) == lock["selected"], "prefix_candidates_changed", "failed")
    report["prefix"] = dict(status="replayed", calls=k, integrity_valid=True,
        world=fingerprint((prefix / "world.json").read_bytes()))
    for arm in SEMANTIC_ERRORS:
        destination = probe.output / arm
        probe.copy(prefix, destination)
        branch = runtime_type(task, destination)
        need(branch.world == before and branch.source == context["backend"], "branch_initialization")
        args = mutate(trace[k]["arguments"], arm, lock["selected"])
        save(probe.output / (arm + "-arguments.json"), args)
        data = probe.call(branch, arm, trace[k]["name"], args,
                          expected=trace[k]["output"] if arm == "positive" else None)
        identifier = data["artifact_id"]
        need(identifier == lock["cut"]["artifact_id"] and data["round"] == 2 and data["kind"] == "review",
             "compose_identity", "failed")
        for event in trace[k + 1:]:
            suffix_args = deepcopy(event["arguments"])
            if event["name"] == "finish_skill" and "output_ids" in suffix_args:
                suffix_args["output_ids"] = [identifier if i == lock["cut"]["artifact_id"] else i
                                              for i in suffix_args.get("output_ids", [])]
            elif event["name"] == "read_library_artifact" and suffix_args["artifact_id"] == lock["cut"]["artifact_id"]:
                suffix_args["artifact_id"] = identifier
            probe.call(branch, arm, event["name"], suffix_args,
                       expected=event["output"] if arm == "positive" or event["name"] != "read_library_artifact" else None)
        if arm == "positive":
            need(branch.world == context["world"], "positive_completed_world_mismatch", "failed")
        row = branch.world["library"]["artifacts"][identifier]
        need(artifact_arguments(row) == {k: v for k, v in args.items() if k != "include_content"}, "rendered_argument_bindings", "failed")
        need(identifier not in before["library"]["artifacts"] and row["path"].endswith(".md")
             and relative_file(destination, row["path"]).read_bytes() == row["content"].encode(), "review_file_not_created", "failed")
        events = branch.world["library"]["events"][len(before["library"]["events"]):]
        need(len(events) == 1 and events[0]["type"] == "artifact.created" and events[0]["data"] == row,
             "new_artifact_event", "failed")
        read = probe.call(branch, arm, "read_library_artifact", {"artifact_id": identifier})
        need(read == row, "artifact_read_mismatch", "failed")
        result = probe.evaluate(evaluator, task, destination, arm)
        outcome_assertion(result, arm, identifier)
        preservation(before, branch.world, destination)
        report["arms"][arm] = dict(status="passed", artifact_id=identifier,
            target=result["rounds"][2], accepted=result["accepted"], integrity_valid=True,
            historical_rounds=result["rounds"][:2], preservation=True)


def load_runtime(context, lock, reader, output):
    source = Path(__file__).resolve().parents[2]
    baseline = context["root"] / "source-v2"
    need(source != baseline and not source.is_relative_to(context["root"]), "use_source_v2_copy_plus_checker")
    need("SKILLHORIZON_CONSTRUCTION_APPROVAL" not in os.environ, "construction_envelope_must_be_inactive")
    for relative in CODE_FILES:
        need(fingerprint(reader.read(source / relative)) == lock["inputs"][str(baseline / relative)],
             "source_copy_diff:" + relative)
    need(not any(n == "mcp_servers" or n.startswith(("mcp_servers.", "integrations.researchops.", "skillhorizon."))
                 for n in sys.modules), "fresh_process_required_run_file_not_package")
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(source), str(source / "src")]
    modules = [importlib.import_module(name) for name in (
        "integrations.researchops.runtime", "integrations.researchops.evaluate",
        "integrations.researchops.kernel", "integrations.researchops.synthesis_evaluate")]
    io = importlib.import_module("integrations.internal_mcp.io")
    backend = io.load_backend(context["backend"])
    loaded = {}
    for module in [*modules, io, backend]:
        name, path = module.__name__, Path(module.__file__).resolve()
        need(path.is_relative_to(source) or path.is_relative_to(context["backend"]), "execution_path_outside_frozen_roots")
        expected = baseline / path.relative_to(source) if path.is_relative_to(source) else path
        need(str(expected) in lock["inputs"], "unfrozen_loaded_module:" + name)
        digest = fingerprint(reader.read(path))
        need(digest == lock["inputs"][str(expected)], "loaded_module_changed:" + name)
        loaded[name] = {"file": str(path), **digest}
    need(Path(backend.__file__).resolve() == context["backend"] / "mcp_servers/server.py", "backend_import_path")
    save(output / "loaded-identity.json", dict(modules=loaded, controller_source=str(context["backend"]),
        checker={"file": str(Path(__file__).resolve()), **fingerprint(reader.read(__file__))}))
    return modules[0].SkillLibrary, modules[1].evaluate


def execute(task, inspected, output, *, deadline=None):
    start = time.monotonic()
    deadline = min(deadline or start + 180, start + 180)
    reader = Inputs(deadline)
    context = read_inputs(task, reader)
    need(context["root"] == FROZEN, "execution_requires_original_cpu8_root")
    original_manifest = deepcopy(reader.manifest)
    lock = reader.read(inspected, json_file=True)
    need(lock["revision"] == REVISION and lock["status"] == "ready" and lock["task"] == str(context["task"])
         and lock["source_v2"] == str(FROZEN / "source-v2") and lock["profile"] == context["profile"]
         and lock["controller_source"] == str(context["backend"]) and lock["inputs"] == original_manifest,
         "saved_inspection_identity")
    need(lock["cut"] == final_cut(context["trace"], context["initial"], context["world"])
         and set(lock["selected"]) == {"withdrawn", "old_ancestor"}
         and lock["selected"] == select_candidates(context["trace"], context["world"], lock["cut"]["k"]),
         "saved_selection_changed")
    output = Path(output).absolute()
    source = Path(__file__).resolve().parents[2]
    need(not output.exists() and not any(p.is_symlink() for p in output.parents)
         and all(not output.is_relative_to(p) and not p.is_relative_to(output)
                 for p in (FROZEN, source, context["backend"])), "unsafe_output")
    need(shutil.disk_usage(output.parent).free >= 2 * 1024 ** 3, "free_space", "budget_blocked")
    need(lock["copy_upper_bound_bytes"] <= 480 * MIB, "copy_budget", "budget_blocked")
    output.mkdir()
    probe = Probe(output, deadline)
    report = dict(status="running", cell=CELL, phase="load_identity", arms={}, provider_calls=0,
                  witness_runs=0, cut=lock["cut"], selected=lock["selected"],
                  scope="construction_development_public_runtime_not_transport_or_model_behavior")
    try:
        save(output / "inspection.json", lock)
        runtime_type, evaluator = load_runtime(context, lock, reader, output)
        support = output / "task-support"
        report["phase"] = "copy_support"
        copy_start = time.monotonic()
        for path in context["support"]:
            target = support / path.relative_to(context["task"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            need(not os.path.samefile(path, target)
                 and fingerprint(target.read_bytes()) == lock["inputs"][str(path)], "support_copy_mismatch")
            probe.guard()
        probe.timings["copy"] += time.monotonic() - copy_start
        controller = json.loads((support / "controller.json").read_text())
        value = Path(controller["source"])
        need((value if value.is_absolute() else support / value).resolve() == context["backend"],
             "support_controller_resolution_changed")
        save(output / "script-identity.json", [{"original": str(p), "executed": str(support / p.relative_to(context["task"])),
            **lock["inputs"][str(p)]} for p in context["support"] if p.suffix == ".py"])
        report["phase"] = "prefix_and_three_arms"
        run_branches(context, lock, support, runtime_type, evaluator, probe, report)
        probe.guard()
        report.update(status="passed", phase="complete")
    except BaseException as error:
        report.update(status=getattr(error, "status", "incomplete" if isinstance(error, (TimeoutError, KeyboardInterrupt)) else "failed"),
                      failure=type(error).__name__, detail=str(error) if isinstance(error, (CheckError, TimeoutError)) else type(error).__name__)
    finally:
        try:
            reader.unchanged()
            report["originals_unchanged"] = True
        except BaseException as error:
            report.update(status="incomplete" if time.monotonic() >= deadline else "failed",
                          originals_unchanged=False if str(error).startswith("original_changed:") else None,
                          postcheck_error=str(error))
        report.update(calls=probe.calls, evaluations=probe.evaluations, phase_seconds=probe.timings,
                      elapsed_seconds=round(time.monotonic() - start, 3), input_bytes_read=reader.bytes,
                      output_bytes_before_report=sum(p.stat().st_size for p in files(output)))
        save(output / "REPORT.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--inspection", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.inspect == bool(args.inspection) or (args.inspect and args.output) or (not args.inspect and not args.output):
        parser.error("use TASK --inspect, or TASK --inspection LOCK --output FRESH_DIRECTORY")
    seconds = 30 if args.inspect else 180
    deadline = time.monotonic() + seconds
    def timeout(*_):
        raise TimeoutError("wall_clock_budget")
    previous = signal.signal(signal.SIGALRM, timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        report = inspection(args.task, deadline=deadline) if args.inspect else execute(
            args.task, args.inspection, args.output, deadline=deadline)
    except (CheckError, OSError, ValueError, KeyError, TypeError, IndexError, StopIteration, TimeoutError) as error:
        report = dict(status=getattr(error, "status", "incomplete" if isinstance(error, TimeoutError) else "evidence_gap"),
                      error=str(error) if isinstance(error, (CheckError, TimeoutError)) else type(error).__name__, provider_calls=0)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    text = json.dumps(report, indent=2, allow_nan=False)
    if len(text.encode()) > MIB:
        report = dict(status="budget_blocked", error="summary_budget", provider_calls=0)
        text = json.dumps(report)
    print(text)
    return 0 if report["status"] in {"ready", "passed"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
