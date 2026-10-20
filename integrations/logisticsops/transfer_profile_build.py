"""Explicit transfer-program construction entry."""
from skillhorizon.construction_provenance import construction_entry, observe_call
from integrations.internal_mcp.topology import disclose_runtime_identity
from integrations.benchmark_suite.program_build import finalize
from .network_build import build as delegate, FORMAT

KEYS = {"passenger": "passenger_id", "flight": "flight_id", "booking": "booking_id",
        "seat": "seat_id", "baggage": "baggage_id", "payment": "payment_id",
        "special_service": "service_id", "loyalty_account": "account_id", "complaint": "complaint_id"}


@construction_entry
def build(source, output, *, seed=9, cohorts=2, legs=3, background=False,
          dependency_pattern="distributed", runtime_identity="task_local_v1"):
    disclose_runtime_identity({}, runtime_identity)
    public = observe_call(delegate, source, output, seed=seed, cohorts=cohorts, legs=legs,
                          background=background, dependency_pattern=dependency_pattern)
    return finalize(output, public, program=FORMAT, family="cross_cohort_transfers", seed=seed,
                    sizes={"cohorts": cohorts, "legs": legs}, background=background,
                    runtime_identity=runtime_identity, keys=KEYS)
