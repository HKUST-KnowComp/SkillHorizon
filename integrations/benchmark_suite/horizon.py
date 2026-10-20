"""Explicit business graphs and executed receipt references, never trace-order DAGs."""

from integrations.internal_mcp.structure import business_nodes, graph_metrics
from integrations.internal_mcp.characterize import SCOPES
from integrations.internal_mcp.io import read_json


def node(identifier, parents, stage=0, group=None, kind="business_record"):
    return {"id": identifier, "parents": sorted(set(parents)), "stage": stage, "group": group, "kind": kind}


def intrinsic_graph(identifier, public, world):
    if identifier.startswith("C"):
        from .programs import SHORT_PROGRAMS, SUPPLEMENTAL_PROGRAMS
        programs = {**SHORT_PROGRAMS, **SUPPLEMENTAL_PROGRAMS}
        if public.get("format") in programs.values() or world.get("format") in programs.values():
            if public.get("format") != programs.get(identifier) or world.get("format") != public["format"]:
                raise ValueError("Public/world business programs differ")
        if identifier == "C06":
            from .programs import EXPOSURE, REACH, MEASURED
            if public.get("format") not in (EXPOSURE, REACH, MEASURED) or public["format"] != world.get("format"):
                raise ValueError("C06 public/world business programs differ")
        nodes = business_nodes(identifier, public, world)
        scope = SCOPES[identifier]
    elif identifier == "FieldOps":
        if not isinstance(world.get("services"), dict) or not world["services"]:
            raise ValueError("FieldOps service prerequisites are missing")
        for key, row in world["services"].items():
            if (type(row.get("stage")) is not int or str(row["stage"]) != key
                    or not isinstance(row.get("parent_stages"), list)
                    or any(type(p) is not int for p in row["parent_stages"])
                    or len(set(row["parent_stages"])) != len(row["parent_stages"])):
                raise ValueError("Invalid FieldOps service prerequisites")
        nodes = [node(str(key), map(str, row["parent_stages"]), row["stage"], kind="service")
                 for key, row in world["services"].items()]
        scope = "Declared service prerequisites; crew, windows, stock and source events are additional constraints."
    elif identifier == "ProcureOps":
        stages = world["contract"]["stages"]
        if type(stages) is not int or stages < 1:
            raise ValueError("Invalid ProcureOps stage count")
        parents = {n: [] for n in range(1, stages + 1)}
        rules = world["assembly_rules"]
        if "material_transfers" in rules:
            transfers = rules["material_transfers"]
            if not isinstance(transfers, list) or not transfers:
                raise ValueError("Invalid explicit ProcureOps material transfers")
            for transfer in transfers:
                if (not isinstance(transfer, dict)
                        or any(type(transfer.get(k)) is not int for k in ("origin_stage", "consumer_stage", "quantity"))
                        or not 1 <= transfer["origin_stage"] < transfer["consumer_stage"] <= stages
                        or transfer["quantity"] <= 0):
                    raise ValueError("Invalid explicit ProcureOps material transfer")
                parents[transfer["consumer_stage"]].append(transfer["origin_stage"])
        else:
            from skillhorizon.fieldops.assembly import bill
            from skillhorizon.fieldops.scenarios import SCENARIOS
            delay_scenarios = {None, *SCENARIOS["procureops"]} - {"irregular_bom", "quality_reuse"}
            if (world.get("business_program") != "assembly_network_v3"
                    or "business_scenario" not in world or world["business_scenario"] not in delay_scenarios):
                raise ValueError("ProcureOps explicit material transfers are missing")
            delays = rules.get("reuse_delays_stages")
            if (not isinstance(delays, list) or not delays
                    or any(type(d) is not int or d <= 0 for d in delays) or len(set(delays)) != len(delays)):
                raise ValueError("Invalid ProcureOps material reuse delays")
            for stage in parents:
                for origin, quantity in bill(world, stage).items():
                    if (type(origin) is not int or not 1 <= origin <= stage
                            or type(quantity) is not int or quantity < 0):
                        raise ValueError("Invalid ProcureOps material bill")
                    if origin < stage and quantity > 0:
                        parents[stage].append(origin)
        nodes = [node(str(n), map(str, parents[n]), n, kind="assembly_stage") for n in parents]
        scope = "Declared material-transfer obligations; liquidity, quality processing and retention are additional constraints."
    else:
        if identifier != "DataOps":
            raise ValueError("Unknown environment")
        periods = public["config"]["periods"]
        nodes = [node(f"P{n:02d}", [f"P{n-1:02d}"] if n > 1 else [], n, kind="accounting_period")
                 for n in range(1, periods + 1)]
        scope = "Accounting-period order only, not regional artifact dataflow; invoice maturity and credit exposure are additional constraints."
    return {"nodes": nodes, "metrics": graph_metrics(nodes), "scope": scope,
            "cross_domain_depth_ranking_valid": False, "minimum_required_horizon": None}


def references(value, fields):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in fields:
                if isinstance(item, str) and item:
                    found.add(item)
                elif isinstance(item, list):
                    found.update(v for v in item if isinstance(v, str) and v)
            found.update(references(item, fields))
    elif isinstance(value, list):
        for item in value:
            found.update(references(item, fields))
    return found


def executed_graph(identifier, world, *, strict=True):
    records, parents = {}, {}
    from .programs import SHORT_PROGRAMS, WORKLOAD, ASSEMBLY, SUPPLEMENTAL_PROGRAMS, CAPACITY, FUNDING, SYNTHESIS, MEASURED
    programs = {**SHORT_PROGRAMS, **SUPPLEMENTAL_PROGRAMS}
    if world.get("format") in programs.values() and world["format"] != programs.get(identifier):
        raise ValueError("Cross-domain executed business program")

    def add_typed(collection, id_field, *, arrays=(), texts=()):
        if not isinstance(collection, dict):
            raise ValueError("Committed collection must be an object")
        for key, row in collection.items():
            if (not isinstance(key, str) or not key or not isinstance(row, dict)
                    or row.get(id_field) != key or key in records):
                raise ValueError("Invalid or duplicate committed identity")
            inputs = set()
            for name in arrays:
                values = row.get(name)
                if (not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values)
                        or len(set(values)) != len(values)):
                    raise ValueError("Invalid committed reference array")
                inputs.update(values)
            for name in texts:
                value = row.get(name)
                if not isinstance(value, str) or not value:
                    raise ValueError("Invalid committed reference identity")
                inputs.add(value)
            records[key], parents[key] = row, inputs

    def add(collection, id_field=None, fields=()):
        entries = collection.items() if isinstance(collection, dict) else ((r[id_field], r) for r in collection)
        for key, row in entries:
            key = row[id_field] if id_field else key
            if key in records:
                raise ValueError("Ambiguous committed record identity")
            records[key] = row
            parents[key] = references(row, set(fields)) - {key}

    def add_nested(collection, id_field, fields, validate):
        if not isinstance(collection, (dict, list)):
            raise ValueError("Committed collection must contain records")
        if isinstance(collection, list) and any(not isinstance(r, dict) for r in collection):
            raise ValueError("Committed records must be objects")
        entries = collection.items() if isinstance(collection, dict) else ((r.get(id_field), r) for r in collection)
        def walk(value):
            found = set()
            if isinstance(value, dict):
                for name, item in value.items():
                    if name in fields:
                        if name.endswith("_ids"):
                            if not isinstance(item, list) or any(not isinstance(v, str) or not v for v in item):
                                raise ValueError("Malformed historical reference list")
                            found.update(item)
                        elif isinstance(item, str) and (item or name=="selected_parent_report_id"):
                            if item: found.add(item)
                        else:
                            raise ValueError("Malformed historical reference")
                    found.update(walk(item))
            elif isinstance(value, list):
                for item in value: found.update(walk(item))
            return found
        for key, row in entries:
            if (not isinstance(key, str) or not key or not isinstance(row, dict)
                    or row.get(id_field) != key or key in records):
                raise ValueError("Invalid committed identity")
            validate(row)
            records[key] = row
            parents[key] = walk({k: v for k, v in row.items() if k != id_field})

    def require_reference_rows(value, field):
        if (not isinstance(value, list) or any(not isinstance(r, dict) or
                not isinstance(r.get(field), str) or not r[field] for r in value)):
            raise ValueError("Missing or malformed required historical references")

    def measured_publication(row):
        content = row.get("content")
        learning = content.get("learning") if isinstance(content, dict) else None
        if not isinstance(learning, dict) or not isinstance(learning.get("report_ids"), list):
            raise ValueError("Missing measured publication learning inputs")

    def measured_report(row):
        require_reference_rows(row.get("observations"), "publication_id")

    def capacity_publication(row):
        if not isinstance(row.get("contract"), dict):
            raise ValueError("Missing publication contract")
        require_reference_rows(row["contract"].get("imports"), "publication_id")

    def funding_ack(row):
        if not isinstance(row.get("funding"), dict):
            raise ValueError("Missing acknowledged funding")
        require_reference_rows(row["funding"].get("incoming_transfers"), "source_dispatch_id")

    def library_artifact(row):
        if row.get("kind") == "bibliography":
            require_reference_rows(row.get("entries"), "paper_id")
        elif row.get("kind") == "review":
            if not isinstance(row.get("bibliography_artifact_id"), str) or not row["bibliography_artifact_id"] or not isinstance(row.get("sections"), list):
                raise ValueError("Missing review dependencies")
            for section in row["sections"]:
                if not isinstance(section, dict):
                    raise ValueError("Malformed review section")
                require_reference_rows(section.get("items"), "annotation_id")
                synthesis=section.get("synthesis")
                if not isinstance(synthesis, dict):
                    raise ValueError("Missing section synthesis")
                for name in ("annotation_id", "calibration_annotation_id"):
                    require_reference_rows(synthesis.get("contributions"), name)
        else:
            raise ValueError("Unknown library artifact")

    if identifier in {"DataOps", "FieldOps", "ProcureOps"}:
        for key, row in world["artifacts"].items():
            records[key] = row
            parents[key] = {v if isinstance(v, str) else v["id"] for v in row["inputs"]
                            if isinstance(v, str) or v["type"] == "artifact"}
        scope = "Recorded artifact input edges, including intermediate computations. Not necessarily current or correct."
    elif identifier == "C09":
        program = world.get("format")
        freight = program == "skillhorizon.c09.freight_network.v2"
        if program not in ("skillhorizon.c09.transfer_network.v1", "skillhorizon.c09.freight_network.v2"):
            raise ValueError("Unknown C09 executed business program")

        def add_airline(collection, id_field, *, required_text=(), optional_text=(), arrays=()):
            if not isinstance(collection, dict):
                raise ValueError("C09 committed collection must be an object")
            for key, row in collection.items():
                if (not isinstance(key, str) or not key or not isinstance(row, dict)
                        or row.get(id_field) != key):
                    raise ValueError("C09 committed record identity differs")
                if key in records:
                    raise ValueError("Ambiguous committed record identity")
                inputs = set()
                for name in (*required_text, *optional_text):
                    if name in optional_text and name not in row:
                        continue
                    value = row.get(name)
                    if not isinstance(value, str):
                        raise ValueError("C09 committed reference must be a string: " + name)
                    if value:
                        inputs.add(value)
                    elif name == "origin_clearance_id":
                        raise ValueError("C09 baggage origin reference cannot be empty")
                for name in arrays:
                    values = row.get(name)
                    if (not isinstance(values, list)
                            or any(not isinstance(value, str) or not value for value in values)):
                        raise ValueError("C09 committed references must be an ID list: " + name)
                    inputs.update(values)
                records[key], parents[key] = row, inputs

        add_airline(world["connection_state"]["clearances"], "clearance_id",
                    required_text=("previous_clearance_id",), optional_text=("transfer_trip_id",))
        network = world["transfer_network"]
        add_airline(network["releases"], "release_id", required_text=("origin_clearance_id",))
        if freight:
            add_airline(network["forwardings"], "forwarding_id",
                        arrays=("baggage_release_ids", "prior_forwarding_ids"))
        add_airline(network["trips"], "trip_id", optional_text=("previous_vehicle_trip_id",),
                    arrays=("inbound_clearance_ids", "baggage_release_ids")
                    + (("baggage_forwarding_ids",) if freight else ()))
        scope = ("Recorded clearance, baggage-origin, inbound-transfer and prior-vehicle references"
                 + (", including freight custody" if freight else "")
                 + "; includes cancelled history, excludes seat/payment dependencies and current-validity claims.")
    elif identifier == "C25":
        if world.get("format") == CAPACITY:
            add_nested(world["planning"]["publications"], "publication_id", {"publication_id"}, capacity_publication)
        else:
            add(world["planning"]["publications"], fields=("publication_id",))
        scope = "Actual version-specific imported publications; excludes review/decision workflow and implicit quota conflicts."
    elif identifier == "C06":
        from .programs import EXPOSURE, REACH
        if world.get("format") not in (EXPOSURE, REACH, MEASURED):
            raise ValueError("Unknown C06 executed business program")
        state = world["social"]
        collections = ("publications", "reports") + (("conversion_records",) if world["format"] == REACH else ())
        for key in collections:
            if world["format"] == MEASURED:
                add_nested(state[key], "publication_id" if key == "publications" else "report_id",
                           {"report_ids", "selected_parent_report_id", "publication_id"},
                           measured_publication if key == "publications" else measured_report)
            else:
                add(state[key], fields=("report_ids", "selected_parent_report_id", "publication_id", "prior_publication_ids", "conversion_snapshot_ids"))
        scope = "Committed publication, learning-report and conversion input references; excludes uncommitted drafts."
        if world["format"] == EXPOSURE:
            scope = "Committed publications, exact historical exposure origins and learning-report inputs; no conversion layer."
        elif world["format"] == MEASURED:
            scope = "Committed publications and measured learning-report inputs; no exposure or conversion layer."
    elif identifier == "C47":
        workload = world["finops"]["workload"]
        if world.get("format") == WORKLOAD:
            add_typed(workload["bindings"], "binding_id", arrays=("source_handoff_ids",))
            add_typed(workload["handoffs"], "handoff_id", texts=("binding_id",))
        else:
            add(workload["bindings"], fields=("source_handoff_ids",))
            add(workload["handoffs"], fields=("binding_id",))
        scope = "Actual workload binding and handoff dependencies; billing, commitment and capacity are extra constraints."
    elif identifier == "C50":
        if world.get("format") == ASSEMBLY:
            add_typed(world["production"]["units"], "unit_id", arrays=("parent_unit_ids",))
        else:
            add(world["production"]["units"], fields=("parent_unit_ids",))
        scope = "Actual per-unit consumed material lineage, including rework; equipment/program dependencies not expanded."
    elif identifier == "C01":
        if world.get("format") == FUNDING:
            add_nested(world["mail"]["acknowledgements"], "dispatch_message_id", {"source_dispatch_id"}, funding_ack)
        else:
            add(world["mail"]["acknowledgements"], id_field="dispatch_message_id", fields=("source_dispatch_id",))
        scope = "Actual acknowledged funding transfers keyed by dispatch identity; ACL and review dependencies not expanded."
    elif identifier == "C11":
        if world.get("format") == SYNTHESIS:
            add_nested(world["tables"]["annotation"], "annotation_id", set(), lambda row: None)
            add_nested(world["library"]["artifacts"], "artifact_id",
                       {"annotation_id", "annotation_ids", "calibration_annotation_id", "bibliography_artifact_id"}, library_artifact)
        else:
            add(world["tables"]["annotation"], id_field="annotation_id")
            add(world["library"]["artifacts"], fields=("annotation_id", "annotation_ids", "calibration_annotation_id", "bibliography_artifact_id"))
        scope = "Authored bibliography/review annotation references; source calibration graph is measured separately."
    else:
        raise ValueError("Unknown environment")
    unresolved = sorted({p for values in parents.values() for p in values if p not in records})
    from .programs import WORKLOAD, ASSEMBLY
    if (identifier in {"C06", "C09"} or world.get("format") in programs.values()) and strict and unresolved:
        raise ValueError(identifier + " committed record contains a dangling historical origin")
    nodes = [node(key, values & records.keys()) for key, values in parents.items()]
    try:
        metrics, graph_error = graph_metrics(nodes), None
        metrics.update(cross_group_edges=None, nonadjacent_stage_edges=None)
    except ValueError as error:
        if strict:
            raise
        metrics, graph_error = None, str(error)
    return {"nodes": nodes, "metrics": metrics, "graph_error": graph_error, "scope": scope,
            **({"extraction_revision": "c09_committed_references_v2",
                "reference_resolution_complete": not unresolved,
                "graph_complete": not unresolved and graph_error is None} if identifier == "C09" else {}),
            "unexpanded_external_references": unresolved,
            "edge_meaning": "explicit_committed_reference_not_attention_or_semantic_correctness",
            "minimum_required_horizon": None}


def episode_horizon(task, bundle):
    """Controller-only overlay on actual executed worlds, including prefix delta."""
    program = None
    if task.get("skill_protocol") == "observational_v1":
        identifier = task["config"]["environment_id"]
        environment = bundle / "checkpoint/environment"
        public = read_json(environment / "public/task.json")
        origin = read_json(environment / "checkpoint/world.json")
        checkpoint = read_json(bundle / "checkpoint/world.json")
        final = read_json(bundle / "runtime/world.json")
    else:
        public = task
        checkpoint = read_json(bundle / "checkpoint/project.json")
        final = read_json(bundle / "runtime/project.json")
        identifier = "DataOps" if task["format"] == "skillhorizon.dataflow.v2" else {
            "fieldops": "FieldOps", "procureops": "ProcureOps"}[checkpoint["domain"]]
        origin = checkpoint
        programs = {"DataOps": {"credit_sales_v2"},
                    "FieldOps": {"pressure_services_v2", "pressure_branches_v3"},
                    "ProcureOps": {"assembly_network_v3"}}
        program = task["config"].get("business_program")
        if not isinstance(program, str) or program not in programs[identifier]:
            return {"environment": identifier, "supported": False,
                    "business_program": program,
                    "reason": "Legacy business profile is outside the ten-domain dependency release"}
        if any(world.get("business_program") != program for world in (checkpoint, final)):
            return {"environment": identifier, "supported": False, "business_program": program,
                    "reason": "Task, checkpoint and final business programs are missing or inconsistent"}
    before, after = executed_graph(identifier, checkpoint), executed_graph(identifier, final, strict=False)
    before_ids = {n["id"] for n in before["nodes"]}
    after_ids = {n["id"] for n in after["nodes"]}
    old_parents = {n["id"]: n["parents"] for n in before["nodes"]}
    new_parents = {n["id"]: n["parents"] for n in after["nodes"]}
    return {"environment": identifier, "supported": True, "business_program": program,
        "business_obligations": intrinsic_graph(identifier, public, origin),
        "checkpoint_dependencies": before, "final_executed_dependencies": after,
        "actor_delta": {"new_record_ids": sorted(after_ids - before_ids), "removed_record_ids": sorted(before_ids - after_ids),
            "changed_parent_record_ids": sorted(key for key in before_ids & after_ids if old_parents[key] != new_parents[key]),
            "new_record_count": len(after_ids - before_ids), "removed_record_count": len(before_ids - after_ids),
            "note": "New records are not necessarily correct deliverables; no prefix record is counted as newly produced."},
        "scope": "Offline executed-world references. Granularity and excluded constraints remain domain-specific."}
