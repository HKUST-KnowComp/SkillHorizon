"""Read-only public contract views; no filesystem paths, task answers or scoring."""

from copy import deepcopy

from . import PROCURE_INTERFACE_REVISION


DOCUMENTS = {
    "procurement-contract": "Procurement resources and contract terms",
    "joint-acceptance": "Joint delivery acceptance: scope and application",
}


def public_document(project, document_id):
    if document_id == "procurement-contract":
        content = {
            "terms": {key: deepcopy(project[key]) for key in
                      ("total_budget_cents", "contract", "assembly_rules") if key in project},
            "records": {"requests": "Read request quantities, deadlines, stages and eligible suppliers.",
                        "suppliers": "Read current prices, capacity, availability and component grades.",
                        "orders": "Read committed, received and canceled quantities.",
                        "artifacts": "Read budgets, receipts, kits and settlement records."},
            "budget_rule": "Allocations replace cumulative caps; they do not add funds or erase commitments. "
                           "Purchasing and any applicable conditioning share the project total cap.",
            "clock": "Delivery slots are business reservations, not model turns or HTTP waiting time.",
        }
        if "assembly_rules" in project:
            content["material_rule"] = "When material_transfers is present it defines the bill of materials, " \
                "otherwise use reuse_delays_stages or reuse_delay_stages. " \
                "Units are finite across kits; low-grade units require paid conditioning."
    elif document_id == "joint-acceptance":
        clauses = [
            {"id": "receipt_coverage", "records": ["requests", "receipt artifacts"],
             "rule": "Supply distinct receipt_ids delivering exactly every request quantity before its deadline."},
            {"id": "commitments", "records": ["orders", "receipt and cancellation artifacts"],
             "rule": "All ordered units must be received or canceled; records and refunds must reconcile."},
            {"id": "budgets", "records": ["current budget artifact", "orders", "total_budget_cents"],
             "rule": "Current cumulative request caps and the project total must cover all committed spend."},
            {"id": "supplier_terms", "records": ["requests", "suppliers", "orders", "receipts"],
             "rule": "Eligibility, current prices, total capacity, availability and per-slot capacity must hold."},
        ]
        if "contract" in project:
            clauses.append({"id": "settlements", "records": ["contract", "settlement and retention artifacts"],
                            "rule": "All stages, funding chains, cash and retention records must reconcile; "
                                    "deliveries must follow stage release and preceding settlement."})
        if "assembly_rules" in project:
            clauses.append({"id": "assembly", "records": ["assembly_rules", "orders", "receipts", "kits", "conditioning artifacts"],
                            "rule": "Kits, finite lot use, grades and conditioning must reconcile. "
                                    "Processing fees and committed purchases must fit the project total."})
        content = {
            "service": "accept-deliveries", "scope": "joint_project_delivery_contract",
            "clauses": clauses,
            "application": "Read the current business records, reconcile the applicable clauses, then submit "
                           "a complete nonduplicated receipt set. A rejected application can be corrected and resubmitted.",
            "result": "A successful application commits an acceptance record. It does not end the agent episode.",
            "coverage": "Covers this public contract, not requirements outside it. "
                        "For a task consisting only of this contract and its acceptance, coverage can equal the whole goal.",
            "feedback": "This document declares rules, not current pass/fail, a complete pending-work list or a future plan.",
        }
    else:
        raise ValueError("Unknown public document")
    return {"document_id": document_id, "title": DOCUMENTS[document_id],
            "interface_revision": PROCURE_INTERFACE_REVISION,
            "project_revision": project["revision"], "content": content}
