"""FieldOps local goals at one first-completion cut, not model process scores."""

from copy import deepcopy

from ..errors import DomainError
from .claim_sources import GOAL_KINDS
from .fieldops import _passing_current_test, evaluate


class FieldOpsGoals:
    def __init__(self, runtime, evidence):
        self.runtime, self.evidence = runtime, evidence
        self.project = runtime.project

    def _target(self, skill, item):
        data = item["data"]
        if skill == "plan-isolation":
            return (tuple(sorted(data["device_ids"])), data["window_id"], data["crew_id"],
                    tuple(sorted(data["upstream_restoration_ids"])), tuple(sorted(data["upstream_service_ids"])))
        if skill == "isolate-devices":
            return tuple(item["inputs"])
        if skill == "release-isolation":
            return data["isolation_id"]
        if skill == "repair-device":
            return (data["device_id"], data["isolation_id"], data["replaces_repair_id"])
        if skill in {"test-device", "retest-device"}:
            return (data["device_id"], data["repair_id"], data["isolation_id"])
        if skill == "restore-group":
            return tuple(sorted(data["device_ids"]))
        if skill == "commission-service":
            return str(data["stage"])
        return self.project["project_id"]

    def _matches(self, item, ref):
        kind, identifier = ref["kind"], ref["id"]
        data = item["data"]
        if kind == "artifact":
            referenced = self.project["artifacts"][identifier]
            return identifier == item["artifact_id"] if referenced["kind"] == item["kind"] else identifier in item["inputs"]
        if kind == "isolation":
            if item["kind"] == "isolation_plan":
                return self.project["artifacts"][identifier]["inputs"] == [item["artifact_id"]]
            return identifier == item["artifact_id"] or identifier == data.get("isolation_id") or identifier in item["inputs"]
        if kind == "device":
            return identifier == data.get("device_id") or identifier in data.get("device_ids", []) or (
                item["kind"] == "service_commission" and identifier in self.project["services"][str(data["stage"])]["device_ids"])
        if kind == "service":
            devices = self.project["services"][identifier]["device_ids"]
            return str(data.get("stage")) == identifier or sorted(data.get("device_ids", [])) == sorted(devices)
        return kind == "project" and item["kind"] == "acceptance" and identifier == self.project["project_id"]

    def _current(self, skill, item):
        p, data = self.project, item["data"]
        if skill == "restore-group":
            tests = [self.runtime.artifact(key, "test") for key in data["test_ids"]]
            isolation = p["isolations"][item["inputs"][0]]
            return {"current_restoration": all(p["devices"][key]["status"] == "restored" and
                       p["devices"][key].get("restoration_id") == item["artifact_id"] for key in data["device_ids"]),
                    "test_coverage": sorted(t["data"]["device_id"] for t in tests) == sorted(data["device_ids"]) == sorted(isolation["device_ids"]),
                    "current_passing_tests": all(_passing_current_test(self.runtime, test) for test in tests),
                    "pressure": data["delivered_pressure"] == min(t["data"]["delivered_pressure"] for t in tests),
                    "supply_revision": data["supply_revision"] == p["network"]["revision"],
                    "isolation_closed": isolation["open"] is False}
        if skill == "commission-service":
            service = p["services"][str(data["stage"])]
            restores = [self.runtime.artifact(key, "restoration") for key in data["restoration_ids"]]
            upstream = [self.runtime.artifact(key, "service_commission") for key in data["upstream_service_ids"]]
            return {"current_commission": service["active"] is True and service["current_id"] == item["artifact_id"],
                    "restoration_coverage": sorted(key for record in restores for key in record["data"]["device_ids"]) == sorted(service["device_ids"]),
                    "current_restorations": all(all(p["devices"][key]["status"] == "restored" and
                        p["devices"][key].get("restoration_id") == record["artifact_id"] for key in record["data"]["device_ids"]) for record in restores),
                    "current_tests": all(_passing_current_test(self.runtime, self.runtime.artifact(p["devices"][key]["test_id"], "test"))
                                         for key in service["device_ids"]),
                    "current_upstream": sorted(data["upstream_service_ids"]) == sorted(p["services"][str(stage)]["current_id"] for stage in service["parent_stages"])
                        and all(p["services"][str(stage)]["active"] for stage in service["parent_stages"]),
                    "supply_revision": all(row["data"]["supply_revision"] == p["network"]["revision"] for row in [item, *restores, *upstream]),
                    "effective_time": data["effective_from"] >= max(row["data"]["window_end"] for row in restores)
                        and all(data["effective_from"] >= row["data"]["effective_from"] for row in upstream),
                    "flow_capacity": service["flow_units"] == data["flow_units"] and service["minimum_flow"] <= data["flow_units"] <= service["maximum_flow"]
                        and sum(row["flow_units"] for row in p["services"].values()) <= p["service_rules"]["capacity_units"],
                    "return_pressure": data["return_pressure"] == min(row["data"]["delivered_pressure"] for row in restores) - data["flow_units"] > 0}
        if skill == "accept-system":
            return {"current_acceptance": p["acceptance"] == item["artifact_id"],
                    "whole_system_business_goal": evaluate(self.runtime)[1]}
        return {}

    def _check(self, skill, item):
        identifier = item["artifact_id"]
        evidence = self.evidence.records.get(identifier)
        if evidence is None:
            return "unknown", "CREATION_HISTORY_UNAVAILABLE", []
        clauses = [{"name": "matching_creation_operation", "passed": evidence["skill_id"] == skill},
                   {"name": "unchanged_complete_artifact", "passed": evidence["artifact"] == item}]
        clauses.extend({"name": key, "passed": value} for key, value in evidence["checks"].items() if key != "business_error_code")
        try:
            clauses.extend({"name": key, "passed": value} for key, value in self._current(skill, item).items())
        except DomainError as error:
            clauses.append({"name": "current_business_preconditions:" + error.code, "passed": False})
        passed = evidence["valid"] and all(clause["passed"] is True for clause in clauses)
        return ("supported", "LOCAL_GOAL_SUPPORTED", clauses) if passed else ("contradicted", "LOCAL_GOAL_CONTRADICTED", clauses)

    def assess(self, claim):
        result = {key: deepcopy(claim[key]) for key in ("run_id", "skill_id", "event_index", "last_resolved_business_dispatch")}
        result.update(scope=deepcopy(claim.get("scope", [])), process_adherence="unobserved", clauses=[])

        def done(verdict, reason, clauses=()):
            return {**result, "goal_verdict": verdict, "reason": reason, "clauses": list(clauses)}

        skill, scope, outputs = claim["skill_id"], claim.get("scope", []), claim.get("outputs", [])
        if skill not in GOAL_KINDS:
            return done("unknown", "UNSUPPORTED_LOCAL_GOAL")
        kinds = {"artifact": "artifacts", "device": "devices", "isolation": "isolations", "service": "services"}
        for ref in [*scope, *outputs]:
            if ref["kind"] not in {*kinds, "project"}:
                return done("unknown", "UNSUPPORTED_REFERENCE_KIND")
            if ref in outputs and ref["kind"] != "artifact":
                return done("unknown", "OUTPUT_REQUIRES_ARTIFACT")
            if ref["kind"] == "project":
                if skill != "accept-system":
                    return done("unknown", "PROJECT_SCOPE_REQUIRES_ACCEPTANCE")
                exists = ref["id"] == self.project["project_id"]
            else:
                exists = ref["id"] in self.project[kinds[ref["kind"]]]
            if not exists:
                return done("contradicted", "EXPLICIT_REFERENCE_MISSING")
        if not scope and not outputs:
            return done("unknown", "LOCAL_TARGET_UNSPECIFIED")
        if outputs:
            candidates = [self.project["artifacts"][ref["id"]] for ref in outputs]
            if any(item["kind"] != GOAL_KINDS[skill] for item in candidates):
                return done("contradicted", "EXPLICIT_OUTPUT_KIND_MISMATCH")
            if any(not all(self._matches(item, ref) for ref in scope) for item in candidates):
                return done("contradicted", "EXPLICIT_OUTPUT_SCOPE_MISMATCH")
        else:
            candidates = [item for item in self.project["artifacts"].values() if item["kind"] == GOAL_KINDS[skill]
                          and all(self._matches(item, ref) for ref in scope)
                          and (item["artifact_id"] not in self.evidence.records
                               or self.evidence.records[item["artifact_id"]]["skill_id"] == skill)]
            if not candidates:
                return done("contradicted", "LOCAL_RESULT_MISSING")
        checked = [self._check(skill, item) for item in candidates]
        if outputs and any(value[0] == "contradicted" for value in checked):
            return done(*next(value for value in checked if value[0] == "contradicted"))
        if len({self._target(skill, item) for item in candidates}) != 1:
            return done("unknown", "MULTIPLE_NON_EQUIVALENT_TARGETS")
        if outputs:
            selected = next((value for value in checked if value[0] == "contradicted"), None)
            selected = selected or next((value for value in checked if value[0] == "unknown"), checked[0])
            return done(*selected)
        selected = next((value for value in checked if value[0] == "supported"), None)
        selected = selected or next((value for value in checked if value[0] == "unknown"), checked[0])
        return done(*selected)
