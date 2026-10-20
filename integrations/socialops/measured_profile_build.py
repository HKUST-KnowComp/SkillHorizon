"""Explicit measured-wave program with no exposure or conversion layer."""
from skillhorizon.construction_provenance import construction_entry, observe_call
from integrations.internal_mcp.topology import disclose_runtime_identity
from integrations.benchmark_suite.program_build import finalize
from .build import build as delegate, FORMAT, KEYS


@construction_entry
def build(source, output, *, seed=6, campaigns=2, waves=1, background=False,
          runtime_identity="task_local_v1", variants_per_brief=2):
    disclose_runtime_identity({}, runtime_identity)
    public = observe_call(delegate, source, output, seed=seed, campaigns=campaigns, waves=waves, background=background, variants_per_brief=variants_per_brief)
    if waves == 1:
        public["goal"] = ("Complete the declared campaign wave: prepare and approve all declared alternatives, publish within shared "
            "account slots, observe performance, select the measured click-through-rate winner and publish its deployment. "
            "Poll the declared post-wave rights notice and preserve the completed publications and measurements.")
    return finalize(output, public, program=FORMAT, family="measured_campaigns", seed=seed,
                    sizes={"campaigns": campaigns, "waves": waves, "variants_per_brief": variants_per_brief}, background=background,
                    runtime_identity=runtime_identity, keys=KEYS)
