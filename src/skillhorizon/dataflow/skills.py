"""Fixed reusable skills. No instance-specific plan or answers are installed."""

from pathlib import Path
import shutil
from ..skill_packages import skill_header
from .funding_skills import SKILLS as FUNDING_SKILLS, INPUT_OBJECTS as FUNDING_INPUT_OBJECTS
from .obligation_skills import SKILLS as OBLIGATION_SKILLS, INPUT_OBJECTS as OBLIGATION_INPUT_OBJECTS
from copy import deepcopy


SKILLS = {
    "harmonize-orders": {
        "inputs": {"source_id": str, "column_map": dict, "amount_unit": str, "key_map_source_id": str, "conflict_policy": str},
        "script": True,
        "description": "Harmonize an external order feed using explicit schema, unit, identity mapping, and conflict rules.",
        "procedure": "Inspect the external source's business_schema and identity map. Supply column_map from canonical order_id/customer_id/region/period/currency/amount/record_revision to actual columns; amount_unit is major or minor. Join external customer keys through the regional key-map source. Keep the highest record_revision for conflicting records (latest_revision); reject same-version conflicts. Identical duplicates are harmless. Produce canonical orders for downstream authorization and audit.",
    },
    "normalize-orders": {
        "inputs": {"source_id": str, "deduplication": str}, "script": True,
        "description": "Normalize a regional order export and remove identical duplicates; deduplication must be first or last.",
        "procedure": "Inspect the source columns and business scope. Choose first or last duplicate retention; both are valid for identical duplicates. Convert decimal amounts to integer minor units, preserve currency and customer keys, and reject conflicting duplicates. The resulting order table is not yet authorized or currency-converted.",
    },
    "prepare-consent": {
        "inputs": {"source_id": str}, "script": False,
        "description": "Prepare the currently authorized customer set for a region.",
        "procedure": "Read the regional authorization source. Include only analytics_allowed=yes customers, keeping customer_id and region together. Authorization is reusable across periods but must be refreshed after that source changes. It does not authorize customers in another region.",
    },
    "prepare-rates": {
        "inputs": {"source_id": str}, "script": True,
        "description": "Prepare positive rational exchange rates to USD.",
        "procedure": "Inspect the public rate table. Preserve each currency's numerator and denominator, reject nonpositive rates, and retain USD. Rates convert minor units to USD cents with half-up rounding and are shared across regional work packages.",
    },
    "join-orders": {
        "inputs": {"orders_id": str, "consent_id": str, "rates_id": str}, "script": True,
        "description": "Join normalized orders with regional authorization and shared exchange rates.",
        "procedure": "Select current prepared artifacts: orders_id has kind orders, consent_id has kind consent for the same region, and rates_id has kind rates for the applicable period. Raw source IDs are not prepared artifacts. Join on customer_id AND region, exclude unauthorized orders, convert each amount using its currency, and round each order half up to USD cents before summation. Do not substitute another region's consent or mix periods. The output is reusable by both statistics and audit skills.",
    },
    "summarize-region": {
        "inputs": {"joined_id": str}, "script": True,
        "description": "Compute authorized order count and USD revenue for one regional period.",
        "procedure": "Inspect a current authorized sales table. Count retained unique orders and sum already-rounded USD cents by period and region. An empty authorized set yields a zero-valued row for its declared region and period. Preserve these dimensions for later reconciliation.",
    },
    "audit-region": {
        "inputs": {"orders_id": str, "joined_id": str, "summary_id": str}, "script": False,
        "description": "Check a regional summary against source orders, current authorization, and currency rules.",
        "procedure": "Select artifacts for the same region and period. Verify actual order contents against the public source, the authorized row set against consent and rates, and counts and revenue against retained rows. Equivalent independently produced artifacts are acceptable. Failed content checks cannot authorize release.",
    },
    "rollup-period": {
        "inputs": {"summary_ids": list[str], "previous_rollup_id": str}, "script": True,
        "description": "Merge every regional summary for one period and compute cumulative totals.",
        "procedure": 'Select exactly one current summary per required region, all for the same period. For the first period use previous_rollup_id="" (an empty JSON string, not null); otherwise consume the immediately preceding period\'s rollup. Compute both period and cumulative counts and revenue. Source changes may invalidate earlier rollups, requiring downstream recomputation without repeating unaffected regional work.',
    },
    "rollup-batch": {
        "inputs": {"summary_ids": list[str]}, "script": True,
        "description": "Compute all period and cumulative totals in a batch from regional summaries.",
        "procedure": "Select exactly one current summary for every regional period. Sort by period within region and compute running totals. This is a valid alternative to chained rollup-period calls. Supply the resulting batch table alone in rollup_ids for review and publication.",
    },
    "review-release": {
        "inputs": {"rollup_ids": list[str], "audit_ids": list[str]}, "script": False,
        "description": "Reconcile all period rollups with valid regional content audits before publication.",
        "procedure": "Collect one rollup per period and one successful audit per regional period. Check coverage and actual cumulative values against the current public sources. Do not use stale audits or incomplete coverage. Read any returned business notices; a source change after review can require a fresh review.",
    },
    "publish-release": {
        "inputs": {"rollup_ids": list[str], "review_id": str}, "script": False,
        "description": "Publish the complete aggregate release using a current successful review.",
        "procedure": "Select the complete set of current period rollups and a successful review covering equivalent business content. Publish only period, region, order count, USD revenue, and cumulative totals, never customer-level rows. In procedural mode close this publication run after its output commits. Unrelated obsolete runs are reported separately and do not invalidate business publication. The delivered CSV, not an archive ID alone, is evaluated.",
    },
}

# Step arguments are business choices, not acknowledgements of a fixed next action.
ALL_SKILLS = {**SKILLS, **FUNDING_SKILLS}
CREDIT_SKILLS = deepcopy(OBLIGATION_SKILLS)
CREDIT_SKILLS["allocate-working-capital"]["inputs"]["credit_assessment_id"] = str
CREDIT_SKILLS["allocate-working-capital"]["procedure"] += (
    " Bind credit_assessment_id from assess-credit for these exact current regional demands and predecessor. "
    "The allocation preserves cash; its assessment separately constrains customer exposure during fulfillment.")
CREDIT_SKILLS["fulfill-orders"]["procedure"] += (
    " In credit_sales_v2, each order must also fit its customer's regional credit quota in the allocation's "
    "assessment. Consume the order's full sale amount from credit and ceil(usd_cents/2) from cash. "
    "Skip an order that exceeds either; unused credit cannot replenish cash.")
CREDIT_SKILLS["assess-credit"] = {
    "inputs": {"joined_ids": list[str], "previous_settlement_id": str, "settlement_ids": list[str], "limits_source_id": str},
    "script": False, "description": "Assess pooled customer exposure across historical invoices and current regional demand.",
    "procedure": "Use one current joined demand per region, the preceding settlement (empty in period one), "
    "and the project's credit_limits source. Explicitly select settlement_ids covering all older ledgers "
    "with still-outstanding invoices. An empty list is valid only when no old invoice remains outstanding. "
    "The environment checks coverage against the predecessor chain; it does not select old inputs for you. "
    "Extra ledgers from that same current predecessor chain are allowed. Future-due invoices consume "
    "the same customer's shared credit across all regions; due invoices are collected before allocation. "
    "Split max(0,limit-outstanding) proportional to current customer demand by region, floor shares and "
    "distribute remainder cents to positive-demand regions in ascending order. Zero demand allocates zero. "
    "Limits lowered below outstanding exposure block new credit but do not erase existing invoices."}
KNOWN_SKILLS = {**ALL_SKILLS, **OBLIGATION_SKILLS, **CREDIT_SKILLS}


def skill_specs(business_program=None):
    if business_program is None:
        return SKILLS
    if business_program not in {"credit_sales_v2", "obligation_sales_v1", "funded_sales_v1"}:
        raise ValueError("Unknown business program")
    base = deepcopy(SKILLS)
    for name in ("rollup-period", "rollup-batch"):
        base[name]["description"] = "Demand-only rollup; not applicable to this funded-sales project's realized-sales accounting."
        base[name]["procedure"] = (
            "This skill aggregates authorized demand in non-funded projects. It is not executable in "
            "funded_sales_v1, obligation_sales_v1 or credit_sales_v2: the environment rejects it with "
            "DEMAND_NOT_REALIZED_SALES. Authorized demand can exceed fulfilled sales. Current "
            "close-funded-period outputs, based on actual settlement records, form the release rollups.")
    base["summarize-region"]["description"] = "Summarize authorized regional demand, not realized funded sales."
    base["summarize-region"]["procedure"] += " In this funded project these totals describe demand, not fulfilled sales or collected cash."
    for name in ("review-release", "publish-release"):
        base[name]["procedure"] += (
            " In this funded project rollup_ids must cover current close-funded-period outputs for "
            "every required period. Regional audits validate authorized demand, while the settlement "
            "and funded rollup chain determines realized sales; a demand-only batch is not a substitute.")
    if business_program == "credit_sales_v2":
        return {**base, **CREDIT_SKILLS}
    if business_program == "obligation_sales_v1":
        return {**base, **OBLIGATION_SKILLS}
    return {**base, **FUNDING_SKILLS}


INPUT_OBJECTS = {
    "harmonize-orders": {"source_id": "raw source, kind external_orders", "key_map_source_id": "raw source, kind identity_map"},
    "normalize-orders": {"source_id": "raw source, kind orders"},
    "prepare-consent": {"source_id": "raw source, kind consent"},
    "prepare-rates": {"source_id": "raw source, kind rates"},
    "join-orders": {"orders_id": "current prepared artifact, kind orders", "consent_id": "current prepared artifact, kind consent",
                    "rates_id": "current prepared artifact, kind rates"},
    "summarize-region": {"joined_id": "current prepared artifact, kind joined"},
    "audit-region": {"orders_id": "current prepared artifact, kind orders", "joined_id": "current prepared artifact, kind joined",
                     "summary_id": "current prepared artifact, kind summary"},
    "rollup-period": {"summary_ids": "list of current prepared artifacts, kind summary",
                      "previous_rollup_id": 'current prepared artifact, kind rollup; empty string "" for the first period'},
    "rollup-batch": {"summary_ids": "list of current prepared artifacts, kind summary"},
    "review-release": {"rollup_ids": "list of current prepared artifacts, kind rollup",
                       "audit_ids": "list of current prepared artifacts, kind audit"},
    "publish-release": {"rollup_ids": "list of current prepared artifacts, kind rollup",
                        "review_id": "current prepared artifact, kind review"},
}


ALL_INPUT_OBJECTS = {**INPUT_OBJECTS, **FUNDING_INPUT_OBJECTS}

PROCEDURES = {
    "join-orders": [
        ("authorize", "orders_id, consent_id, join_keys (list): match customer_id and region; emits authorized rows"),
        ("convert", "authorized_id, rates_id, rounding: convert the intermediate rows; rounding=half_up"),
    ],
    "audit-region": [
        ("reconcile_rows", "orders_id, joined_id, key_columns (list): reconcile by order_id against current public sources"),
        ("check_summary", "reconciliation_id, summary_id, group_by (list): aggregate by region and period and compare count/revenue"),
    ],
    "review-release": [
        ("check_coverage", "rollup_ids, audit_ids, required_periods (lists): assemble complete regional-period coverage"),
        ("check_totals", "coverage_id, method: reconcile using source_totals or running_sum"),
    ],
}

SCRIPT = '''import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from table_ops import compute
payload = json.load(sys.stdin)
print(json.dumps(compute(payload["operation"], payload["tables"], payload["options"]), sort_keys=True))
'''


def install_skills(root, *, business_program=None, skill_protocol="procedural"):
    if skill_protocol not in {"procedural", "skill_declaration_v1"}:
        raise ValueError("Unknown DataOps skill protocol")
    requirements = deepcopy({**ALL_INPUT_OBJECTS, **OBLIGATION_INPUT_OBJECTS}
                            if business_program in {"obligation_sales_v1", "credit_sales_v2"} else ALL_INPUT_OBJECTS)
    if business_program == "credit_sales_v2":
        requirements["assess-credit"] = {"joined_ids": "one current joined artifact per region",
            "previous_settlement_id": "preceding obligation_settlement, empty in period one",
            "settlement_ids": "explicit current historical ledgers covering every still-outstanding invoice",
            "limits_source_id": "public credit_limits source"}
        requirements["allocate-working-capital"]["credit_assessment_id"] = "current matching credit_assessment"
    for name, skill in skill_specs(business_program).items():
        directory = Path(root) / name
        directory.mkdir(parents=True)
        text = [f"# {name}", "", skill["description"], "", "## Procedure", "", skill["procedure"],
                "", "## Execution", "", "Inspect inputs with read_table. Start the skill with your selected input IDs. "
                "Follow the steps below. Committed outputs are usable by downstream skills before run closure. "
                "Prefer finishing a run promptly; finish returns a receipt, not the table contents. "
                "Closing the publication run is required for final handoff. Other run cleanup is recorded separately "
                "and is not a prerequisite for business publication or ending after handoff. "
                "Reads and repeated valid computations are legal. Do not assume the largest artifact ID is current.",
                "", "Inputs: " + ", ".join(skill["inputs"]) + ".",
                "Use public object IDs, never CSV paths. Raw sources and prepared artifacts are different object categories. "
                "Input types: " + ", ".join(f"{key}: {kind.__name__ if kind in (str, dict) else 'list[str]'}" for key, kind in skill["inputs"].items()) + ".",
                "Input object requirements: " + "; ".join(f"{key}: {value}" for key, value in requirements[name].items()) + ".",
                "To change a run's selected inputs, call revise_skill with run_id and complete replacement arguments. "
                "This clears its step progress and output reference but retains all committed tables. "
                "Execute again from the first step; restarting a run does not erase its earlier attempts from evaluation. "
                "A committed publication run must be finished or abandoned before a new run; it cannot be revised."]
        if skill_protocol == "skill_declaration_v1":
            procedure = skill["procedure"].replace(
                "In procedural mode close this publication run after its output commits. "
                "Unrelated obsolete runs are reported separately and do not invalidate business publication. ", "")
            text = [f"# {name}", "", skill["description"], "", "## Procedure", "", procedure,
                "", "## Business execution", "",
                "Inspect inputs with read_table. Call execute_step with skill_id, step_id and explicit arguments. "
                "Business operations do not require a skill declaration. Each step commits its actual business output; "
                "reuse valid intermediate artifact IDs across declarations without recomputing them. "
                "Missing, stale or incompatible business inputs remain invalid. Do not assume the largest artifact ID is current.",
                "Inputs: " + ", ".join(skill["inputs"]) + ".",
                "Use public object IDs, never CSV paths. Raw sources and prepared artifacts are different object categories. "
                "Input types: " + ", ".join(f"{key}: {kind.__name__ if kind in (str, dict) else 'list[str]'}" for key, kind in skill["inputs"].items()) + ".",
                "Input object requirements: " + "; ".join(f"{key}: {value}" for key, value in requirements[name].items()) + "."]
        if name in PROCEDURES:
            text += ["", "Steps (pass these arguments to execute_step):"]
            text += [f"- {step}: {description}." for step, description in PROCEDURES[name]]
            if skill_protocol == "skill_declaration_v1":
                text += ["Use the actual intermediate artifact returned by the prerequisite business step. "
                         "A declaration cannot supply or replace that artifact. After a failed operation, "
                         "explicitly correct the business inputs; already committed artifacts remain available."]
            else:
                text += ["Intermediate artifact IDs are returned by the preceding operation and remain readable. "
                         "A failed step can be retried with corrected step parameters that preserve selected inputs. "
                         "For changed selected inputs, revise_skill or abandon and start a new run."]
        else:
            text += ["", "Step compute: pass the skill's complete business arguments." if skill_protocol == "skill_declaration_v1"
                     else "Step compute: pass the same arguments selected at begin_skill."]
        if skill["script"]:
            text += ["", "Optional scripts/execute.py performs the actual table computation. "
                     "execute_step performs the same operation without using the script. For join-orders the script performs "
                     "only convert after a successful authorize step; it cannot select inputs or advance other skills."]
            scripts = directory / "scripts"
            scripts.mkdir()
            (scripts / "execute.py").write_text(SCRIPT)
            shutil.copyfile(Path(__file__).with_name("table_ops.py"), scripts / "table_ops.py")
        if skill_protocol == "skill_declaration_v1":
            if skill["script"]:
                text += ["Call run_skill_script with skill_id, step_id and explicit arguments. "
                         "For join-orders use step_id=convert with the existing authorized_id, rates_id and rounding; "
                         "other script-capable simple skills use step_id=compute. The same business preconditions apply."]
            text += ["", "## Skill declarations", "",
                "You may declare use with begin_skill(run_id, skill_id, scope), using an episode-local label "
                "and optional public references. Associate each business call using the client's skill_run_id envelope, "
                "not its business arguments. No active declaration is inherited implicitly.",
                "finish_skill(run_id, status, outputs, note) records completed, blocked or abandoned. "
                "completed means you believe this skill's local goal was achieved, not merely that work stopped. "
                "It does not certify output, commit business changes, close accounting obligations or submit the task. "
                "Neither scope nor outputs selects business inputs or returns hidden verification. "
                "Use explicit public references for the scope and outputs; unspecified or ambiguous local goals may be unknown. "
                "Repeated declarations cannot move the first completion point. Business handoff depends on current valid "
                "accounting artifacts, not closure of a declaration run."]
        (directory / "SKILL.md").write_text(skill_header(name, skill["description"]) + "\n".join(text) + "\n")
