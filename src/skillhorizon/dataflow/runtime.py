"""Stateful table execution. The runtime never reads a reference plan or evaluator file."""

from copy import deepcopy
import csv
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from ..errors import DomainError
from ..util import append_jsonl, load_json, load_jsonl, write_json
from ..experiment.arguments import business_arguments_equal
from ..public_contract import Offset, PageLimit, completion, page, public_progress, register_tools, validate_fields
from .skills import SKILLS, ALL_SKILLS, SCRIPT, PROCEDURES, skill_specs
from .table_ops import compute
from .funding import FIELDS as FUNDING_FIELDS, INTEGER_FIELDS as FUNDING_INTEGERS, execute_funding
from .funding_skills import SKILLS as FUNDING_SKILLS
from .obligation_skills import FIELDS as OBLIGATION_FIELDS
from .observations import PublicObservations, artifact_receipt


FIELDS = {
    "orders": ["order_id", "customer_id", "region", "period", "currency", "amount_cents"],
    "consent": ["customer_id", "region"],
    "rates": ["currency", "numerator", "denominator"],
    "joined": ["order_id", "customer_id", "region", "period", "usd_cents"],
    "summary": ["period", "region", "order_count", "usd_cents"],
    "rollup": ["period", "region", "order_count", "usd_cents", "cumulative_order_count", "cumulative_usd_cents"],
    "audit": ["region", "period", "passed"],
    "review": ["passed"],
}
FIELDS["publication"] = FIELDS["rollup"]
FIELDS["authorized"] = FIELDS["orders"]
FIELDS["reconciliation"] = FIELDS["joined"]
FIELDS["coverage"] = FIELDS["rollup"]
FIELDS.update(FUNDING_FIELDS)
FIELDS.update(OBLIGATION_FIELDS)
INTEGER_FIELDS = {"amount_cents", "numerator", "denominator", "usd_cents", "order_count",
                  "cumulative_order_count", "cumulative_usd_cents", "passed"}
INTEGER_FIELDS.update(FUNDING_INTEGERS)
INTEGER_FIELDS.add("delay_periods")
INTEGER_FIELDS.update({"credit_limit_cents", "outstanding_cents", "credit_quota_cents"})
FIELDS["credit_assessment"] = ["period", "region", "customer_id", "credit_limit_cents", "outstanding_cents", "credit_quota_cents"]

PROCEDURE_FIELDS = {
    "authorize": {"orders_id": str, "consent_id": str, "join_keys": list[str]},
    "convert": {"authorized_id": str, "rates_id": str, "rounding": str},
    "reconcile_rows": {"orders_id": str, "joined_id": str, "key_columns": list[str]},
    "check_summary": {"reconciliation_id": str, "summary_id": str, "group_by": list[str]},
    "check_coverage": {"rollup_ids": list[str], "audit_ids": list[str], "required_periods": list[str]},
    "check_totals": {"coverage_id": str, "method": str},
}


def write_table(path, rows, fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_table_file(path, *, numeric=True):
    with Path(path).open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if numeric:
        return [{k: int(v) if k in INTEGER_FIELDS else v for k, v in r.items()} for r in rows]
    return rows


def ordered(rows):
    return sorted(rows, key=lambda r: json.dumps(r, sort_keys=True))


class DataflowRuntime(PublicObservations):
    skills = SKILLS

    def __init__(self, root):
        self.root = Path(root)
        self.reload()

    def reload(self):
        self.project = load_json(self.root / "project.json")
        self.skills = ({**type(self).skills, **skill_specs(self.project["business_program"])} if self.project.get("business_program")
                       else type(self).skills)

    def save(self):
        write_json(self.root / "project.json", self.project)

    def _require(self, condition, code, message):
        if not condition:
            raise DomainError(code, message)

    def _binding_error(self, code, message, identifier, category, kind, parameter):
        other = "artifact" if category == "source" else "source"
        observed_category = next((c for c in (category, other) if identifier in self.project[c + "s"]), "missing")
        observed = {"category": observed_category}
        if observed_category != "missing":
            observed["kind"] = self.project[observed_category + "s"][identifier]["kind"]
        details = {"supplied_id": identifier, "expected": {"category": category, "kind": kind}, "observed": observed}
        if parameter is not None:
            details["parameter"] = parameter
        raise DomainError(code, message, details)

    def _source(self, source_id, kind=None, *, parameter=None):
        source = self.project["sources"].get(source_id)
        if source is None:
            self._binding_error("UNKNOWN_SOURCE", "Unknown public source", source_id, "source", kind, parameter)
        if kind is not None and source["kind"] != kind:
            self._binding_error("SOURCE_KIND", "Source has the wrong business type", source_id, "source", kind, parameter)
        return source

    def current(self, artifact_id, visiting=None):
        # Shared financing ancestors must be checked once per query, not once per DAG path.
        memo = {}
        def walk(identifier, path):
            if identifier in path:
                return False
            if identifier in memo:
                return memo[identifier]
            artifact = self.project["artifacts"].get(identifier)
            if artifact is None or not (self.root / artifact["path"]).is_file():
                memo[identifier] = False
                return False
            active = path | {identifier}
            valid = all((p["id"] in self.project["sources"] and
                         self.project["sources"][p["id"]]["revision"] == p["revision"])
                        if p["type"] == "source" else walk(p["id"], active)
                        for p in artifact["inputs"])
            memo[identifier] = valid
            return valid
        return walk(artifact_id, set() if visiting is None else set(visiting))

    def _artifact(self, artifact_id, kind=None, *, parameter=None):
        artifact = self.project["artifacts"].get(artifact_id)
        if artifact is None:
            self._binding_error("UNKNOWN_ARTIFACT", "Unknown project artifact", artifact_id, "artifact", kind, parameter)
        if kind is not None and artifact["kind"] != kind:
            self._binding_error("ARTIFACT_KIND", "Artifact has the wrong business type", artifact_id, "artifact", kind, parameter)
        if not self.current(artifact_id):
            self._binding_error("STALE_ARTIFACT", "Artifact depends on a changed source; inspect current inputs",
                                artifact_id, "artifact", kind, parameter)
        return artifact

    def _rows(self, entity):
        return read_table_file(self.root / entity["path"])

    def _source_ref(self, source_id):
        return {"type": "source", "id": source_id, "revision": self._source(source_id)["revision"]}

    def _artifact_refs(self, ids):
        return [{"type": "artifact", "id": value} for value in ids]

    def _validate_arguments(self, skill_id, arguments):
        self._require(skill_id in self.skills, "UNKNOWN_SKILL", "Unknown reusable skill")
        fields = self.skills[skill_id]["inputs"]
        self._require(isinstance(arguments, dict) and set(arguments) == set(fields),
                      "ARGUMENTS", "Supply exactly the skill's declared inputs")
        for key, kind in fields.items():
            value = arguments[key]
            good = (isinstance(value, str) if kind is str else
                    isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()) if kind is dict else
                    isinstance(value, list) and all(isinstance(v, str) for v in value))
            self._require(good, "ARGUMENTS", f"Wrong value type for {key}")
            if isinstance(value, list):
                self._require(len(value) == len(set(value)), "DUPLICATE_INPUT", "Use each input once")

    def _observe_output(self, output):
        result = deepcopy(output)
        if isinstance(result, dict) and "artifact_id" in result:
            result["current"] = self.current(result["artifact_id"])
        return result

    def public_progress(self):
        identifier = self.project["publication"]
        state = "missing" if identifier is None else "current" if self.current(identifier) else "stale"
        return public_progress(self.project.get("active", {}), identifier, state, {})

    def _public_notices(self, notices):
        from .support import NOTICE_POLICIES
        policy = self.project["notice_policy"]
        if policy not in NOTICE_POLICIES:
            raise ValueError("Unknown source-change notice policy")
        return [{k: deepcopy(v) for k, v in notice.items()
                 if policy == "managed" or k != "invalidated_artifacts"} for notice in notices]

    def read_table(self, table_id, offset, limit):
        validate_fields({"offset": offset, "limit": limit}, {"offset": Offset, "limit": PageLimit})
        entity = self.project["sources"].get(table_id) or self.project["artifacts"].get(table_id)
        self._require(entity is not None, "UNKNOWN_TABLE", "Unknown public table ID")
        rows = self._rows(entity)
        result = page(rows, offset, limit)
        return {"table_id": table_id, "rows": result["items"], "row_count": result["total"], "offset": offset,
                "next_offset": result["next_offset"],
                "current": table_id in self.project["sources"] or self.current(table_id)}

    def read_skill_file(self, skill_id, path):
        self._require(skill_id in self.skills, "UNKNOWN_SKILL", "Unknown reusable skill")
        self._require(path in {"SKILL.md", "scripts/execute.py", "scripts/table_ops.py"},
                      "FILE_NOT_EXPOSED", "Only installed skill instructions and code are public")
        file = self.root / "skills" / skill_id / path
        self._require(file.is_file() and not file.is_symlink(), "FILE_NOT_FOUND", "This skill file is unavailable")
        return {"skill_id": skill_id, "path": path, "content": file.read_text()}

    def expected_region(self, region, period):
        # An independent, row-wise oracle checks the SQL-backed operations against public inputs.
        source = self._source(f"orders-{region}-{period}")
        consent = self._rows(self._source(f"consent-{region}"))
        permitted = {(r["customer_id"], r["region"]) for r in consent if r["analytics_allowed"] == "yes"}
        package = next(w for w in self.project["work_packages"] if w["region"] == region and w["period"] == period)
        rates = {r["currency"]: (int(r["numerator"]), int(r["denominator"]))
                 for r in self._rows(self._source(package["rates_source"]))}
        normalized, authorized = {}, []
        raw_rows = self._rows(source)
        if source["kind"] == "external_orders":
            schema = source["business_schema"]
            identities = {row["external_customer"]: row["customer_id"]
                          for row in self._rows(self._source(schema["key_map_source_id"])) if row["region"] == region}
            latest = {}
            for raw in raw_rows:
                row = {key: str(raw[column]).strip() for key, column in schema["column_map"].items()}
                row["customer_id"] = identities[row["customer_id"]]
                version = int(row.pop("record_revision"))
                if schema["amount_unit"] == "minor":
                    row["amount"] = str(Decimal(row["amount"]) / 100)
                previous = latest.get(row["order_id"])
                if previous and version == previous[0] and row != previous[1]:
                    raise ValueError("Source has a same-version conflict")
                if previous is None or version > previous[0]:
                    latest[row["order_id"]] = (version, row)
            raw_rows = [item[1] for item in latest.values()]
        for raw in raw_rows:
            row = {k: raw[k].strip() for k in ("order_id", "customer_id", "region", "period", "currency")}
            value = Decimal(raw["amount"]) * 100
            if not value.is_finite() or value < 0 or value != value.to_integral_value():
                raise ValueError("Invalid source amount")
            row["amount_cents"] = int(value)
            if row["order_id"] in normalized and normalized[row["order_id"]] != row:
                raise ValueError("Conflicting source orders")
            normalized[row["order_id"]] = row
        for row in normalized.values():
            if (row["customer_id"], row["region"]) in permitted:
                numerator, denominator = rates[row["currency"]]
                usd = int((Decimal(row["amount_cents"]) * numerator / denominator).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                authorized.append({k: row[k] for k in ("order_id", "customer_id", "region", "period")} | {"usd_cents": usd})
        summary = {"region": region, "period": period, "order_count": len(authorized),
                   "usd_cents": sum(r["usd_cents"] for r in authorized)}
        return ordered(list(normalized.values())), ordered(authorized), summary

    def expected_release(self):
        if self.project.get("business_program") in {"funded_sales_v1", "obligation_sales_v1", "credit_sales_v2"}:
            from .verification import release_from_public_sources
            return ordered(release_from_public_sources(self.root, project=self.project))
        total = {region: [0, 0] for region in self.project["regions"]}
        rows = []
        for period in self.project["periods"]:
            for region in self.project["regions"]:
                _, _, summary = self.expected_region(region, period)
                total[region][0] += summary["order_count"]
                total[region][1] += summary["usd_cents"]
                rows.append({**summary, "cumulative_order_count": total[region][0], "cumulative_usd_cents": total[region][1]})
        return ordered(rows)

    def _same_scope(self, *artifacts):
        self._require(len({(a["region"], a["period"]) for a in artifacts}) == 1,
                      "WORK_PACKAGE_MISMATCH", "Inputs must describe the same regional period")

    def _rollup_set(self, ids):
        artifacts = [self._artifact(value, "rollup", parameter=f"rollup_ids[{i}]") for i, value in enumerate(ids)]
        rows = ordered([r for a in artifacts for r in self._rows(a)])
        required = {(r, p) for r in self.project["regions"] for p in self.project["periods"]}
        self._require(len(rows) == len(required) and {(r["region"], r["period"]) for r in rows} == required,
                      "PERIOD_COVERAGE", "Supply complete nonduplicated regional-period rows, as separate rollups or one batch")
        self._require(rows == self.expected_release(), "CONTENT_MISMATCH", "Release values do not match current public business inputs")
        return rows

    def _audit_valid(self, audit):
        self._artifact(audit["artifact_id"], "audit")
        region, period = audit["region"], audit["period"]
        order = self._artifact(audit["arguments"]["orders_id"], "orders")
        joined = self._artifact(audit["arguments"]["joined_id"], "joined")
        summary = self._artifact(audit["arguments"]["summary_id"], "summary")
        self._same_scope(audit, order, joined, summary)
        expected_orders, expected_joined, expected_summary = self.expected_region(region, period)
        return (self._rows(audit) == [{"region": region, "period": period, "passed": 1}]
                and ordered(self._rows(order)) == expected_orders and ordered(self._rows(joined)) == expected_joined
                and self._rows(summary) == [expected_summary])

    def _review_inputs(self, arguments):
        rows = self._rollup_set(arguments["rollup_ids"])
        audits = [self._artifact(value, "audit", parameter=f"audit_ids[{i}]") for i, value in enumerate(arguments["audit_ids"])]
        required = {(r, p) for r in self.project["regions"] for p in self.project["periods"]}
        self._require(len(audits) == len(required) and {(v["region"], v["period"]) for v in audits} == required
                      and all(self._audit_valid(v) for v in audits),
                      "AUDIT_COVERAGE", "Supply a successful current content audit for each regional period")
        if self.project.get("business_program") == "funded_sales_v1":
            from .funding import validate_financing
            validate_financing(self, arguments["rollup_ids"])
        elif self.project.get("business_program") in {"obligation_sales_v1", "credit_sales_v2"}:
            from .obligation_verification import validate_ancestors
            validate_ancestors(self, arguments["rollup_ids"])
        return rows

    def _compute(self, operation, tables, options, use_script, script_skill=None):
        if not use_script:
            return compute(operation, tables, options)
        script_skill = script_skill or operation
        script = self.root / "skills" / script_skill / "scripts/execute.py"
        support = script.with_name("table_ops.py")
        self._require(ALL_SKILLS[script_skill]["script"] and script.is_file() and support.is_file(),
                      "NO_SCRIPT", "This skill has no optional script")
        self._require(not script.is_symlink() and not support.is_symlink() and script.read_text() == SCRIPT
                      and support.read_bytes() == Path(__file__).with_name("table_ops.py").read_bytes(),
                      "SCRIPT_CHANGED", "Only the installed fixed computation is executable")
        with tempfile.TemporaryDirectory(prefix="skillhorizon-table-") as tmp:
            from skillhorizon.experiment.construction_envelope import temporary_environment
            result = subprocess.run([sys.executable, "-I", str(script.resolve())],
                input=json.dumps({"operation": operation, "tables": tables, "options": options}),
                text=True, capture_output=True, cwd=tmp, env=temporary_environment(), timeout=30, check=False)
        if result.returncode:
            raise DomainError("TABLE_OPERATION", "The table script rejected its inputs")
        return json.loads(result.stdout)

    def execute(self, name, arguments, use_script=False):
        self._validate_arguments(name, arguments)
        self._require(not use_script or self.skills[name]["script"], "NO_SCRIPT", "This skill is procedure-only")
        if name == "assess-credit":
            from .credit import assess
            return assess(self, arguments)
        if self.project.get("business_program") in {"obligation_sales_v1", "credit_sales_v2"} and name in {
                "allocate-working-capital", "settle-period", "close-funded-period", "collect-invoices", "reconcile-disputed-invoices"}:
            from .obligations import execute_obligations
            return execute_obligations(self, name, arguments, use_script)
        if name in FUNDING_SKILLS:
            return execute_funding(self, name, arguments, use_script)
        if self.project.get("business_program") in {"funded_sales_v1", "obligation_sales_v1", "credit_sales_v2"}:
            self._require(name not in {"rollup-period", "rollup-batch"}, "DEMAND_NOT_REALIZED_SALES",
                          "Demand summaries cannot be published as realized funded sales")
        a = arguments
        region = period = None
        inputs = []
        if name == "harmonize-orders":
            source = self._source(a["source_id"], "external_orders", parameter="source_id")
            key_map = self._source(a["key_map_source_id"], "identity_map", parameter="key_map_source_id")
            self._require(key_map["region"] == source["region"], "WORK_PACKAGE_MISMATCH", "Identity mapping belongs to another region")
            region, period, kind = source["region"], source["period"], "orders"
            rows = self._compute(name, [self._rows(source), self._rows(key_map)], a, use_script)
            inputs = [self._source_ref(a["source_id"]), self._source_ref(a["key_map_source_id"])]
        elif name in {"normalize-orders", "prepare-consent", "prepare-rates"}:
            kind = {"normalize-orders": "orders", "prepare-consent": "consent", "prepare-rates": "rates"}[name]
            source = self._source(a["source_id"], kind, parameter="source_id")
            region, period = source.get("region"), source.get("period")
            inputs = [self._source_ref(a["source_id"])]
            rows = self._compute(name, [self._rows(source)], a, use_script)
        elif name == "join-orders":
            orders = self._artifact(a["orders_id"], "orders", parameter="orders_id")
            consent = self._artifact(a["consent_id"], "consent", parameter="consent_id")
            rates = self._artifact(a["rates_id"], "rates", parameter="rates_id")
            self._require(orders["region"] == consent["region"], "WORK_PACKAGE_MISMATCH", "Consent belongs to another region")
            self._require(rates["period"] in (None, orders["period"]), "WORK_PACKAGE_MISMATCH", "Rates do not apply to this period")
            region, period, kind = orders["region"], orders["period"], "joined"
            inputs = self._artifact_refs([a["orders_id"], a["consent_id"], a["rates_id"]])
            rows = self._compute(name, [self._rows(v) for v in (orders, consent, rates)], a, use_script)
        elif name == "summarize-region":
            joined = self._artifact(a["joined_id"], "joined", parameter="joined_id")
            region, period, kind = joined["region"], joined["period"], "summary"
            inputs = self._artifact_refs([a["joined_id"]])
            rows = self._compute(name, [self._rows(joined)], {"region": region, "period": period}, use_script)
        elif name == "audit-region":
            orders = self._artifact(a["orders_id"], "orders", parameter="orders_id")
            joined = self._artifact(a["joined_id"], "joined", parameter="joined_id")
            summary = self._artifact(a["summary_id"], "summary", parameter="summary_id")
            self._same_scope(orders, joined, summary)
            region, period, kind = orders["region"], orders["period"], "audit"
            wanted_orders, wanted_joined, wanted_summary = self.expected_region(region, period)
            self._require(ordered(self._rows(orders)) == wanted_orders and ordered(self._rows(joined)) == wanted_joined
                          and self._rows(summary) == [wanted_summary], "CONTENT_MISMATCH", "Regional artifact contents fail source reconciliation")
            inputs = self._artifact_refs([a[k] for k in ("orders_id", "joined_id", "summary_id")])
            rows = [{"region": region, "period": period, "passed": 1}]
        elif name == "rollup-period":
            summaries = [self._artifact(value, "summary", parameter=f"summary_ids[{i}]") for i, value in enumerate(a["summary_ids"])]
            self._require(len(summaries) == len(self.project["regions"]) and
                          {v["region"] for v in summaries} == set(self.project["regions"])
                          and len({v["period"] for v in summaries}) == 1,
                          "REGION_COVERAGE", "Merge one summary per region from a single period")
            period, kind = summaries[0]["period"], "rollup"
            position = self.project["periods"].index(period)
            previous = []
            ids = list(a["summary_ids"])
            if position:
                prior = self._artifact(a["previous_rollup_id"], "rollup", parameter="previous_rollup_id")
                self._require(prior["period"] == self.project["periods"][position - 1],
                              "PERIOD_DEPENDENCY", "Consume the immediately preceding cumulative rollup")
                previous = self._rows(prior)
                ids.append(a["previous_rollup_id"])
            else:
                self._require(a["previous_rollup_id"] == "", "PERIOD_DEPENDENCY", "The first period has no previous rollup")
            inputs = self._artifact_refs(ids)
            rows = self._compute(name, [[r for s in summaries for r in self._rows(s)], previous], a, use_script)
        elif name == "review-release":
            self._review_inputs(a)
            kind, rows = "review", [{"passed": 1}]
            inputs = self._artifact_refs(a["rollup_ids"] + a["audit_ids"])
        elif name == "rollup-batch":
            summaries = [self._artifact(value, "summary", parameter=f"summary_ids[{i}]") for i, value in enumerate(a["summary_ids"])]
            required = {(r, p) for r in self.project["regions"] for p in self.project["periods"]}
            self._require(len(summaries) == len(required) and
                          {(v["region"], v["period"]) for v in summaries} == required,
                          "REGION_COVERAGE", "Supply one summary for every regional period")
            inputs = self._artifact_refs(a["summary_ids"])
            kind = "rollup"
            rows = self._compute(name, [[r for s in summaries for r in self._rows(s)]], a, use_script)
        elif name == "publish-release":
            rows = self._rollup_set(a["rollup_ids"])
            review = self._artifact(a["review_id"], "review", parameter="review_id")
            self._require(self._rows(review) == [{"passed": 1}], "INVALID_REVIEW", "A successful content review is required")
            self._review_inputs(review["arguments"])
            kind = "publication"
            inputs = self._artifact_refs(a["rollup_ids"] + [a["review_id"]])
        else:
            raise DomainError("UNKNOWN_SKILL", "Unknown reusable skill")
        return self._commit(name, a, kind, rows, inputs, region, period)

    def _commit(self, name, a, kind, rows, inputs, region=None, period=None, *, intermediate=False):
        identifier = f"table-{self.project['next_artifact']:05d}"
        artifact = {"artifact_id": identifier, "kind": kind, "region": region, "period": period,
                    "work_package": f"{region}/{period}" if region and period else
                                    f"authorization/{region}" if region else f"project/{period}" if period else "project",
                    "producer_skill": name, "inputs": inputs, "arguments": deepcopy(a),
                    "intermediate": intermediate,
                    "path": f"tables/{identifier}.csv", "row_count": len(rows)}
        write_table(self.root / artifact["path"], rows, FIELDS[kind])
        self.project["artifacts"][identifier] = artifact
        self.project["next_artifact"] += 1
        self.project["revision"] += 1
        if kind == "publication":
            self.project["publication"] = identifier
        notices = [] if intermediate else self._maybe_event(name, artifact)
        self.save()
        return {"artifact_id": identifier, "kind": kind, "region": region, "period": period,
                "row_count": len(rows), "rows": rows[:20], "rows_truncated": len(rows) > 20,
                "current": self.current(identifier), "notices": notices}

    def _maybe_event(self, name, artifact):
        event = (next((item for item in self.project["scheduled_changes"] if not item["applied"]), None)
                 if "scheduled_changes" in self.project else self.project["scheduled_change"])
        if event is None or event["applied"]:
            return []
        trigger = event["trigger"]
        ready = ((trigger == "authorized_rows_ready" and artifact["kind"] == "joined" and
                  artifact["region"] == event["region"] and artifact["period"] == event["period"]) or
                 (trigger == "period_aggregate_ready" and artifact["kind"] == "rollup" and
                  any(row["period"] == event["period"] for row in self._rows(artifact))) or
                 (trigger == "period_settlement_ready" and artifact["kind"] == "obligation_settlement" and
                  artifact["period"] == event["trigger_period"]) or
                 (trigger == "release_review_ready" and artifact["kind"] == "review"))
        if not ready:
            return []
        source = self._source(event["source_id"])
        rows = self._rows(source)
        before = {key for key in self.project["artifacts"] if self.current(key)}
        if event["kind"] == "consent_revocation":
            for row in rows:
                if row["customer_id"] == event["customer_id"]:
                    row["analytics_allowed"] = "no"
        elif event["kind"] == "order_correction":
            schema = source.get("business_schema")
            key = schema["column_map"]["order_id"] if schema else "order_id"
            amount = schema["column_map"]["amount"] if schema else "amount"
            unit = schema["amount_unit"] if schema else "major"
            for row in rows:
                if row[key].strip() == event["order_id"]:
                    row[amount] = str(int(row[amount]) + 137) if unit == "minor" else str(Decimal(row[amount]) + Decimal("1.37"))
            late = dict(rows[0])
            late[key] = f"{event['region']}-{event['period']}-LATE"
            rows.append(late)
        elif event["kind"] == "rate_change":
            for row in rows:
                if row["currency"] == "EUR":
                    row["numerator"] += 9
        elif event["kind"] == "treasury_correction":
            # A fixed accounting restatement, not a transfer of new period cash.
            rows[0]["cash_cents"] //= 2
        elif event["kind"] == "credit_limit_correction":
            for row in rows:
                if row["customer_id"] == event["customer_id"]:
                    row["credit_limit_cents"] //= 3
        else:
            raise ValueError("Unknown frozen business event")
        source["revision"] += 1
        source["path"] = f"sources/{event['source_id']}-v{source['revision']}.csv"
        source["row_count"] = len(rows)
        write_table(self.root / source["path"], rows, list(rows[0]))
        event["applied"] = True
        notice = {"type": event["kind"], "source_id": event["source_id"],
                  "revision": source["revision"], "effective_region": source.get("region"),
                  "effective_period": source.get("period"),
                  "invalidated_artifacts": sorted(key for key in before if not self.current(key))}
        self.project["notices"].append(notice)
        return self._public_notices([notice])

    def begin_skill(self, skill_id, arguments):
        self._validate_arguments(skill_id, arguments)
        identifier = f"run-{self.project['next_run']}"
        self.project["next_run"] += 1
        self.project["active"][identifier] = {"skill_id": skill_id, "arguments": deepcopy(arguments),
                                             "output": None, "steps": []}
        self.project["revision"] += 1
        self.save()
        return {"run_id": identifier, "skill_id": skill_id}

    def _active(self, run_id):
        self._require(run_id in self.project["active"], "NO_ACTIVE_SKILL", "Unknown active run")
        return self.project["active"][run_id]

    def revise_skill(self, run_id, arguments):
        run = self._active(run_id)
        self._require(not (run["output"] and run["output"]["artifact_id"] == self.project["publication"]),
                      "STEP_COMPLETE", "Close a committed publication run with finish_skill or abandon_skill before starting another run")
        self._validate_arguments(run["skill_id"], arguments)
        retained = [s["artifact_id"] for s in run["steps"]]
        if run["output"] and run["output"]["artifact_id"] not in retained:
            retained.append(run["output"]["artifact_id"])
        run.update(arguments=deepcopy(arguments), steps=[], output=None)
        self.project["revision"] += 1
        self.save()
        return {"run_id": run_id, "skill_id": run["skill_id"], "step_progress_reset": True,
                "retained_artifact_ids": retained, "committed_effects_retained": True}

    def execute_step(self, run_id, step_id, arguments):
        run = self._active(run_id)
        self._require(run["output"] is None, "STEP_COMPLETE", "Computation already committed; finish or abandon the run")
        if run["skill_id"] in PROCEDURES:
            return self._procedure_step(run, step_id, arguments)
        self._require(step_id == "compute", "STEP", "This simple skill uses step compute")
        self._require(business_arguments_equal(arguments, run["arguments"]), "RUN_INPUTS", "Use the selected inputs, revise_skill, or start a new run")
        result = self.execute(run["skill_id"], arguments)
        run["output"] = result
        self.save()
        return result

    def _procedure_step(self, run, step, args, use_script=False):
        name, selected = run["skill_id"], run["arguments"]
        position = len(run["steps"])
        self._require(step == PROCEDURES[name][position][0], "STEP_ORDER", "Read this skill's procedure and resume its unfinished step")
        args = validate_fields(args, PROCEDURE_FIELDS[step])
        if position:
            input_key = {"convert": "authorized_id", "check_summary": "reconciliation_id", "check_totals": "coverage_id"}[step]
            self._require(args[input_key] == run["steps"][-1]["artifact_id"],
                          "RUN_INPUTS", "Consume this run's actual intermediate artifact")
        result = self._business_step(name, step, args, selected=selected, use_script=use_script)
        run["steps"].append({"step_id": step, "arguments": deepcopy(args), "artifact_id": result["artifact_id"]})
        if position:
            run["output"] = result
        self.save()
        return result

    def _step_intermediate(self, identifier, name, kind, parameter):
        previous = self._artifact(identifier, kind, parameter=parameter)
        self._require(previous["producer_skill"] == name and previous["intermediate"],
                      "INTERMEDIATE_PROVENANCE", "Consume the skill's actual intermediate artifact")
        step = PROCEDURES[name][0][0]
        binding = validate_fields(previous["arguments"], PROCEDURE_FIELDS[step])
        keys = {"authorized": ("orders_id", "consent_id"),
                "reconciliation": ("orders_id", "joined_id"),
                "coverage": ("rollup_ids", "audit_ids")}[kind]
        ids = [identifier for key in keys for identifier in
               (binding[key] if isinstance(binding[key], list) else [binding[key]])]
        self._require(previous["inputs"] == self._artifact_refs(list(dict.fromkeys(ids))),
                      "INTERMEDIATE_PROVENANCE", "Intermediate sources do not match its recorded inputs")
        if kind == "authorized":
            orders = self._artifact(binding["orders_id"], "orders")
            consent = self._artifact(binding["consent_id"], "consent")
            self._same_scope(previous, orders)
            self._require(consent["region"] == orders["region"], "WORK_PACKAGE_MISMATCH", "Consent belongs to another region")
        elif kind == "reconciliation":
            orders = self._artifact(binding["orders_id"], "orders")
            joined = self._artifact(binding["joined_id"], "joined")
            self._same_scope(previous, orders, joined)
            self._require(ordered(self._rows(previous)) == ordered(self._rows(joined)),
                          "CONTENT_MISMATCH", "Reconciliation differs from its recorded joined rows")
        else:
            self._require(previous["region"] is None and previous["period"] is None,
                          "WORK_PACKAGE_MISMATCH", "Coverage must describe the project")
            self._require(len(binding["required_periods"]) == len(self.project["periods"]) and
                          set(binding["required_periods"]) == set(self.project["periods"]),
                          "PERIOD_COVERAGE", "Use every required period")
        return previous, binding

    def _business_step(self, name, step, args, *, selected=None, use_script=False):
        """One reducer for procedural adapters and run-free business entry points."""
        self._require(name in self.skills, "UNKNOWN_SKILL", "Unknown reusable skill")
        self._require(name in PROCEDURES and step in {item[0] for item in PROCEDURES[name]},
                      "STEP", "Use a documented step for this skill")
        self._require(not use_script or (name == "join-orders" and step == "convert"),
                      "NO_SCRIPT", "Only the conversion step has an optional procedure script")
        args = validate_fields(args, PROCEDURE_FIELDS[step])
        for key in args.keys() & (selected or {}).keys():
            self._require(business_arguments_equal({key: args[key]}, {key: selected[key]}),
                          "RUN_INPUTS", "Step input differs from this run's selected input")
        intermediate = step == PROCEDURES[name][0][0]
        bound = {}
        region = period = None
        if not intermediate:
            kind, input_key = {"convert": ("authorized", "authorized_id"),
                               "check_summary": ("reconciliation", "reconciliation_id"),
                               "check_totals": ("coverage", "coverage_id")}[step]
            previous_id = args[input_key]
            previous, bound = self._step_intermediate(previous_id, name, kind, input_key)
            for key in bound.keys() & (selected or {}).keys():
                self._require(business_arguments_equal({key: bound[key]}, {key: selected[key]}),
                              "RUN_INPUTS", "Intermediate differs from this run's selected inputs")
            region, period = previous["region"], previous["period"]
        if step == "authorize":
            orders = self._artifact(args["orders_id"], "orders", parameter="orders_id")
            consent = self._artifact(args["consent_id"], "consent", parameter="consent_id")
            self._require(orders["region"] == consent["region"], "WORK_PACKAGE_MISMATCH", "Consent belongs to another region")
            region, period = orders["region"], orders["period"]
            rows = compute("authorize", [self._rows(orders), self._rows(consent)], args)
            kind, ids = "authorized", [args["orders_id"], args["consent_id"]]
        elif step == "convert":
            rates = self._artifact(args["rates_id"], "rates", parameter="rates_id")
            self._require(rates["period"] in (None, period), "WORK_PACKAGE_MISMATCH", "Rates do not apply to this period")
            rows = self._compute("convert", [self._rows(previous), self._rows(rates)], args, use_script, name)
            kind, ids = "joined", [previous_id, args["rates_id"]]
        elif step == "reconcile_rows":
            orders = self._artifact(args["orders_id"], "orders", parameter="orders_id")
            joined = self._artifact(args["joined_id"], "joined", parameter="joined_id")
            self._same_scope(orders, joined)
            region, period = orders["region"], orders["period"]
            keys = args["key_columns"]
            self._require(keys == ["order_id"], "RECONCILIATION_KEY", "Reconcile unique order IDs, not just totals")
            wanted_orders, wanted_joined, _ = self.expected_region(region, period)
            keyed = lambda rows: {tuple(r[k] for k in keys): r for r in rows}
            self._require(keyed(self._rows(orders)) == keyed(wanted_orders) and
                          len(self._rows(orders)) == len(wanted_orders) and
                          keyed(self._rows(joined)) == keyed(wanted_joined) and len(self._rows(joined)) == len(wanted_joined),
                          "CONTENT_MISMATCH", "Public source reconciliation failed")
            rows, kind, ids = self._rows(joined), "reconciliation", [args["orders_id"], args["joined_id"]]
        elif step == "check_summary":
            summary = self._artifact(args["summary_id"], "summary", parameter="summary_id")
            self._same_scope(previous, summary)
            keys = args["group_by"]
            self._require(len(keys) == 2 and set(keys) == {"region", "period"}, "GROUPING", "Preserve region and period")
            grouped = {}
            for row in self._rows(previous):
                key = tuple(row[k] for k in keys)
                item = grouped.setdefault(key, {**{k: row[k] for k in keys}, "order_count": 0, "usd_cents": 0})
                item["order_count"] += 1
                item["usd_cents"] += row["usd_cents"]
            expected = list(grouped.values()) or [{"region": region, "period": period, "order_count": 0, "usd_cents": 0}]
            self._require(ordered(expected) == ordered(self._rows(summary)), "CONTENT_MISMATCH", "Summary disagrees with reconciled rows")
            rows, kind = [{"region": region, "period": period, "passed": 1}], "audit"
            ids = [previous_id, bound["joined_id"], bound["orders_id"], args["summary_id"]]
        elif step == "check_coverage":
            self._require(len(args["required_periods"]) == len(self.project["periods"]) and
                          set(args["required_periods"]) == set(self.project["periods"]), "PERIOD_COVERAGE", "Use every required period")
            rollups = [self._artifact(v, "rollup", parameter=f"rollup_ids[{i}]") for i, v in enumerate(args["rollup_ids"])]
            audits = [self._artifact(v, "audit", parameter=f"audit_ids[{i}]") for i, v in enumerate(args["audit_ids"])]
            rows = [r for a in rollups for r in self._rows(a) if r["period"] in args["required_periods"]]
            required = {(r, p) for r in self.project["regions"] for p in args["required_periods"]}
            self._require(len(rows) == len(required) and {(r["region"], r["period"]) for r in rows} == required and
                          len(audits) == len(required) and {(a["region"], a["period"]) for a in audits} == required,
                          "AUDIT_COVERAGE", "Coverage is incomplete or duplicated")
            kind, ids = "coverage", args["rollup_ids"] + args["audit_ids"]
        elif step == "check_totals":
            self._require(args["method"] in {"source_totals", "running_sum"}, "RECONCILIATION_METHOD", "Use source_totals or running_sum")
            rows = self._rows(previous)
            if args["method"] == "running_sum":
                base = [{k: r[k] for k in FIELDS["summary"]} for r in rows]
                self._require(ordered(compute("rollup-batch", [base], {})) == ordered(rows), "CONTENT_MISMATCH", "Cumulative values do not reconcile")
            self._require(ordered(rows) == self.expected_release(), "CONTENT_MISMATCH", "Release differs from public sources")
            self._review_inputs(bound)
            rows, kind, ids = [{"passed": 1}], "review", [previous_id, *bound["rollup_ids"], *bound["audit_ids"]]
        # Legacy callers reuse final outputs by their original skill-input dictionary.
        committed_args = selected if selected is not None and not intermediate else {**bound, **args}
        return self._commit(name, committed_args, kind, rows,
                            self._artifact_refs(list(dict.fromkeys(ids))), region, period, intermediate=intermediate)

    def run_skill_script(self, run_id):
        run = self._active(run_id)
        self._require(run["output"] is None, "STEP_COMPLETE", "Computation already committed")
        if run["skill_id"] in PROCEDURES:
            self._require(run["skill_id"] == "join-orders", "NO_SCRIPT", "This skill is procedure-only")
            self._require(len(run["steps"]) == 1, "STEP_ORDER", "Authorize rows before invoking the conversion script")
            return self._procedure_step(run, "convert", {"authorized_id": run["steps"][0]["artifact_id"],
                "rates_id": run["arguments"]["rates_id"], "rounding": "half_up"}, use_script=True)
        result = self.execute(run["skill_id"], run["arguments"], use_script=True)
        run["output"] = result
        self.save()
        return result

    def finish_skill(self, run_id):
        run = self._active(run_id)
        self._require(run["output"] is not None, "SKILL_INCOMPLETE", "Execute the business computation before finishing")
        result = artifact_receipt(self, run["output"])
        del self.project["active"][run_id]
        self.project["revision"] += 1
        self.save()
        return {**result, "run_id": run_id, "closed": True}

    def abandon_skill(self, run_id):
        self._active(run_id)
        del self.project["active"][run_id]
        self.project["revision"] += 1
        self.save()
        return {"run_id": run_id, "abandoned": True, "committed_effects_retained": True}

    def _dispatch(self, name, arguments):
        if name in self.skills:
            return self.execute(name, arguments)
        if name in {"read_project", "read_table", "read_skill_file", "begin_skill", "revise_skill", "execute_step",
                    "run_skill_script", "finish_skill", "abandon_skill"} | {entry[0] for entry in self.query_tools}:
            return getattr(self, name)(**arguments)
        raise DomainError("UNKNOWN_SKILL", "Unknown public tool")

    def call(self, name, arguments):
        self.reload()
        before = self.project["revision"]
        try:
            public = {key: fields for key, _, fields in self.tool_entries("procedural")}
            if name in public:
                arguments = validate_fields(arguments, public[name])
            result = self._dispatch(name, arguments)
            result = {"ok": True, **result}
        except DomainError as exc:
            result = {"ok": False, "error": exc.to_dict()}
        except (TypeError, ValueError, KeyError, ArithmeticError) as exc:
            result = {"ok": False, "error": {"code": "INVALID_INPUT", "message": str(exc)}}
        actions = load_jsonl(self.root / "model_skill_actions.jsonl")
        append_jsonl(self.root / "model_skill_actions.jsonl", {"skill_id": name, "arguments": deepcopy(arguments),
            "result": result, "node_id": f"action-{len(actions) + 1}" if self.project["revision"] > before else None})
        return result

    def score(self):
        self.reload()
        cases = {}
        errors = []
        for region in self.project["regions"]:
            for period in self.project["periods"]:
                audits = [a for a in self.project["artifacts"].values() if a["kind"] == "audit"
                          and a["region"] == region and a["period"] == period and self.current(a["artifact_id"])]
                good = False
                for audit in audits:
                    try:
                        good |= self._audit_valid(audit)
                    except (DomainError, ValueError, KeyError, OSError):
                        continue
                cases[f"{region}/{period}"] = {"success": good}
        publication = self.project.get("publication")
        success = False
        if publication:
            try:
                artifact = self._artifact(publication, "publication")
                self._rollup_set(artifact["arguments"]["rollup_ids"])
                review = self._artifact(artifact["arguments"]["review_id"], "review")
                self._require(self._rows(review) == [{"passed": 1}], "INVALID_REVIEW", "Publication review is invalid")
                self._review_inputs(review["arguments"])
                success = (all(v["success"] for v in cases.values())
                           and ordered(self._rows(artifact)) == self.expected_release())
                if not success:
                    errors.append("Publication content or regional audits fail the business goal")
            except (DomainError, ValueError, KeyError, OSError) as exc:
                errors.append(str(exc))
        return {**completion(success, self.project.get("active", {}), publication),
                "cases": cases, "completed_cases": sum(v["success"] for v in cases.values()),
                "total_cases": len(cases), "state_errors": errors,
                "publication": publication}

    def snapshot(self):
        return {"project": load_json(self.root / "project.json"),
                "tables": {str(p.relative_to(self.root)): p.read_text()
                           for folder in ("sources", "tables") for p in sorted((self.root / folder).glob("*.csv"))}}

    def tool_entries(self, execution_mode):
        return dataflow_tool_entries(self, execution_mode)


def create_dataflow_server(root, execution_mode="macro", *, runtime_class=DataflowRuntime):
    from mcp.server import MCPServer
    runtime = runtime_class(root)
    server = MCPServer("SkillHorizon Dataflow Project")
    register_tools(server, runtime, runtime.tool_entries(execution_mode))
    return server


def dataflow_tool_entries(runtime, execution_mode):
    entries = [
        ("read_project", "Inspect goal, inventory and public progress. handoff_recorded describes records, not verified task success. Open runs and deliverable freshness are separate.", {}),
        ("read_table", "Read a public source_id or artifact_id, not a CSV path. Old artifacts remain readable. Use offset >= 0 and limit 1..200; follow next_offset until null.", {"table_id": str, "offset": Offset, "limit": PageLimit}),
        ("read_skill_file", "Read a reusable skill's instructions at path SKILL.md or optional code listed in its public file inventory. Paths are case-sensitive and relative to the selected skill.", {"skill_id": str, "path": str}),
    ]
    if runtime.query_tools:
        entries[0] = ("read_project", "Inspect goal, collection counts, pending obligations, deliverable freshness and run lifecycle. handoff_recorded is not semantic success. Query collections for details.", {})
    entries += runtime.query_tools
    if execution_mode == "macro":
        entries += [(name, spec["description"], spec["inputs"]) for name, spec in runtime.skills.items()]
    elif execution_mode == "procedural":
        entries += [
            ("begin_skill", "Select a reusable skill and its business inputs.", {"skill_id": str, "arguments": dict}),
            ("revise_skill", "Replace this run's complete selected inputs and reset its steps. Committed tables are retained; no rollback.", {"run_id": str, "arguments": dict}),
            ("execute_step", "Execute a documented business step with selected parameters and intermediate artifact IDs.", {"run_id": str, "step_id": str, "arguments": dict}),
            ("run_skill_script", "Execute the optional installed table-processing script using the run's selected inputs.", {"run_id": str}),
            ("finish_skill", "Finish a run after its computation has committed.", {"run_id": str}),
            ("abandon_skill", "Abandon a run, retaining any committed business effects.", {"run_id": str}),
        ]
    else:
        raise ValueError("Unknown dataflow execution mode")
    return entries
