"""Small creation facts derived during the existing, exact business replay."""

from copy import copy, deepcopy

from ..errors import DomainError
from .calculations import calculate
from .fieldops import _plan, _passing_current_test, check_reservation
from .planning import assess


GOAL_KINDS = {
    "plan-isolation": "isolation_plan", "isolate-devices": "isolation",
    "release-isolation": "isolation_release", "repair-device": "repair",
    "test-device": "test", "retest-device": "test", "restore-group": "restoration",
    "commission-service": "service_commission", "accept-system": "acceptance",
}


def _creation_checks(runtime, before, item, skill, args):
    """Read pure rules at the creation state; never redispatch a business action."""
    prior = copy(runtime)
    prior.project = before
    after, data, parents = runtime.project, item["data"], item["inputs"]
    checks = {"artifact_kind": item["kind"] == GOAL_KINDS[skill]}
    if skill == "plan-isolation":
        draft = prior.artifact(args["draft_id"], "planning_draft")
        check = prior.artifact(args["check_id"], "planning_check")
        selected = draft["data"]["arguments"]
        expected = assess(prior, skill, selected)
        checks.update(
            draft_algorithm=draft["data"]["skill_id"] == skill,
            check_binding=check["data"]["draft_id"] == draft["artifact_id"] and check["inputs"] == [draft["artifact_id"]],
            feasible_content=check["data"]["feasible"] is True and check["data"]["evaluated_plan"] == expected == data,
            dependencies=parents == [*selected["upstream_restoration_ids"], *selected["upstream_service_ids"],
                                     draft["artifact_id"], check["artifact_id"]])
    elif skill == "isolate-devices":
        plan = prior.artifact(args["plan_id"], "isolation_plan")
        expected = _plan(prior, {key: plan["data"][key] for key in runtime.skills["plan-isolation"][0]})
        check_reservation(prior, expected)
        checks.update(plan_content=data == plan["data"] == expected, dependencies=parents == [args["plan_id"]],
                      reservation=after["isolations"][item["artifact_id"]] == {
                          **data, "open": True, "work_hours": {key: 0 for key in data["device_ids"]}},
                      device_state=all(after["devices"][key]["status"] == "isolated" for key in data["device_ids"]))
    elif skill == "release-isolation":
        old = before["isolations"][args["isolation_id"]]
        checks.update(was_open=old["open"] is True,
            release_content=data == {"isolation_id": args["isolation_id"], "device_ids": old["device_ids"],
                                     "work_hours": old["work_hours"]},
            dependencies=parents == [args["isolation_id"]],
            reservation_retained=after["isolations"][args["isolation_id"]] == {**old, "open": False},
            device_state=all(after["devices"][key]["status"] == (
                "needs_revalidation" if before["devices"][key].get("repair_id") else "pending") for key in old["device_ids"]))
    elif skill in {"repair-device", "test-device", "retest-device"}:
        device = before["devices"][args["device_id"]]
        repair = None if skill == "repair-device" else prior.artifact(args["repair_id"], "repair")
        isolation_id = args["isolation_id"] if skill != "test-device" else repair["data"]["isolation_id"]
        isolation = before["isolations"][isolation_id]
        hours = device["duration"] if skill == "repair-device" else 1
        elapsed = isolation["work_hours"][args["device_id"]] + hours
        checks.update(isolation_open=isolation["open"] is True and args["device_id"] in isolation["device_ids"],
            work_accounting=data["work_hours"] == hours and data["elapsed_device_hours"] == elapsed
                and elapsed <= isolation["end"] - isolation["start"]
                and after["isolations"][isolation_id]["work_hours"][args["device_id"]] == elapsed)
        if skill == "repair-device":
            previous = device.get("repair_id")
            checks.update(repair_content=data == {**args, "gain": device["repair_methods"][args["method"]],
                          "device_revision": device["revision"], "replaces_repair_id": previous,
                          "work_hours": hours, "elapsed_device_hours": elapsed},
                dependencies=parents == [isolation_id, *([previous] if previous else [])],
                repair_cycle=after["devices"][args["device_id"]].get("repair_id") == item["artifact_id"]
                    and "test_id" not in after["devices"][args["device_id"]],
                stock=all(after["parts"][key]["quantity"] == part["quantity"] - int(
                    args["method"] == "replace" and key == args["part_id"]) for key, part in before["parts"].items()))
        else:
            selected = {key: isolation[key] for key in runtime.skills["plan-isolation"][0]}
            plan = _plan(prior, selected)
            measured = calculate("test-device", dict(severity=device["severity"], repair_gain=repair["data"]["gain"],
                                 load=args["test_load"], rated_pressure=min(device["rated_pressure"], plan["inlet_pressure"])))
            passed = measured["residual"] <= device["maximum_residual"] and measured["delivered_pressure"] >= device["minimum_pressure"]
            checks.update(measurement_content=data == {**args, **measured, "supply_revision": before["network"]["revision"],
                          "inlet_pressure": plan["inlet_pressure"], "isolation_id": isolation_id,
                          "device_revision": device["revision"], "passed": passed,
                          "work_hours": 1, "elapsed_device_hours": elapsed},
                repair_binding=repair["data"]["device_id"] == args["device_id"]
                    and device.get("repair_id") == args["repair_id"] and repair["data"]["device_revision"] == device["revision"],
                qualifying_load=device["minimum_test_load"] <= args["test_load"] <= 100,
                dependencies=parents == list(dict.fromkeys([args["repair_id"], isolation_id])),
                unchanged_stock=before["parts"] == after["parts"],
                current_measurement=after["devices"][args["device_id"]].get("test_id") == item["artifact_id"])
    elif skill == "restore-group":
        isolation = before["isolations"][args["isolation_id"]]
        tests = [prior.artifact(key, "test") for key in args["test_ids"]]
        checks.update(full_test_coverage=sorted(t["data"]["device_id"] for t in tests) == sorted(isolation["device_ids"]),
            tests_passed_at_creation=all(_passing_current_test(prior, test) for test in tests),
            restoration_content=data == {"device_ids": isolation["device_ids"], "test_ids": args["test_ids"],
                "delivered_pressure": min(t["data"]["delivered_pressure"] for t in tests),
                "window_end": isolation["end"], "supply_revision": before["network"]["revision"]},
            dependencies=parents == [args["isolation_id"], *args["test_ids"]],
            closed_reservation=isolation["open"] is True and after["isolations"][args["isolation_id"]] == {**isolation, "open": False})
    elif skill == "commission-service":
        from .services import descendants
        service = before["services"][str(args["stage"]) ]
        restores = [prior.artifact(key, "restoration") for key in args["restoration_ids"]]
        upstream = [before["services"][str(stage)]["current_id"] for stage in service["parent_stages"]]
        old = service["current_id"]
        pressure = min(row["data"]["delivered_pressure"] for row in restores) - args["flow_units"]
        changed = bool(old and (service["flow_units"] != args["flow_units"] or before["artifacts"][old]["data"]["return_pressure"] != pressure))
        affected = [stage for stage in descendants(before, args["stage"])
                    if before["services"][str(stage)]["active"] or any(before["devices"][key]["status"] == "restored"
                    for key in before["services"][str(stage)]["device_ids"])] if changed else []
        effective = max(row["data"]["window_end"] for row in restores)
        if changed:
            effective = max(effective, max(row["end"] for row in before["isolations"].values()))
        checks.update(commission_content=data == {**args, "upstream_service_ids": upstream, "return_pressure": pressure,
            "supply_revision": before["network"]["revision"], "effective_from": effective,
            "supersedes": old, "invalidated_service_stages": affected},
            dependencies=parents == [*args["restoration_ids"], *upstream, *([old] if old else [])],
            flow_capacity=sum(row["flow_units"] for row in before["services"].values()) - service["flow_units"]
                + args["flow_units"] <= before["service_rules"]["capacity_units"],
            service_update=after["services"][str(args["stage"])] == {
                **service, "current_id": item["artifact_id"], "active": True, "flow_units": args["flow_units"]})
    elif skill == "accept-system":
        checks.update(acceptance_content=data == {"device_ids": sorted(before["devices"]), "restoration_ids": args["restoration_ids"]},
                      dependencies=parents == args["restoration_ids"],
                      current_acceptance=after["acceptance"] == item["artifact_id"])
    return checks


class CreationEvidence:
    """No world snapshots retained and no declaration-run execution attribution."""
    def __init__(self):
        self.records = {}
        self.dispatches = 0

    def __call__(self, before, runtime, action, index):
        if index != self.dispatches:
            raise ValueError("Creation evidence requires consecutive zero-prefix business dispatches")
        self.dispatches += 1
        if action["skill_id"] not in {"execute_step", "run_skill_script"} or action["result"].get("ok") is not True:
            return
        args = action["arguments"]
        skill, step = args["skill_id"], args["step_id"]
        if skill not in GOAL_KINDS or step != ("submit" if skill == "plan-isolation" else "compute"):
            return
        identifier = action["result"].get("artifact_id")
        if identifier is None or identifier in before["artifacts"] or identifier in self.records:
            return
        item = runtime.project["artifacts"][identifier]
        try:
            checks = _creation_checks(runtime, before, item, skill, args["arguments"])
        except DomainError as error:
            checks = {"creation_preconditions": False, "business_error_code": error.code}
        self.records[identifier] = dict(artifact=deepcopy(item), skill_id=skill,
            business_cut=index + 1, checks=checks,
            valid=all(value is True for key, value in checks.items() if key != "business_error_code"))
