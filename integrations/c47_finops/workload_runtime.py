"""Workload modes and reserved data volumes drive actual rightsizing and billing."""

from copy import deepcopy

from .runtime import SkillFinOps
from .kernel import require, schema
from .workload_build import FORMAT


class WorkloadFinOps(SkillFinOps):
    expected_format = FORMAT

    def __init__(self, task, runtime):
        super().__init__(task, runtime)
        require(self.public["format"] == self.world["format"] == self.expected_format, "WORKLOAD_NETWORK_FORMAT")
        text, count = {"type": "string", "minLength": 1}, {"type": "integer", "minimum": 0}
        self.metadata.update({
            "list_workload_handoffs": {"description": "Read actual metered producer handoffs with their future consumer reservations; no computed due-input manifest.", "input_schema": schema({})},
            "bind_workload_inputs": {"description": "Validate the actor's exact producer joins, input record sum and mode-specific capacity; replace planning binding without rewriting old assessments.",
                "input_schema": schema({"job_id": text, "mode": {"enum": ["raw", "compact"]}, "source_handoff_ids": {"type": "array", "items": text, "uniqueItems": True, "maxItems": 20}, "input_records": count, "required_capacity": count})},
            "publish_workload_handoff": {"description": "Publish actual billed processing output and conserve all future consumer reservations. Requires the current binding and real bill for this resource.",
                "input_schema": schema({"job_id": text, "binding_id": text, "bill_id": text, "output_records": count,
                    "allocations": {"type": "array", "items": schema({"job_id": text, "delivery_stage": count, "records": count}), "maxItems": 20}, "retained_records": count})}})

    @property
    def workload(self):
        return self.state["workload"]

    def job(self, identifier):
        matches = [j for j in self.public["workload_policy"]["jobs"] if j["job_id"] == identifier]
        require(len(matches) == 1, "WORKLOAD_JOB_NOT_FOUND")
        return matches[0]

    def current_binding(self, job_id):
        identifier = self.workload["current_bindings"].get(job_id)
        require(identifier in self.workload["bindings"], "WORKLOAD_INPUTS_NOT_BOUND")
        return self.workload["bindings"][identifier]

    def capacity_context(self, usage):
        job = next(j for j in self.public["workload_policy"]["jobs"] if j["period_id"] == usage["period_id"] and j["resource_id"] == usage["resource_id"])
        return {"workload_binding_id": self.current_binding(job["job_id"])["binding_id"]}

    def input_values(self, job, rows, mode):
        incoming = 0
        for row in rows:
            self.current_binding(row["job_id"])
            quota = next((r for r in row["allocations"] if r["job_id"] == job["job_id"] and r["delivery_stage"] == job["stage"]), None)
            require(quota is not None, "WORKLOAD_CONSUMER_STAGE_QUOTA")
            incoming += quota["records"]
        records = job["local_records"] + incoming
        policy = self.public["workload_policy"]
        capacity = (records + policy["records_per_capacity"] - 1) // policy["records_per_capacity"] + policy["modes"][mode]["capacity_overhead"]
        return {"input_records": records, "required_capacity": capacity}

    def output_values(self, job, binding):
        divisor = self.public["workload_policy"]["modes"][binding["mode"]]["output_divisor"]
        output = (binding["input_records"] + divisor - 1) // divisor
        weights = sum(c["weight"] for c in job["consumers"])
        allocations = [{"job_id": c["job_id"], "delivery_stage": c["delivery_stage"], "records": output * c["weight"] // weights} for c in job["consumers"]]
        return {"output_records": output, "allocations": allocations, "retained_records": output - sum(c["records"] for c in allocations)}

    def dispatch(self, name, args):
        if name == "read_finops_period":
            result = super().dispatch(name, args)
            result["workload_bindings"] = [deepcopy(self.workload["bindings"][i]) for i in self.workload["current_bindings"].values() if self.workload["bindings"][i]["period_id"] == result["period"]["period_id"]]
            return result
        if name == "list_workload_handoffs":
            return {"handoffs": deepcopy(list(self.workload["handoffs"].values()))}
        if name == "bind_workload_inputs":
            job = self.job(args["job_id"])
            require(self.state["phase"] == "planning" and self.current()["period_id"] == job["period_id"], "CURRENT_PLANNING_JOB_REQUIRED")
            producers = [j for j in self.public["workload_policy"]["jobs"] if any(c["job_id"] == job["job_id"] for c in j["consumers"])]
            rows = [self.workload["handoffs"].get(i) for i in args["source_handoff_ids"]]
            require(all(rows) and len(rows) == len(producers) and {r["job_id"] for r in rows} == {p["job_id"] for p in producers}, "EXACT_WORKLOAD_PRODUCER_JOIN")
            expected = self.input_values(job, rows, args["mode"])
            require(all(args[k] == v for k, v in expected.items()), "ACTUAL_WORKLOAD_VOLUME_AND_CAPACITY")
            prior_id = self.workload["current_bindings"].get(job["job_id"])
            if prior_id and all(self.workload["bindings"][prior_id][k] == v for k, v in args.items()):
                return deepcopy(self.workload["bindings"][prior_id])
            identifier = f"workload-binding-{self.workload['next_id']}"
            self.workload["next_id"] += 1
            row = {**deepcopy(args), "binding_id": identifier, "period_id": job["period_id"], "resource_id": job["resource_id"], "created_sequence": self.world["sequence"]}
            self.workload["bindings"][identifier] = row
            self.workload["current_bindings"][job["job_id"]] = identifier
            usage = next(u for u in self.state["usage"].values() if u["period_id"] == job["period_id"] and u["resource_id"] == job["resource_id"])
            usage["required_capacity"] = expected["required_capacity"]
            return deepcopy(row)
        if name == "execute_rightsizing_action":
            assessment = self.state["assessments"].get(args["assessment_id"])
            require(assessment is not None, "ASSESSMENT_NOT_FOUND")
            job = next(j for j in self.public["workload_policy"]["jobs"] if j["period_id"] == assessment["period_id"] and j["resource_id"] == assessment["resource_id"])
            require(assessment.get("workload_binding_id") == self.current_binding(job["job_id"])["binding_id"], "STALE_WORKLOAD_CAPACITY_ASSESSMENT")
        if name == "publish_workload_handoff":
            job = self.job(args["job_id"])
            require(self.current()["period_id"] == job["period_id"] and self.state["phase"] == "metered", "CURRENT_METERED_WORKLOAD_REQUIRED")
            binding = self.current_binding(job["job_id"])
            bill = self.state["bills"][job["period_id"]]
            require(args["binding_id"] == binding["binding_id"] and args["bill_id"] == bill["bill_id"], "WORKLOAD_BINDING_AND_BILL_IDENTITY")
            line = next(l for l in bill["lines"] if l["resource"]["resource_id"] == job["resource_id"])
            require(line["required_capacity"] == binding["required_capacity"], "WORKLOAD_ACTUAL_METERED_CAPACITY")
            expected = self.output_values(job, binding)
            require(all(args[k] == v for k, v in expected.items()), "WORKLOAD_OUTPUT_AND_FUTURE_RESERVATION_CONSERVATION")
            prior = next((h for h in self.workload["handoffs"].values() if h["job_id"] == job["job_id"]), None)
            if prior:
                require(all(prior[k] == v for k, v in args.items()), "METERED_WORKLOAD_IMMUTABLE")
                return deepcopy(prior)
            identifier = f"workload-handoff-{self.workload['next_id']}"
            self.workload["next_id"] += 1
            row = {**deepcopy(args), "handoff_id": identifier, "created_sequence": self.world["sequence"]}
            self.workload["handoffs"][identifier] = row
            return deepcopy(row)
        if name == "close_billing_period":
            required = {j["job_id"] for j in self.public["workload_policy"]["jobs"] if j["period_id"] == self.current()["period_id"]}
            require(required <= {h["job_id"] for h in self.workload["handoffs"].values()}, "WORKLOAD_OUTPUT_HANDOFFS_INCOMPLETE")
        return super().dispatch(name, args)

    def meter(self):
        if self.state["phase"] == "planning":
            for job in self.public["workload_policy"]["jobs"]:
                if job["period_id"] == self.current()["period_id"]:
                    self.current_binding(job["job_id"])
        return super().meter()
