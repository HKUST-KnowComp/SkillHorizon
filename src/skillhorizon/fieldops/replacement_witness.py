"""Efficient public reference for heterogeneous repair and selective recovery."""
from .demand_witness import DemandFieldWitness
from .replacement_profiles import pressure_requirements, choose_method


class ReplacementWitness(DemandFieldWitness):
    async def call(self, name, arguments=None):
        result = await super().call(name, arguments)
        if name == 'read_project':
            self.supply_pressure = result['rules']['network']['supply_pressure']
        for notice in result.get('notices', []):
            if notice.get('type') == 'supply_pressure_changed':
                self.supply_pressure = notice['supply_pressure']
        return result

    async def field(self):
        windows, crews = await self.field_windows(), await self.objects("crews")
        reservations = await self.objects("isolations")
        while True:
            devices, parts = await self.objects("devices"), await self.objects("parts")
            services = await self.objects("services") if self.service_rules else {}
            remaining = [key for key, d in devices.items() if d["status"] != "restored"]
            for service in services.values():
                if not service["active"]:
                    remaining.extend(key for key in service["device_ids"] if key not in remaining)
            if not remaining:
                break
            ready = [key for key in remaining if all(devices[parent]["status"] == "restored" and
                     (not services or services[str(devices[parent]["stage"])]["active"])
                     for parent in devices[key]["predecessors"])]
            if not ready:
                raise ValueError("No dependency-ready public maintenance boundary")
            if self.alternate and self.service_rules and self.service_rules.get("topology"):
                first = min(ready, key=lambda key: (devices[key]["earliest_start"], -devices[key]["stage"], key))
            else:
                first = min(ready, key=lambda key: (devices[key]["earliest_start"], key))
            boundary = devices[first]["boundaries"]
            selected = [key for key, d in devices.items() if set(d["boundaries"]) & set(boundary)]
            if services and all(devices[key]["status"] == "restored" for key in selected):
                service = services[str(devices[first]["stage"])]
                await self.run_skill("commission-service", {"stage": service["stage"],
                    "restoration_ids": sorted({devices[key]["restoration_id"] for key in selected}),
                    "flow_units": service["flow_units"] or service["minimum_flow"]})
                continue
            predecessors = {key for d in selected for key in devices[d]["predecessors"]}
            upstream = sorted({devices[key]["restoration_id"] for key in predecessors})
            for key in upstream:
                await self.artifact(key)
            earliest = max([devices[key]["earliest_start"] for key in selected] +
                           [self.artifacts[key]["data"]["window_end"] for key in upstream])
            # Public supply-change notices carry the new effective time. Other
            # business operations do not change it; keep the observed value.
            earliest = max(earliest, self.network_effective_from)
            service_args = {}
            if services:
                service = services[str(devices[first]["stage"])]
                service_ids = [services[str(s)]["current_id"] for s in service["parent_stages"]]
                for key in service_ids:
                    await self.artifact(key)
                earliest = max([earliest]+[self.artifacts[key]["data"]["effective_from"] for key in service_ids])
                service_args["upstream_service_ids"] = service_ids
            # The public plan contract requires repair+test capacity even for retests.
            work = max(devices[key]["duration"]+1 for key in selected)
            earliest = max([earliest]+[devices[key].get("effective_from",0) for key in selected])
            start, window_id, crew = await self.select_field_window(
                windows, crews, reservations, boundary, earliest, work)
            plan = await self.run_skill("plan-isolation", dict(device_ids=selected, boundary_ids=boundary,
                window_id=window_id, crew_id=crew, upstream_restoration_ids=upstream, **service_args))
            isolation = await self.run_skill("isolate-devices", {"plan_id": plan["artifact_id"]})
            reservations[isolation["artifact_id"]] = isolation["data"]
            tests = []
            for key in reversed(selected) if self.alternate else selected:
                device = devices[key]
                current_repair = (await self.artifact(device["repair_id"]))["data"] if device.get("repair_id") else None
                if current_repair and current_repair["device_revision"] == device["revision"]:
                    repair = device["repair_id"]
                    test = await self.run_skill("retest-device", dict(device_id=key, isolation_id=isolation["artifact_id"], repair_id=repair, test_load=device["minimum_test_load"]))
                else:
                    inlet = plan["data"]["inlet_pressure"]
                    # Preserve the published design supply margin through future source corrections.
                    inlet -= max(0, self.supply_pressure-self.service_rules["design_supply_pressure"])
                    required = pressure_requirements(devices, services)[device["stage"]]
                    method, _ = choose_method(device, inlet, required)
                    part = next((k for k, p in parts.items() if p["quantity"] > 0 and p["compatible_type"] == device["type"]), "") if method == "replace" else ""
                    repair = await self.run_skill("repair-device", dict(device_id=key, isolation_id=isolation["artifact_id"], method=method, part_id=part))
                    if part:
                        parts[part]["quantity"] -= 1
                    test = await self.run_skill("test-device", dict(device_id=key, repair_id=repair["artifact_id"], test_load=device["minimum_test_load"]))
                if not test["data"]["passed"]:
                    raise ValueError("Public reference measurement failed; retain this construction failure")
                tests.append(test["artifact_id"])
            await self.run_skill("restore-group", dict(isolation_id=isolation["artifact_id"], test_ids=tests))
        restorations = sorted({d["restoration_id"] for d in devices.values()})
        await self.run_skill("accept-system", {"restoration_ids": restorations})

