"""Controller-side contracts for the existing independent domain evaluators."""


COUNTS = {
    "C09": ("completed_bookings", "required_bookings"),
    "C25": ("completed_pages", "required_pages"),
    "C06": ("completed_briefs", "required_briefs"),
    "C47": ("closed_periods", "required_periods"),
    "C50": ("completed_orders", "required_orders"),
    "C01": ("completed_nodes", "required_nodes"),
    "C11": ("completed_rounds", "required_rounds"),
}

# Existing evaluator codes for fixed inputs, entity identity or event/state
# reconstruction. Business completion/quality errors are deliberately not here.
INVALID_EVIDENCE = {
    "C09": {"PUBLIC_NETWORK_OBLIGATIONS_CHANGED", "NETWORK_FORMAT", "NETWORK_DEPENDENCY_CYCLE",
        "FROZEN_CARGO_ORIGIN_AND_MATURITY", "FREIGHT_FROZEN_RELEASE_IDENTITY", "FREIGHT_CUSTODY_CYCLE",
        "FREIGHT_FROZEN_PHYSICAL_WEIGHT"},
    "C25": {"ENTITY_IDENTITY_OR_OWNERSHIP", "TASK_IDENTITY", "ENVIRONMENT_FORMAT",
        "PAGE_SET_CHANGED", "AMENDMENT_EVENT_EVIDENCE"},
    "C06": {"TASK_POST_IDENTITY", "IMMUTABLE_BUSINESS_INPUT_CHANGED", "EXPOSURE_PUBLIC_POLICY_CHANGED",
        "RIGHTS_UPDATE_EVIDENCE", "ATTACHMENT_COUNT", "FROZEN_COHORT_RESPONSE_CHANGED",
        "FROZEN_OBSERVED_CONVERSION_RECORDS", "FROZEN_EXPOSURE_BUDGET"},
    "C47": {"TASK_IDENTITY_OR_PUBLIC_POLICY_CHANGED", "IMMUTABLE_INPUT_CHANGED", "REPORT_IDENTITY_SET",
        "DUPLICATE_COMMITMENT", "RESOURCE_EVENT_STATE_MISMATCH", "MAPPING_EVENT_STATE_MISMATCH",
        "UNWITNESSED_BUSINESS_ARTIFACT", "UNWITNESSED_COMMITMENT", "EVENT_ORDER_OR_IDENTITY",
        "BUSINESS_AUDIT_EVENT_SET", "BUSINESS_AUDIT_LINEAGE", "FIXED_BUDGET_IDENTITY", "UNKNOWN_BUSINESS_EVENT"},
    "C50": {"TASK_OR_PUBLIC_POLICY_CHANGED", "IMMUTABLE_BUSINESS_INPUT_CHANGED", "IMMUTABLE_FIRST_PASS_VARIATION",
        "EVENT_IDENTITY_ORDER", "EVENT_CELL_IDENTITY", "UNKNOWN_BUSINESS_EVENT",
        "PUBLIC_AUDIT_EVENT_SET", "PUBLIC_AUDIT_EVENT_LINEAGE"},
}


def _errors(value):
    return type(value) is list and all(type(item) is str for item in value)


def validate_outcome(identifier, result):
    """Reject missing/unscorable reports, not ordinary unfinished obligations.

    Only C01/C11 currently expose a separate domain integrity verdict. Other
    domains retain their existing scoring contracts; this does not invent an
    independent integrity certificate for them.
    """
    if identifier not in COUNTS or type(result) is not dict or type(result.get("accepted")) is not bool:
        raise ValueError("Domain outcome lacks an explicit business verdict")
    if identifier in {"C01", "C11"} and result.get("integrity_valid") is not True:
        raise ValueError("Domain evidence integrity is invalid or unknown")
    if "integrity_valid" in result and result["integrity_valid"] is not True:
        raise ValueError("Domain evidence integrity is invalid or unknown")
    errors = result.get("global_errors") if identifier in {"C25", "C06"} else result.get("errors")
    if not _errors(errors):
        raise ValueError("Domain outcome lacks its error contract")
    if any(e.startswith(("MALFORMED_", "malformed_state:")) or
           e == "MISSING_OR_MALFORMED_BUSINESS_EVIDENCE" for e in errors):
        raise ValueError("Domain evidence cannot be scored reliably")
    if any(e in INVALID_EVIDENCE.get(identifier, set()) or
           (identifier == "C50" and e.startswith(("EVENT_STATE_MISMATCH_", "ENTITY_STATE_MISMATCH_")))
           for e in errors):
        raise ValueError("Domain fixed inputs, identity or event evidence are invalid")
    # Freight delegates to the network evaluator, which delegates to the base
    # booking evaluator. An outer false must not hide a malformed inner report.
    if identifier == "C09" and "network_evaluation" in result:
        validate_outcome(identifier, result["network_evaluation"])
        if result["accepted"] != (not errors and result["network_evaluation"]["accepted"]):
            raise ValueError("Freight success contradicts its network evidence")
        return
    complete, required = (result.get(key) for key in COUNTS[identifier])
    if type(complete) is not int or type(required) is not int or not 0 <= complete <= required or required <= 0:
        raise ValueError("Domain outcome lacks valid obligation counts")
    if identifier == "C09":
        base = result.get("base_evaluation")
        if (type(base) is not dict or type(base.get("success")) is not bool
                or not _errors(base.get("errors"))
                or any(e.startswith("malformed_state:") for e in base["errors"])):
            raise ValueError("Base booking evidence cannot be scored reliably")
        if (base["success"] != (not base["errors"]) or
                result["accepted"] != (base["success"] and not errors)):
            raise ValueError("Network verdict contradicts its booking evidence")
        fixed = ("background_changed:", "duplicate_ids:", "flight_definition_changed:",
                 "original_payment_changed:", "original_service_identity:")
        if any(e in {"task_identity", "passengers_changed"} or e.startswith(fixed) for e in base["errors"]):
            raise ValueError("Base booking fixed inputs or identities changed")
    if identifier in {"C25", "C06", "C11"}:
        rows = result.get({"C25": "pages", "C06": "briefs", "C11": "rounds"}[identifier])
        if type(rows) is not list or len(rows) != required:
            raise ValueError("Domain outcome omits obligation rows")
        for row in rows:
            if type(row) is not dict or type(row.get("accepted")) is not bool or not _errors(row.get("errors")):
                raise ValueError("Domain obligation has no explicit verdict")
            if row["accepted"] != (not row["errors"]):
                raise ValueError("Domain obligation verdict contradicts its errors")
        if complete != sum(row["accepted"] for row in rows):
            raise ValueError("Domain completed count contradicts obligation verdicts")
        if identifier in {"C25", "C06"} and result["accepted"] != (not errors and all(row["accepted"] for row in rows)):
            raise ValueError("Domain verdict contradicts its obligation evidence")
    if identifier in {"C01", "C11", "C47", "C50"} and result["accepted"] != (not errors):
        raise ValueError("Domain verdict contradicts its error evidence")
    if result["accepted"] and (errors or complete != required):
        raise ValueError("Domain success contradicts its completion evidence")


def require_unfinished(identifier, evaluation):
    if type(evaluation) is not dict or evaluation.get("valid") is not True:
        raise ValueError("Checkpoint replay or domain evaluation is invalid")
    if type(evaluation.get("task_success")) is not bool:
        raise ValueError("Checkpoint business outcome is unknown")
    domain = evaluation.get("domain_evaluation")
    validate_outcome(identifier, domain)
    if evaluation["task_success"] != domain["accepted"]:
        raise ValueError("Checkpoint and domain business verdicts disagree")
    if evaluation["task_success"]:
        raise ValueError("Checkpoint business goal is already complete")
