"""Construction-only world generator and public-procedure reference policy."""

import asyncio
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from skillhorizon.construction_provenance import construction_entry, observe_call
import random
import shutil

from ..experiment.mcp_tools import provider_tools_from_mcp_listing
from ..experiment.tasks import action_messages, user_message
from ..util import canonical_json, load_json, load_jsonl, write_json
from . import FORMAT
from .runtime import DataflowRuntime, create_dataflow_server, write_table
from .skills import SKILLS, PROCEDURES, install_skills
from .support import environment_support

MID_HORIZON_PROGRAMS = {"mid_horizon_restatement_v1", "mid_horizon_treasury_restatement_v1"}


def initialize_dataflow(root, *, seed=20260905, regions=3, periods=1, rows_per_region=12, revision=False,
                        family="regional_sales", event_kind=None, event_trigger="release_review_ready", event_region=1, event_period=1,
                        notice_policy="managed", event_program=None, business_program=None, business_scenario=None,
                        skill_protocol="procedural", credit_event_customer=None, credit_event_trigger_period=None):
    if skill_protocol not in {"procedural", "skill_declaration_v1"}:
        raise ValueError("Unknown DataOps skill protocol")
    if skill_protocol == "skill_declaration_v1" and business_program != "credit_sales_v2":
        raise ValueError("Declaration initialization currently requires credit_sales_v2")
    from .scenarios import validate, configure
    validate(business_scenario, business_program=business_program, periods=periods, family=family,
             revision=revision, event_kind=event_kind, event_program=event_program,
             credit_event_customer=credit_event_customer, credit_event_trigger_period=credit_event_trigger_period)
    environment_support(notice_policy)
    if business_program not in {None, "funded_sales_v1", "obligation_sales_v1", "credit_sales_v2"}:
        raise ValueError("Unknown business program")
    if type(seed) is not int or type(regions) is not int or not 2 <= regions <= 32:
        raise ValueError("Use an integer seed and 2..32 regions")
    maximum_periods = 48 if business_scenario == "credit_horizon" else 24
    if type(periods) is not int or not 1 <= periods <= maximum_periods or type(rows_per_region) is not int or not 3 <= rows_per_region <= 10000:
        raise ValueError(f"Use 1..{maximum_periods} periods and 3..10000 orders per regional period")
    if type(revision) is not bool:
        raise ValueError("revision must be boolean")
    if family not in {"regional_sales", "heterogeneous_merge"}:
        raise ValueError("Unknown DataOps family")
    if event_kind not in {None, "consent_revocation", "order_correction", "rate_change"}:
        raise ValueError("Unknown business event")
    if event_trigger not in {"authorized_rows_ready", "period_aggregate_ready", "release_review_ready"}:
        raise ValueError("Unknown business milestone")
    if not 1 <= event_region <= regions or not 1 <= event_period <= periods:
        raise ValueError("Event scope is outside the project")
    if event_program not in {None, "rolling_reconciliation_v1"} | MID_HORIZON_PROGRAMS:
        raise ValueError("Unknown business event program")
    if event_program and (revision or event_kind is not None or regions < 3 or periods < 2):
        raise ValueError("Rolling reconciliation needs 3+ regions, 2+ periods and no single-event override")
    if event_program and event_trigger != "release_review_ready":
        raise ValueError("Rolling reconciliation uses completed release-review boundaries")
    if event_program in MID_HORIZON_PROGRAMS and (business_program not in {"obligation_sales_v1", "credit_sales_v2"} or periods < 6):
        raise ValueError("Mid-horizon restatement requires persistent obligations and at least six periods")
    root = Path(root)
    root.mkdir(parents=True)
    rng = random.Random(seed)
    region_ids = [f"R{i + 1:02d}" for i in range(regions)]
    period_ids = [f"P{i + 1:02d}" for i in range(periods)]
    sources = {}
    def source(identifier, kind, rows, fields, region=None, period=None):
        path = f"sources/{identifier}-v1.csv"
        write_table(root / path, rows, fields)
        sources[identifier] = {"source_id": identifier, "kind": kind, "region": region,
                               "period": period, "revision": 1, "path": path, "row_count": len(rows)}
    rate_sources = {}
    for period in period_ids:
        identifier = "exchange-rates" if period == period_ids[0] else f"exchange-rates-{period}"
        rate_sources[period] = identifier
        source(identifier, "rates", [
            {"currency": "USD", "numerator": 1, "denominator": 1},
            {"currency": "EUR", "numerator": 107 + seed % 7, "denominator": 100},
            {"currency": "HKD", "numerator": 1, "denominator": 8},
        ], ["currency", "numerator", "denominator"], period=period)
    for region in region_ids:
        if family == "heterogeneous_merge":
            source(f"identity-{region}", "identity_map", [
                {"external_customer": f"{region}-account-{i + 1}", "region": region, "customer_id": f"C{i + 1:02d}"}
                for i in range(4)], ["external_customer", "region", "customer_id"], region=region)
        source(f"consent-{region}", "consent", [
            {"customer_id": f"C{i + 1:02d}", "region": region, "analytics_allowed": "no" if i == 3 else "yes"}
            for i in range(4)], ["customer_id", "region", "analytics_allowed"], region=region)
        for period in period_ids:
            rows = []
            for index in range(rows_per_region):
                cents = rng.randrange(100, 50000)
                rows.append({"order_id": f"{region}-{period}-{index + 1:04d}", "customer_id": f"C{index % 4 + 1:02d}",
                             "region": region, "period": period, "currency": ("USD", "EUR", "HKD")[index % 3],
                             "amount": f"{cents // 100}.{cents % 100:02d}"})
            rows.append({**rows[0], "customer_id": " C01 "})
            rng.shuffle(rows)
            identifier = f"orders-{region}-{period}"
            if family == "regional_sales":
                source(identifier, "orders", rows,
                       ["order_id", "customer_id", "region", "period", "currency", "amount"], region, period)
            else:
                minor = region_ids.index(region) % 2 == 1
                columns = dict(zip(("order_id", "customer_id", "region", "period", "currency", "amount", "record_revision"),
                                   ("sale_ref", "account", "territory", "cycle", "ccy", "minor_amount", "edition") if minor else
                                   ("transaction", "client_key", "market", "month", "denomination", "gross", "revision_no")))
                external = []
                for row in rows:
                    value = {**row, "customer_id": f"{region}-account-{int(row['customer_id'].strip()[1:])}", "record_revision": "1"}
                    if minor:
                        value["amount"] = str(int(Decimal(row["amount"]) * 100))
                    external.append({columns[k]: v for k, v in value.items()})
                amended = dict(external[0])
                amended[columns["record_revision"]] = "2"
                amended[columns["amount"]] = str(Decimal(amended[columns["amount"]]) + (211 if minor else Decimal("2.11")))
                external.append(amended)
                source(identifier, "external_orders", external, list(columns.values()), region, period)
                sources[identifier]["business_schema"] = {"column_map": columns, "amount_unit": "minor" if minor else "major",
                    "key_map_source_id": f"identity-{region}", "conflict_policy": "latest_revision"}
    kind = event_kind or ("consent_revocation" if revision else None)
    target_region, target_period = region_ids[event_region - 1], period_ids[event_period - 1]
    event_source = {"consent_revocation": f"consent-{target_region}",
                    "order_correction": f"orders-{target_region}-{target_period}",
                    "rate_change": rate_sources[target_period]}.get(kind)
    project = {
        "project_id": f"DATAFLOW-{family}-{seed}-R{regions}-P{periods}-N{rows_per_region}",
        "family": family,
        "notice_policy": notice_policy,
        "environment": "dataops-integrated", "regions": region_ids, "periods": period_ids,
        "goal": "Publish an aggregate sales release for every required region and period, using current analytics authorization. "
                "Deduplicate identical orders, convert each authorized order to USD cents with half-up rounding, and report "
                "period order_count/usd_cents and cumulative_order_count/cumulative_usd_cents. "
                "Audit every regional period and review the complete release. Do not publish customer-level data. "
                "Public source corrections, authorization, and period-scoped rate rules may change at business milestones; "
                "retain unaffected valid work and update affected results. External feeds use the declared schema and latest revision rule.",
        "work_packages": [{"id": f"{r}/{p}", "region": r, "period": p, "orders_source": f"orders-{r}-{p}",
                           "consent_source": f"consent-{r}", "rates_source": rate_sources[p]}
                          for p in period_ids for r in region_ids],
        "sources": sources, "artifacts": {}, "next_artifact": 1, "active": {}, "next_run": 1,
        "revision": 0, "publication": None, "notices": [],
        "scheduled_change": {"kind": kind, "source_id": event_source, "customer_id": "C01",
                             "order_id": f"{target_region}-{target_period}-0001", "region": target_region,
                             "period": target_period, "trigger": event_trigger, "applied": False} if kind else None,
    }
    if event_program:
        project["event_program"] = event_program
        project["project_id"] += "-rolling"
        project["scheduled_changes"] = [
            {"kind": event, "source_id": identifier, "customer_id": "C01",
             "order_id": f"{target_region}-{target_period}-0001", "region": region,
             "period": target_period, "trigger": "release_review_ready", "applied": False}
            for event, identifier, region in (
                ("order_correction", f"orders-{target_region}-{target_period}", target_region),
                ("rate_change", rate_sources[target_period], target_region),
                ("consent_revocation", f"consent-{region_ids[event_region % regions]}", region_ids[event_region % regions]))]
        project["goal"] += (
            " Each completed release review closes an upstream reconciliation round. "
            "Queued amendments, if any, become available at that boundary and are announced "
            "as source changes. Publish only against a current review after reconciliation. "
            "Unchanged outputs remain usable; reread data only when useful for your work.")
        if event_program in MID_HORIZON_PROGRAMS:
            project["project_id"] = project["project_id"].removesuffix("-rolling") + "-mid-restatement"
            project["scheduled_changes"] = [{"kind": "rate_change", "source_id": rate_sources[period_ids[0]],
                "region": None, "period": period_ids[0], "trigger_period": period_ids[3],
                "trigger": "period_settlement_ready", "applied": False}]
            if event_program == "mid_horizon_treasury_restatement_v1":
                project["project_id"] += "-treasury"
                project["scheduled_changes"][0].update(kind="treasury_correction", source_id="opening-treasury")
    if business_program:
        source("opening-treasury", "treasury", [{"cash_cents": regions * rows_per_region *
               (4000 if business_program in {"obligation_sales_v1", "credit_sales_v2"} else 1000)}], ["cash_cents"], period=period_ids[0])
        terms_sources = {}
        if business_program in {"obligation_sales_v1", "credit_sales_v2"}:
            source("invoice-payment-terms", "invoice_terms", [
                {"region": region, "customer_id": f"C{i + 1:02d}", "delay_periods": (3, 1, 5, 2)[i],
                 "payment_route": "disputed" if (region_ids.index(region) + i + seed) % 3 == 0 else "normal"}
                for region in region_ids for i in range(4)], ["region", "customer_id", "delay_periods", "payment_route"])
            terms_sources = {period: "invoice-payment-terms" for period in period_ids}
        else:
            for period in period_ids:
                identifier = f"collection-terms-{period}"
                source(identifier, "collection_terms", [{"numerator": 3, "denominator": 4}],
                       ["numerator", "denominator"], period=period)
                terms_sources[period] = identifier
        project["business_program"] = business_program
        project["funding"] = {"treasury_source": "opening-treasury", "terms_sources": terms_sources,
                              "allocation_policy": "proportional_demand_region_order_remainder",
                              "fulfillment_policy": "whole_orders_ascending_id_skip_unaffordable",
                              "procurement_cost": "ceil(usd_cents/2)",
                              "receivables_policy": "collect_remaining_at_next_period_opening",
                              "unfilled_orders": "expire_at_period_end"}
        project["project_id"] += "-funded"
        project["goal"] = (
            "Publish a cash-constrained funded sales release for every required region and period. "
            "Normalize public orders, apply current analytics authorization, and convert each authorized order "
            "to USD cents using per-order half-up rounding. External feeds use their declared schema and latest revision. "
            "The reported sales are fulfilled orders, "
            "not all authorized demand. Share opening treasury across regions in proportion to demand. "
            "Fulfill whole affordable orders, settle costs and collections, and use the previous period's "
            "closing cash plus collected receivables to finance the next period. Never replenish from opening "
            "treasury after period one. Audit authorized demand, but close and publish realized funded sales. "
            "Ordinary demand rollups (including rollup-batch) do not implement this accounting contract. "
            "Upstream corrections restate the accounting scenario, including affected downstream financing; "
            "this synthetic task does not model reversal of real-world shipments. "
            "Publish period and cumulative order_count/usd_cents without customer-level rows. "
            "Read source-change notices at business milestones; retain unaffected valid work. "
            "A completed release review may close a reconciliation round and announce amendments; "
            "publish only against a current review after reconciliation.")
        if business_program in {"obligation_sales_v1", "credit_sales_v2"}:
            project["project_id"] = project["project_id"].removesuffix("-funded") + "-obligations"
            project["funding"].update(invoice_terms_source="invoice-payment-terms",
                receivables_policy="order_level_due_dates_and_evidence_dependent_collection")
            project["goal"] = (
                "Publish realized funded sales and complete invoice accounting for all required periods and regions. "
                "Use current authorization, per-order half-up USD conversion and declared source normalization rules. "
                "Share cash proportional to regional demand and fulfill whole affordable orders in ascending order ID, "
                "with procurement cost ceil(usd_cents/2). Unfilled orders expire. Customer payment terms determine "
                "when each fulfilled order's invoice becomes due and whether it needs dispute reconciliation. "
                "Collect all due invoices in their due period, using original fulfillment evidence when disputed. "
                "Future invoices are not cash; keep those due beyond the horizon recorded as outstanding. "
                "Never replenish opening treasury after period one or credit an invoice twice. Review regional demand "
                "audits, actual sales and accounting records before publishing aggregate period/cumulative counts "
                "and USD cents without customer-level data. Source amendments may appear at reconciliation "
                "milestones: restate affected accounting and reuse unaffected work. This is a synthetic accounting "
                "scenario, not permission to reverse real-world shipments. Publish only against a current review.")
        if business_program == "credit_sales_v2":
            source("customer-credit-limits", "credit_limits", [
                {"customer_id": f"C{i+1:02d}", "credit_limit_cents": regions * rows_per_region * (3500 + 500*i)}
                for i in range(4)], ["customer_id", "credit_limit_cents"])
            project["funding"]["credit_limits_source"] = "customer-credit-limits"
            project["project_id"] += "-pooled-credit-v2"
            project["goal"] += (
                " Customer IDs denote the same corporate customer across regions. Their public credit limits "
                "are shared across every region, not renewed per region or period. Before allocation, assess-credit "
                "joins current regional demand to the complete preceding settlement chain. Invoices due this "
                "period are collected before new orders; explicitly select the old settlement_ids covering "
                "all future-due invoices, which consume credit even beyond the "
                "project horizon. Allocate remaining customer credit proportional to that customer's current "
                "regional demand, with remainder cents in region order. An order must fit both its regional cash "
                "budget and remaining regional customer-credit quota. Unused credit is not cash; unfilled orders "
                "expire. Bind the assessment to working-capital allocation; do not reuse another period's assessment.")
        if event_program in MID_HORIZON_PROGRAMS:
            project["goal"] += (" Period settlement can reveal an amendment to an earlier period's shared inputs. "
                                "Retain original records, restate affected financing and reuse unaffected preparation. "
                                "A successful early settlement does not make its dependencies immutable.")
    configure(root, project, business_scenario, credit_event_customer=credit_event_customer,
              credit_event_trigger_period=credit_event_trigger_period)
    if skill_protocol == "skill_declaration_v1":
        project["skill_protocol"] = skill_protocol
        project["project_id"] += "-declaration-v1"
        del project["active"], project["next_run"]
    write_json(root / "project.json", project)
    install_skills(root / "skills", business_program=business_program, skill_protocol=skill_protocol)
    return project


def execute_reference(runtime, *, execution_mode="macro", use_scripts=True, alternate=False, batch=False):
    if batch and runtime.project.get("business_program"):
        raise ValueError("Demand batch rollup is not a funded-sales reference policy")
    class SourceChanged(Exception):
        pass
    stages = []
    read_skills = set()
    def call(name, args):
        result = runtime.call(name, args)
        if not result["ok"]:
            raise ValueError(f"Reference operation failed: {name}: {result}")
        return result
    def run(name, args):
        start = len(load_jsonl(runtime.root / "model_skill_actions.jsonl"))
        if execution_mode == "macro":
            output = call(name, args)
        elif execution_mode == "procedural":
            if name not in read_skills:
                call("read_skill_file", {"skill_id": name, "path": "SKILL.md"})
                read_skills.add(name)
            opened = call("begin_skill", {"skill_id": name, "arguments": args})
            def step(identifier, arguments):
                return call("execute_step", {"run_id": opened["run_id"], "step_id": identifier, "arguments": arguments})
            if name == "join-orders":
                mid = step("authorize", {"orders_id": args["orders_id"], "consent_id": args["consent_id"],
                                         "join_keys": ["region", "customer_id"] if alternate else ["customer_id", "region"]})
                if use_scripts:
                    call("run_skill_script", {"run_id": opened["run_id"]})
                else:
                    step("convert", {"authorized_id": mid["artifact_id"], "rates_id": args["rates_id"], "rounding": "half_up"})
            elif name == "audit-region":
                mid = step("reconcile_rows", {"orders_id": args["orders_id"], "joined_id": args["joined_id"], "key_columns": ["order_id"]})
                step("check_summary", {"reconciliation_id": mid["artifact_id"], "summary_id": args["summary_id"], "group_by": ["region", "period"]})
            elif name == "review-release":
                mid = step("check_coverage", {**args, "required_periods": list(runtime.project["periods"])})
                step("check_totals", {"coverage_id": mid["artifact_id"], "method": "running_sum" if alternate else "source_totals"})
            elif use_scripts and runtime.skills[name]["script"]:
                call("run_skill_script", {"run_id": opened["run_id"]})
            else:
                call("execute_step", {"run_id": opened["run_id"], "step_id": "compute", "arguments": args})
            output = call("finish_skill", {"run_id": opened["run_id"]})
        else:
            raise ValueError("Unknown execution mode")
        stages.append({"skill_id": name, "arguments": deepcopy(args), "artifact_id": output["artifact_id"],
                       "action_start": start, "action_end": len(load_jsonl(runtime.root / "model_skill_actions.jsonl"))})
        if output.get("notices") or not runtime.current(output["artifact_id"]):
            raise SourceChanged()
        return output["artifact_id"]
    def ensure(name, args):
        for artifact in reversed(list(runtime.project["artifacts"].values())):
            if not artifact.get("intermediate") and artifact["producer_skill"] == name and artifact["arguments"] == args and runtime.current(artifact["artifact_id"]):
                return artifact["artifact_id"]
        return run(name, args)
    regions = list(runtime.project["regions"])
    if alternate:
        regions.reverse()
    def attempt():
        consents = {r: ensure("prepare-consent", {"source_id": f"consent-{r}"}) for r in regions}
        audits, rollups, all_summaries = [], [], []
        previous = ""
        previous_settlement = ""
        settled_books = []
        for period in runtime.project["periods"]:
            rate_source = next(w["rates_source"] for w in runtime.project["work_packages"] if w["period"] == period)
            rates = ensure("prepare-rates", {"source_id": rate_source})
            summaries = []
            joined_ids = []
            orders = {}
            for r in regions:
                source_id = f"orders-{r}-{period}"
                source = runtime.project["sources"][source_id]
                if source["kind"] == "external_orders":
                    orders[r] = ensure("harmonize-orders", {"source_id": source_id, **source["business_schema"]})
                else:
                    orders[r] = ensure("normalize-orders", {"source_id": source_id, "deduplication": "last" if alternate else "first"})
            for region in regions:
                joined = ensure("join-orders", {"orders_id": orders[region], "consent_id": consents[region], "rates_id": rates})
                joined_ids.append(joined)
                summary = ensure("summarize-region", {"joined_id": joined})
                summaries.append(summary)
                audits.append(ensure("audit-region", {"orders_id": orders[region], "joined_id": joined, "summary_id": summary}))
            all_summaries += summaries
            if runtime.project.get("business_program"):
                funding = runtime.project["funding"]
                selected = {"summary_ids": summaries, "previous_settlement_id": previous_settlement,
                            "treasury_source_id": funding["treasury_source"]}
                if runtime.project["business_program"] in {"obligation_sales_v1", "credit_sales_v2"}:
                    collections = []
                    for route, operation in (("normal", "collect-invoices"), ("disputed", "reconcile-disputed-invoices")):
                        books, invoices, proof = [], [], []
                        for identifier in settled_books:
                            book = runtime.project["artifacts"][identifier]
                            due = [row for row in runtime._rows(book) if row["record_type"] == "invoice" and
                                   row["due_period"] == period and row["payment_route"] == route]
                            if due:
                                books.append(identifier)
                                invoices.extend(row["invoice_id"] for row in due)
                                proof.extend(v for v in book["arguments"]["fulfillment_ids"]
                                             if runtime.project["artifacts"][v]["region"] in {row["region"] for row in due})
                        if invoices:
                            args = {"settlement_ids": books, "invoice_ids": invoices, "period": period}
                            if route == "disputed":
                                args["fulfillment_ids"] = list(dict.fromkeys(proof))
                            collections.append(ensure(operation, args))
                    selected["collection_ids"] = collections
                    if "credit_limits_source" in funding:
                        selected["credit_assessment_id"] = ensure("assess-credit", {
                            "joined_ids": joined_ids, "previous_settlement_id": previous_settlement,
                            "settlement_ids": [key for key in settled_books if any(row["record_type"] == "invoice" and
                                int(row["due_period"][1:]) > int(period[1:]) for row in runtime._rows(runtime.project["artifacts"][key]))],
                            "limits_source_id": funding["credit_limits_source"]})
                allocation = ensure("allocate-working-capital", selected)
                fulfilled = [ensure("fulfill-orders", {"joined_id": joined, "allocation_id": allocation}) for joined in joined_ids]
                previous_settlement = ensure("settle-period", {"fulfillment_ids": fulfilled, "allocation_id": allocation,
                    "terms_source_id": funding["terms_sources"][period]})
                settled_books.append(previous_settlement)
                previous = ensure("close-funded-period", {"settlement_id": previous_settlement, "previous_rollup_id": previous})
                rollups.append(previous)
            elif not batch:
                previous = ensure("rollup-period", {"summary_ids": summaries, "previous_rollup_id": previous})
                rollups.append(previous)
        if batch and not runtime.project.get("business_program"):
            rollups = [ensure("rollup-batch", {"summary_ids": all_summaries})]
        review = run("review-release", {"rollup_ids": rollups, "audit_ids": audits})
        if runtime.current(review):
            run("publish-release", {"rollup_ids": rollups, "review_id": review})
            return
        raise SourceChanged()
    events = runtime.project.get("scheduled_changes", [runtime.project.get("scheduled_change")])
    pending = sum(bool(event and not event["applied"]) for event in events)
    for _ in range(pending + 1):
        try:
            attempt()
            break
        except SourceChanged:
            continue
    else:
        raise ValueError("Reference recovery did not converge")
    if not runtime.score()["success"]:
        raise ValueError("Reference did not satisfy content-based project goals")
    return stages


def structure_metrics(runtime, stages):
    from ..experiment.structure_metrics import business_data_structure
    artifacts = runtime.project["artifacts"]
    order = {s["artifact_id"]: i for i, s in enumerate(stages)}
    stage_index = 0
    for identifier in artifacts:
        if identifier in order:
            stage_index = order[identifier] + 1
        else:
            order[identifier] = stage_index
    depths, edges = {}, []
    for identifier, artifact in artifacts.items():
        parents = [p["id"] for p in artifact["inputs"] if p["type"] == "artifact"]
        depths[identifier] = 1 + max((depths[p] for p in parents), default=0)
        for parent in parents:
            edges.append({"producer": parent, "consumer": identifier,
                          "cross_work_package": artifacts[parent]["work_package"] != artifact["work_package"],
                          "skill_gap": order[identifier] - order[parent]})
    return {"measurement": "reference_execution_dataflow_not_minimal_plan", "reference_skill_stages": len(stages),
            "business_data_graph": business_data_structure(runtime.project, stages, load_jsonl(runtime.root / "model_skill_actions.jsonl")),
            "regional_period_work_packages": len(runtime.project["work_packages"]),
            "independent_region_branches": 0 if runtime.project.get("business_program") else len(runtime.project["regions"]),
            "parallel_preparation_regions": len(runtime.project["regions"]), "periods": len(runtime.project["periods"]),
            "reference_data_depth": max(depths.values(), default=0), "data_edges": len(edges),
            "cross_work_package_data_edges": sum(e["cross_work_package"] for e in edges),
            "content_join_count": sum(not a.get("intermediate") and sum(p["type"] == "artifact" for p in a["inputs"]) > 1
                                      for a in artifacts.values()),
            "intermediate_artifact_count": sum(bool(a.get("intermediate")) for a in artifacts.values()),
            "max_data_reuse_gap_skills": max((e["skill_gap"] for e in edges), default=0),
            "unique_skills": len({s["skill_id"] for s in stages}),
            "control_only_edges": 0, "resource_constraints": int(bool(runtime.project.get("business_program"))),
            "business_program": runtime.project.get("business_program"),
            "funding_horizon": funding_metrics(runtime),
            "event_count": len(runtime.project["notices"]),
            "event_impacts": [{"type": n["type"], "region": n.get("effective_region"), "period": n.get("effective_period"),
                               "invalidated_artifact_count": len(n["invalidated_artifacts"])} for n in runtime.project["notices"]],
            "event_temporal_constraints_included_in_data_depth": False,
            "edges": edges}


def funding_metrics(runtime):
    if runtime.project.get("business_program") in {"obligation_sales_v1", "credit_sales_v2"}:
        from .obligation_metrics import obligation_metrics
        return obligation_metrics(runtime)
    if runtime.project.get("business_program") != "funded_sales_v1":
        return None
    current = [a for a in runtime.project["artifacts"].values() if runtime.current(a["artifact_id"])]
    settlements = [a for a in current if a["kind"] == "settlement"]
    fulfilled = [a for a in current if a["kind"] == "fulfillment"]
    constrained = {(a["region"], a["period"]) for a in fulfilled if any(not r["fulfilled"] for r in runtime._rows(a))}
    return {"required_financing_chain_stages": 3 * len(runtime.project["periods"]),
            "basis": "allocation -> regional fulfillment -> settlement per period; excludes preparation and publication",
            "minimum_total_plan_claim": False,
            "settled_periods": sorted({a["period"] for a in settlements}),
            "cash_constrained_work_packages": len(constrained),
            "cross_period_settlement_dependencies": sum(a["kind"] == "allocation" and
                bool(a["arguments"]["previous_settlement_id"]) for a in current),
            "scope": "current executed state; repeated equivalent artifacts may occur"}


async def _tools(root, mode):
    from mcp import Client
    async with Client(create_dataflow_server(root, mode)) as client:
        return provider_tools_from_mcp_listing(await client.list_tools())


@construction_entry
def build_dataflow(output, *, seed=20260905, regions=3, periods=1, rows_per_region=12,
                   revision=False, suffix_skills=None, execution_mode="macro", family="regional_sales",
                   event_kind=None, event_trigger="release_review_ready", event_region=1, event_period=1,
                   notice_policy="managed", event_program=None, business_program=None, business_scenario=None):
    root = Path(output).resolve()
    if root.exists():
        raise FileExistsError(root)
    if execution_mode not in {"macro", "procedural"}:
        raise ValueError("Unknown execution mode")
    if suffix_skills is not None and (type(suffix_skills) is not int or suffix_skills < 1):
        raise ValueError("suffix_skills must be positive")
    root.mkdir(parents=True)
    config = dict(seed=seed, regions=regions, periods=periods, rows_per_region=rows_per_region,
                  revision=revision, suffix_skills=suffix_skills, execution_mode=execution_mode,
                  family=family, event_kind=event_kind, event_trigger=event_trigger, event_region=event_region, event_period=event_period,
                  notice_policy=notice_policy, event_program=event_program,
                  business_program=business_program, business_scenario=business_scenario)
    write_json(root / "BUILD.json", {"status": "building", "config": config})
    observe_call(initialize_dataflow, root / "initial", seed=seed, regions=regions, periods=periods,
                        rows_per_region=rows_per_region, revision=revision, family=family,
                        event_kind=event_kind, event_trigger=event_trigger, event_region=event_region, event_period=event_period,
                        notice_policy=notice_policy, event_program=event_program, business_program=business_program,
                        business_scenario=business_scenario)
    shutil.copytree(root / "initial", root / "reference")
    reference = DataflowRuntime(root / "reference")
    stages = execute_reference(reference, execution_mode=execution_mode)
    remaining = len(stages) if suffix_skills is None else suffix_skills
    if remaining > len(stages):
        raise ValueError("suffix_skills exceeds actual reference stages")
    prefix_stages = len(stages) - remaining
    prefix_calls = stages[prefix_stages - 1]["action_end"] if prefix_stages else 0
    actions = load_jsonl(root / "reference/model_skill_actions.jsonl")
    shutil.copytree(root / "initial", root / "checkpoint")
    checkpoint = DataflowRuntime(root / "checkpoint")
    for action in actions[:prefix_calls]:
        if checkpoint.call(action["skill_id"], action["arguments"]) != action["result"]:
            raise ValueError("Reference replay is not deterministic")
    brief = {"project": DataflowRuntime(root / "initial").read_project(), "skill_catalog": [
        {"skill_id": name, "description": skill["description"], "script_available": skill["script"],
         "entrypoint": "SKILL.md", "files": ["SKILL.md"] + (["scripts/execute.py", "scripts/table_ops.py"] if skill["script"] else [])}
        for name, skill in checkpoint.skills.items()],
        "interaction": "Select reusable skills independently. Public tables and skill files can be read normally. "
                       "History records completed operations, not a prescribed remaining plan."}
    messages = [user_message("ACTIVE PROJECT. " + canonical_json(brief)),
                *action_messages(actions[:prefix_calls], "project-history"),
                user_message(f"Continue active project {checkpoint.project['project_id']} to its business goal using the available tools.")]
    metrics = structure_metrics(reference, stages)
    task = {"format": FORMAT, "version": 2, "task_id": checkpoint.project["project_id"], "config": config,
            "messages": messages, "tools": asyncio.run(_tools(root / "checkpoint", execution_mode)),
            "checkpoint_action_count": prefix_calls, "remaining_skill_count": remaining,
            "reference_skill_count": len(stages), "reference_tool_call_count": len(actions) - prefix_calls,
            "skill_ids": list(checkpoint.skills), "execution_mode": execution_mode,
            "history_source": f"executed_{execution_mode}_expert_prefix", "static_dag_exposed": False,
            "observation_protocol": "chronological_summary_query_v2",
            "feedback_protocol": "current_at_observation_v1",
            "skill_discovery_protocol": "public_entrypoint_and_file_inventory_v1",
            "structure": {k: v for k, v in metrics.items() if k != "edges"}}
    from ..benchmark_spec import BENCHMARK_REVISION
    task["benchmark_revision"] = BENCHMARK_REVISION
    task["environment_support"] = environment_support(notice_policy)
    public_qualification = None
    if business_program == "credit_sales_v2" and execution_mode == "procedural":
        from .public_qualification import qualify_public_credit
        try:
            public_qualification = qualify_public_credit(root / "initial", root / "public_qualification", tools=task["tools"])
            if public_qualification.get("accepted") is not True:
                raise ValueError("Public credit qualification did not pass")
        except Exception as error:
            write_json(root / "BUILD.json", {"status": "failed", "config": config,
                "stage": "public_qualification", "failure_type": type(error).__name__,
                "public_qualification": "public_qualification/QUALIFICATION.json", "external_calls": 0})
            raise
    write_json(root / "task.json", task)
    write_json(root / "continuation.json", actions[prefix_calls:])
    if public_qualification is not None and prefix_calls == 0:
        from ..experiment.reference_trace import dataops_policy, freeze_task
        freeze_task(root, task, load_jsonl(root / "public_qualification/runtime/model_skill_actions.jsonl"),
                    dataops_policy(), lineage=load_json(root / "public_qualification/REFERENCE_POLICY.json"))
    write_json(root / "construction.json", {"stages": stages, "structure": metrics})
    write_json(root / "BUILD.json", {"status": "complete", "config": config, "expert_success": True,
                                    "external_calls": 0, "skill_stages": len(stages),
                                    **({"public_qualification": "public_qualification/QUALIFICATION.json"}
                                       if public_qualification is not None else {})})
    return task
