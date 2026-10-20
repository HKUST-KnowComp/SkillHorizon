"""Two-metric tolerance stacks with real setup-time occupancy."""

import argparse
from datetime import datetime, timedelta
from pathlib import Path
from skillhorizon.construction_provenance import construction_entry, observe_call

from integrations.internal_mcp.io import read_json, write_json
from .assembly_build import build as assembly_build
from integrations.internal_mcp.topology import PATTERNS, disclose_runtime_identity

FORMAT = "skillhorizon.c50.coupled_tolerance.v2"
FAMILIES = ("standard", "equipment_windows", "partial_rework", "constrained_rework")


@construction_entry
def build(source, output, *, seed=50, levels=4, terminal_quantity=1, background=True, scenario="standard", dependency_pattern="staggered", runtime_identity="legacy"):
    disclose_runtime_identity({}, runtime_identity)
    if scenario not in FAMILIES:
        raise ValueError("Unknown production scenario")
    public = observe_call(assembly_build, source, output, seed=seed, levels=levels, terminal_quantity=terminal_quantity, background=background, dependency_pattern=dependency_pattern)
    disclose_runtime_identity(public, runtime_identity)
    output = Path(output)
    world = read_json(output / "checkpoint/world.json")
    public.pop("material_tolerance_policy")
    public["coupled_material_policy"] = {"metrics": ["fit_units", "torque_units"], "control_bounds": [-10, 10],
        "pressure_seconds_per_unit": 2, "shim_seconds_per_unit": 3,
        "rule": "For each actual input group, independently sum every produced unit fit and torque deviation from its producing profile target; raw stock contributes zero. "
                "Fit receives inherited fit + pressure + shim; torque receives inherited torque + pressure - shim. Local response, process offset and measured tool compensation remain. "
                "Both controls are integer values in [-10,10]. Each group adds 2*abs(pressure)+3*abs(shim) setup seconds to actual equipment occupancy. "
                "Rework uses the actual quarantined unit measurements, not a clean substitute. This is a synthetic linearized process, not physical robotics."}
    public["format"] = world["format"] = FORMAT
    public["policy"]["measurement_model"] = "target + local variation + process offset + calibrated tool bias + metric-specific input stack + coupled controls"
    public["goal"] = public["goal"].replace("and compensate actual inherited material tolerances using historical per-unit observations.",
        "and compensate both inherited fit and torque using historical per-unit observations, with actual setup-time occupancy.")
    if scenario in ("equipment_windows", "constrained_rework"):
        origin = datetime.fromisoformat(public["origin"].replace("Z", "+00:00"))
        order = public["orders"][0]
        public["equipment_calendar"] = [{"reservation_id": f"maintenance-{i}", "kind": "maintenance",
            **{k: order[k] for k in ("cell_id", "tool_id", "fixture_id")},
            "scheduled_start": (origin + timedelta(seconds=start)).isoformat().replace("+00:00", "Z"),
            "scheduled_end": (origin + timedelta(seconds=end)).isoformat().replace("+00:00", "Z")}
            for i, (start, end) in enumerate(((0, 120 + seed % 3 * 30), (420, 600)), 1)]
        for program in world["tables"]["program"]:
            order = world["production"]["orders"][program["order_id"]]
            program["cycle_time_sec"] = 8 + 3 * order["level"] + 2 * order["lane"]
        public["goal"] += " Respect published maintenance windows and actual heterogeneous processing durations on shared equipment."
    if scenario in ("partial_rework", "constrained_rework"):
        root = public["orders"][seed % 2]
        slot = seed % root["output_quantity"]
        world["production"]["first_pass_deviations"] = {root["order_id"]: {str(slot): {"fit_units": 4, "torque_units": -3}}}
        public["policy"]["first_pass_variation"] = "Some primary batches may have a transient per-unit disturbance, observed only through actual QC. " \
            "The transient does not repeat on rework, but the defective unit's measured material deviation remains and must be compensated."
        public["goal"] += " Recover quarantined units selectively while retaining accepted siblings and conserving the shared supplier stock."
    if scenario == "standard":
        public["scenario"] = {"family": scenario, "seed": seed, "scope": "construction_development",
            "world_size": {"levels": levels, "terminal_quantity": terminal_quantity},
            "mechanisms": ["inherited_dual_metric_tolerances", "shared_equipment_occupancy"]}
    else:
        public["task_id"] += "-" + scenario.replace("_", "-")
        world["task_id"] = public["task_id"]
        for rows in world["tables"].values():
            for row in rows:
                row["task_id"] = public["task_id"]
        public["scenario"] = {"family": scenario, "seed": seed, "scope": "construction_development",
            "world_size": {"levels": levels, "terminal_quantity": terminal_quantity},
            "mechanisms": (["maintenance_calendar", "heterogeneous_processing_time", "shared_equipment_and_materials"]
                if scenario == "equipment_windows" else ["partial_lot_quarantine", "measured_unit_rework", "accepted_sibling_reuse"])}
        if scenario == "constrained_rework":
            public["scenario"].update(composition_of=["equipment_windows", "partial_rework"],
                mechanisms=["maintenance_calendar", "heterogeneous_processing_time", "shared_equipment_and_materials",
                            "partial_lot_quarantine", "measured_unit_rework", "accepted_sibling_reuse"])
    path = output / "public/skills/schedule-material-bound-run/SKILL.md"
    text = path.read_text().split("\n\n## Heterogeneous assembly inputs")[0]
    path.write_text(text + "\n\n## Coupled material tolerances\n\nMatch exact heterogeneous BOM quantities and preserve shared remaining stock. "
        "Read both fit_units and torque_units for every produced input and subtract that producing run's corresponding targets. "
        "Choose bounded integer pressure_units and shim_units per output group using the public coupled rule. Fit and torque are distinct: "
        "equal fit does not imply interchangeable torque, and a single scalar material offset is not accepted. Include actual per-group setup seconds "
        "in the shared equipment interval before reserving. Rework consumes the real quarantined unit and must use its actual measurements.\n", encoding="utf-8")
    if scenario in ("equipment_windows", "constrained_rework"):
        path.write_text(path.read_text() + "\nRead the equipment calendar as well as queued runs. Include maintenance and all shared cell, tool or fixture intervals when selecting a start. "
            "Per-group setup and program-specific cycle time both occupy the equipment. Another independent cell may remain usable during maintenance.\n", encoding="utf-8")
    for folder in ("checkpoint", "runtime"):
        write_json(output / folder / "world.json", world)
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", {"public": public, "initial_tables": world["tables"], "initial_state": world["production"]})
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=50)
    parser.add_argument("--levels", type=int, default=4)
    parser.add_argument("--terminal-quantity", type=int, default=1)
    parser.add_argument("--scenario", choices=FAMILIES, default="standard")
    parser.add_argument("--dependency-pattern", choices=PATTERNS, default="staggered")
    parser.add_argument("--runtime-identity", choices=("legacy", "task_local_v1"), default="legacy")
    print(build(**vars(parser.parse_args()))["task_id"])
