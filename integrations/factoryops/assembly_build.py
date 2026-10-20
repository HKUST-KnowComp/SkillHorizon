"""Heterogeneous multi-age material joins with measured tolerance propagation."""

import argparse
from copy import deepcopy
from pathlib import Path
from skillhorizon.construction_provenance import observe_call
import random

from integrations.internal_mcp.io import read_json, write_json
from .build import build as base_build
from integrations.internal_mcp.topology import parent_positions, disclose_pattern


FORMAT = "skillhorizon.c50.assembly_network.v1"


def build(source, output, *, seed=50, levels=4, terminal_quantity=1, background=True, dependency_pattern="staggered", terminal_lanes=2, supplier_lanes=2):
    parent_positions(0, 0, 2, seed, dependency_pattern)
    output = Path(output)
    public = observe_call(base_build, source, output, seed=seed, levels=levels, terminal_quantity=terminal_quantity, background=background, assembly_network=True, terminal_lanes=terminal_lanes, supplier_lanes=supplier_lanes)
    world = read_json(output / "checkpoint/world.json")
    state = world["production"]
    orders = list(state["orders"].values())
    by_position = {(o["level"], o["lane"]): o for o in orders}
    for order in orders:
        level, lane = order["level"], order["lane"]
        if level == 0:
            components = []
        elif level == 1:
            components = [(0, lane, 2), (0, 1 - lane, 1)]
        elif level == 2:
            components = [(1, lane, 1), (0, 1 - lane, 1 + lane)]
        else:
            components = [(level - 1, lane, 1), (level - 2, 1 - lane, 1), (0, lane, 1 + lane)]
        if supplier_lanes==1:
            components=[] if level==0 else [(0,0,2)]
        elif dependency_pattern != "staggered":
            components = [(s, i, 1 + (seed + lane + s + i) % 2)
                          for s, i in parent_positions(level, lane, 2, seed, dependency_pattern)]
        order["bom"] = [{"parent_order_id": by_position[(origin, producer_lane)]["order_id"], "units_per_output": count} for origin, producer_lane, count in components]
        order["parent_order_ids"] = [c["parent_order_id"] for c in order["bom"]]
        order["components_per_output"] = sum(c["units_per_output"] for c in order["bom"]) or 1
        order["output_quantity"] = terminal_quantity if order["terminal"] else 0
    for order in reversed(orders):
        for component in order["bom"]:
            state["orders"][component["parent_order_id"]]["output_quantity"] += order["output_quantity"] * component["units_per_output"]
    if any(order["output_quantity"]<=0 for order in orders):
        raise ValueError("Every required assembly order must supply an actual terminal demand")
    rng = random.Random(seed + 5001)
    for order in orders:
        metrics = next(p["metric_definitions"] for p in world["tables"]["quality_check_profile"] if p["profile_id"] == order["profile_id"])
        state["response_profiles"][order["order_id"]] = {str(slot): {m["metric_name"]: rng.choice((-1, 0, 1)) for m in metrics} for slot in range(order["output_quantity"])}
        if not order["level"]:
            lot = state["lots"][order["raw_lot_ids"][0]]
            template = deepcopy(state["units"][lot["unit_ids"][0]])
            for identifier in lot["unit_ids"]:
                del state["units"][identifier]
            lot["unit_ids"] = []
            for slot in range(order["output_quantity"]):
                identifier = f"{lot['lot_id']}-unit-{slot}"
                state["units"][identifier] = {**deepcopy(template), "unit_id": identifier, "output_slot": slot}
                lot["unit_ids"].append(identifier)
    public["format"] = world["format"] = FORMAT
    disclose_pattern(public, dependency_pattern, seed)
    public["orders"] = deepcopy(orders)
    public["material_tolerance_policy"] = {"metric": "fit_units", "adjustment_bounds": [-10, 10],
        "rule": "Each input group's material bias is the sum of each produced input unit's measured fit_units minus that producing run's fit target; raw stock contributes zero. Add that bias and the actor's per-group material_offset_units to every output QC metric. The ordinary process offset, local variation and tool calibration still apply. Rework consumes the actual quarantined unit and may correct its measured bias. All offsets are declared execution parameters, never evaluator answers."}
    public["policy"]["measurement_model"] += " + summed_actual_input_fit_bias + actor_per_group_material_offset"
    public["goal"] += " Join exact heterogeneous BOM quantities across nonadjacent supplier levels, conserve shared stock, and compensate actual inherited material tolerances using historical per-unit observations."
    if supplier_lanes==1:
        public["goal"]="Produce and dispatch the required terminal units through a two-stage serial material handoff. Prepare current programs, calibrate equipment, reserve actual supplier units, execute timed batches, collect mandatory QC and compensate inherited material tolerance. Poll the tooling drift after the single root order completes; future runs require fresh calibration. Preserve unit lineage, failed attempts and material conservation."
        public["dependency_scope"]="serial_material_handoff_not_multi_supplier_join"
        inspect_path=output/'public/skills/inspect-production-order/SKILL.md'
        inspect_path.write_text(inspect_path.read_text().replace('after both root orders','after all required root orders'),encoding='utf-8')
    skill_path = output / "public/skills/schedule-material-bound-run/SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\n\n## Heterogeneous assembly inputs\n\nUse the exact per-parent counts in bom, not one item from each adjacent level. Reserve only this batch's quantities; other future consumers share the remaining stock. "
        "Read actual input-unit QC from the producing runs. Sum fit deviations from each producing profile's target and choose a per-group material_offset_units within the declared bounds. "
        "Accepted input quality does not mean zero physical deviation. Preserve actual input identities in each group, including on rework.\n", encoding="utf-8")
    for folder in ("checkpoint", "runtime"):
        write_json(output / folder / "world.json", world)
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", {"public": public, "initial_tables": world["tables"], "initial_state": state})
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=50)
    parser.add_argument("--levels", type=int, default=4)
    parser.add_argument("--terminal-quantity", type=int, default=1)
    print(build(**vars(parser.parse_args()))["task_id"])
