"""Explicit assembly-program construction entry."""
from skillhorizon.construction_provenance import construction_entry, observe_call
from integrations.internal_mcp.topology import disclose_runtime_identity
from integrations.benchmark_suite.program_build import finalize
from .assembly_build import build as delegate, FORMAT
from .build import KEYS


@construction_entry
def build(source, output, *, seed=50, levels=2, terminal_quantity=1, background=False,
          dependency_pattern="distributed", runtime_identity="task_local_v1", terminal_lanes=2, supplier_lanes=2):
    disclose_runtime_identity({}, runtime_identity)
    public = observe_call(delegate, source, output, seed=seed, levels=levels,
                          terminal_quantity=terminal_quantity, background=background,
                          dependency_pattern=dependency_pattern, terminal_lanes=terminal_lanes, supplier_lanes=supplier_lanes)
    return finalize(output, public, program=FORMAT, family="serial_material_handoff" if supplier_lanes==1 else "heterogeneous_assembly", seed=seed,
                    sizes={"levels": levels, "terminal_quantity": terminal_quantity, "terminal_lanes": terminal_lanes, "supplier_lanes": supplier_lanes},
                    background=background, runtime_identity=runtime_identity, keys=KEYS,
                    private_state="production")
