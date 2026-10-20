"""Fixed domain skills. Each operator commits one meaningful business operation."""
from pathlib import Path
from copy import deepcopy
import shutil
from typing import get_args, get_origin
from ..skill_packages import skill_header


FIELD_SKILLS = {
    "plan-isolation": ({"device_ids": list[str], "boundary_ids": list[str], "window_id": str, "crew_id": str,
                        "upstream_restoration_ids": list[str]},
        "Plan a safe isolation closure, maintenance window and crew; include all devices sharing selected boundaries. "
        "For staged work supply upstream restorations with sufficient delivered pressure. Planning does not reserve resources."),
    "isolate-devices": ({"plan_id": str}, "Acquire the plan's boundaries and crew during its public window. Overlapping reservations conflict."),
    "release-isolation": ({"isolation_id": str},
        "Close an unfinished isolation without declaring devices restored. Physical repairs, spent parts, work hours and "
        "the historical reservation remain recorded. Devices return to pending or need revalidation; old tests cannot restore them. "
        "Use a new feasible isolation for subsequent work. Closing a skill run alone does not close an isolation."),
    "repair-device": ({"device_id": str, "isolation_id": str, "method": str, "part_id": str},
        "Repair or rework a device inside its open isolation. Choose recalibrate (part_id empty) or replace with a compatible spare. "
        "A new repair invalidates the previous test and requires testing again; every replacement consumes another spare. "
        "Every repair consumes device.duration maintenance hours, including rework; abandon does not refund time or parts. "
        "A failed load test may be followed by a different repair method before restoration."),
    "test-device": ({"device_id": str, "repair_id": str, "test_load": int},
        "Measure a repaired device at a qualifying test_load. A valid test always emits a readable report with passed true or false. "
        "Each valid measurement consumes one maintenance hour, even when passed=false. "
        "A failing measurement completes this test operation, but requires rework and a new passing report before restoration."),
    "restore-group": ({"isolation_id": str, "test_ids": list[str]},
        "Restore an isolated group using one passing current test per device; release its boundaries and crew. Emits measured group pressure."),
    "accept-system": ({"restoration_ids": list[str]},
        "Accept the complete restored system after every device is healthy and all isolations are closed. Equivalent restoration groupings are legal."),
}
PROCURE_SKILLS = {
    "allocate-budget": ({"allocations": list[dict]},
        "Replace the cumulative spending caps for all requests, not add new funds or assign only the remaining purchase costs. "
        "Each row has request_id and budget_cents. Each cap includes received purchases and outstanding uncanceled orders; "
        "it must cover existing committed spend plus any new purchases. Allocations must cover all requests and sum within the project total budget. "
        "Reallocation does not erase prior spending or orders, and supersedes the previous budget_id."),
    "plan-purchase": ({"budget_id": str, "lines": list[dict]},
        "Price proposed lines without reserving stock. Each line has request_id, supplier_id, quantity. Use public prices and capacities; split suppliers are allowed. "
        "Use the current budget_id: existing committed spend plus the proposed purchase cost must fit each request's cumulative cap and the project total. "
        "Received and outstanding uncanceled purchases both count as committed spend."),
    "place-orders": ({"plan_id": str},
        "Place a priced purchase plan, reserving supplier quantities and debiting request and project budgets. A plan cannot be placed twice."),
    "receive-shipment": ({"order_id": str, "delivery_slot": int, "quantities": list[dict]},
        "Receive selected line quantities at a delivery slot. Rows have request_id, supplier_id, quantity; respect outstanding orders, supplier availability and per-slot delivery capacity."),
    "cancel-remainder": ({"order_id": str, "quantities": list[dict]},
        "Cancel undelivered quantities (request_id, supplier_id, quantity), releasing committed stock and reducing committed spend by the canceled purchase cost. "
        "This increases available headroom within the existing cumulative budget caps; it does not increase the caps or project total. Received goods cannot be canceled."),
    "accept-deliveries": ({"receipt_ids": list[str]},
        "Apply for joint project delivery acceptance, not only a quantity check. Read the joint-acceptance public document "
        "for scope: exact nonduplicated receipts, closed commitments, current budgets and supplier terms, plus any "
        "contract settlements, assembly and processing fees. Reconcile rejected applications and resubmit. "
        "Acceptance commits a business record; it does not end the agent episode."),
}
SCRIPT_SKILLS = {"test-device", "plan-purchase"}
SCRIPT = '''import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from calculations import calculate
payload = json.load(sys.stdin)
print(json.dumps(calculate(payload["operation"], payload["data"]), sort_keys=True))
'''

def input_type(kind):
    if get_origin(kind) is list:
        item = get_args(kind)[0]
        return {str: "array of unique strings", dict: "array of objects"}[item]
    return {str: "string", int: "integer"}[kind]

def skill_specs(domain, business_program=None, business_scenario=None):
    result = deepcopy(FIELD_SKILLS if domain == "fieldops" else PROCURE_SKILLS)
    if business_program in {"pressure_network_v1", "pressure_services_v2", "pressure_branches_v3"}:
        result["retest-device"] = ({"device_id": str, "isolation_id": str, "repair_id": str, "test_load": int},
            "Remeasure an unchanged physical repair inside a new open isolation using current upstream pressure. "
            "Consumes one maintenance hour, not another spare; a failing result still requires corrective work.")
    if business_program in {"staged_contracts_v1", "assembly_contracts_v2", "assembly_network_v3"}:
        result["plan-purchase"][0].update(previous_settlement_id=str, retention_ids=list[str])
        result["plan-purchase"] = (result["plan-purchase"][0], result["plan-purchase"][1] +
            ' previous_settlement_id is required: use the empty string "" for the first stage, '
            'and the preceding stage settlement ID thereafter. Do not omit it or use null. '
            'retention_ids is a required array; use [] when no uncollected matured claims are being used.')
        result["settle-contract"] = ({"stage": int, "receipt_ids": list[str], "previous_settlement_id": str},
            "Reconcile exact current-stage delivery receipts and closed commitments against the previous settlement. "
            "Emit closing cash and a retention claim with its future maturity; only closing cash funds the next stage. "
            'previous_settlement_id is required: use "" for stage 1, otherwise the preceding stage settlement ID; never null.')
    if business_program in {"pressure_services_v2", "pressure_branches_v3"}:
        result["plan-isolation"][0]["upstream_service_ids"] = list[str]
        result["plan-isolation"] = (result["plan-isolation"][0], result["plan-isolation"][1] +
            " Also bind exact active upstream_service_ids. Their actual return pressures and remote feed flow determine inlet pressure; "
            "the window must follow each upstream service's effective_from time.")
        result["accept-system"] = (result["accept-system"][0], result["accept-system"][1] +
            " Every service must also have a current active commission and shared flow reservations must reconcile.")
        result["commission-service"] = ({"stage": int, "restoration_ids": list[str], "flow_units": int},
            "Commission a restored service or replace its existing flow reservation. Bind exactly its current restorations. "
            "Respect minimum/maximum flow and shared capacity. Return pressure is restored pressure minus flow. "
            "Changing flow suspends all dependent descendants; their repairs remain usable but certifications must be renewed.")
    if business_program in {"assembly_contracts_v2", "assembly_network_v3"}:
        result["settle-contract"][0]["kit_id"] = str
        result["settle-contract"] = (result["settle-contract"][0], result["settle-contract"][1] +
            " Bind the completed kit_id. Processing fees reduce closing cash. The milestone reimbursement uses this stage's "
            "request quantity, including material reserved for future kits, not the number of kit input units.")
        result["condition-components"] = ({"stage": int, "inputs": list[dict]},
            "Upgrade low-grade receipt units for kit assembly. Each input has receipt_id, request_id, supplier_id, quantity. "
            "Only unconsumed, not already conditioned units qualify. Consumes public per-unit cash and stage work capacity; "
            "abandoning a run does not refund work or fees. Grade and available quantities are derived from actual orders and receipts. "
            "Work is charged to the current unsettled stage; its certification remains valid for later consumption of the same lot.")
        result["assemble-kit"] = ({"stage": int, "inputs": list[dict], "conditioning_ids": list[str]},
            "Consume exact current and older-stage material under the public bill of materials. Inputs have receipt_id, "
            "request_id, supplier_id, quantity. For each low-grade lot bind the conditioning records covering its use. "
            "Lot units are finite across all kits. A kit is required before its stage can be settled.")
    if business_program == "assembly_network_v3":
        result["claim-retention"] = ({"retention_ids": list[str]},
            "Collect distinct matured retention claims not yet credited by orders or this skill. "
            "Can run before or after purchasing and after final settlement; late collection credits a separate "
            "closeout record without rewriting history. No future-within-stage or beyond-horizon claim is cash. "
            "Do not put already explicitly collected claims in purchase retention_ids.")
        rule = (" Public material_transfers specifies exact origin_stage, consumer_stage and quantity. "
                "Reserve each outbound quantity and consume every incoming quantity at its consumer stage, "
                "plus unreserved current units. Do not infer fixed reuse delays." if business_scenario in {"irregular_bom", "quality_reuse"} else
                " For every public reuse_delays_stages entry, reserve one current unit when a future consumer exists, "
                "and consume one unit of the corresponding older origin when due. Do not use the old single-delay rule.")
        result["assemble-kit"] = (result["assemble-kit"][0], result["assemble-kit"][1] + rule)
    return result

def install_skills(root, domain, business_program=None, business_scenario=None, *, skill_protocol="procedural"):
    if skill_protocol not in {"procedural", "skill_declaration_v1"}:
        raise ValueError("Unknown Operations skill protocol")
    declared = skill_protocol == "skill_declaration_v1"
    if declared and (domain != "fieldops" or business_program != "pressure_branches_v3"):
        raise ValueError("Declaration skills require FieldOps pressure_branches_v3")
    from .planning import PLANNING_SKILLS
    for name, (inputs, description) in skill_specs(domain, business_program, business_scenario).items():
        if declared:
            description = description.replace("Closing a skill run alone does not close an isolation.",
                                              "Closing a declaration does not close an isolation.").replace(
                "abandon does not refund time or parts", "abandoning a declaration does not refund time or parts")
        path = Path(root) / name
        path.mkdir(parents=True)
        text = f"# {name}\n\n{description}\n\nInputs: {', '.join(inputs)}.\n\n"
        text += "All listed inputs are required; do not send extra fields or null. Input types:\n"
        text += "".join(f"- {key}: {input_type(kind)}.\n" for key, kind in inputs.items())
        text += "\n"
        text += "Inspect the project summary; search_objects and read_object provide public resource records, and read_artifact provides committed contents. "
        if declared:
            text += "Call execute_step(skill_id, step_id, arguments) with explicit business inputs; no declaration is required. "
        else:
            text += "In procedural mode select inputs with begin_skill. "
        if declared and name == "plan-isolation":
            text += ("Use draft with the complete inputs to commit a planning_draft. "
                     "Use check with {draft_id} to commit feasibility, concrete issues and evaluated plan content. "
                     "Use revise with {draft_id, arguments} and complete replacement inputs to create a new draft without changing its parent. "
                     "Use submit with {draft_id, check_id}; the check must refer to that exact draft. "
                     "Submission reassesses current business conditions. An older applicable draft/check remains usable; "
                     "another draft or an unrelated revision does not supersede it. "
                     "Draft, check and submit do not reserve resources; isolate-devices acquires them. ")
        elif name in PLANNING_SKILLS:
            text += ("Use execute_step draft with those inputs to save an inspectable planning_draft. "
                     "Use check with {draft_id} to obtain a planning_check containing feasibility, concrete violations and evaluated plan content. "
                     "To change choices, use revise with {draft_id, arguments} supplying all replacement business inputs, then check again. "
                     "Use submit with {draft_id, check_id} for this run's latest feasible draft/check, then finish_skill. "
                     "Submission rechecks current business constraints. Drafts and plans do not reserve resources; the separate execution skill does. ")
        elif declared:
            text += "Use step_id=compute with all listed business inputs. The original resource, state and version constraints still apply. "
        else:
            text += ("Use execute_step with step_id=compute and arguments containing the same complete business inputs "
                     "supplied to begin_skill (not an empty object), and finish_skill after commitment. ")
        if declared:
            text += ("Committed outputs may be consumed across declarations using their actual artifact IDs. "
                     "Reads and alternative valid scheduling are legal. Declarations do not reserve resources, "
                     "submit plans, close isolations, refund work or certify acceptance. "
                     "Optional begin_skill(run_id, skill_id, scope) and finish_skill(run_id, status, outputs, note) "
                     "belong to the declaration gateway, not these business tools. completed claims a local goal; "
                     "blocked and abandoned do not claim completion. Business completion does not require declaration closure.\n")
        else:
            text += "Committed outputs are usable before run closure. finish_skill returns a small receipt, not repeated contents. Reads and alternative valid scheduling are legal. "
            text += ("Obsolete runs can be abandoned without undoing committed business effects. "
                     "Finish the final acceptance run after it commits. Unrelated obsolete runs are reported separately and do not invalidate business acceptance.\n")
        if name in SCRIPT_SKILLS:
            text += "\nOptional run_skill_script performs only this skill's public arithmetic, never another skill or project planning.\n"
            if declared:
                text += "Use run_skill_script(skill_id, step_id, arguments), with skill_id=test-device and step_id=compute. Supply the same complete measurement inputs as execute_step.\n"
            if name == "plan-purchase":
                text += "For plan-purchase the script performs only check after draft/revise; it cannot choose suppliers, submit or place orders.\n"
            (path / "scripts").mkdir()
            (path / "scripts/execute.py").write_text(SCRIPT)
            shutil.copyfile(Path(__file__).with_name("calculations.py"), path / "scripts/calculations.py")
        (path / "SKILL.md").write_text(skill_header(name, description) + text)
