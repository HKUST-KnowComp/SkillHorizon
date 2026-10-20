"""Offline field comparison, not execution qualification or a provenance ledger."""

from pathlib import Path
import random

from integrations.internal_mcp.io import read_json
from skillhorizon.util import canonical_json, load_jsonl
from skillhorizon.experiment.business_binding import normalize_business_binding, business_binding
from skillhorizon.experiment.config import RunConfig
from skillhorizon.experiment.workspace import normalize_workspace_config
from skillhorizon.experiment.workspace_bundle import workspace_config
from skillhorizon.experiment.python_execution import normalize_python_execution
from skillhorizon.experiment.execution_bundle import execution_config
from .catalog import effective_parameter
from skillhorizon.construction_provenance import KEY, REVISION, provenance_evidence


MISSING = object()


def field(record, *path):
    for key in path:
        if not isinstance(record, dict) or key not in record:
            return MISSING
        record = record[key]
    return record


def compare(name, expected, actual, *, nullable=False):
    missing = expected is MISSING or actual is MISSING
    unknown = missing or (not nullable and (expected is None or actual is None))
    status = "unknown" if unknown else "match" if canonical_json(expected) == canonical_json(actual) else "mismatch"
    # Reports contain field names and verdicts, never histories or arbitrary PLAN values.
    return {"field": name, "status": status}


def summary(checks):
    states = {row["status"] for row in checks}
    return {"status": "mismatch" if "mismatch" in states else "unknown" if "unknown" in states or not states else "match",
        "checks": checks}


def field_layout_checks(profile, task, world):
    """Reconstruct layout facts without calling the device-layout generator."""
    counts = effective_parameter(profile, "device_counts")
    if counts is None:
        return []
    revision = "field_device_layout_v1"
    checks = [compare("task.device_layout_revision", revision, field(task, "device_layout_revision")),
              compare("checkpoint.device_counts", counts, field(world, "device_counts")),
              compare("checkpoint.device_layout.revision", revision, field(world, "device_layout", "revision"))]
    parts = field(world, "parts")
    checks.append(compare("checkpoint.parts.keys", ["spare-A", "spare-B"],
                          sorted(parts) if isinstance(parts, dict) else MISSING))
    prefix = field(task, "checkpoint_action_count")
    checks.append(compare("task.checkpoint_action_count.valid", True,
                          MISSING if prefix is MISSING else type(prefix) is int and prefix >= 0))
    expected, roles, ordinal = {}, {}, 1
    for stage, count in enumerate(counts, 1):
        identifiers = [f"D{i:02d}" for i in range(ordinal, ordinal + count)]
        values = ["low", "high"] if count == 2 else ["low" if stage % 2 else "high"]
        expected[str(stage)] = {"device_ids": identifiers, "severity_roles": values}
        roles.update(zip(identifiers, values))
        ordinal += count
    checks.append(compare("checkpoint.device_layout.stages", expected, field(world, "device_layout", "stages")))
    for collection, keys in (("services", expected), ("devices", roles)):
        actual = field(world, collection)
        checks.append(compare("checkpoint." + collection + ".keys", sorted(keys),
                              sorted(actual) if isinstance(actual, dict) else MISSING))
    p = profile["parameters"]
    for stage, row in enumerate(expected.values(), 1):
        parents = sorted({stage - 1, stage - 3}) if stage > 3 else ([stage - 1] if stage > 1 else [])
        if p["business_program"] == "pressure_branches_v3":
            parents = [n for n in (stage - 4, stage - 2) if n > 0]
            if stage == len(counts):
                parents = sorted({stage - 1, stage - 2, stage - 4})
        if p["business_scenario"] in {"mesh_convergence", "mesh_supply_shocks"}:
            parents = [] if stage <= 3 else [stage - 3]
            if stage >= 6:
                parents = sorted(set(parents + [stage - 2, stage - 5]))
            if stage == len(counts):
                parents = sorted(set(parents + [stage - 1, 1]))
        for name, value in (("stage", stage), ("device_ids", row["device_ids"]), ("parent_stages", parents)):
            checks.append(compare(f"checkpoint.services.{stage}.{name}", value,
                                  field(world, "services", str(stage), name)))
        predecessors = [key for parent in parents for key in expected[str(parent)]["device_ids"]]
        for key in row["device_ids"]:
            for name, value in (("stage", stage), ("type", "pump"), ("boundaries", [f"feed-{stage}"]),
                                ("predecessors", predecessors)):
                checks.append(compare(f"checkpoint.devices.{key}.{name}", value, field(world, "devices", key, name)))
            severity = field(world, "devices", key, "severity")
            valid = MISSING if severity is MISSING else (type(severity) is int and
                (6 <= severity <= 9 if roles[key] == "low" else 10 <= severity <= 12))
            checks.append(compare(f"checkpoint.devices.{key}.severity_role", True, valid))
    for part, quantity in (("spare-A", sum(role == "high" for role in roles.values())), ("spare-B", 0)):
        checks.append(compare(f"checkpoint.parts.{part}.initial_quantity", quantity,
                              field(world, "parts", part, "initial_quantity")))
        checks.append(compare(f"checkpoint.parts.{part}.compatible_type", "pump",
                              field(world, "parts", part, "compatible_type")))
        current = field(world, "parts", part, "quantity")
        checks.append(compare(f"checkpoint.parts.{part}.quantity_integer", True,
                              MISSING if current is MISSING else type(current) is int))
        if type(prefix) is int and prefix == 0:
            checks.append(compare(f"checkpoint.parts.{part}.initial_stock", quantity, current))
    return checks


def procure_contract_checks(profile, world):
    """Independently bind quality-reuse requests and obligations to the seed."""
    parameters = profile["parameters"]
    if parameters.get("business_program") != "assembly_network_v3" or parameters.get("business_scenario") != "quality_reuse":
        return []
    groups, seed = parameters["groups"], parameters["seed"]
    rng = random.Random(seed)
    quantities = [4 + rng.randrange(3) for _ in range(groups)]
    transfers = []
    for origin in range(1, groups):
        consumers = list(range(origin + 1, groups + 1))
        rng.shuffle(consumers)
        for index, consumer in enumerate(sorted(consumers[:2])):
            transfers.append({"origin_stage": origin, "consumer_stage": consumer,
                              "quantity": 2 if index == 0 else 1})
    contract = {"stages": groups, "opening_cash_cents": 800, "immediate_cents_per_unit": 100,
                "retention_cents_per_unit": 25, "retention_delay_stages": 3}
    rules = {"reuse_delays_stages": [2, 5], "minimum_grade": 3,
             "conditioning_cost_cents_per_unit": 5, "conditioning_units_per_stage": 8,
             "material_transfers": transfers}
    checks = [compare(f"checkpoint.{collection}.{name}", expected, field(world, collection, name))
              for collection, values in (("contract", contract), ("assembly_rules", rules))
              for name, expected in values.items()]
    checks.append(compare("checkpoint.total_budget_cents", sum(quantities) * 130, field(world, "total_budget_cents")))
    requests = field(world, "requests")
    keys = [f"REQ{i:02d}" for i in range(1, groups + 1)]
    checks.append(compare("checkpoint.requests.keys", sorted(keys),
                          sorted(requests) if isinstance(requests, dict) else MISSING))
    for stage, (key, quantity) in enumerate(zip(keys, quantities), 1):
        values = {"stage": stage, "quantity": quantity, "earliest_slot": (stage - 1) * 8 + 1,
                  "delivery_deadline": stage * 8 + 8, "eligible_suppliers": ["SUP-A", "SUP-B", "SUP-C"]}
        for name, expected in values.items():
            checks.append(compare(f"checkpoint.requests.{key}.{name}", expected, field(world, "requests", key, name)))
    suppliers = field(world, "suppliers")
    checks.append(compare("checkpoint.suppliers.keys", ["SUP-A", "SUP-B", "SUP-C"],
                          sorted(suppliers) if isinstance(suppliers, dict) else MISSING))
    for key, price, grade in (("SUP-A", 100, 2), ("SUP-B", 110, 3), ("SUP-C", 125, 3)):
        for name, expected in {"unit_price_cents": price, "component_grade": grade,
                               "capacity": sum(quantities), "per_slot_capacity": 3, "earliest_slot": 1}.items():
            checks.append(compare(f"checkpoint.suppliers.{key}.{name}", expected, field(world, "suppliers", key, name)))
    return checks


def c06_exposure_checks(profile, public, world, private):
    """Check real grid/ownership facts, independently of the wrapper's metadata."""
    from collections import Counter
    p = profile["parameters"]
    checks = [compare("exposure.family", "persistent_exposure", field(public, "scenario", "family")),
        compare("exposure.task_identity", f'c06-exposure-{p["seed"]}-{p["campaigns"]}c-{p["waves"]}w',
                field(world, "task_id")),
        compare("exposure.private.public", public, field(private, "public"))]
    for name, expected in (("initial_tables", field(world, "tables")),
                           ("briefs", field(world, "social", "briefs")),
                           ("response_profiles", field(world, "social", "response_profiles")),
                           ("scheduled_updates", field(world, "social", "scheduled_updates"))):
        checks.append(compare("exposure.private." + name, expected, field(private, name)))
    campaigns, posts, segments, assets = [field(world, "tables", name) for name in
                                          ("campaign", "content_post", "audience_segment", "asset")]
    briefs = field(world, "social", "briefs")
    values = {"campaigns": campaigns, "posts": posts, "segments": segments, "assets": assets, "briefs": briefs}
    for name, value in values.items():
        required_type = dict if name == "briefs" else list
        checks.append(compare("exposure." + name + ".shape", True,
                              MISSING if value is MISSING else isinstance(value, required_type)))
    if not all(isinstance(v, dict if k == "briefs" else list) for k, v in values.items()):
        return checks

    def fact(name, check):
        try:
            value = check()
        except KeyError:
            value = MISSING
        except (TypeError, ValueError, IndexError):
            value = False
        checks.append(compare("exposure." + name, True, value))

    fact("campaign_count", lambda: len(campaigns) == p["campaigns"] == len({c["campaign_id"] for c in campaigns}))
    fact("brief_grid", lambda: all(type(b["wave"]) is int for b in briefs.values()) and
         Counter((b["campaign_id"], b["wave"]) for b in briefs.values()) ==
         Counter({(c["campaign_id"], wave): 1 for c in campaigns for wave in range(1, p["waves"] + 1)}))
    fact("public_briefs", lambda: public["brief_ids"] == list(briefs) and
         all(key == b["brief_id"] for key, b in briefs.items()))
    fact("post_count", lambda: len(posts) == p["campaigns"] * p["waves"] * 3 == len({r["post_id"] for r in posts}))
    fact("post_ownership", lambda: all(r["brief_id"] in briefs and
         r["campaign_id"] == briefs[r["brief_id"]]["campaign_id"] for r in posts))
    fact("post_roles", lambda: all(Counter((r["role"], r["variant_index"]) for r in posts if r["brief_id"] == key) ==
         Counter({("variant", 0): 1, ("variant", 1): 1, ("deployment", None): 1}) for key in briefs))
    fact("post_references", lambda: all(b["variant_post_ids"] == [r["post_id"] for r in posts
         if r["brief_id"] == key and r["role"] == "variant"] and
         [b["deployment_post_id"]] == [r["post_id"] for r in posts if r["brief_id"] == key and r["role"] == "deployment"]
         for key, b in briefs.items()))
    def exact_parents():
        from integrations.internal_mcp.topology import parent_positions
        campaign_ids = [c["campaign_id"] for c in campaigns]
        grid = {(b["wave"] - 1, b["campaign_id"]): key for key, b in briefs.items()}
        for b in briefs.values():
            expected = {grid[stage, campaign_ids[lane]] for stage, lane in parent_positions(
                b["wave"] - 1, campaign_ids.index(b["campaign_id"]), p["campaigns"], p["seed"], p["dependency_pattern"])}
            if len(set(b["parent_brief_ids"])) != len(b["parent_brief_ids"]) or set(b["parent_brief_ids"]) != expected:
                return False
        return True
    fact("parent_relationships", exact_parents)
    fact("segment_count", lambda: len(segments) == 1 + p["campaigns"] * 2 == len({r["segment_id"] for r in segments}))
    fact("segment_policy", lambda: public["exposure_policy"]["segments"] ==
         [{"segment_id": r["segment_id"], "memory_hours": 168 if i == 0 else 72 if i % 2 else 120}
          for i, r in enumerate(segments)])
    fact("allowed_audiences", lambda: all(b["allowed_audience_ids"] == [r["segment_id"] for r in segments] and
         all(set(ids) <= set(b["allowed_audience_ids"]) for ids in b["initial_variant_audiences"]) for b in briefs.values()))
    responses = field(world, "social", "response_profiles")
    if responses is MISSING:
        checks.append(compare("exposure.response_profiles", True, MISSING))
    else:
        fact("response_keys", lambda: set(responses) == {r["post_id"] for r in posts if r["role"] == "variant"})
        fact("response_ranges", lambda: all(type(responses[r["post_id"]]["clicks"]) is int and
             responses[r["post_id"]]["impressions"] == 2000 and
             (110 if r["variant_index"] == 0 else 150) <= responses[r["post_id"]]["clicks"] <=
             (122 if r["variant_index"] == 0 else 162) for r in posts if r["role"] == "variant"))
    fact("exposure_rule_inputs", lambda: all(b["variant_exposure_units"] == [3+i, 5+i] and
         b["variant_fatigue_sensitivity"] == [1, 5] for i, c in enumerate(campaigns)
         for b in briefs.values() if b["campaign_id"] == c["campaign_id"]))
    updates = field(world, "social", "scheduled_updates")
    if updates is MISSING:
        checks.append(compare("exposure.rights_update", True, MISSING))
    else:
        fact("rights_update", lambda: len(updates) == 1 and updates[0]["after_wave"] == 1 and
             updates[0]["asset_id"] == [a["asset_id"] for a in assets if a["campaign_id"] == campaigns[0]["campaign_id"]][0])
    return checks


def construction_identity(profile, records, *, provenance_checks=None):
    """Read only facts actually serialized by the selected task/world producers."""
    ops = profile["source"] is None
    task = records.get("task.json", {})
    public = records.get("checkpoint/environment/public/task.json", records.get("public/task.json", {}))
    checks = []
    provenance = field(records, "BUILD.json", KEY)
    if provenance is not MISSING:
        checks.extend(provenance_checks if provenance_checks is not None else
            [{"field": "provenance.binding", "status": "unknown"}])
        checks.append(compare("provenance.revision", REVISION, field(provenance, "revision")))
        calls = field(provenance, "calls")
        calls = calls if isinstance(calls, list) else []
        entry = calls[0] if calls else {}
        checks.append(compare("provenance.callable", profile["builder"], field(entry, "callable")))
        checks.append(compare("provenance.entry_phase", "requested", field(entry, "phase")))
        checks.append(compare("provenance.returned", True, field(entry, "returned")))
        for name, expected in profile["parameters"].items():
            checks.append(compare("provenance.requested." + name, expected,
                field(entry, "arguments", name), nullable=True))
        consumers = profile.get("consumers", [])
        observed = [field(call, "callable") for call in calls[1:]]
        if not consumers or any(value is MISSING for value in observed) or (
                observed == consumers[:len(observed)] and len(observed) < len(consumers)):
            checks.append({"field": "provenance.consumers", "status": "unknown"})
        else:
            checks.append(compare("provenance.consumers", consumers, observed))
        for call in calls[1:]:
            if field(call, "phase") != "delegated" or field(call, "returned") is not True:
                checks.append({"field": "provenance.delegated_call", "status": "mismatch"})
                continue
            arguments = field(call, "arguments")
            if isinstance(arguments, dict):
                for name in profile["parameters"]:
                    if name not in arguments:
                        continue
                    value = arguments[name]
                    checks.append(compare("provenance.delegated." + name,
                        effective_parameter(profile, name), value, nullable=True))
        # A returned call proves invocation, not that the callee used a value.
        # Missing serialized facts stay unknown, even for a complete call chain.
    if ops:
        checks.append(compare("task.format", profile["environment_format"], field(task, "format")))
        for name, expected in profile["parameters"].items():
            actual = field(task, "config", name)
            checks.append(compare("task.config." + name, expected, actual, nullable=True))
        checks.append(compare("task.execution_mode", profile["skill_protocol"], field(task, "execution_mode")))
        world = records.get("checkpoint/project.json", {})
        checks.append(compare("checkpoint.project_id", field(task, "task_id"), field(world, "project_id")))
        if profile["environment_id"] != "DataOps":
            checks.append(compare("checkpoint.domain", profile["environment_id"].lower(), field(world, "domain")))
            for name in ("composition", "resource_profile"):
                checks.append(compare("checkpoint." + name, profile["parameters"][name], field(world, name)))
        for name in ("family", "business_program", "business_scenario"):
            checks.append(compare("checkpoint." + name, profile["parameters"][name], field(world, name),
                nullable=name == "business_scenario" and profile["environment_id"] in {"FieldOps", "ProcureOps"}))
        if profile["environment_id"] == "FieldOps" and "device_counts" in profile["parameters"]:
            checks.extend(field_layout_checks(profile, task, world))
        if profile["environment_id"] == "ProcureOps":
            checks.extend(procure_contract_checks(profile, world))
        if profile["environment_id"] == "DataOps":
            checks.append(compare("checkpoint.environment", "dataops-integrated", field(world, "environment")))
            checks.append(compare("checkpoint.notice_policy", profile["parameters"]["notice_policy"], field(world, "notice_policy")))
            for name in ("regions", "periods"):
                values = field(world, name)
                checks.append(compare("checkpoint." + name, profile["parameters"][name],
                    len(values) if isinstance(values, list) else MISSING))
        not_applicable = ["background", "dependency_pattern", "runtime_identity", "backend_source"]
    else:
        checks.append(compare("public.format", profile["environment_format"], field(public, "format")))
        from .programs import EXPOSURE
        from .short_program_facts import checks as short_checks
        prefix = "checkpoint/environment/" if "checkpoint/environment/public/task.json" in records else ""
        checks.extend(short_checks(profile, public, records.get("checkpoint/world.json", {}),
                                   records.get(prefix + "private/initial_obligations.json", {})))
        from .supplemental_program_facts import checks as supplemental_checks
        checks.extend(supplemental_checks(profile, public, records.get("checkpoint/world.json", {}),
                                         records.get(prefix + "private/initial_obligations.json", {})))
        if profile["environment_format"] == EXPOSURE:
            prefix = "checkpoint/environment/" if "checkpoint/environment/public/task.json" in records else ""
            checks.extend(c06_exposure_checks(profile, public, records.get("checkpoint/world.json", {}),
                          records.get(prefix + "private/initial_obligations.json", {})))
        if profile["environment_id"] in {"C25", "C47"}:
            prefix = "checkpoint/environment/" if "checkpoint/environment/public/task.json" in records else ""
            private = records.get(prefix + "private/initial_obligations.json", {})
            expected, actual = field(public, "scenario"), field(private, "public", "scenario")
            required = [("family",), ("seed",), ("world_size",)]
            required += [("world_size", key) for key in
                         (("width", "depth") if profile["environment_id"] == "C25" else
                          ("units", "periods", "resources_per_unit"))]
            private_checks = [compare("private.public.scenario." + ".".join(path),
                                     field(expected, *path), field(actual, *path)) for path in required]
            checks.extend(private_checks)
            if all(row["status"] != "unknown" for row in private_checks):
                checks.append(compare("private.public.scenario", expected, actual))
        world = records.get("checkpoint/world.json", {})
        background = records.get("checkpoint/environment/background.json", records.get("background.json", MISSING))
        for name, expected in profile["parameters"].items():
            if name == "scenario":
                actual = field(public, "scenario", "family")
            elif name == "seed":
                key = "construction_seed" if profile["environment_id"] in {"C01", "C11"} else "seed"
                actual = field(public, "scenario", key)
            elif name == "dependency_pattern":
                disclosed = field(public, "dependency_construction", "pattern")
                actual = field(world, "construction", name) if profile["environment_id"] == "C25" else MISSING
                if actual is MISSING:
                    actual = disclosed
                elif disclosed is not MISSING:
                    checks.append(compare("public.dependency_construction.pattern", actual, disclosed))
            elif name == "runtime_identity":
                actual = field(public, name)
            elif name == "decode_records_per_capacity":
                actual = field(public, "workload_policy", name)
            elif name == "amendment" and profile["environment_id"] == "C25":
                windows = field(public, "planned_change_windows")
                actual = bool(windows) if type(windows) is int and windows >= 0 else MISSING
            elif name == "background":
                actual = field(world, "construction", "background_retained")
                if actual is MISSING and isinstance(background, dict) and background and all(type(rows) is list for rows in background.values()):
                    actual = any(background.values())
            else:
                actual = field(public, "scenario", "world_size", name)
            checks.append(compare("public." + name, effective_parameter(profile, name), actual, nullable=True))
        declarations = field(world, "construction")
        required = {"C09": ("seed", "cohorts", "legs", "background_retained"),
                    "C25": ("seed", "width", "depth")}.get(profile["environment_id"], ())
        if declarations is not MISSING and not isinstance(declarations, dict):
            checks.append({"field": "checkpoint.construction", "status": "mismatch"})
        declarations = declarations if isinstance(declarations, dict) else {}
        for name in sorted(set(declarations) | set(required)):
            actual = declarations.get(name, MISSING)
            parameter = "background" if name == "background_retained" else name
            if parameter in profile["parameters"]:
                checks.append(compare("checkpoint.construction." + name,
                    effective_parameter(profile, parameter), actual, nullable=True))
        public_sizes = field(public, "scenario", "world_size")
        if isinstance(public_sizes, dict):
            for name, actual in public_sizes.items():
                if name in profile["parameters"]:
                    checks.append(compare("public.world_size." + name,
                        effective_parameter(profile, name), actual, nullable=True))
        checks.append(compare("checkpoint.task_id", field(public, "task_id"), field(world, "task_id")))
        checks.append(compare("checkpoint.format", profile["environment_format"], field(world, "format")))
        if task:
            checks.extend([compare("task.environment_id", profile["environment_id"], field(task, "config", "environment_id")),
                compare("task.environment_format", profile["environment_format"], field(task, "environment_format")),
                compare("task.skill_protocol", profile["skill_protocol"], field(task, "skill_protocol")),
                compare("task.task_id", field(public, "task_id"), field(task, "task_id")),
                compare("task.scenario", field(public, "scenario"), field(task, "config", "scenario"))])
        # A copied backend has no source-path provenance field. Do not infer one from its name.
        source_checks = [row for row in (provenance_checks or [])
            if row["field"] in {"provenance.source_content", "provenance.backend_copy", "backend_source"}]
        checks.append({"field": "backend_source", "status": summary(source_checks)["status"]})
        not_applicable = ["business_program", "execution_mode.procedural"]
    return {**summary(checks), "not_applicable": not_applicable}


def run_identity(profile, plan, records, exported, *, task_root):
    checks = [compare("plan.environment_id", profile["environment_id"], field(plan, "environment_id"))]
    task, copy = records.get("task.json", {}), exported.get("private_task.json", {})
    actual_path = str(Path(task_root).resolve())
    declared_path = field(plan, "task")
    if isinstance(declared_path, str):
        declared_path = str(Path(declared_path).resolve())
    checks.append(compare("plan.task", declared_path, actual_path))
    for name in ("task_id", "checkpoint_action_count", "history_source"):
        checks.append(compare("plan." + name, field(plan, name), field(task, name)))
        checks.append(compare("export." + name, field(task, name), field(copy, name)))
    if profile["source"] is not None:
        for name in ("environment_format", "skill_protocol", "instance_identity"):
            checks.append(compare("plan." + name, field(plan, name), field(task, name)))
        checks.append(compare("plan.scenario", field(plan, "scenario"), field(task, "config", "scenario")))
    actions = records.get("checkpoint/model_skill_actions.jsonl", MISSING)
    copied_actions = exported.get("checkpoint/model_skill_actions.jsonl", MISSING)
    checks.append(compare("checkpoint.prefix_count", field(task, "checkpoint_action_count"),
        len(actions) if isinstance(actions, list) else MISSING))
    checks.append(compare("export.prefix_actions", actions, copied_actions))
    checks.append(compare("export.task", task or MISSING, copy or MISSING))
    # Empty lists are observed zero-length prefixes, unlike missing JSONL files.
    for name in ("checkpoint/project.json", "checkpoint/world.json", "checkpoint/environment/public/task.json"):
        if name in records or name in exported:
            checks.append(compare("export." + name, records.get(name, MISSING), exported.get(name, MISSING)))
    return summary(checks)


def capability_identity(plan, exported, bundle):
    """Reuse control.check_plan_workspace's validators without importing its CLI/SDK."""
    keys = ("workspace", "python_execution", "ptc", "business_transport", "skill_documentation", "delivery_contract", "config")
    checks = [compare("plan." + key, field(plan, key), field(plan, key),
        nullable=key in {"workspace", "python_execution", "ptc", "delivery_contract"}) for key in keys]
    if any(row["status"] == "unknown" for row in checks):
        return summary(checks)
    manifest, public = exported.get("manifest.json", {}), exported.get("public/task.json", {})
    try:
        workspace = normalize_workspace_config(plan["workspace"]) if plan["workspace"] is not None else None
        execution = normalize_python_execution(plan["python_execution"], workspace)
        config = RunConfig.from_record(plan["config"])
        binding = normalize_business_binding(ptc=plan["ptc"], transport=plan["business_transport"],
            observation_mode=plan["skill_documentation"] if plan["business_transport"] == "managed_worker" else "full",
            workspace=workspace, execution=execution, max_actions=config.max_tool_calls or config.max_turns)
        expected = {**binding, "workspace": workspace, "python_execution": execution,
            "delivery_contract": plan["delivery_contract"]}
        if execution is not None:
            expected["execution_max_actions"] = config.max_tool_calls or config.max_turns
        for key, value in expected.items():
            for source, record in (("manifest", manifest), ("public", public)):
                checks.append(compare(source + "." + key, value, field(record, key), nullable=True))
        if workspace is not None:
            checks.append(compare("plan.workspace_documentation", "full", plan["skill_documentation"]))
        # These also validate public schemas/categories and cross-document declarations.
        checks.extend([compare("validated.workspace", workspace, workspace_config(bundle), nullable=True),
            compare("validated.execution", execution, execution_config(bundle), nullable=True),
            compare("validated.business_binding", binding, business_binding(bundle))])
    except (FileNotFoundError, KeyError):
        checks.append({"field": "export.capability_validation", "status": "unknown"})
    except (ValueError, TypeError):
        checks.append({"field": "export.capability_validation", "status": "mismatch"})
    return summary(checks)


def read_records(root, *, exported=False, environment=None):
    root = Path(root)
    names = ["checkpoint/project.json", "checkpoint/world.json", "checkpoint/environment/public/task.json", "public/task.json",
             "background.json", "checkpoint/environment/background.json"]
    names += ["BUILD.json", *(["manifest.json", "private_task.json"] if exported else ["task.json"])]
    records = {}
    for name in names:
        try:
            records[name] = read_json(root / name)
        except FileNotFoundError:
            pass
    from .programs import EXPOSURE, SHORT_PROGRAMS, SUPPLEMENTAL_PROGRAMS
    initial_programs = {**SHORT_PROGRAMS, **SUPPLEMENTAL_PROGRAMS}
    prefix = "checkpoint/environment/" if "checkpoint/environment/public/task.json" in records else ""
    if (environment in {"C25", "C47"} or (environment == "C06"
            and field(records.get(prefix + "public/task.json", {}), "format") == EXPOSURE)
            or (environment in initial_programs and
                field(records.get(prefix + "public/task.json", {}), "format") == initial_programs[environment])):
        name = prefix + "private/initial_obligations.json"
        try:
            records[name] = read_json(root / name)
        except FileNotFoundError:
            pass
    path = root / "checkpoint/model_skill_actions.jsonl"
    if path.is_file():
        records["checkpoint/model_skill_actions.jsonl"] = load_jsonl(path)
    return records


def check_profile(profile, task_root, *, plan=None, export_bundle=None):
    records = read_records(task_root, environment=profile["environment_id"])
    _, provenance_checks = provenance_evidence(task_root, expected_source=profile["source"])
    construction = construction_identity(profile, records, provenance_checks=provenance_checks)
    exported = read_records(export_bundle, exported=True, environment=profile["environment_id"]) if export_bundle is not None else {}
    run = run_identity(profile, plan or {}, records, exported, task_root=task_root)
    capabilities = capability_identity(plan or {}, exported, export_bundle) if export_bundle is not None else summary([])
    if export_bundle is not None:
        _, export_checks = provenance_evidence(export_bundle, expected_source=profile["source"])
        if field(records, "BUILD.json", KEY) is not MISSING:
            export_checks.append(compare("provenance.export_calls", field(records, "BUILD.json", KEY, "calls"),
                field(exported, "BUILD.json", KEY, "calls")))
        exported_construction = construction_identity(profile, {
            **exported, "task.json": exported.get("private_task.json", {})}, provenance_checks=export_checks)
        if field(records, "BUILD.json", KEY) is not MISSING and field(exported, "BUILD.json", KEY) is MISSING:
            exported_construction = summary([*exported_construction["checks"], *export_checks])
    else:
        exported_construction = summary([])
    states = [part["status"] for part in (construction, exported_construction, run, capabilities)]
    return {"construction": construction, "export_construction": exported_construction,
        "run": run, "capabilities": capabilities,
        "release_profile_match": all(state == "match" for state in states),
        "classification": "mechanism_fixture" if "mismatch" in states else
            "profile_match_only" if all(state == "match" for state in states) else "unverified",
        "scope": "Identity fields only; no startup, business success, release qualification or model evidence"}
