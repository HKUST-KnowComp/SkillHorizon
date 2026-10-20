"""Read-only local goals for one replay cut. No declaration execution or scoring.

Create one DataflowGoals per cut; instances share source/ancestor checks between
claims at that cut only. Unexpected exceptions deliberately reach the consumer.
"""

from copy import deepcopy

from ..errors import DomainError
from .claim_sources import ClaimSources, UnsupportedSource
from .obligations import prior_chain, receipt_row, period_number
from .obligation_verification import check_artifact
from .runtime import ordered, PROCEDURE_FIELDS
from .skills import skill_specs


GOAL_KINDS = {
    "normalize-orders": "orders", "harmonize-orders": "orders",
    "prepare-consent": "consent", "prepare-rates": "rates", "join-orders": "joined",
    "summarize-region": "summary", "audit-region": "audit", "assess-credit": "credit_assessment",
    "allocate-working-capital": "allocation", "fulfill-orders": "fulfillment",
    "settle-period": "obligation_settlement", "collect-invoices": "collection",
    "reconcile-disputed-invoices": "dispute_resolution", "close-funded-period": "rollup",
    "review-release": "review", "publish-release": "publication",
    "rollup-period": None, "rollup-batch": None,
}
SOURCE_GOALS = {"normalize-orders": "orders", "harmonize-orders": "external_orders",
                "prepare-consent": "consent", "prepare-rates": "rates"}
REGIONAL_GOALS = {"join-orders", "summarize-region", "audit-region", "fulfill-orders"}
RELEASE_GOALS = {"review-release", "publish-release"}
COLLECTION_GOALS = {"collect-invoices", "reconcile-disputed-invoices"}


class _Counterexample(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise _Counterexample(reason)


class DataflowGoals:
    def __init__(self, runtime):
        self.runtime = runtime
        self.project = runtime.project
        self.sources = ClaimSources(runtime)
        self.specs = skill_specs("credit_sales_v2")
        self.checked = set()
        self.visiting = set()

    def _arguments(self, artifact, fields):
        args = artifact["arguments"]
        require(isinstance(args, dict) and set(args) == set(fields), "argument_fields")
        for key, kind in fields.items():
            value = args[key]
            if kind is str:
                valid = isinstance(value, str)
            elif kind is dict:
                valid = isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
            else:
                valid = isinstance(value, list) and all(isinstance(v, str) for v in value)
                valid = valid and len(value) == len(set(value))
            require(valid, "argument_type_or_duplicates")
        return args

    def _artifact(self, identifier, kind=None):
        require(identifier in self.project["artifacts"], "missing_predecessor")
        item = self.project["artifacts"][identifier]
        require(kind is None or item["kind"] == kind, "artifact_kind")
        self._check(identifier)
        return item

    def _source(self, identifier, kind):
        require(identifier in self.project["sources"], "missing_source")
        item = self.project["sources"][identifier]
        require(item["kind"] == kind, "source_kind")
        return item

    def _edges(self, item, ids=(), sources=(), *, deduplicate=False):
        ids = list(dict.fromkeys(ids)) if deduplicate else list(ids)
        expected = [{"type": "artifact", "id": v} for v in ids]
        expected += [{"type": "source", "id": v, "revision": self.project["sources"][v]["revision"]} for v in sources]
        require(item["inputs"] == expected, "input_provenance")

    def _content(self, item, expected):
        actual = self.sources.rows(item)
        require(item["row_count"] == len(actual) and ordered(actual) == ordered(expected), "content_mismatch")

    def _same_scope(self, item, *others):
        require(all((v["region"], v["period"]) == (item["region"], item["period"]) for v in others), "scope_mismatch")

    def _regional_inputs(self, ids, kind, period):
        values = [self._artifact(v, kind) for v in ids]
        require(len(values) == len(self.project["regions"]) and
                {v["region"] for v in values} == set(self.project["regions"]) and
                all(v["period"] == period for v in values), "regional_coverage")
        return values

    def _chain(self, identifier, period):
        if identifier:
            self._artifact(identifier, "obligation_settlement")
        try:
            chain = prior_chain(self.runtime, identifier, period)
        except DomainError as exc:
            raise _Counterexample("predecessor_chain: " + exc.code) from exc
        for book in chain:
            self._artifact(book["artifact_id"], "obligation_settlement")
        return chain

    def _accounted(self, item):
        result = check_artifact(self.runtime, item, self.sources.accounting())
        if result is None:
            raise UnsupportedSource("accounting_kind_uncovered")
        require(result is True, "accounting_content")
        require(item["row_count"] == len(self.sources.rows(item)), "row_count")

    def _release_inputs(self, rollups, audits):
        expected_scope = {(r, p) for r in self.project["regions"] for p in self.project["periods"]}
        books = [self._artifact(v, "rollup") for v in rollups]
        checks = [self._artifact(v, "audit") for v in audits]
        require(len(books) == len(self.project["periods"]) and
                {v["period"] for v in books} == set(self.project["periods"]), "rollup_coverage")
        require(len(checks) == len(expected_scope) and
                {(v["region"], v["period"]) for v in checks} == expected_scope, "audit_coverage")
        rows = [row for book in books for row in self.sources.rows(book)]
        require(ordered(rows) == ordered(self.sources.accounting()["release"]), "release_content")
        return rows

    def _check(self, identifier):
        if identifier in self.checked:
            return
        require(identifier not in self.visiting, "cyclic_provenance")
        item = self.project["artifacts"][identifier]
        root = self.runtime.root.resolve()
        if not (root / item["path"]).resolve().is_relative_to(root):
            raise ValueError("Claim evidence path escapes replay root")
        require(self.runtime.current(identifier), "stale_or_missing_artifact")
        require(item["artifact_id"] == identifier, "artifact_identity")
        self.visiting.add(identifier)
        try:
            self._check_kind(item)
        finally:
            self.visiting.remove(identifier)
        self.checked.add(identifier)

    def _check_kind(self, item):
        kind, period, region = item["kind"], item["period"], item["region"]
        intermediate = {"authorized": ("join-orders", "authorize"),
                        "reconciliation": ("audit-region", "reconcile_rows"),
                        "coverage": ("review-release", "check_coverage")}
        require(item["intermediate"] is (kind in intermediate), "intermediate_flag")
        if kind in intermediate:
            producer, step = intermediate[kind]
            require(item["producer_skill"] == producer, "intermediate_producer")
            a = self._arguments(item, PROCEDURE_FIELDS[step])
        elif kind in {"joined", "audit", "review"}:
            first, last = {"joined": ("authorize", "convert"), "audit": ("reconcile_rows", "check_summary"),
                           "review": ("check_coverage", "check_totals")}[kind]
            a = self._arguments(item, {**PROCEDURE_FIELDS[first], **PROCEDURE_FIELDS[last]})
        else:
            skill = {v: k for k, v in GOAL_KINDS.items() if v is not None}.get(kind)
            if kind == "orders":
                source_id = item["arguments"].get("source_id")
                require(source_id in self.project["sources"], "missing_source")
                skill = "harmonize-orders" if self.project["sources"][source_id]["kind"] == "external_orders" else "normalize-orders"
            if skill is None:
                raise UnsupportedSource("artifact_kind_uncovered")
            a = self._arguments(item, self.specs[skill]["inputs"])

        if kind in {"orders", "consent", "rates"}:
            source = self._source(a["source_id"], "external_orders" if kind == "orders" and "column_map" in a else kind)
            self._same_scope(item, source)
            source_ids = [a["source_id"]]
            if source["kind"] == "external_orders":
                require({k: a[k] for k in a if k != "source_id"} == source["business_schema"], "external_schema_binding")
                source_ids.append(a["key_map_source_id"])
            elif kind == "orders":
                require(a["deduplication"] in {"first", "last"}, "deduplication")
            if kind == "orders":
                require(self.sources.package(region, period)["orders_source"] == a["source_id"], "orders_source_binding")
            self._edges(item, sources=source_ids)
            self._content(item, self.sources.source(a["source_id"]))
            return

        if kind in {"authorized", "reconciliation"}:
            orders = self._artifact(a["orders_id"], "orders")
            if kind == "authorized":
                other = self._artifact(a["consent_id"], "consent")
                require(other["region"] == region and set(a["join_keys"]) == {"customer_id", "region"}, "authorization_binding")
                require(other["arguments"]["source_id"] == self.sources.package(region, period)["consent_source"], "consent_source_binding")
                ids = [a["orders_id"], a["consent_id"]]
            else:
                other = self._artifact(a["joined_id"], "joined")
                self._same_scope(item, other)
                require(a["key_columns"] == ["order_id"], "reconciliation_key")
                ids = [a["orders_id"], a["joined_id"]]
            self._same_scope(item, orders)
            self._edges(item, ids, deduplicate=True)
            self._content(item, self.sources.regional(region, period)[kind])
            return

        if kind in {"joined", "audit", "review"}:
            mid_key, mid_kind, last = {"joined": ("authorized_id", "authorized", "convert"),
                "audit": ("reconciliation_id", "reconciliation", "check_summary"),
                "review": ("coverage_id", "coverage", "check_totals")}[kind]
            mid = self._artifact(a[mid_key], mid_kind)
            self._same_scope(item, mid)
            require(a == {**mid["arguments"], **{k: a[k] for k in PROCEDURE_FIELDS[last]}}, "intermediate_argument_binding")
            ids = [a[mid_key]]
            if kind == "joined":
                rates = self._artifact(a["rates_id"], "rates")
                require(rates["period"] in (None, period) and a["rounding"] == "half_up", "conversion_binding")
                require(rates["arguments"]["source_id"] == self.sources.package(region, period)["rates_source"], "rates_source_binding")
                ids.append(a["rates_id"])
                expected = self.sources.regional(region, period)["joined"]
            elif kind == "audit":
                summary = self._artifact(a["summary_id"], "summary")
                self._same_scope(item, summary)
                require(set(a["group_by"]) == {"region", "period"}, "audit_grouping")
                ids += [a["joined_id"], a["orders_id"], a["summary_id"]]
                expected = [{"region": region, "period": period, "passed": 1}]
            else:
                require(a["method"] in {"source_totals", "running_sum"}, "review_method")
                ids += a["rollup_ids"] + a["audit_ids"]
                expected = [{"passed": 1}]
            self._edges(item, ids, deduplicate=True)
            self._content(item, expected)
            return

        if kind == "summary":
            joined = self._artifact(a["joined_id"], "joined")
            self._same_scope(item, joined)
            self._edges(item, [a["joined_id"]])
            self._content(item, self.sources.regional(region, period)["summary"])
            return

        if kind in {"coverage", "publication"}:
            require(region is None and period is None, "release_scope")
            if kind == "coverage":
                require(set(a["required_periods"]) == set(self.project["periods"]), "required_periods")
                expected = self._release_inputs(a["rollup_ids"], a["audit_ids"])
                ids = a["rollup_ids"] + a["audit_ids"]
            else:
                review = self._artifact(a["review_id"], "review")
                expected = self._release_inputs(a["rollup_ids"], review["arguments"]["audit_ids"])
                ids = a["rollup_ids"] + [a["review_id"]]
            self._edges(item, ids, deduplicate=kind == "coverage")
            self._content(item, expected)
            return

        require(period in self.project["periods"], "period_scope")
        require(region in self.project["regions"] if kind == "fulfillment" else region is None, "accounting_scope")
        if kind in {"collection", "dispute_resolution"}:
            require(a["period"] == period and bool(a["invoice_ids"]), "collection_scope")
            invoices = {}
            for value in a["settlement_ids"]:
                book = self._artifact(value, "obligation_settlement")
                require(period_number(book["period"]) < period_number(period), "invoice_origin")
                for row in self.sources.rows(book):
                    if row["record_type"] == "invoice":
                        require(row["invoice_id"] not in invoices or invoices[row["invoice_id"]] == row, "invoice_conflict")
                        invoices[row["invoice_id"]] = row
            evidence = []
            for value in a.get("fulfillment_ids", []):
                evidence += self.sources.rows(self._artifact(value, "fulfillment"))
            for value in a["invoice_ids"]:
                require(value in invoices, "invoice_binding")
                row = invoices[value]
                require(row["due_period"] == period and row["payment_route"] == (
                    "normal" if kind == "collection" else "disputed"), "invoice_due_route")
                if kind == "dispute_resolution":
                    require(any(e["fulfilled"] == 1 and e["usd_cents"] == row["amount_cents"] and
                                all(e[k] == row[k] for k in ("order_id", "customer_id", "region", "period"))
                                for e in evidence), "invoice_fulfillment_evidence")
            self._edges(item, a["settlement_ids"] + a.get("fulfillment_ids", []))
        elif kind == "credit_assessment":
            self._regional_inputs(a["joined_ids"], "joined", period)
            chain = self._chain(a["previous_settlement_id"], period)
            for value in a["settlement_ids"]:
                self._artifact(value, "obligation_settlement")
            required = {book["artifact_id"] for book in chain if any(row["record_type"] == "invoice" and
                period_number(row["due_period"]) > period_number(period) for row in self.sources.rows(book))}
            require(required <= set(a["settlement_ids"]) <= {b["artifact_id"] for b in chain}, "credit_ledger_coverage")
            require(a["limits_source_id"] == self.project["funding"]["credit_limits_source"], "limits_source_binding")
            self._source(a["limits_source_id"], "credit_limits")
            self._edges(item, a["joined_ids"] + list(dict.fromkeys(a["settlement_ids"] +
                        ([a["previous_settlement_id"]] if chain else []))), [a["limits_source_id"]])
        elif kind == "allocation":
            summaries = self._regional_inputs(a["summary_ids"], "summary", period)
            chain = self._chain(a["previous_settlement_id"], period)
            require(a["treasury_source_id"] == self.project["funding"]["treasury_source"], "treasury_binding")
            self._source(a["treasury_source_id"], "treasury")
            due = {row["invoice_id"]: receipt_row(row, period) for book in chain for row in self.sources.rows(book)
                   if row["record_type"] == "invoice" and row["due_period"] == period}
            receipts = {}
            for value in a["collection_ids"]:
                receipt = self._artifact(value)
                require(receipt["kind"] in {"collection", "dispute_resolution"} and receipt["period"] == period, "collection_binding")
                for row in self.sources.rows(receipt):
                    require(row["invoice_id"] not in receipts and due.get(row["invoice_id"]) == row, "duplicate_or_wrong_receipt")
                    receipts[row["invoice_id"]] = row
            require(receipts == due, "due_collection_coverage")
            assessment = self._artifact(a["credit_assessment_id"], "credit_assessment")
            require(assessment["period"] == period and assessment["arguments"]["previous_settlement_id"] == a["previous_settlement_id"] and
                    set(assessment["arguments"]["joined_ids"]) == {s["arguments"]["joined_id"] for s in summaries}, "credit_binding")
            self._edges(item, a["summary_ids"] + a["collection_ids"] +
                        ([a["previous_settlement_id"]] if chain else []) + [a["credit_assessment_id"]],
                        [] if chain else [a["treasury_source_id"]])
        elif kind == "fulfillment":
            joined = self._artifact(a["joined_id"], "joined")
            allocation = self._artifact(a["allocation_id"], "allocation")
            self._same_scope(item, joined)
            require(allocation["period"] == period, "allocation_period")
            summaries = [self._artifact(v, "summary") for v in allocation["arguments"]["summary_ids"]]
            demand = next(s for s in summaries if s["region"] == region)
            original = self._artifact(demand["arguments"]["joined_id"], "joined")
            require(ordered(self.sources.rows(joined)) == ordered(self.sources.rows(original)), "allocated_demand_binding")
            self._edges(item, [a["joined_id"], a["allocation_id"]])
        elif kind == "obligation_settlement":
            values = self._regional_inputs(a["fulfillment_ids"], "fulfillment", period)
            allocation = self._artifact(a["allocation_id"], "allocation")
            require(allocation["period"] == period and all(v["arguments"]["allocation_id"] == a["allocation_id"] for v in values), "settlement_allocation_binding")
            require(a["terms_source_id"] == self.project["funding"]["invoice_terms_source"], "terms_source_binding")
            self._source(a["terms_source_id"], "invoice_terms")
            self._edges(item, a["fulfillment_ids"] + [a["allocation_id"]], [a["terms_source_id"]])
        elif kind == "rollup":
            settlement = self._artifact(a["settlement_id"], "obligation_settlement")
            require(settlement["period"] == period, "settlement_period")
            index = self.project["periods"].index(period)
            ids = [a["settlement_id"]]
            if index:
                prior = self._artifact(a["previous_rollup_id"], "rollup")
                require(prior["period"] == self.project["periods"][index - 1], "rollup_predecessor")
                ids.append(a["previous_rollup_id"])
            else:
                require(a["previous_rollup_id"] == "", "rollup_predecessor")
            self._edges(item, ids)
            self._content(item, [row for row in self.sources.accounting()["release"] if row["period"] == period])
            return
        else:
            raise UnsupportedSource("artifact_kind_uncovered")
        self._accounted(item)

    def _invoice_reference(self, identifier, outputs):
        def invoice_rows(books):
            return [row for book in books for row in self.sources.rows(book)
                    if row["record_type"] == "invoice" and row["invoice_id"] == identifier]

        rows, anchored = [], False
        for _, _, output in outputs:
            if output["kind"] not in {"collection", "dispute_resolution"}:
                continue
            name = "collect-invoices" if output["kind"] == "collection" else "reconcile-disputed-invoices"
            args = self._arguments(output, self.specs[name]["inputs"])
            if identifier not in args["invoice_ids"]:
                continue
            # Recorded parameters alone are not provenance. Do not replace a
            # specified old/bad receipt with a different current receipt/book.
            self._edges(output, args["settlement_ids"] + args.get("fulfillment_ids", []))
            books = []
            for value in args["settlement_ids"]:
                book = self.project["artifacts"].get(value)
                require(book is not None and book["kind"] == "obligation_settlement", "invoice_ledger_binding")
                books.append(book)
            bound = invoice_rows(books)
            require(bool(bound), "invoice_binding")
            rows.extend(bound)
            anchored = True
        if not anchored:
            books = [a for a in self.project["artifacts"].values() if a["kind"] == "obligation_settlement"]
            rows = invoice_rows([a for a in books if self.runtime.current(a["artifact_id"])])
            if not rows:
                # History can locate a target, never certify current completion.
                rows = invoice_rows(books)
        if not rows:
            return None
        fields = ("invoice_id", "order_id", "customer_id", "region", "period", "due_period", "payment_route")
        identities = {tuple(row[key] for key in fields) for row in rows}
        if len(identities) != 1:
            return None
        # Amount belongs to content verification, not target identity. Return no
        # arbitrarily selected monetary version when identities are equivalent.
        identity = dict(zip(fields, next(iter(identities))))
        return {**identity, "origin_period": identity["period"], "period": identity["due_period"]}

    def _references(self, refs, *, invoice_outputs=()):
        if not isinstance(refs, list):
            raise UnsupportedSource("scope_unresolved")
        found = []
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"kind", "id"} or not isinstance(ref["id"], str):
                raise UnsupportedSource("scope_unresolved")
            kind, identifier = ref["kind"], ref["id"]
            if kind in {"source", "artifact"}:
                item = self.project[kind + "s"].get(identifier)
            elif kind == "work_package":
                matches = [p for p in self.project["work_packages"] if p["id"] == identifier]
                item = matches[0] if len(matches) == 1 else None
            elif kind == "period":
                item = {"period": identifier} if identifier in self.project["periods"] else None
            elif kind == "project":
                item = {} if identifier == self.project["project_id"] else None
            elif kind == "invoice":
                item = self._invoice_reference(identifier, invoice_outputs)
            else:
                item = None
            if item is None:
                raise UnsupportedSource("scope_unresolved")
            found.append((kind, identifier, item))
        return found

    def _universe(self, name):
        if name in SOURCE_GOALS:
            return {(identifier,) for identifier, source in self.project["sources"].items() if source["kind"] == SOURCE_GOALS[name]}
        if name in REGIONAL_GOALS:
            return {(p["region"], p["period"]) for p in self.project["work_packages"]}
        if name in RELEASE_GOALS:
            return {(self.project["project_id"],)}
        return {(p,) for p in self.project["periods"]}

    def _matching(self, name, reference, targets):
        kind, identifier, item = reference
        if name in RELEASE_GOALS:
            return targets if kind == "project" or (kind == "artifact" and item["kind"] in {"coverage", "review", "publication"}) else set()
        if kind == "project":
            return targets
        if name in SOURCE_GOALS:
            field = {"normalize-orders": "orders_source", "harmonize-orders": "orders_source",
                     "prepare-consent": "consent_source", "prepare-rates": "rates_source"}[name]
            if kind == "source":
                return {t for t in targets if t[0] == identifier}
            if kind == "artifact" and item["kind"] == GOAL_KINDS[name]:
                return {t for t in targets if t[0] == item["arguments"].get("source_id")}
            if kind == "work_package":
                return {t for t in targets if t[0] == item[field]}
            if kind == "period":
                return {t for t in targets if any(p["period"] == identifier and p[field] == t[0]
                                                 for p in self.project["work_packages"])}
            return {t for t in targets if self.project["sources"][t[0]].get("period") == item.get("period") and item.get("period") is not None}
        if name in REGIONAL_GOALS:
            if kind == "source":
                return {t for t in targets if identifier in [self.sources.package(*t)[field] for field in
                        ("orders_source", "consent_source", "rates_source")]}
            return {t for t in targets if (item.get("region") is None or item["region"] == t[0]) and
                    (item.get("period") is None or item["period"] == t[1])}
        return {t for t in targets if item.get("period") is None or item["period"] == t[0]}

    def _target(self, name, item):
        if name in SOURCE_GOALS:
            return (item["arguments"].get("source_id"),)
        if name in REGIONAL_GOALS:
            return item["region"], item["period"]
        if name in RELEASE_GOALS:
            return (self.project["project_id"],)
        return (item["period"],)

    def _candidate(self, name, identifier):
        item = self.project["artifacts"][identifier]
        require(item["kind"] == GOAL_KINDS[name] and not item["intermediate"], "output_kind")
        self._check(identifier)
        if name == "publish-release":
            require(self.project["publication"] == identifier, "not_delivered_publication")

    def assess(self, claim):
        report = {key: deepcopy(claim[key]) for key in
                  ("run_id", "skill_id", "event_index", "last_resolved_business_dispatch")}
        report.update(scope=deepcopy(claim.get("scope")), clauses=[],
                      process_adherence="unobserved",
                      process_adherence_reason="no_trusted_per_run_business_attribution")
        name = claim["skill_id"]
        try:
            if self.project.get("business_program") != "credit_sales_v2" or name not in GOAL_KINDS or GOAL_KINDS[name] is None:
                raise UnsupportedSource("not_applicable")
            outputs = self._references(claim.get("outputs", []))
            if any(kind != "artifact" for kind, _, _ in outputs):
                raise UnsupportedSource("output_unresolved")
            scope = self._references(claim.get("scope", []),
                                     invoice_outputs=outputs if name in COLLECTION_GOALS else ())
            universe = self._universe(name)
            if not universe:
                raise UnsupportedSource("not_applicable")
            targets = universe
            for ref in scope:
                targets = self._matching(name, ref, targets)
            if outputs:
                for _, _, item in outputs:
                    require(item["kind"] == GOAL_KINDS[name] and not item["intermediate"], "output_kind")
                output_targets = {self._target(name, item) for _, _, item in outputs}
                if len(output_targets) != 1:
                    raise UnsupportedSource("ambiguous_output_targets")
                require(output_targets <= targets, "output_scope_mismatch")
                targets = output_targets
            if not scope and not outputs or len(targets) != 1:
                raise UnsupportedSource("scope_unresolved")
            target = next(iter(targets))
            report["resolved_goal"] = list(target)
            candidates = [identifier for _, identifier, _ in outputs] if outputs else [
                identifier for identifier, item in self.project["artifacts"].items()
                if item["kind"] == GOAL_KINDS[name] and not item["intermediate"] and self._target(name, item) == target]
            wanted_invoices = {identifier for kind, identifier, _ in scope if kind == "invoice"}
            if name in COLLECTION_GOALS:
                if not wanted_invoices:
                    if outputs:
                        wanted_invoices = {v for _, _, item in outputs for v in item["arguments"].get("invoice_ids", [])}
                    else:
                        route = "normal" if name == "collect-invoices" else "disputed"
                        wanted_invoices = {v for (p, v), row in self.sources.accounting()["collections"].items()
                                           if p == target[0] and row["payment_route"] == route}
                if not wanted_invoices:
                    raise UnsupportedSource("no_invoice_target")
            supported, unknown, covered = [], False, set()
            for identifier in candidates:
                try:
                    self._candidate(name, identifier)
                    if name in COLLECTION_GOALS:
                        ids = set(self.project["artifacts"][identifier]["arguments"]["invoice_ids"])
                        matched = ids & wanted_invoices
                        require(bool(matched), "output_invoice_scope")
                        covered.update(matched)
                    supported.append(identifier)
                    verdict, reason = "supported", "content_and_bindings"
                except _Counterexample as exc:
                    verdict, reason = "contradicted", str(exc)
                except UnsupportedSource as exc:
                    verdict, reason, unknown = "unknown", str(exc), True
                report["clauses"].append({"artifact_id": identifier, "clause": "local_content_and_required_provenance",
                    "verdict": verdict, "reason": reason,
                    "basis": "shared_pure_compute; source_only_accounting; explicit_read_only_bindings"})
            if outputs and any(c["verdict"] == "contradicted" for c in report["clauses"]):
                raise _Counterexample("explicit_output_failed")
            enough = bool(supported) and (name not in COLLECTION_GOALS or covered == wanted_invoices)
            if enough and (not outputs or len(supported) == len(candidates)):
                report.update(goal_verdict="supported", reason="goal_satisfied")
            elif unknown:
                raise UnsupportedSource("necessary_semantics_uncovered")
            else:
                raise _Counterexample("missing_or_invalid_required_artifact")
        except UnsupportedSource as exc:
            report.update(goal_verdict="unknown", reason=str(exc))
        except _Counterexample as exc:
            report.update(goal_verdict="contradicted", reason=str(exc))
        return report
