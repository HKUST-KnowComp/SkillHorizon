"""Skill contracts for persistent invoice obligations; no instance answers."""

from copy import deepcopy
from .funding_skills import SKILLS as FUNDING_SKILLS, INPUT_OBJECTS as FUNDING_INPUTS

SKILLS = deepcopy(FUNDING_SKILLS)
INPUT_OBJECTS = deepcopy(FUNDING_INPUTS)
SKILLS["allocate-working-capital"]["inputs"]["collection_ids"] = list[str]
SKILLS["allocate-working-capital"]["procedure"] = (
    "For obligation_sales_v1, select one current demand summary per region for one period. "
    "Select the immediately preceding obligation settlement, or an empty previous_settlement_id in period one. "
    "Opening cash is previous closing cash plus this period's verified collection receipts, not total receivables. "
    "Only period one uses the opening treasury source. collection_ids must cover every invoice due this period "
    "from the preceding settlement chain exactly once. Inspect older invoice ledgers and collect normal invoices "
    "or reconcile disputed invoices using the relevant skills. Supply an empty list when no invoice is due. "
    "Allocate cash proportional to current regional demand, flooring shares and distributing remainder cents "
    "in ascending region order among positive-demand regions. Zero demand leaves cash unallocated.")
INPUT_OBJECTS["allocate-working-capital"].update(
    previous_settlement_id='current artifact, kind obligation_settlement; empty string "" only in the first period',
    collection_ids="list of current artifacts, kind collection or dispute_resolution")
SKILLS["settle-period"]["description"] = "Close cash spending and issue order-level invoices with customer payment terms."
SKILLS["settle-period"]["procedure"] = (
    "Select exactly one fulfillment per region from one allocation and period, plus the public invoice_terms source. "
    "Close cash at opening cash minus procurement spending. Issue an invoice for each fulfilled positive-value "
    "order. Each invoice retains order, customer, region and origin period; customer terms specify delay_periods "
    "and payment_route (normal or disputed). Invoice due period is origin plus delay. No invoice is collected "
    "automatically. The output ledger contains one cash row and invoice rows. Future-due invoices remain "
    "outstanding at project end; do not force early collection. This is an accounting restatement, not live shipment reversal.")
INPUT_OBJECTS["settle-period"]["terms_source_id"] = "raw source, kind invoice_terms"
SKILLS["close-funded-period"]["procedure"] = (
    "Select a current obligation_settlement and the preceding funded rollup (empty string in period one). "
    "Aggregate the settlement's actual fulfillment records into realized sales and cumulative totals. "
    "Review-release checks demand audits, the invoice ledgers and collections; publication also requires all "
    "payments due within the horizon to be settled. Future-due invoices stay recorded, not silently forgiven.")
INPUT_OBJECTS["close-funded-period"]["settlement_id"] = "current artifact, kind obligation_settlement"

for name, disputed in (("collect-invoices", False), ("reconcile-disputed-invoices", True)):
    SKILLS[name] = {
        "inputs": {"settlement_ids": list[str], "invoice_ids": list[str], "period": str,
                   **({"fulfillment_ids": list[str]} if disputed else {})},
        "script": False,
        "description": ("Resolve disputed invoices using original fulfillment evidence and record payment."
                        if disputed else "Record payment for normal invoices due in this business period."),
        "procedure": (
            "Read current obligation settlement ledgers. Select explicit business invoice IDs due in the requested "
            "period from the supplied settlement_ids. Multiple origins and regions may be combined, or processed "
            "in separate calls. Invoice IDs are not artifact IDs. Never collect early, twice in one allocation, "
            "or credit a receipt to another period. " +
            ("Only disputed invoices apply. Provide current fulfillment artifacts proving the original order was "
             "fulfilled for this customer and amount. Equal totals from unrelated orders are not proof."
             if disputed else "Only normal-route invoices apply; disputed invoices need reconciliation evidence.") +
            " A receipt is a reusable accounting record, not an immediate second mutation of the cash balance; "
            "allocation credits each due invoice once. An empty request is not a business operation."),
    }
    INPUT_OBJECTS[name] = {"settlement_ids": "list of current artifacts, kind obligation_settlement",
                          "invoice_ids": "list of business invoice IDs found in the supplied ledgers"}
    if disputed:
        INPUT_OBJECTS[name]["fulfillment_ids"] = "list of current artifacts, kind fulfillment"

FIELDS = {
    "obligation_settlement": ["record_type", "period", "region", "customer_id", "order_id", "invoice_id",
                              "due_period", "payment_route", "amount_cents", "closing_cash_cents"],
    "collection": ["period", "invoice_id", "origin_period", "region", "customer_id", "order_id", "amount_cents", "payment_route"],
}
FIELDS["dispute_resolution"] = FIELDS["collection"]
