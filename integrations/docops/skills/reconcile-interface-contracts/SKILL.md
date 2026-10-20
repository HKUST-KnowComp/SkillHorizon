---
name: reconcile-interface-contracts
description: Combine actual upstream interface contracts with local fields and operational limits.
---

# Reconcile Interface Contracts

Read the selected page's requirements and the full publications of its declared parents. Construct a
contract with `fields`, `limits` and `imports`. Preserve the exact union of local and parent fields,
including type and unit. Conflicting definitions require investigation, not a silent overwrite.
Use the minimum source/local batch-size limit and retention cap. Bind each import to the correct page,
content version and publication ID. A similarly named or older publication is not interchangeable.

The optional `scripts/merge_contracts.py` accepts JSON containing `requirements` and `publications`,
and returns a proposed `contract`. It only computes from supplied public inputs. It cannot read the
environment, determine whether a supplied publication is still current, edit pages or grant approval.
Inspect its result before revising the actual document. Manual reconciliation is also valid.

Outputs: proposed contract and the source references used to construct it.
