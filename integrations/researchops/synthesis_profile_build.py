"""Explicit measured synthesis, without multi-parent calibration maps."""
from skillhorizon.construction_provenance import construction_entry, observe_call
from integrations.internal_mcp.topology import disclose_runtime_identity
from integrations.benchmark_suite.program_build import finalize
from .synthesis_build import build as delegate, FORMAT
from .build import KEYS


@construction_entry
def build(source, output, *, seed=11, sections=2, rounds=2, studies_per_section=1,
          alias_copies=0, distractors_per_section=0, background=False, runtime_identity="task_local_v1"):
    disclose_runtime_identity({}, runtime_identity)
    sizes = dict(sections=sections, rounds=rounds, studies_per_section=studies_per_section,
                 alias_copies=alias_copies, distractors_per_section=distractors_per_section)
    public = observe_call(delegate, source, output, seed=seed, background=background, **sizes)
    return finalize(output, public, program=FORMAT, family="measured_synthesis", seed=seed,
                    sizes=sizes, background=background, runtime_identity=runtime_identity,
                    keys=KEYS, private_state="library", seed_key="construction_seed")
