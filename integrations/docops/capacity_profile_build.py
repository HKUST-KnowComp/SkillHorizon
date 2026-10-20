"""Explicit capacity-reservation program, without portfolio origin rights."""
from skillhorizon.construction_provenance import construction_entry, observe_call
from integrations.internal_mcp.topology import disclose_runtime_identity
from integrations.benchmark_suite.program_build import finalize
from .capacity_build import build as delegate, FORMAT
from .build import KEYS


@construction_entry
def build(source, output, *, seed=25, width=3, depth=2, background=False,
          amendment=False, dependency_pattern="staggered", runtime_identity="task_local_v1"):
    disclose_runtime_identity({}, runtime_identity)
    if type(amendment) is not bool:
        raise ValueError("Capacity amendment must be boolean")
    public = observe_call(delegate, source, output, seed=seed, width=width, depth=depth,
                          background=background, amendment=amendment, dependency_pattern=dependency_pattern)
    return finalize(output, public, program=FORMAT, family="capacity_reservations", seed=seed,
                    sizes={"width": width, "depth": depth}, background=background,
                    runtime_identity=runtime_identity, keys=KEYS, json_text_fields=(("page_block", "content"),),
                    dependency_pattern=dependency_pattern)
