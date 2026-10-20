"""Public-tool-only construction witness. Does not access evaluator or runtime files."""


class WitnessCallLimit(BaseException):
    def __init__(self, calls):
        super().__init__("Production public reference call limit reached")
        self.calls = calls


async def run(call, *, alternative=False, use_scripts=True, exercise_recovery=False, stop_after_runs=None,
              stop_at=None, root_process_offset=0, efficient_reads=False, skill_protocol=None, max_calls=None):
    declared = skill_protocol == "skill_declaration_v1"
    if skill_protocol not in (None, "skill_declaration_v1") or (
            declared and (type(max_calls) is not int or max_calls < 1)) or (not declared and max_calls is not None):
        raise ValueError("Declaration reference requires an explicit positive call limit")
    calls = 0
    if declared:
        original = call

        async def call(name, **arguments):
            nonlocal calls
            if calls >= max_calls:
                raise WitnessCallLimit(calls)
            calls += 1
            try:
                return await original(name, **arguments)
            except BaseException as error:
                error.witness_calls = calls
                raise

    read_skills, observations = set(), {}
    task = await call("read_task")
    if declared and (task.get("format") != "skillhorizon.c50.coupled_tolerance.v2"
                     or task.get("runtime_identity") != "task_local_v1"
                     or task.get("policy", {}).get("skill_protocol") != skill_protocol):
        raise ValueError("Declaration reference requires the actual task-local Coupled V2 profile")
    actor = task["actor"]
    completed = 0

    async def begin(skill, order=None):
        if not efficient_reads or skill not in read_skills:
            await call("read_skill_file", skill_id=skill)
            read_skills.add(skill)
        if declared:
            return None
        return (await call("begin_skill", skill_id=skill, order_ids=[order["order_id"]] if order else []))["run_id"]

    async def finish(identifier, outputs=()):
        if not declared:
            await call("finish_skill", run_id=identifier, output_ids=list(outputs))

    async def prepare(order, existing_runs, *, rework):
        skill = await begin("prepare-program-revision", order)
        detail = await call("inspect_program_detail", program_id=order["program_id"])
        program, tool = detail["program"], detail["active_tool"]
        offset = 5 if exercise_recovery and order["order_id"] == task["order_ids"][0] and not rework and not existing_runs else 0
        if not exercise_recovery and order["order_id"] == task["order_ids"][0] and not rework:
            offset = root_process_offset
        if program["status"] == "released":
            release = await call("read_program_release", release_id=program["release_id"])
            current = release["snapshot"]["tool"]["revision"] == tool["revision"] and release["snapshot"]["tool"]["calibration_id"] == tool["calibration_id"] and tool["calibration_status"] == "valid"
            if current and not rework and program["process_offset_units"] == offset:
                await finish(skill, [program["program_id"]])
                return release
            program = await call("create_program_revision", source_program_id=program["program_id"], new_name=f"{order['name']} attempt {len(existing_runs) + 1}", actor=actor)
        await call("update_program_metadata", program_id=program["program_id"], process_offset_units=offset, actor=actor)
        await finish(skill, [program["program_id"]])
        skill = await begin("calibrate-and-validate-program", order)
        detail = await call("inspect_program_detail", program_id=program["program_id"])
        tool = detail["active_tool"]
        if tool["calibration_status"] != "valid":
            measurement = await call("measure_tool_calibration", tool_id=tool["tool_id"])
            await call("read_tool_calibration", calibration_id=measurement["calibration_id"])
            await call("update_tool_calibration", tool_id=tool["tool_id"], calibration_id=measurement["calibration_id"], compensation_units=-measurement["observed_bias_units"], actor=actor)
        validation = await call("validate_production_program", program_id=program["program_id"])
        if not validation["passed"]:
            raise AssertionError("Feasible witness program validation failed: " + str(validation["checks"]))
        release = await call("publish_program_release", program_id=program["program_id"], validation_id=validation["validation_id"], actor=actor)
        await finish(skill, [validation["validation_id"], release["release_id"]])
        return release

    orders = sorted(task["orders"], key=lambda o: (o["level"], -o["lane"] if alternative else o["lane"]))
    for order in orders:
        while True:
            status = await call("read_production_order", order_id=order["order_id"])
            if len(status["output_units"]) == order["output_quantity"]:
                break
            skill = await begin("inspect-production-order", order)
            if order["level"]:
                await call("poll_production_updates")
            await call("get_cell_overview", cell_id=order["cell_id"])
            async def inventory_for_order():
                if not efficient_reads or not order["level"]:
                    return await call("read_material_inventory")
                rows = [await call("read_material_inventory", order_id=i)
                        for i in dict.fromkeys([order["order_id"], *order["parent_order_ids"]])]
                return {key: [item for row in rows for item in row[key]] for key in ("lots", "units")}

            inventory = await inventory_for_order()
            pending = [r for r in status["runs"] if r["run_status"] in {"queued", "running"}]
            if len(pending) > 1:
                raise AssertionError("Witness supports one outstanding attempt per order")
            attempt = pending[0] if pending else None
            if attempt and attempt["run_status"] == "queued":
                detail = await call("inspect_program_detail", program_id=attempt["program_id"])
                release = await call("read_program_release", release_id=attempt["release_id"])
                actual, bound = detail["active_tool"], release["snapshot"]["tool"]
                if actual["calibration_status"] != "valid" or actual["revision"] != bound["revision"] or actual["calibration_id"] != bound["calibration_id"]:
                    await call("cancel_program_run", run_id=attempt["run_id"], actor=actor)
                    attempt = None
                    inventory = await inventory_for_order()
                    status = await call("read_production_order", order_id=order["order_id"])
            await finish(skill)
            if attempt is None:
                quarantined = [u for u in inventory["units"] if u["order_id"] == order["order_id"] and u["status"] == "quarantined"]
                rework = bool(quarantined)
                release = await prepare(order, status["runs"], rework=rework)
                skill = await begin("schedule-material-bound-run", order)
                if rework:
                    groups = [[u["unit_id"]] for u in quarantined]
                elif not order["level"]:
                    groups = [[u["unit_id"]] for u in inventory["units"] if u["lot_id"] in order["raw_lot_ids"] and u["status"] == "available"]
                else:
                    if "bom" in order:
                        groups = [[] for _ in range(order["output_quantity"])]
                        for component in order["bom"]:
                            count = component["units_per_output"]
                            units = [u["unit_id"] for u in inventory["units"] if u["order_id"] == component["parent_order_id"] and u["status"] == "available" and u["quality_accepted"]]
                            if alternative:
                                units.reverse()
                            if len(units) < count * order["output_quantity"]:
                                raise AssertionError("Insufficient actual shared supplier stock")
                            for index, group in enumerate(groups):
                                group.extend(units[index * count:(index + 1) * count])
                    else:
                        groups_by_parent = [[u["unit_id"] for u in inventory["units"] if u["order_id"] == parent and u["status"] == "available" and u["quality_accepted"]][:order["output_quantity"]]
                            for parent in order["parent_order_ids"]]
                        groups = [list(group) for group in zip(*groups_by_parent)]
                material = {}
                setup_seconds = 0
                if "coupled_material_policy" in task:
                    units = {u["unit_id"]: u for u in inventory["units"]}
                    producing_runs = {units[i]["produced_run_id"] for group in groups for i in group if units[i]["produced_run_id"]}
                    deviations = {}
                    for producer_id in sorted(producing_runs):
                        if not efficient_reads or producer_id not in observations:
                            observations[producer_id] = await call("read_run_observations", run_id=producer_id)
                        observed = observations[producer_id]
                        targets = {m["metric_name"]: m["target"] for m in observed["run"]["execution_snapshot"]["profile"]["metric_definitions"]}
                        for measurement in observed["checks"]:
                            deviations[(measurement["unit_id"], measurement["metric_name"])] = measurement["measured_value"] - targets[measurement["metric_name"]]
                    pressure, shims = [], []
                    for group in groups:
                        fit = sum(deviations.get((i, "fit_units"), 0) for i in group)
                        torque = sum(deviations.get((i, "torque_units"), 0) for i in group)
                        p = -((fit + torque) // 2) if alternative else -(fit + torque) // 2
                        pressure.append(p)
                        shims.append(-fit - p)
                    material.update(pressure_units=pressure, shim_units=shims)
                    policy = task["coupled_material_policy"]
                    setup_seconds = sum(abs(p) * policy["pressure_seconds_per_unit"] + abs(s) * policy["shim_seconds_per_unit"] for p, s in zip(pressure, shims))
                elif "material_tolerance_policy" in task:
                    units = {u["unit_id"]: u for u in inventory["units"]}
                    producing_runs = {units[i]["produced_run_id"] for group in groups for i in group if units[i]["produced_run_id"]}
                    biases = {}
                    for producer_id in sorted(producing_runs):
                        if not efficient_reads or producer_id not in observations:
                            observations[producer_id] = await call("read_run_observations", run_id=producer_id)
                        observed = observations[producer_id]
                        target = next(m["target"] for m in observed["run"]["execution_snapshot"]["profile"]["metric_definitions"] if m["metric_name"] == "fit_units")
                        biases.update({c["unit_id"]: c["measured_value"] - target for c in observed["checks"] if c["metric_name"] == "fit_units"})
                    material["material_offset_units"] = [-sum(biases.get(i, 0) for i in group) for group in groups]
                clock = await call("read_production_clock")
                start = clock["now"]
                if use_scripts:
                    slot = await call("run_skill_script", skill_id="schedule-material-bound-run", inputs={"earliest": start,
                        "duration_seconds": len(groups) * release["snapshot"]["program"]["cycle_time_sec"] + setup_seconds,
                        "resources": {k: order[k] for k in ("cell_id", "tool_id", "fixture_id")}, "reservations": clock["reservations"]})
                    start = slot["start_at"]
                else:
                    from datetime import datetime, timedelta
                    def at(value):
                        return datetime.fromisoformat(value.replace("Z", "+00:00"))
                    candidate = at(start)
                    duration = timedelta(seconds=len(groups) * release["snapshot"]["program"]["cycle_time_sec"] + setup_seconds)
                    intervals = sorted((at(r["scheduled_start"]), at(r["scheduled_end"])) for r in clock["reservations"]
                        if any(r[k] == order[k] for k in ("cell_id", "tool_id", "fixture_id")))
                    for left, right in intervals:
                        if candidate + duration <= left:
                            break
                        if candidate < right:
                            candidate = right
                    start = candidate.isoformat().replace("+00:00", "Z")
                attempt = await call("schedule_program_run", order_id=order["order_id"], release_id=release["release_id"], start_at=start,
                    input_groups=groups, rework=rework, operator_name=actor, **material)
                await finish(skill, [attempt["run_id"]])
                if stop_at == "queued":
                    return
            skill = await begin("execute-and-observe-batch", order)
            if attempt["run_status"] == "queued":
                await call("advance_production_clock", to_time=attempt["scheduled_start"])
                attempt = await call("execute_program_run", run_id=attempt["run_id"], actor=actor)
            if stop_at == "running":
                await finish(skill, [attempt["run_id"]])
                return
            if not attempt["observed_at"]:
                clock = await call("read_production_clock")
                if clock["now"] < attempt["scheduled_end"]:
                    await call("advance_production_clock", to_time=attempt["scheduled_end"])
                await call("collect_run_observations", run_id=attempt["run_id"])
            observed = await call("read_run_observations", run_id=attempt["run_id"])
            native_checks = []
            while True:
                page = await call("list_quality_checks", run_id=attempt["run_id"], limit=100, offset=len(native_checks))
                native_checks.extend(page["checks"])
                if len(native_checks) >= page["total_count"]:
                    break
                if not page["checks"]:
                    raise AssertionError("QC pagination stopped before its declared total")
            if {(c["check_id"], c["unit_id"], c["metric_name"], c["measured_value"]) for c in native_checks} != {
                (c["check_id"], c["unit_id"], c["metric_name"], c["measured_value"]) for c in observed["checks"]}:
                raise AssertionError("Source-native QC inspection differs from actual observations")
            await finish(skill, [observed["run"]["output_lot_id"]])
            if stop_at == "observed":
                return
            skill = await begin("disposition-and-recover-lot", order)
            unit_ids = [u["unit_id"] for u in observed["units"]]
            if use_scripts:
                partition = await call("run_skill_script", skill_id="disposition-and-recover-lot", inputs={"run_id": attempt["run_id"], "release_id": attempt["release_id"],
                    "profile_id": order["profile_id"], "unit_ids": unit_ids, "metric_names": [m["metric_name"] for m in observed["run"]["execution_snapshot"]["profile"]["metric_definitions"]],
                    "checks": observed["checks"]})
                accepted, failed = partition["accepted_unit_ids"], partition["quarantined_unit_ids"]
            else:
                failed = sorted({c["unit_id"] for c in observed["checks"] if not c["expected_min"] <= c["measured_value"] <= c["expected_max"]})
                accepted = [i for i in unit_ids if i not in failed]
            disposition = await call("finalize_program_run", run_id=attempt["run_id"], accepted_unit_ids=accepted, quarantined_unit_ids=failed, operator_name=actor)
            await call("read_lot_disposition", disposition_id=disposition["disposition_id"])
            await finish(skill, [disposition["disposition_id"], disposition["lot_id"]])
            completed += 1
            if stop_after_runs is not None and completed >= stop_after_runs:
                return

    skill = await begin("reconcile-production-handoff")
    inventory = await call("read_material_inventory")
    terminal = [u for u in inventory["units"] if u["order_id"] in task["terminal_order_ids"] and u["quality_accepted"] and u["status"] in {"available", "dispatched"}]
    dispositions = set()
    for identifier in sorted({u["produced_run_id"] for u in terminal}):
        attempt = await call("read_program_run", run_id=identifier)
        disposition = await call("read_lot_disposition", disposition_id=attempt["disposition_id"])
        dispositions.add(disposition["disposition_id"])
    result = await call("reconcile_production_handoff", unit_ids=[u["unit_id"] for u in terminal], disposition_ids=sorted(dispositions))
    await finish(skill, result["unit_ids"])
