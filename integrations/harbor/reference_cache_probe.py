"""Native partial trial proving public settlement-row reuse, not task success."""

import argparse
from pathlib import Path
import time

from integrations.benchmark_suite.native_transport_audit import _strict_json, audit_events
from integrations.benchmark_suite.resource_sample import ResourceSamples
from skillhorizon.dataflow.declaration_tasks import build_declaration_dataflow
from skillhorizon.dataflow.public_witness import PublicCreditWitness

from .backend import write_record
from .config import job_config
from .export import export_task
from .orchestration import run_jobs
from .reference_fixture import ReferenceActor
from .reference_run import LoopbackReferenceProxy, append_events, trial_result


class _CacheReuseObserved(Exception):
    pass


class _ProbeWitness(PublicCreditWitness):
    def __init__(self, invoke, probe):
        super().__init__(invoke, skill_protocol="skill_declaration_v1",
                         max_calls=1500, max_rounds=1, page_size=200)
        self.probe = probe

    async def skill(self, name, arguments, steps=None):
        artifact = await super().skill(name, arguments, steps)
        if name == "assess-credit" and self.probe.freshness_seen:
            self.probe.assessment_after_freshness = True
            raise _CacheReuseObserved()
        return artifact


class CacheReusePolicy:
    def __init__(self, invoke):
        self.invoke = invoke
        self.first_settlement = None
        self.first_reads = 0
        self.freshness_seen = False
        self.assessment_after_freshness = False

    async def call(self, name, arguments):
        result = await self.invoke(name, arguments)
        if result.get("ok") is not True:
            return result
        if (name in {"execute_step", "run_skill_script"}
                and arguments.get("skill_id") == "settle-period"
                and arguments.get("step_id") == "compute"
                and self.first_settlement is None):
            self.first_settlement = result.get("artifact_id")
        if (name == "read_table" and arguments.get("table_id") == self.first_settlement
                and arguments.get("offset") == 0):
            self.first_reads += 1
        if name == "search_objects" and arguments.get("collection") == "artifacts":
            filters = {item["field"]: item["value"] for item in arguments.get("filters", [])}
            if filters == {"producer_skill": "settle-period", "status": "current"}:
                self.freshness_seen |= any(item.get("id") == self.first_settlement
                                           for item in result.get("items", []))
        return result

    async def run(self):
        witness = _ProbeWitness(self.call, self)
        try:
            await witness.run()
        except _CacheReuseObserved:
            if (not isinstance(self.first_settlement, str) or not self.first_settlement
                    or self.first_reads != 1 or not self.freshness_seen
                    or not self.assessment_after_freshness):
                raise ValueError("Native cache reuse probe did not reach its public milestone")
            return {"status": "policy_complete", "cache_reuse_observed": True,
                    "first_settlement_read_passes": self.first_reads,
                    "freshness_checked": self.freshness_seen,
                    "assessment_after_freshness": self.assessment_after_freshness}
        raise ValueError("Native cache reuse probe completed without reuse milestone")


def run(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    resources = ResourceSamples(output, output / "harbor-task/environment")
    actor = proxy = None
    sampler_started = False
    error = None
    result = None
    try:
        resources.start()
        sampler_started = True
        source = output / "task"
        task = build_declaration_dataflow(source, seed=7, periods=8, regions=3,
            rows_per_region=6, family="regional_sales", notice_policy="source_only",
            business_program="credit_sales_v2", business_scenario="receivables_rollover")
        config = task["config"]
        if (task.get("skill_protocol") != "skill_declaration_v1"
                or task.get("checkpoint_action_count") != 0
                or any(config.get(key) != value for key, value in {
                    "seed": 7, "periods": 8, "regions": 3, "rows_per_region": 6,
                    "family": "regional_sales", "notice_policy": "source_only",
                    "business_program": "credit_sales_v2",
                    "business_scenario": "receivables_rollover"}.items())):
            raise ValueError("Cache probe source task differs from frozen P8 profile")
        write_record(output / "CACHE_SOURCE.json", {"task_id": task["task_id"],
            "config": config, "checkpoint_action_count": 0, "external_provider_calls": 0})
        export_task(source, output / "harbor-task", max_calls=350, include_oracle=False,
                    require_reference_contract=True)
        actor = ReferenceActor(CacheReusePolicy,
            on_events=lambda events: append_events(output / "NATIVE_VISIBLE.jsonl", events))
        proxy = LoopbackReferenceProxy(actor, max_responses=700)

        def config(job, api_base):
            value = job_config(job["task"], output / "jobs" / "DataOps",
                agent="terminus-2", model="openai/gpt-5.6-sol", api_base=api_base,
                max_turns=700, total_window=1000000, compact_at=900000,
                max_output=8192, observed=True, agent_timeout=2700)
            value["agents"][0]["kwargs"]["enable_summarize"] = False
            return value

        receipt = run_jobs(output, [{"label": "DataOps", "task": output / "harbor-task",
            "max_turns": 700, "agent_timeout": 2700}], proxy=proxy, build_config=config,
            declaration={"purpose": "partial native public cache reuse probe", "external_provider_calls": 0},
            cleanup_mode="stop_copy", deadline=time.monotonic() + 3300)
        trial = trial_result(receipt)
        gateway = Path(trial["trial_result"]).parent / "business_private/gateway.jsonl"
        visible = [_strict_json(line) for line in (output / "NATIVE_VISIBLE.jsonl").read_text().splitlines() if line]
        events = [_strict_json(line) for line in gateway.read_text().splitlines() if line]
        transport = audit_events(visible, events)
        policy = actor.policy_result
        passed = (receipt["all_trials_valid"] is True and trial.get("business_success") is False
                  and actor.native_complete and actor.failure is None and actor.meter.invalid is False
                  and isinstance(policy, dict) and policy.get("status") == "policy_complete"
                  and policy.get("cache_reuse_observed") is True
                  and policy.get("first_settlement_read_passes") == 1
                  and policy.get("freshness_checked") is True
                  and policy.get("assessment_after_freshness") is True
                  and transport["status"] == "passed"
                  and transport["public_calls"] == actor.ordinal)
        result = {"status": "passed" if passed else "failed", "native_requests": actor.requests,
                  "public_calls": actor.ordinal, "native_complete": actor.native_complete,
                  "business_success": trial.get("business_success"),
                  "cache_reuse_observed": bool(isinstance(policy, dict) and policy.get("cache_reuse_observed")),
                  "terminal_pages": transport["terminal_pages"],
                  "all_actions_and_results_match": transport["all_actions_and_results_match"],
                  "external_provider_calls": 0}
    except BaseException as failure:
        error = failure
    finally:
        if proxy is not None:
            try:
                proxy.stop_new_requests()
            except BaseException as cleanup_error:
                if error is None:
                    error = cleanup_error
        if actor is not None:
            try:
                actor.close()
            except BaseException as cleanup_error:
                if error is None:
                    error = cleanup_error
        try:
            sample = resources.finish()
        except BaseException as cleanup_error:
            sample = {"errors": [{"source": "sampler", "type": type(cleanup_error).__name__}]}
            if error is None:
                error = cleanup_error
        if not sampler_started:
            sample.setdefault("errors", []).append({"source": "sampler", "type": "startup_failure"})
        write_record(output / "RESOURCE.json", sample)
        if sample.get("errors") and error is None:
            error = RuntimeError("Probe resource accounting incomplete")
        if error is not None:
            result = {"status": "failed", "type": type(error).__name__,
                      "native_requests": actor.requests if actor else 0,
                      "public_calls": actor.ordinal if actor else 0,
                      "external_provider_calls": 0}
        write_record(output / "CACHE_PROBE.json", result)
    if error is not None:
        raise error
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = run(args.output)
    print(result, flush=True)
    if result["status"] != "passed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
