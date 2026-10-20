"""Public contracts for pooled working-capital sales execution."""

SKILLS = {
    "allocate-working-capital": {
        "inputs": {"summary_ids": list[str], "previous_settlement_id": str, "treasury_source_id": str},
        "script": True,
        "description": "Allocate available cash across regional demand for one period.",
        "procedure": "For funded_sales_v1, select one current demand summary per region for the same period. "
                     "Use treasury_source_id for the public opening cash. In the first period previous_settlement_id "
                     "is an empty string; thereafter consume the immediately preceding settlement: available cash is "
                     "its closing cash plus its receivables collected at the next period opening. Allocate cash in "
                     "proportion to regional demand usd_cents, round down, and distribute remaining cents in "
                     "ascending region order among positive-demand regions. With no demand leave cash unallocated. "
                     "These are spending limits, not realized sales or additional cash grants.",
    },
    "fulfill-orders": {
        "inputs": {"joined_id": str, "allocation_id": str}, "script": True,
        "description": "Fulfill affordable authorized orders within a regional cash allocation.",
        "procedure": "Use current joined orders and the allocation for the same period covering that region and "
                     "equivalent demand. Process ascending order_id. Each whole order costs ceil(usd_cents/2) to "
                     "fulfill. Accept an order only if its cost fits remaining regional cash; otherwise skip it "
                     "and continue. Unfilled orders expire at period end, not as backorders. Return every order "
                     "with fulfilled=0/1 and cost_cents (zero for unfilled). Do not count unfilled orders as sales.",
    },
    "settle-period": {
        "inputs": {"fulfillment_ids": list[str], "allocation_id": str, "terms_source_id": str},
        "script": False,
        "description": "Settle all regional fulfillment against the shared cash account and collection terms.",
        "procedure": "Select exactly one fulfillment per region, all from the same allocation and period. "
                     "Sum spending and fulfilled revenue. Collect floor(total revenue * numerator/denominator) "
                     "using that period's public collection terms; the remainder is a receivable collected at "
                     "the next period opening. Closing cash equals opening cash minus spending plus collections. "
                     "Unspent allocations remain cash. This settlement, not demand or a cumulative sales table, "
                     "finances the next period. Source amendments require restating affected settlements.",
    },
    "close-funded-period": {
        "inputs": {"settlement_id": str, "previous_rollup_id": str}, "script": False,
        "description": "Report realized regional sales from a settled funded period, including cumulative totals.",
        "procedure": "Read a current settlement and its regional fulfillment inputs. Aggregate only fulfilled "
                     "orders. Use an empty previous_rollup_id in the first period, otherwise the immediately "
                     "preceding funded rollup. This reports realized sales, unlike rollup-period/rollup-batch "
                     "which report demand and are not applicable to funded sales. Supply these rollups with "
                     "regional demand audits to review-release and publish-release.",
    },
}

INPUT_OBJECTS = {
    "allocate-working-capital": {"summary_ids": "list of current artifacts, kind summary",
        "previous_settlement_id": 'current artifact, kind settlement; empty string "" only in the first period',
        "treasury_source_id": "raw source, kind treasury"},
    "fulfill-orders": {"joined_id": "current artifact, kind joined", "allocation_id": "current artifact, kind allocation"},
    "settle-period": {"fulfillment_ids": "list of current artifacts, kind fulfillment",
        "allocation_id": "current artifact, kind allocation", "terms_source_id": "raw source, kind collection_terms"},
    "close-funded-period": {"settlement_id": "current artifact, kind settlement",
        "previous_rollup_id": 'current artifact, kind rollup; empty string "" only in the first period'},
}
