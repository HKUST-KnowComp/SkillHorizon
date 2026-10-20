"""A programmed public-API usability witness, not model behavior or a gold tool.

Receives only an async tool caller. No filesystem, runtime, reference, or evaluator
access is available to its policy. It covers regional maintenance, not arbitrary
unknown skill instructions or heterogeneous schema inference.
"""


class PublicMaintenanceWitness:
    def __init__(self, invoke):
        self.invoke = invoke
        self.calls = []
        self.read_skills = set()

    async def call(self, name, **arguments):
        result = await self.invoke(name, arguments)
        self.calls.append({"skill_id": name, "arguments": arguments, "result": result})
        if not result["ok"]:
            raise ValueError(f"Public witness rejected at {name}: {result}")
        return result

    async def objects(self, collection, **filters):
        offset, result = 0, {}
        while offset is not None:
            found = await self.call("search_objects", collection=collection, text="",
                filters=[{"field": key, "value": value} for key, value in filters.items()], offset=offset, limit=200)
            for item in found["items"]:
                record = await self.call("read_object", collection=collection, object_id=item["id"])
                result[item["id"]] = record["object"]
            offset = found["next_offset"]
        return result

    async def skill(self, name, arguments, steps):
        if name not in self.read_skills:
            await self.call("read_skill_file", skill_id=name, path="SKILL.md")
            self.read_skills.add(name)
        opened = await self.call("begin_skill", skill_id=name, arguments=arguments)
        previous = None
        for step, bindings in steps:
            previous = await self.call("execute_step", run_id=opened["run_id"], step_id=step,
                                       arguments=bindings(previous) if callable(bindings) else bindings)
        return await self.call("finish_skill", run_id=opened["run_id"])

    async def ensure(self, name, arguments, steps=None):
        for identifier, artifact in (await self.objects("artifacts", producer_skill=name)).items():
            if artifact["current"] and not artifact.get("intermediate") and artifact["arguments"] == arguments:
                return identifier
        return (await self.skill(name, arguments, steps or [("compute", arguments)]))["artifact_id"]

    async def run(self):
        project = await self.call("read_project")
        changes = await self.objects("changes")
        for identifier, change in changes.items():
            if change["resolution"]:
                continue
            evidence = (await self.call("read_object", collection="evidence", object_id=change["evidence_id"]))["object"]
            policies = await self.objects("policies", region=change["region"], period=change["period"])
            delta = evidence["settled_amount_cents"] - evidence["booked_amount_cents"]
            policy = next(key for key, p in policies.items() if abs(delta) <= p["max_adjustment_cents"])
            decision = await self.skill("review-adjustment", {"change_id": identifier, "policy_id": policy, "supersedes_id": ""}, [
                ("compare", {"delta_cents": delta}),
                ("decide", {"choice": "apply" if evidence["status"] == "confirmed" else "reject",
                            "rationale": "Use the component settlement and regional policy; the ticket is an unverified claim.",
                            "alternatives": ["Apply the ticket claim without settlement reconciliation"]})])
            await self.skill("apply-adjustment", {"decision_id": decision["decision_id"]}, [
                ("prepare", {}), ("commit", lambda plan: {"plan_id": plan["plan_id"]})])
        sources = await self.objects("sources")
        def source(kind, region=None, period=None):
            return next(key for key, s in sources.items() if s["kind"] == kind
                        and (region is None or s.get("region") == region)
                        and (period is None or s.get("period") == period))
        consumers = await self.objects("consumers")
        pinned = {(c["region"], c["period"]): c["audit_id"] for c in consumers.values()}
        consents = {region: await self.ensure("prepare-consent", {"source_id": source("consent", region)})
                    for region in project["regions"]}
        audits, rollups, previous = [], [], ""
        for period in project["periods"]:
            rates = await self.ensure("prepare-rates", {"source_id": source("rates", period=period)})
            summaries = []
            for region in project["regions"]:
                orders = await self.ensure("normalize-orders", {"source_id": source("orders", region, period), "deduplication": "first"})
                selected = {"orders_id": orders, "consent_id": consents[region], "rates_id": rates}
                joined = await self.ensure("join-orders", selected, [
                    ("authorize", {"orders_id": orders, "consent_id": consents[region], "join_keys": ["customer_id", "region"]}),
                    ("convert", lambda mid: {"authorized_id": mid["artifact_id"], "rates_id": rates, "rounding": "half_up"})])
                summary = await self.ensure("summarize-region", {"joined_id": joined})
                summaries.append(summary)
                if (region, period) in pinned:
                    audit = pinned[region, period]
                    record = await self.call("read_object", collection="artifacts", object_id=audit)
                    if not record["object"]["current"]:
                        raise ValueError("A required consumer audit unexpectedly became stale")
                else:
                    audit = await self.ensure("audit-region", {"orders_id": orders, "joined_id": joined, "summary_id": summary}, [
                        ("reconcile_rows", {"orders_id": orders, "joined_id": joined, "key_columns": ["order_id"]}),
                        ("check_summary", lambda mid: {"reconciliation_id": mid["artifact_id"], "summary_id": summary, "group_by": ["region", "period"]})])
                audits.append(audit)
            previous = await self.ensure("rollup-period", {"summary_ids": summaries, "previous_rollup_id": previous})
            rollups.append(previous)
        selected = {"rollup_ids": rollups, "audit_ids": audits}
        review = await self.skill("review-release", selected, [
            ("check_coverage", {**selected, "required_periods": project["periods"]}),
            ("check_totals", lambda mid: {"coverage_id": mid["artifact_id"], "method": "source_totals"})])
        await self.skill("publish-release", {"rollup_ids": rollups, "review_id": review["artifact_id"]}, [
            ("compute", {"rollup_ids": rollups, "review_id": review["artifact_id"]})])
        return await self.call("read_project")


class PublicWitnessFailure(ValueError):
    """A bounded policy failure, never evidence of business success."""

    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


class _PublicSourceChanged(Exception):
    pass


class PublicCreditWitness:
    """Fixed credit_sales_v2 policy receiving only async call(name, arguments).

    Instance data comes exclusively from public responses. Limits are policy
    bounds, not inferred event counts. This is not an arbitrary-skill interpreter.
    The legacy maintenance policy above remains a separate evidence path.
    """

    def __init__(self, call, *, skill_protocol="procedural", max_calls=5000, max_rounds=8, page_size=200):
        if skill_protocol not in ("procedural", "skill_declaration_v1"):
            raise ValueError("Unknown DataOps skill protocol")
        for value in (max_calls, max_rounds, page_size):
            if type(value) is not int or value < 1:
                raise ValueError("Policy bounds must be positive integers")
        if page_size > 200:
            raise ValueError("Public page size must not exceed 200")
        self.invoke = call
        self.skill_protocol = skill_protocol
        self.max_calls, self.max_rounds, self.page_size = max_calls, max_rounds, page_size
        self.call_count = self.rounds = 0
        self.instructions = {}
        self.script_files = set()
        self.artifacts = {}
        self.artifact_records = {}
        self.periods = []

    async def call(self, name, **arguments):
        if self.call_count >= self.max_calls:
            raise PublicWitnessFailure("CALL_LIMIT", "Public call budget exhausted")
        self.call_count += 1
        result = await self.invoke(name, arguments)
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            raise PublicWitnessFailure("MISSING_PUBLIC_INFORMATION", f"Malformed response: {name}")
        if not result["ok"]:
            code = result.get("error", {}).get("code")
            if code == "STALE_ARTIFACT":
                raise _PublicSourceChanged()
            raise PublicWitnessFailure("PUBLIC_TOOL_FAILED", f"{name}: {code}")
        return result

    @staticmethod
    def require(condition, detail):
        if not condition:
            raise PublicWitnessFailure("MISSING_PUBLIC_INFORMATION", detail)

    async def pages(self, name, field, **arguments):
        rows, offset = [], 0
        while offset is not None:
            response = await self.call(name, **arguments, offset=offset, limit=self.page_size)
            if response.get("current") is False:
                raise _PublicSourceChanged()
            batch, following = response[field], response["next_offset"]
            self.require(isinstance(batch, list) and len(batch) <= self.page_size,
                         "Invalid public page contents")
            self.require(following is None or (type(following) is int and following > offset
                         and following == offset + len(batch)), "Invalid public pagination cursor")
            rows.extend(batch)
            total = response["row_count" if name == "read_table" else "total"]
            self.require(type(total) is int and total >= len(rows), "Invalid public row count")
            if following is None:
                self.require(len(rows) == total, "Incomplete public pagination")
            offset = following
        return rows

    async def objects(self, collection):
        result = {}
        for item in await self.pages("search_objects", "items", collection=collection, text="", filters=[]):
            identifier = item["id"]
            self.require(identifier not in result, "Duplicate public object")
            result[identifier] = (await self.call("read_object", collection=collection,
                                                  object_id=identifier))["object"]
        return result

    async def rows(self, identifier):
        return await self.pages("read_table", "rows", table_id=identifier)

    async def skill(self, name, arguments, steps=None):
        if name not in self.instructions:
            content = (await self.call("read_skill_file", skill_id=name, path="SKILL.md"))["content"]
            self.require(isinstance(content, str) and bool(content.strip()), "Missing public procedure")
            self.instructions[name] = content
        scripted = "Optional scripts/execute.py" in self.instructions[name]
        if scripted and name not in self.script_files:
            content = (await self.call("read_skill_file", skill_id=name, path="scripts/execute.py"))["content"]
            self.require(isinstance(content, str) and bool(content.strip()), "Missing installed script")
            self.script_files.add(name)
        if self.skill_protocol == "skill_declaration_v1":
            previous = None
            for step, bindings in steps or [("compute", arguments)]:
                tool = "run_skill_script" if scripted and (
                    step == "compute" or (name == "join-orders" and step == "convert")) else "execute_step"
                previous = await self.call(tool, skill_id=name, step_id=step,
                                           arguments=bindings(previous) if callable(bindings) else bindings)
                if previous.get("notices") or previous.get("current") is False:
                    # Committed effects survive retry; there is no business run to abandon.
                    raise _PublicSourceChanged()
                self.require(previous.get("current") is True
                             and isinstance(previous.get("artifact_id"), str)
                             and bool(previous["artifact_id"]), "Missing committed artifact receipt")
            return previous["artifact_id"]
        opened = await self.call("begin_skill", skill_id=name, arguments=arguments)
        changed = False
        try:
            previous = None
            for step, bindings in steps or [("compute", arguments)]:
                if scripted and (step == "compute" or (name == "join-orders" and step == "convert")):
                    previous = await self.call("run_skill_script", run_id=opened["run_id"])
                else:
                    previous = await self.call("execute_step", run_id=opened["run_id"], step_id=step,
                                               arguments=bindings(previous) if callable(bindings) else bindings)
                changed |= bool(previous.get("notices")) or previous.get("current") is False
                if changed:
                    # A first procedural step may have triggered a source update.
                    # Abandon its unfinished run; retain all committed effects.
                    raise _PublicSourceChanged()
            result = await self.call("finish_skill", run_id=opened["run_id"])
        except _PublicSourceChanged:
            await self.call("abandon_skill", run_id=opened["run_id"])
            raise
        if result.get("notices") or result.get("current") is False:
            raise _PublicSourceChanged()
        self.require(result.get("current") is True and result.get("closed") is True
                     and isinstance(result.get("artifact_id"), str), "Missing committed artifact receipt")
        return result["artifact_id"]

    async def declaration_reusable(self, name, arguments, steps, item):
        if (item.get("current") is not True or item.get("intermediate")
                or item.get("producer_skill") != name):
            return False
        procedures = {
            "join-orders": ("joined", "authorized", "authorized_id", "authorize", "convert",
                            ("orders_id", "consent_id", "rates_id")),
            "audit-region": ("audit", "reconciliation", "reconciliation_id", "reconcile_rows", "check_summary",
                             ("orders_id", "joined_id", "summary_id")),
            "review-release": ("review", "coverage", "coverage_id", "check_coverage", "check_totals",
                               ("rollup_ids", "audit_ids")),
        }
        if name not in procedures:
            return item.get("arguments") == arguments and (steps is None or steps == [("compute", arguments)])
        kind, mid_kind, mid_key, first_step, last_step, business_keys = procedures[name]
        actual = item.get("arguments", {})
        if (item.get("kind") != kind or set(arguments) != set(business_keys)
                or any(actual.get(key) != arguments[key] for key in business_keys)
                or not steps or [step for step, _ in steps] != [first_step, last_step]):
            return False
        mid_id = actual.get(mid_key)
        if not isinstance(mid_id, str) or not mid_id:
            return False
        if mid_id not in self.artifact_records:
            self.artifact_records[mid_id] = (await self.call(
                "read_object", collection="artifacts", object_id=mid_id))["object"]
        mid = self.artifact_records[mid_id]
        first = steps[0][1]
        first = first(None) if callable(first) else first
        last = steps[1][1]
        last = last({"artifact_id": mid_id}) if callable(last) else last
        # Compare complete step dictionaries, never an arbitrary argument subset.
        # These are this fixed policy's parameters, not all legal equivalent plans.
        if (mid.get("current") is not True or mid.get("intermediate") is not True
                or mid.get("producer_skill") != name or mid.get("kind") != mid_kind
                or mid.get("arguments") != first or last.get(mid_key) != mid_id
                or actual != {**first, **last}
                or any(actual.get(key) != arguments[key] for key in business_keys)
                or any(item.get(key) != mid.get(key) for key in ("region", "period"))):
            return False
        # Check both recorded edges: matching flattened business inputs alone
        # cannot establish that the final table used this actual intermediate.
        if name == "join-orders":
            before = [arguments["orders_id"], arguments["consent_id"]]
            after = [mid_id, arguments["rates_id"]]
            legal = first.get("join_keys") in (["customer_id", "region"], ["region", "customer_id"])
            legal = legal and last.get("rounding") == "half_up"
            legal = legal and set(first) == {"orders_id", "consent_id", "join_keys"}
            legal = legal and set(last) == {"authorized_id", "rates_id", "rounding"}
        elif name == "audit-region":
            before = [arguments["orders_id"], arguments["joined_id"]]
            after = [mid_id, arguments["joined_id"], arguments["orders_id"], arguments["summary_id"]]
            legal = first.get("key_columns") == ["order_id"]
            legal = legal and last.get("group_by") in (["region", "period"], ["period", "region"])
            legal = legal and set(first) == {"orders_id", "joined_id", "key_columns"}
            legal = legal and set(last) == {"reconciliation_id", "summary_id", "group_by"}
        else:
            before = arguments["rollup_ids"] + arguments["audit_ids"]
            after = [mid_id, *before]
            required = first.get("required_periods", [])
            legal = len(required) == len(self.periods) and set(required) == set(self.periods)
            legal = legal and last.get("method") in ("source_totals", "running_sum")
            legal = legal and set(first) == {"rollup_ids", "audit_ids", "required_periods"}
            legal = legal and set(last) == {"coverage_id", "method"}
        refs = lambda ids: [{"type": "artifact", "id": value} for value in dict.fromkeys(ids)]
        return legal and mid.get("inputs") == refs(before) and item.get("inputs") == refs(after)

    async def ensure(self, name, arguments, steps=None):
        if self.skill_protocol == "skill_declaration_v1":
            # A new attempt refreshes these records through public observations.
            for identifier, item in list(self.artifact_records.items()):
                if await self.declaration_reusable(name, arguments, steps, item):
                    return identifier
            identifier = await self.skill(name, arguments, steps)
            self.artifact_records[identifier] = (await self.call(
                "read_object", collection="artifacts", object_id=identifier))["object"]
            return identifier
        from json import dumps
        key = dumps([name, arguments], sort_keys=True)
        # Only current public bootstrap records or successful finish receipts enter
        # this attempt's cache. Consumers still reject stale source dependencies.
        if self.artifacts.get(key):
            return self.artifacts[key][-1]
        identifier = await self.skill(name, arguments, steps)
        self.artifacts.setdefault(key, []).append(identifier)
        return identifier

    @staticmethod
    def period_number(period):
        if not isinstance(period, str) or not period.startswith("P") or not period[1:].isdigit():
            raise PublicWitnessFailure("MISSING_PUBLIC_INFORMATION", "Unsupported public invoice period")
        return int(period[1:])

    async def attempt(self):
        from json import dumps
        self.artifacts.clear()
        self.artifact_records.clear()
        project = await self.call("read_project")
        self.require(project["business_program"] == "credit_sales_v2" and bool(project["goal"]),
                     "Expected a public credit_sales_v2 goal")
        regions, periods, funding = project["regions"], project["periods"], project["funding"]
        self.periods = periods
        self.require(regions and periods and len(set(regions)) == len(regions)
                     and len(set(periods)) == len(periods), "Missing or duplicate public scope")
        self.require([self.period_number(p) for p in periods] == list(range(1, len(periods) + 1)),
                     "Public periods must form a consecutive business horizon")
        sources = await self.objects("sources")
        packages = {}
        for package in (await self.objects("work_packages")).values():
            key = package["region"], package["period"]
            self.require(key not in packages, "Ambiguous public work package")
            packages[key] = package
        self.require(set(packages) == {(r, p) for r in regions for p in periods},
                     "Incomplete public work packages")
        # Source metadata binds installed scripts to current inputs. Only tables
        # whose rows determine a policy decision are read into public history.
        self.artifact_records = await self.objects("artifacts")
        for identifier, item in self.artifact_records.items():
            if not item.get("intermediate") and item["current"] is True:
                key = dumps([item["producer_skill"], item["arguments"]], sort_keys=True)
                self.artifacts.setdefault(key, []).append(identifier)

        def source(identifier, kind):
            self.require(identifier in sources and sources[identifier]["kind"] == kind,
                         f"Missing public {kind} source")
            return identifier

        treasury = source(funding["treasury_source"], "treasury")
        limits = source(funding["credit_limits_source"], "credit_limits")
        audits, rollups, books = [], [], []
        settlement_rows = {}
        previous_settlement = previous_rollup = ""
        for period in periods:
            joined, summaries = [], []
            for region in regions:
                package = packages[region, period]
                consent = await self.ensure("prepare-consent", {
                    "source_id": source(package["consent_source"], "consent")})
                rates = await self.ensure("prepare-rates", {
                    "source_id": source(package["rates_source"], "rates")})
                identifier = package["orders_source"]
                item = sources[identifier]
                if item["kind"] == "external_orders":
                    schema = item["business_schema"]
                    orders = await self.ensure("harmonize-orders", {"source_id": identifier,
                        **{key: schema[key] for key in ("column_map", "amount_unit", "key_map_source_id", "conflict_policy")}})
                else:
                    orders = await self.ensure("normalize-orders", {
                        "source_id": source(identifier, "orders"), "deduplication": "first"})
                selected = {"orders_id": orders, "consent_id": consent, "rates_id": rates}
                joined_id = await self.ensure("join-orders", selected, [
                    ("authorize", {"orders_id": orders, "consent_id": consent, "join_keys": ["customer_id", "region"]}),
                    ("convert", lambda mid: {"authorized_id": mid["artifact_id"], "rates_id": rates, "rounding": "half_up"})])
                joined.append(joined_id)
                summary = await self.ensure("summarize-region", {"joined_id": joined_id})
                summaries.append(summary)
                audits.append(await self.ensure("audit-region", {
                    "orders_id": orders, "joined_id": joined_id, "summary_id": summary}, [
                    ("reconcile_rows", {"orders_id": orders, "joined_id": joined_id, "key_columns": ["order_id"]}),
                    ("check_summary", lambda mid: {"reconciliation_id": mid["artifact_id"], "summary_id": summary,
                                                    "group_by": ["region", "period"]})]))
            cached_books = set(books).intersection(settlement_rows)
            if cached_books:
                current = {item["id"] for item in await self.pages("search_objects", "items",
                    collection="artifacts", text="", filters=[
                        {"field": "producer_skill", "value": "settle-period"},
                        {"field": "status", "value": "current"}])}
                if not cached_books.issubset(current):
                    raise _PublicSourceChanged()
            ledgers = {}
            for book in books:
                if book not in settlement_rows:
                    settlement_rows[book] = await self.rows(book)
                ledgers[book] = settlement_rows[book]
            collections = []
            for route, operation in (("normal", "collect-invoices"), ("disputed", "reconcile-disputed-invoices")):
                selected_books, invoices, proof = [], [], []
                for book, rows in ledgers.items():
                    due = [row for row in rows if row["record_type"] == "invoice"
                           and row["due_period"] == period and row["payment_route"] == route]
                    if not due:
                        continue
                    selected_books.append(book)
                    invoices.extend(row["invoice_id"] for row in due)
                    if route == "disputed":
                        item = (await self.call("read_object", collection="artifacts", object_id=book))["object"]
                        for identifier in item["arguments"]["fulfillment_ids"]:
                            evidence = (await self.call("read_object", collection="artifacts", object_id=identifier))["object"]
                            if evidence["region"] in {row["region"] for row in due}:
                                proof.append(identifier)
                if invoices:
                    selected = {"settlement_ids": selected_books, "invoice_ids": invoices, "period": period}
                    if route == "disputed":
                        selected["fulfillment_ids"] = list(dict.fromkeys(proof))
                    collections.append(await self.ensure(operation, selected))
            outstanding = [book for book, rows in ledgers.items() if any(row["record_type"] == "invoice"
                and self.period_number(row["due_period"]) > self.period_number(period) for row in rows)]
            assessment = await self.ensure("assess-credit", {"joined_ids": joined,
                "previous_settlement_id": previous_settlement, "settlement_ids": outstanding, "limits_source_id": limits})
            allocation = await self.ensure("allocate-working-capital", {"summary_ids": summaries,
                "previous_settlement_id": previous_settlement, "treasury_source_id": treasury,
                "collection_ids": collections, "credit_assessment_id": assessment})
            fulfilled = [await self.ensure("fulfill-orders", {"joined_id": item, "allocation_id": allocation}) for item in joined]
            previous_settlement = await self.ensure("settle-period", {"fulfillment_ids": fulfilled,
                "allocation_id": allocation, "terms_source_id": source(funding["terms_sources"][period], "invoice_terms")})
            books.append(previous_settlement)
            previous_rollup = await self.ensure("close-funded-period", {"settlement_id": previous_settlement,
                                                                       "previous_rollup_id": previous_rollup})
            rollups.append(previous_rollup)
        selected = {"rollup_ids": rollups, "audit_ids": audits}
        review = await self.skill("review-release", selected, [
            ("check_coverage", {**selected, "required_periods": periods}),
            ("check_totals", lambda mid: {"coverage_id": mid["artifact_id"], "method": "source_totals"})])
        publication = await self.skill("publish-release", {"rollup_ids": rollups, "review_id": review})
        return {"publication_id": publication, "public_progress": await self.call("read_project")}

    async def run(self):
        try:
            for self.rounds in range(1, self.max_rounds + 1):
                try:
                    return {"status": "policy_complete", **(await self.attempt()),
                            "public_calls": self.call_count, "rounds": self.rounds}
                except _PublicSourceChanged:
                    continue
            raise PublicWitnessFailure("ROUND_LIMIT", "Public source reconciliation bound exhausted")
        except (KeyError, TypeError, IndexError) as exc:
            raise PublicWitnessFailure("MISSING_PUBLIC_INFORMATION", "Required public response field unavailable") from exc
