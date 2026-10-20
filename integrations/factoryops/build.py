"""Construct a two-lane assembly BOM using source-native manufacturing entities."""

import argparse
from copy import deepcopy
from pathlib import Path
import random
import shutil
import sys

from integrations.internal_mcp.io import materialize_skills, read_json, write_json


KEYS = {"robot_cell": "cell_id", "program": "program_id", "waypoint": "waypoint_id", "robot_tool": "tool_id", "fixture": "fixture_id",
    "safety_zone": "zone_id", "safety_event": "event_id", "quality_check_profile": "profile_id", "quality_check": "check_id", "run": "run_id", "audit_log": "audit_id"}
FORMAT = "skillhorizon.c50.material_execution.v1"
SKILLS = Path(__file__).with_name("skills")
ORIGIN = "2026-10-01T08:00:00Z"


def build(source, output, *, seed=50, levels=3, terminal_quantity=2, background=True, assembly_network=False, terminal_lanes=2, supplier_lanes=2):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Use fresh isolated output")
    if type(seed) is not int or type(levels) is not int or not 2 <= levels <= 5 or type(terminal_quantity) is not int or not 1 <= terminal_quantity <= 8:
        raise ValueError("Use integer seed, 2..5 levels and 1..8 terminal units per lane")
    if type(terminal_lanes) is not int or terminal_lanes not in (1,2) or (terminal_lanes!=2 and not assembly_network):
        raise ValueError("One terminal lane requires assembly construction")
    if type(supplier_lanes) is not int or supplier_lanes not in (1,2) or (supplier_lanes==1 and not (assembly_network and levels==2 and terminal_lanes==1)):
        raise ValueError("One supplier requires a two-level single-terminal assembly")
    original = {kind: read_json(source / f"data/database/{kind}s.json") for kind in KEYS}
    rng = random.Random(seed)
    task_id = f"c50-{'assembly' if assembly_network else 'production'}-{seed}-{levels}l-{terminal_quantity}q"
    if terminal_lanes!=2:
        task_id+=f"-{terminal_lanes}t"
    if supplier_lanes!=2:
        task_id+=f"-{supplier_lanes}s"
    tables = {kind: [] for kind in KEYS}

    def add(kind, identifier, values):
        # Keep source schema shape, but every active identity and operational value is explicit.
        row = {**deepcopy(rng.choice(original[kind])), **values, KEYS[kind]: identifier, "task_id": task_id, "created_at": ORIGIN, "updated_at": ORIGIN}
        tables[kind].append(row)
        return row

    cells, tools, fixtures, zones = ([f"{task_id}-{kind}-{lane}" for lane in range(2)] for kind in ("cell", "tool", "fixture", "zone"))
    orders, lots, units, responses, biases = {}, {}, {}, {}, {}
    actor = "production-coordinator"
    pose = {"x_mm": 0, "y_mm": 0, "z_mm": 100, "rx_deg": 0, "ry_deg": 0, "rz_deg": 0}
    for lane in range(2):
        add("robot_cell", cells[lane], {"site_name": "Synthetic assembly lab", "line_name": f"Lane {lane}", "controller_model": "synthetic-discrete-controller",
            "controller_serial": f"synthetic-{lane}", "status": "active", "mode": "manual", "network_state": "online", "risk_level": "medium",
            "active_fixture_id": fixtures[lane], "current_program_id": None})
        add("fixture", fixtures[lane], {"cell_id": cells[lane], "fixture_type": "assembly", "name": f"Lane {lane} fixture", "home_pose_verified": True,
            "position_state": "clamped", "occupancy_state": "empty", "last_reset_at": ORIGIN, "revision": 1})
        add("robot_tool", tools[lane], {"cell_id": cells[lane], "name": f"Lane {lane} assembly tool", "tool_type": "gripper", "status": "available",
            "calibration_status": "due", "last_calibrated_at": None, "payload_kg": 10, "tcp_pose": pose, "compatible_program_ids": [],
            "revision": 1, "calibration_id": None, "compensation_units": 0})
        biases[tools[lane]] = rng.choice((-3, -2, 2, 3))
        add("safety_zone", zones[lane], {"cell_id": cells[lane], "name": f"Lane {lane} speed envelope", "zone_type": "speed_limit", "enabled": True,
            "severity": "protective_stop", "max_speed_mm_s": 300, "polygon_points": [{"x_mm": -500, "y_mm": -500}, {"x_mm": 500, "y_mm": -500}, {"x_mm": 500, "y_mm": 500}, {"x_mm": -500, "y_mm": 500}],
            "linked_program_ids": [], "last_validated_at": ORIGIN, "revision": 1})
        if lane>=supplier_lanes:
            continue
        raw_id = f"{task_id}-raw-lot-{lane}"
        raw_units = []
        for slot in range(terminal_quantity * 2 ** (levels - 1)):
            identifier = raw_id + f"-unit-{slot}"
            raw_units.append(identifier)
            units[identifier] = {"unit_id": identifier, "lot_id": raw_id, "order_id": None, "level": -1, "output_slot": slot,
                "parent_unit_ids": [], "produced_run_id": None, "status": "available", "quality_accepted": True, "reserved_by": None, "consumed_by": None}
        lots[raw_id] = {"lot_id": raw_id, "order_id": None, "level": -1, "lane": lane, "unit_ids": raw_units, "produced_run_id": None, "status": "accepted", "version": 1}
    for level in range(levels):
        quantity = terminal_quantity * 2 ** (levels - level - 1)
        for lane in range(terminal_lanes if level==levels-1 else supplier_lanes):
            cell_lane = 0 if level % 2 else lane
            cell_id, tool_id, fixture_id, zone_id = cells[cell_lane], tools[cell_lane], fixtures[cell_lane], zones[cell_lane]
            identifier = f"{task_id}-order-{level}-{lane}"
            program_id, profile_id = identifier + "-program", identifier + "-profile"
            parents = [f"{task_id}-order-{level-1}-{p}" for p in range(supplier_lanes)] if level else []
            order = {"order_id": identifier, "name": f"Assembly level {level}, lane {lane}", "level": level, "lane": lane, "cell_id": cell_id,
                "fixture_id": fixture_id, "tool_id": tool_id, "program_id": program_id, "profile_id": profile_id, "output_quantity": quantity,
                "parent_order_ids": parents, "raw_lot_ids": [f"{task_id}-raw-lot-{lane}"] if not level else [],
                "components_per_output": 2 if level else 1, "max_attempts": 4, "terminal": level == levels - 1}
            orders[identifier] = order
            metrics = [{"metric_name": "fit_units", "unit": "synthetic_unit", "target": 100, "expected_min": 98, "expected_max": 102},
                {"metric_name": "torque_units", "unit": "synthetic_unit", "target": 50, "expected_min": 48, "expected_max": 52}]
            add("quality_check_profile", profile_id, {"cell_id": cell_id, "program_id": program_id, "name": order["name"] + " mandatory QC",
                "acceptance_rule": "every_metric_for_every_unit_inclusive_bounds", "metric_definitions": metrics, "status": "active", "revision": 1})
            waypoint_ids = []
            for index in range(1, 5):
                wp_id = program_id + f"-waypoint-{index}"
                waypoint_ids.append(wp_id)
                add("waypoint", wp_id, {"cell_id": cell_id, "program_id": program_id, "sequence_index": index, "name": f"Operation point {index}",
                    "pose": {**pose, "x_mm": index * 20}, "blend_radius_mm": 0, "speed_mm_s": 150, "approach": "linear", "taught_by": actor, "locked": False})
            add("program", program_id, {"cell_id": cell_id, "name": order["name"], "active_tool_id": tool_id, "quality_profile_id": profile_id,
                "cycle_time_sec": 10, "payload_kg": 1, "revision_notes": "Initial unvalidated process", "status": "draft", "version": 1,
                "waypoint_ids": waypoint_ids, "safety_zone_ids": [zone_id], "created_by": actor, "order_id": identifier, "process_offset_units": 0,
                "validation_id": None, "release_id": None})
            tables["robot_tool"][cell_lane]["compatible_program_ids"].append(program_id)
            tables["safety_zone"][cell_lane]["linked_program_ids"].append(program_id)
            responses[identifier] = {str(slot): {m["metric_name"]: rng.choice((-1, 0, 1)) for m in metrics} for slot in range(quantity)}
    update = {"event_id": task_id + "-calibration-due", "tool_id": tools[0], "after_level": 0, "new_bias_units": biases[tools[0]] + 2,
        "reason": "Declared tooling drift after the first assembly layer; future runs need a fresh calibration and release binding."}
    skills = [{"name": p.name, "files": [str(f.relative_to(p)) for f in sorted(p.rglob("*")) if f.is_file() and f.suffix in {".md", ".py"}]}
        for p in sorted(SKILLS.iterdir()) if p.is_dir()] if SKILLS.exists() else []
    public = {"format": FORMAT, "task_id": task_id, "actor": actor, "cell_ids": cells, "order_ids": list(orders), "orders": list(orders.values()),
        "terminal_order_ids": [o["order_id"] for o in orders.values() if o["terminal"]], "origin": ORIGIN, "levels": levels, "skills": skills,
        "goal": "Produce and dispatch the exact terminal assembly units. Prepare and validate current programs, reserve compatible equipment and actual parent materials, "
            "execute timed batches, collect environment-owned mandatory QC, and disposition or rework every output. Preserve failed attempts and prevent double consumption. "
            "Poll the declared tooling update after completing both level-zero orders; it affects future work only.",
        "policy": {"skill_protocol": "observational_v1", "simulation": "discrete_manufacturing_not_robot_dynamics", "mandatory_qc": "every_unit_every_metric",
            "measurement_model": "target + fixed_unit_deviation + process_offset + tool_bias + applied_compensation",
            "calibration_rule": "applied_compensation = -observed_bias", "declared_tool_update_count": 1, "program_offset_bounds": [-10, 10],
            "workspace_coordinate_bounds_mm": [-500, 500], "maximum_clock_seconds": 86400, "released_history_immutable": True}}
    state = {"next_id": 1, "orders": orders, "lots": lots, "units": units, "response_profiles": responses, "tool_biases": biases,
        "calibrations": {}, "validations": {}, "releases": {}, "dispositions": {}, "dispatch": None, "scheduled_updates": [update], "applied_updates": [],
        "events": [], "skill_runs": {}, "active_skill_run": None}
    world = {"format": FORMAT, "task_id": task_id, "tables": tables, "production": state, "sequence": 0, "clock": ORIGIN}
    write_json(output / "background.json", original if background else {k: [] for k in KEYS})
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", {"public": public, "initial_tables": tables, "initial_state": state})
    write_json(output / "checkpoint/world.json", world)
    write_json(output / "controller.json", {"source": str(source), "provider_calls": 0, "qualification": "UNVERIFIED"})
    shutil.copytree(output / "checkpoint", output / "runtime")
    if SKILLS.exists():
        materialize_skills(SKILLS, output / "public/skills")
    write_json(output / "mcp.json", {"mcpServers": {"c50-production": {"command": sys.executable,
        "args": ["-m", "integrations.factoryops.server", str(output), str(output / "runtime")], "cwd": str(Path(__file__).resolve().parents[2])}}})
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=50)
    parser.add_argument("--levels", type=int, default=3)
    parser.add_argument("--terminal-quantity", type=int, default=2)
    args = parser.parse_args()
    print(build(args.source, args.output, seed=args.seed, levels=args.levels, terminal_quantity=args.terminal_quantity)["task_id"])
