"""Experiment usage excludes infrastructure attempts, regardless of billing."""


EXPERIMENT_ACCOUNTING = {
    "include_infra_in_experiment_cost": False,
    "charge_infra_to_task_budget": False,
    "scope": "accepted_responses_only",
}
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def experiment_cost_summary(accountings):
    """Read the accepted ledger only; never fall back to physical totals."""
    usage = dict.fromkeys(TOKEN_FIELDS, 0)
    responses = 0
    for accounting in accountings:
        task = accounting["task"]
        for key in ("responses", *TOKEN_FIELDS):
            if type(task[key]) is not int or task[key] < 0:
                raise ValueError("Accepted response usage is missing or invalid")
        if task["prompt_tokens"] + task["completion_tokens"] != task["total_tokens"]:
            raise ValueError("Accepted response token totals disagree")
        if task["responses"] == 0 and task["total_tokens"] != 0:
            raise ValueError("Usage without an accepted response")
        responses += task["responses"]
        for key in TOKEN_FIELDS:
            usage[key] += task[key]
    return {
        **EXPERIMENT_ACCOUNTING,
        "accepted_requests": responses,
        "usage": usage,
        "accepted_usage_complete": True,
        "cost_usd": None,
        "cost_usd_status": "not_computed_without_verified_pricing_and_billing_details",
    }
