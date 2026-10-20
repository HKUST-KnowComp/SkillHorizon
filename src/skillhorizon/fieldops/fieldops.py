"""Isolation closure, physical repairs, load tests and staged system restoration."""


def _device(r, identifier):
    r.require(identifier in r.project["devices"], "UNKNOWN_OBJECT", "Unknown device")
    return r.project["devices"][identifier]


def _work_time(r, isolation, device_id, hours):
    used = isolation["work_hours"][device_id]
    r.require(used + hours <= isolation["end"] - isolation["start"], "MAINTENANCE_TIME",
              "Repair and test work exceeds this device's remaining maintenance window",
              {"device_id": device_id, "used_hours": used, "required_hours": hours,
               "available_hours": isolation["end"] - isolation["start"] - used})
    return used + hours


def _plan(r, a):
    p = r.project
    devices = [_device(r, d) for d in a["device_ids"]]
    r.require(bool(devices), "COVERAGE", "Select at least one device")
    boundaries = {b for d in devices for b in d["boundaries"]}
    r.require(set(a["boundary_ids"]) == boundaries, "ISOLATION_BOUNDARY", "Isolation boundaries must exactly cover the selected devices")
    closure = {key for key, device in p["devices"].items() if set(device["boundaries"]) & boundaries}
    r.require(closure == set(a["device_ids"]), "ISOLATION_CLOSURE", "Include every device affected by the isolation boundary")
    r.require(a["window_id"] in p["windows"] and a["crew_id"] in p["crews"], "UNKNOWN_RESOURCE", "Unknown window or crew")
    window = p["windows"][a["window_id"]]
    r.require(window["start"] >= max(max(d["earliest_start"], d.get("effective_from", 0)) for d in devices), "MAINTENANCE_WINDOW", "Window precedes the public maintenance phase")
    r.require(window["end"] - window["start"] >= max(d["duration"] + 1 for d in devices),
              "MAINTENANCE_WINDOW", "Window must fit at least one repair and qualifying test per device")
    r.require(p["crews"][a["crew_id"]]["qualified"], "CREW_QUALIFICATION", "Crew is not qualified")
    predecessors = {v for d in devices for v in d["predecessors"]}
    upstream = [r.artifact(v, "restoration") for v in a["upstream_restoration_ids"]]
    supplied = {d for v in upstream for d in v["data"]["device_ids"]}
    r.require(predecessors <= supplied and all(_device(r, d)["status"] == "restored" for d in predecessors),
              "UPSTREAM_DEPENDENCY", "Required upstream equipment must have a current restoration output")
    if predecessors:
        r.require(all(all(_device(r, d).get("restoration_id") == v["artifact_id"] for d in v["data"]["device_ids"])
                      for v in upstream), "STALE_ARTIFACT", "Use current upstream restoration records")
        r.require(all(v["data"]["delivered_pressure"] >= p["minimum_upstream_pressure"] for v in upstream),
                  "UPSTREAM_CONTENT", "Upstream measured pressure is insufficient for staged work")
        r.require(window["start"] >= max(v["data"]["window_end"] for v in upstream),
                  "MAINTENANCE_WINDOW", "Downstream work starts before upstream restoration completes")
    extra = {}
    if "network" in p:
        r.require(window["start"] >= p["network"]["effective_from"], "MAINTENANCE_WINDOW",
                  "Maintenance cannot precede the current supply update's effective time")
        r.require(supplied == predecessors, "UPSTREAM_DEPENDENCY", "Bind exactly this stage's required upstream devices")
        r.require(all(v["data"].get("supply_revision") == p["network"]["revision"] for v in upstream),
                  "STALE_ARTIFACT", "Upstream certifications precede the current supply measurement")
        extra = {"supply_revision": p["network"]["revision"], "inlet_pressure":
                 min(v["data"]["delivered_pressure"] for v in upstream) if upstream else p["network"]["supply_pressure"]}
        if "services" in p:
            from .services import plan_inputs
            extra["inlet_pressure"] = plan_inputs(r, a)
    if p.get('service_rules', {}).get('allow_restored_rework') and any(d['status']=='restored' for d in devices):
        from .services import descendants
        stage = devices[0]['stage']
        affected = {stage, *descendants(p, stage)}
        prior = [i for i in p['isolations'].values()
                 if any(p['devices'][key]['stage'] in affected for key in i['device_ids'])]
        r.require(not any(i['open'] for i in prior), 'RESOURCE_CONFLICT',
                  'Close affected downstream isolations before reworking this boundary')
        earliest = max((i['end'] for i in prior), default=0)
        r.require(window['start'] >= earliest, 'MAINTENANCE_WINDOW',
                  'Corrective maintenance must follow completed affected work', {'earliest_start': earliest})
    return {**a, **extra, "start": window["start"], "end": window["end"],
            "device_revisions": {d: _device(r, d)["revision"] for d in a["device_ids"]}}


def execute(r, name, a, use_script):
    p = r.project
    if name == "release-isolation":
        isolation = p["isolations"].get(a["isolation_id"])
        r.require(isolation and isolation["open"], "ISOLATION_REQUIRED", "Select an open isolation")
        output = r.emit("isolation_release", {"isolation_id": a["isolation_id"],
            "device_ids": isolation["device_ids"], "work_hours": dict(isolation["work_hours"])}, [a["isolation_id"]])
        isolation["open"] = False
        for key in isolation["device_ids"]:
            device = _device(r, key)
            device["status"] = "needs_revalidation" if device.get("repair_id") else "pending"
        return output
    if name == "commission-service":
        from .services import commission
        return commission(r, a)
    if name == "plan-isolation":
        data = _plan(r, a)
        return r.emit("isolation_plan", data, [*a["upstream_restoration_ids"], *a.get("upstream_service_ids", [])])
    if name == "isolate-devices":
        plan = r.artifact(a["plan_id"], "isolation_plan")
        selected = {k: plan["data"][k] for k in r.skills["plan-isolation"][0]}
        data = _plan(r, selected)
        r.require(data == plan["data"], "STALE_ARTIFACT", "Device conditions changed; create a current isolation plan")
        check_reservation(r, data)
        if p.get('service_rules', {}).get('allow_restored_rework') and any(
                _device(r, d)['status']=='restored' for d in data['device_ids']):
            from .services import suspend_descendants
            stage = _device(r, data['device_ids'][0])['stage']
            affected = suspend_descendants(p, stage)
            p['services'][str(stage)]['active'] = False
            p['notices'].append({'type':'maintenance_reopened', 'affected_service_stages':[stage, *affected],
                                 'state_changed':True})
        output = r.emit("isolation", data, [a["plan_id"]])
        p["isolations"][output["artifact_id"]] = {**data, "open": True,
            "work_hours": {d: 0 for d in data["device_ids"]}}
        for d in data["device_ids"]:
            _device(r, d)["status"] = "isolated"
        return output
    if name == "repair-device":
        device = _device(r, a["device_id"])
        isolation = p["isolations"].get(a["isolation_id"])
        r.require(isolation and isolation["open"] and a["device_id"] in isolation["device_ids"], "ISOLATION_REQUIRED", "Device must be inside an open isolation")
        r.require(device["status"] in {"isolated", "repaired", "tested"}, "DEVICE_STATE", "Repair or rework a device inside its open isolation")
        r.require(a["method"] in device["repair_methods"], "REPAIR_METHOD", "Repair method does not address this device fault")
        elapsed = _work_time(r, isolation, a["device_id"], device["duration"])
        if a["method"] == "replace":
            part = p["parts"].get(a["part_id"])
            r.require(part and part["compatible_type"] == device["type"] and part["quantity"] > 0, "PART_STOCK", "Select a compatible stocked spare")
            part["quantity"] -= 1
        else:
            r.require(a["part_id"] == "", "REPAIR_METHOD", "Recalibration does not consume a spare")
        gain = device["repair_methods"][a["method"]]
        previous = device.get("repair_id")
        output = r.emit("repair", {**a, "gain": gain, "device_revision": device["revision"],
            "replaces_repair_id": previous, "work_hours": device["duration"], "elapsed_device_hours": elapsed},
            [a["isolation_id"], *([previous] if previous else [])])
        isolation["work_hours"][a["device_id"]] = elapsed
        device.update(status="repaired", repair_id=output["artifact_id"])
        device.pop("test_id", None)
        return output
    if name in {"test-device", "retest-device"}:
        device = _device(r, a["device_id"])
        repair = r.artifact(a["repair_id"], "repair")
        r.require(repair["data"]["device_id"] == a["device_id"] and device.get("repair_id") == a["repair_id"],
                  "OBJECT_MISMATCH", "Repair belongs to a different device or repair cycle")
        r.require(device["status"] in ({"isolated", "repaired", "tested"} if name == "retest-device" else {"repaired", "tested"}) and repair["data"]["device_revision"] == device["revision"],
                  "STALE_ARTIFACT", "Device must have a current completed repair")
        r.require(device["minimum_test_load"] <= a["test_load"] <= 100, "TEST_LOAD", "Use a qualifying load between the public minimum and 100")
        isolation_id = a["isolation_id"] if name == "retest-device" else repair["data"]["isolation_id"]
        isolation = p["isolations"][isolation_id]
        r.require(isolation["open"] and a["device_id"] in isolation["device_ids"], "ISOLATION_REQUIRED", "Test inside the selected open isolation")
        elapsed = _work_time(r, isolation, a["device_id"], 1)
        extra = {}
        rated = device["rated_pressure"]
        if "network" in p:
            current_plan = _plan(r, {k: isolation[k] for k in r.skills["plan-isolation"][0]})
            r.require(current_plan["supply_revision"] == isolation["supply_revision"] and
                      current_plan["inlet_pressure"] == isolation["inlet_pressure"], "STALE_ARTIFACT", "Replan the isolation for changed upstream conditions")
            rated = min(rated, current_plan["inlet_pressure"])
            extra = {"supply_revision": p["network"]["revision"], "inlet_pressure": current_plan["inlet_pressure"]}
        measured = r.arithmetic("test-device", {"severity": device["severity"], "repair_gain": repair["data"]["gain"],
            "load": a["test_load"], "rated_pressure": rated}, use_script)
        passed = (measured["residual"] <= device["maximum_residual"] and
                  measured["delivered_pressure"] >= device["minimum_pressure"])
        output = r.emit("test", {**a, **measured, **extra, "isolation_id": isolation_id, "device_revision": device["revision"], "passed": passed,
            "work_hours": 1, "elapsed_device_hours": elapsed}, list(dict.fromkeys([a["repair_id"], isolation_id])))
        isolation["work_hours"][a["device_id"]] = elapsed
        device.update(status="tested", test_id=output["artifact_id"])
        return output
    if name == "restore-group":
        isolation = p["isolations"].get(a["isolation_id"])
        r.require(isolation and isolation["open"], "ISOLATION_REQUIRED", "Select an open isolation")
        tests = [r.artifact(v, "test") for v in a["test_ids"]]
        r.require(len(tests) == len(isolation["device_ids"]) and
                  {t["data"]["device_id"] for t in tests} == set(isolation["device_ids"]), "TEST_COVERAGE", "Supply one test for every isolated device")
        r.require(all(_device(r, t["data"]["device_id"]).get("test_id") == t["artifact_id"] and
                      _device(r, t["data"]["device_id"])["status"] == "tested" for t in tests), "STALE_ARTIFACT", "Tests must belong to the current repair cycle")
        r.require(all(_passing_current_test(r, t) for t in tests), "TEST_NOT_PASSING",
                  "Current measurements do not meet acceptance limits; inspect the reports and rework before restoration")
        output = r.emit("restoration", {"device_ids": isolation["device_ids"], "test_ids": a["test_ids"],
            "delivered_pressure": min(t["data"]["delivered_pressure"] for t in tests), "window_end": isolation["end"],
            **({"supply_revision": p["network"]["revision"]} if "network" in p else {})},
            [a["isolation_id"], *a["test_ids"]])
        isolation["open"] = False
        for d in isolation["device_ids"]:
            _device(r, d).update(status="restored", restoration_id=output["artifact_id"])
        return output
    if name == "accept-system":
        restorations = [r.artifact(v, "restoration") for v in a["restoration_ids"]]
        covered = [d for v in restorations for d in v["data"]["device_ids"]]
        r.require(len(covered) == len(p["devices"]) and set(covered) == set(p["devices"]), "SYSTEM_COVERAGE", "Cover every required device exactly once")
        r.require(all(_device(r, d).get("restoration_id") == v["artifact_id"]
                      for v in restorations for d in v["data"]["device_ids"]),
                  "STALE_ARTIFACT", "Accept only each device's current restoration record")
        cases, healthy = evaluate(r, require_acceptance=False)
        r.require(healthy, "SYSTEM_CONTENT", "Equipment tests, repair stock or isolation state do not satisfy the public goal")
        output = r.emit("acceptance", {"device_ids": sorted(covered), "restoration_ids": a["restoration_ids"]}, a["restoration_ids"])
        p["acceptance"] = output["artifact_id"]
        return output
    raise ValueError("Unknown FieldOps operation")


def check_reservation(r, data):
    allowed = {'pending', 'needs_revalidation'}
    if r.project.get('service_rules', {}).get('allow_restored_rework'):
        allowed.add('restored')
    r.require(all(_device(r, d)['status'] in allowed for d in data['device_ids']), 'DEVICE_STATE',
              'Select devices eligible for isolation under the public maintenance policy')
    for reservation in r.project["isolations"].values():
        overlap = max(data["start"], reservation["start"]) < min(data["end"], reservation["end"])
        shared = data["crew_id"] == reservation["crew_id"] or bool(set(data["boundary_ids"]) & set(reservation["boundary_ids"]))
        r.require(not (overlap and shared), "RESOURCE_CONFLICT", "An overlapping isolation holds the crew or boundary")


def _passing_current_test(r, artifact):
    test = artifact["data"]
    device = _device(r, test["device_id"])
    repair = r.artifact(test["repair_id"], "repair")["data"]
    residual = max(0, device["severity"] - device["repair_methods"][repair["method"]]) + test["test_load"] // 25
    rated = device["rated_pressure"]
    if "network" in r.project:
        isolation = r.project["isolations"][test["isolation_id"]]
        upstream = [r.artifact(v, "restoration") for v in isolation["upstream_restoration_ids"]]
        expected = {key for d in isolation["device_ids"] for key in _device(r, d)["predecessors"]}
        if {key for v in upstream for key in v["data"]["device_ids"]} != expected:
            return False
        if (test.get("supply_revision") != r.project["network"]["revision"] or
                any(v["data"].get("supply_revision") != r.project["network"]["revision"] or
                    any(_device(r, d).get("restoration_id") != v["artifact_id"] or _device(r, d)["status"] != "restored"
                        for d in v["data"]["device_ids"]) for v in upstream)):
            return False
        inlet = min(v["data"]["delivered_pressure"] for v in upstream) if upstream else r.project["network"]["supply_pressure"]
        if "services" in r.project:
            from .services import inlet_from_records
            stage = device["stage"]
            parents = r.project["services"][str(stage)]["parent_stages"]
            if (set(isolation["upstream_service_ids"]) != {r.project["services"][str(s)]["current_id"] for s in parents} or
                    not all(r.project["services"][str(s)]["active"] for s in parents)):
                return False
            inlet = inlet_from_records(r.project, stage, isolation["upstream_service_ids"])
        if test.get("inlet_pressure") != inlet:
            return False
        rated = min(rated, inlet)
    return (test["passed"] is True and device.get("test_id") == artifact["artifact_id"] and
            test["repair_id"] == device.get("repair_id") and repair["device_id"] == test["device_id"] and
            test["device_revision"] == repair["device_revision"] == device["revision"] and
            device["minimum_test_load"] <= test["test_load"] <= 100 and
            test["residual"] == residual <= device["maximum_residual"] and
            test["delivered_pressure"] == rated - residual >= device["minimum_pressure"])


def evaluate(r, require_acceptance=True):
    p = r.project
    cases = {}
    for identifier, device in p["devices"].items():
        good = False
        if device["status"] == "restored":
            test = r.artifact(device["test_id"], "test")
            good = test["data"]["device_id"] == identifier and _passing_current_test(r, test)
        cases[identifier] = {"success": good}
    healthy = all(c["success"] for c in cases.values()) and not any(v["open"] for v in p["isolations"].values())
    reservations = list(p["isolations"].items())
    for i, (isolation_id, reservation) in enumerate(reservations):
        for device_id in reservation["device_ids"]:
            repairs = {a["artifact_id"]: a for a in p["artifacts"].values() if a["kind"] == "repair"
                       and a["data"]["isolation_id"] == isolation_id
                       and a["data"]["device_id"] == device_id}
            work = sum(_device(r, device_id)["duration"] for _ in repairs)
            work += sum(a["kind"] == "test" and a["data"].get("isolation_id") == isolation_id and
                        a["data"]["device_id"] == device_id for a in p["artifacts"].values())
            healthy &= reservation["work_hours"][device_id] == work <= reservation["end"] - reservation["start"]
        boundary = {b for d in reservation["device_ids"] for b in _device(r, d)["boundaries"]}
        closure = {d for d, value in p["devices"].items() if boundary & set(value["boundaries"])}
        window = p["windows"][reservation["window_id"]]
        healthy &= (boundary == set(reservation["boundary_ids"]) and closure == set(reservation["device_ids"]) and
                    window["start"] == reservation["start"] and window["end"] == reservation["end"] and
                    p["crews"][reservation["crew_id"]]["qualified"])
        if "network" in p:
            upstream = [r.artifact(key, "restoration")["data"] for key in reservation["upstream_restoration_ids"]]
            expected = {key for d in reservation["device_ids"] for key in _device(r, d)["predecessors"]}
            inlet = min(v["delivered_pressure"] for v in upstream) if upstream else p["network"]["supply_history"][str(reservation["supply_revision"])]
            if "services" in p:
                records = [p["artifacts"][key]["data"] for key in reservation["upstream_service_ids"]]
                stage = p["devices"][reservation["device_ids"][0]]["stage"]
                feeds = p["services"][str(stage)].get("remote_feed_stages", [stage-p["service_rules"]["remote_feed_gap"]])
                remote = [v for v in records if v["stage"] in feeds]
                inlet = (min(v["return_pressure"] for v in records) if records else
                         p["network"]["supply_history"][str(reservation["supply_revision"])]) - sum(v["flow_units"]//2 for v in remote)
                healthy &= (sorted(v["stage"] for v in records) == p["services"][str(stage)]["parent_stages"] and
                            all(v["supply_revision"] == reservation["supply_revision"] and
                                v["effective_from"] <= reservation["start"] for v in records))
            healthy &= (expected == {key for v in upstream for key in v["device_ids"]} and
                        reservation["start"] >= p["network"]["effective_history"][str(reservation["supply_revision"])] and
                        all(v["window_end"] <= reservation["start"] and v["supply_revision"] == reservation["supply_revision"] for v in upstream) and
                        reservation["inlet_pressure"] == inlet)
        for _, other in reservations[:i]:
            overlap = max(reservation["start"], other["start"]) < min(reservation["end"], other["end"])
            shared = reservation["crew_id"] == other["crew_id"] or bool(set(reservation["boundary_ids"]) & set(other["boundary_ids"]))
            healthy &= not (overlap and shared)
    for artifact in p["artifacts"].values():
        if artifact["kind"] == "test" and "network" in p:
            test = artifact["data"]
            repair = r.artifact(test["repair_id"], "repair")["data"]
            isolation = p["isolations"][test["isolation_id"]]
            device = _device(r, test["device_id"])
            if "physical_history" in p:
                device = p["physical_history"][test["device_id"]][str(test["device_revision"])]
                healthy &= repair["device_revision"] == test["device_revision"]
            residual = max(0, device["severity"] - device["repair_methods"][repair["method"]]) + test["test_load"] // 25
            pressure = min(device["rated_pressure"], isolation["inlet_pressure"]) - residual
            healthy &= (test["inlet_pressure"] == isolation["inlet_pressure"] and
                        test["supply_revision"] == isolation["supply_revision"] and
                        repair["device_id"] == test["device_id"] and test["device_id"] in isolation["device_ids"] and
                        test["residual"] == residual and test["delivered_pressure"] == pressure and
                        test["passed"] == (residual <= device["maximum_residual"] and pressure >= device["minimum_pressure"]))
        if artifact["kind"] == "restoration":
            tests = [r.artifact(v, "test")["data"] for v in artifact["data"]["test_ids"]]
            healthy &= artifact["data"]["delivered_pressure"] == min(t["delivered_pressure"] for t in tests)
    for part_id, part in p["parts"].items():
        used = sum(a["kind"] == "repair" and a["data"]["part_id"] == part_id for a in p["artifacts"].values())
        healthy &= part["quantity"] == part["initial_quantity"] - used >= 0
    if "services" in p:
        from .services import verify
        healthy &= verify(p)
    if require_acceptance:
        if not p["acceptance"]:
            return cases, False
        accepted = r.artifact(p["acceptance"], "acceptance")["data"]
        healthy &= accepted["device_ids"] == sorted(p["devices"])
        covered = [d for value in accepted["restoration_ids"] for d in r.artifact(value, "restoration")["data"]["device_ids"]]
        healthy &= len(covered) == len(p["devices"]) and set(covered) == set(p["devices"])
        for value in accepted["restoration_ids"]:
            restoration = r.artifact(value, "restoration")
            healthy &= all(_device(r, d).get("restoration_id") == value for d in restoration["data"]["device_ids"])
    return cases, bool(healthy)
