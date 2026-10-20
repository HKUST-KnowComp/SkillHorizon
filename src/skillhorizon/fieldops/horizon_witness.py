"""Programmed public-tool policy for fresh horizon worlds, not a model baseline."""
from copy import deepcopy


class ReferenceCallLimit(ValueError):
    code = "REFERENCE_CALL_LIMIT"


class PublicBusinessRejection(ValueError):
    code = "PUBLIC_BUSINESS_REJECTION"


class HorizonWitness:
    def __init__(self, invoke, *, mode="procedural", scripts=True, alternate=False, supplier_preference="price",
                 skill_protocol=None, max_calls=None):
        if skill_protocol not in (None, "skill_declaration_v1"):
            raise ValueError("Unknown horizon witness skill protocol")
        if max_calls is not None and (type(max_calls) is not int or max_calls <= 0):
            raise ValueError("max_calls must be a positive integer")
        if skill_protocol == "skill_declaration_v1" and (mode != "procedural" or max_calls is None):
            raise ValueError("Declaration witness requires procedural steps and a finite max_calls")
        if supplier_preference not in {"price", "grade"}:
            raise ValueError("Unknown public supplier preference")
        self.skill_protocol, self.max_calls, self.call_count = skill_protocol, max_calls, 0
        self.supplier_preference = supplier_preference
        self.invoke, self.mode, self.scripts, self.alternate = invoke, mode, scripts, alternate
        self.calls, self.stages, self.read = [], [], set()
        self.artifacts = {}
        self.closed_orders = {}
        self.materials, self.material_used, self.conditioned, self.conditioning_records = {}, {}, {}, {}
        self.assembly_rules = None
        self.service_rules = None
        self.network_effective_from = None

    async def call(self, name, arguments=None):
        args = arguments or {}
        if self.max_calls is not None and self.call_count >= self.max_calls:
            raise ReferenceCallLimit("Public horizon witness call budget exhausted")
        # Count actual invoke attempts, including reads, business rejection and
        # transport exceptions. calls retains only responses actually received.
        self.call_count += 1
        result = await self.invoke(name, args)
        self.calls.append({"skill_id": name, "arguments": deepcopy(args), "result": deepcopy(result)})
        if not result["ok"]:
            raise PublicBusinessRejection(f"Public horizon witness rejected at {name}: {result}")
        return result

    async def objects(self, collection):
        result, offset = {}, 0
        while offset is not None:
            page = await self.call("search_objects", dict(collection=collection, text="", filters=[], offset=offset, limit=200))
            for item in page["items"]:
                key = item["id"]
                if collection == "orders" and key in self.closed_orders:
                    result[key] = deepcopy(self.closed_orders[key])
                    continue
                result[key] = (await self.call("read_object", dict(collection=collection, object_id=key)))["object"]
                # Fully received/canceled orders cannot be changed by any legal business action.
                if (collection == "orders" and result[key].get("lines") and all(
                        row["quantity"] == row["received"] + row["canceled"] for row in result[key]["lines"])):
                    self.closed_orders[key] = deepcopy(result[key])
            offset = page["next_offset"]
        return result

    async def artifact(self, key):
        if key not in self.artifacts:
            self.artifacts[key] = await self.call("read_artifact", {"artifact_id": key})
        return self.artifacts[key]

    async def run_skill(self, name, args):
        start = len(self.calls)
        if self.mode == "macro":
            output = await self.call(name, args)
        else:
            if name not in self.read:
                await self.call("read_skill_file", {"skill_id": name, "path": "SKILL.md"})
                self.read.add(name)
            if self.skill_protocol == "skill_declaration_v1":
                if name == "plan-isolation":
                    draft = await self.call("execute_step", dict(skill_id=name, step_id="draft", arguments=args))
                    check = await self.call("execute_step", dict(skill_id=name, step_id="check",
                                            arguments={"draft_id": draft["artifact_id"]}))
                    output = await self.call("execute_step", dict(skill_id=name, step_id="submit", arguments={
                        "draft_id": draft["artifact_id"], "check_id": check["artifact_id"]}))
                else:
                    tool = "run_skill_script" if self.scripts and name == "test-device" else "execute_step"
                    output = await self.call(tool, dict(skill_id=name, step_id="compute", arguments=args))
            else:
                run = (await self.call("begin_skill", {"skill_id": name, "arguments": args}))["run_id"]
                if name in {"plan-isolation", "plan-purchase"}:
                    draft = await self.call("execute_step", dict(run_id=run, step_id="draft", arguments=args))
                    check = (await self.call("run_skill_script", {"run_id": run}) if self.scripts and name == "plan-purchase" else
                             await self.call("execute_step", dict(run_id=run, step_id="check", arguments={"draft_id": draft["artifact_id"]})))
                    output = await self.call("execute_step", dict(run_id=run, step_id="submit", arguments={
                        "draft_id": draft["artifact_id"], "check_id": check["artifact_id"]}))
                elif self.scripts and name == "test-device":
                    output = await self.call("run_skill_script", {"run_id": run})
                else:
                    output = await self.call("execute_step", dict(run_id=run, step_id="compute", arguments=args))
                await self.call("finish_skill", {"run_id": run})
        self.artifacts[output["artifact_id"]] = output
        for notice in output.get("notices", []):
            if notice.get("type") == "supply_pressure_changed":
                self.network_effective_from = notice["effective_from"]
        self.stages.append({"skill_id": name, "arguments": deepcopy(args), "artifact_id": output["artifact_id"],
                            "action_start": start, "action_end": len(self.calls)})
        if self.skill_protocol == "skill_declaration_v1":
            self.stages[-1]["measurement"] = "public_policy_business_stage_not_declaration_run"
        return output

    async def run(self):
        project = await self.call("read_project")
        if self.skill_protocol == "skill_declaration_v1" and (
                project.get("domain") != "fieldops" or project.get("business_program") != "pressure_branches_v3"):
            raise ValueError("Declaration witness requires public FieldOps pressure_branches_v3")
        if project["domain"] == "procureops":
            documents = {row["document_id"] for row in project["public_documents"]}
            if not {"procurement-contract", "joint-acceptance"} <= documents:
                raise ValueError("Public procurement contract documents are unavailable")
            terms = (await self.call("read_public_document", {"document_id": "procurement-contract"}))["content"]["terms"]
            await self.call("read_public_document", {"document_id": "joint-acceptance"})
            project["rules"] = {**project["rules"], **terms}
        self.assembly_rules = project["rules"].get("assembly_rules")
        self.service_rules = project["rules"].get("service_rules")
        self.network_effective_from = project["rules"].get("network", {}).get("effective_from")
        if project["business_program"] in {"pressure_network_v1", "pressure_services_v2", "pressure_branches_v3"}:
            await self.field()
        elif project["business_program"] in {"staged_contracts_v1", "assembly_contracts_v2", "assembly_network_v3"}:
            await self.procure(project["rules"]["contract"], project["rules"]["total_budget_cents"])
        else:
            raise ValueError("Horizon witness supports only the two declared fresh-world profiles")
        return await self.call("read_project")

    async def field_windows(self):
        return await self.objects("windows")

    def available_field_windows(self, windows, crews, reservations, boundary, earliest, work):
        available = []
        for window_id, window in windows.items():
            for crew_id, crew in crews.items():
                if window["start"] < earliest or not crew["qualified"] or window["end"]-window["start"] < work:
                    continue
                conflicts = any(max(window["start"], old["start"]) < min(window["end"], old["end"]) and
                    (crew_id == old["crew_id"] or set(boundary) & set(old["boundary_ids"]))
                    for old in reservations.values())
                if not conflicts:
                    available.append((window["start"], window_id, crew_id))
        return sorted(available)

    async def select_field_window(self, windows, crews, reservations, boundary, earliest, work):
        available = self.available_field_windows(windows, crews, reservations, boundary, earliest, work)
        if not available:
            raise ValueError("No public maintenance window remains")
        start, window_id, crew = available[0]
        if self.alternate:
            crew = next(v[2] for v in reversed(available) if v[:2] == (start, window_id))
        return start, window_id, crew

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
            work = max(1 if devices[key].get("repair_id") else devices[key]["duration"]+1 for key in selected)
            start, window_id, crew = await self.select_field_window(
                windows, crews, reservations, boundary, earliest, work)
            plan = await self.run_skill("plan-isolation", dict(device_ids=selected, boundary_ids=boundary,
                window_id=window_id, crew_id=crew, upstream_restoration_ids=upstream, **service_args))
            isolation = await self.run_skill("isolate-devices", {"plan_id": plan["artifact_id"]})
            reservations[isolation["artifact_id"]] = isolation["data"]
            tests = []
            for key in reversed(selected) if self.alternate else selected:
                device = devices[key]
                if device.get("repair_id"):
                    repair = device["repair_id"]
                    test = await self.run_skill("retest-device", dict(device_id=key, isolation_id=isolation["artifact_id"], repair_id=repair, test_load=50))
                else:
                    method = "recalibrate" if device["severity"] <= device["repair_methods"]["recalibrate"] else "replace"
                    part = next((k for k, p in parts.items() if p["quantity"] > 0 and p["compatible_type"] == device["type"]), "") if method == "replace" else ""
                    repair = await self.run_skill("repair-device", dict(device_id=key, isolation_id=isolation["artifact_id"], method=method, part_id=part))
                    if part:
                        parts[part]["quantity"] -= 1
                    test = await self.run_skill("test-device", dict(device_id=key, repair_id=repair["artifact_id"], test_load=50))
                if not test["data"]["passed"]:
                    raise ValueError("Public reference measurement failed; retain this construction failure")
                tests.append(test["artifact_id"])
            await self.run_skill("restore-group", dict(isolation_id=isolation["artifact_id"], test_ids=tests))
        restorations = sorted({d["restoration_id"] for d in devices.values()})
        await self.run_skill("accept-system", {"restoration_ids": restorations})

    async def procure(self, contract, total_budget_cents):
        requests = await self.objects("requests")
        total_units = sum(value["quantity"] for value in requests.values())
        allocations = [{"request_id": key, "budget_cents": total_budget_cents * value["quantity"] // total_units}
                       for key, value in requests.items()]
        allocations[-1]["budget_cents"] += total_budget_cents - sum(row["budget_cents"] for row in allocations)
        budget = await self.run_skill("allocate-budget", {"allocations": allocations})
        settlements, claims, all_receipts, used, slot_use = {}, {}, [], set(), {}
        for stage in range(1, contract["stages"] + 1):
            req = next(key for key, value in requests.items() if value["stage"] == stage)
            retentions = [key for key, data in claims.items() if data["maturity_stage"] <= stage and key not in used]
            direct_collection = self.assembly_rules and "reuse_delays_stages" in self.assembly_rules
            if direct_collection:
                if retentions and not (self.alternate and stage == contract["stages"]):
                    await self.run_skill("claim-retention", {"retention_ids": retentions})
                    used.update(retentions)
                retentions = []
            previous = settlements.get(stage-1, "")
            stage_receipts = []
            for _ in range(100):
                suppliers, orders = await self.objects("suppliers"), await self.objects("orders")
                usage = {key: sum(line["quantity"]-line["canceled"] for o in orders.values() for line in o["lines"] if line["supplier_id"] == key) for key in suppliers}
                pending = [(key, row) for key, o in orders.items() for row in o["lines"] if row["request_id"] == req and row["quantity"] > row["received"]+row["canceled"]]
                over = {key for key in suppliers if usage[key] > suppliers[key]["capacity"]}
                if over:
                    for order_id, row in pending:
                        if row["supplier_id"] in over:
                            await self.run_skill("cancel-remainder", {"order_id": order_id, "quantities": [{
                                "request_id": req, "supplier_id": row["supplier_id"], "quantity": row["quantity"]-row["received"]-row["canceled"]}]})
                    continue
                committed = sum(row["quantity"]-row["canceled"] for o in orders.values() for row in o["lines"] if row["request_id"] == req)
                needed = requests[req]["quantity"]-committed
                if needed:
                    def preference(key):
                        supplier = suppliers[key]
                        return ((-supplier.get("component_grade", 0), supplier["unit_price_cents"], key)
                                if self.supplier_preference == "grade" else (supplier["unit_price_cents"], key))
                    supplier = next(key for key in sorted(suppliers, key=preference)
                                    if key in requests[req].get("eligible_suppliers", suppliers)
                                    and suppliers[key]["capacity"]-usage[key] >= needed)
                    quantity = max(1, needed//2) if self.alternate and needed > 1 else needed
                    plan = await self.run_skill("plan-purchase", {"budget_id": budget["artifact_id"],
                        "lines": [{"request_id": req, "supplier_id": supplier, "quantity": quantity}],
                        "previous_settlement_id": previous, "retention_ids": retentions})
                    await self.run_skill("place-orders", {"plan_id": plan["artifact_id"]})
                    continue
                if not pending:
                    break
                order_id, row = pending[-1] if self.alternate else pending[0]
                supplier = row["supplier_id"]
                earliest = max(requests[req]["earliest_slot"], suppliers[supplier]["earliest_slot"],
                               self.artifacts[previous]["data"]["last_delivery_slot"]+1 if previous else 1)
                slot = next(i for i in range(earliest, requests[req]["delivery_deadline"]+1)
                            if slot_use.get((supplier, i), 0) < suppliers[supplier]["per_slot_capacity"])
                quantity = min(row["quantity"]-row["received"]-row["canceled"], suppliers[supplier]["per_slot_capacity"]-slot_use.get((supplier, slot), 0))
                if self.alternate:
                    quantity = min(quantity, 2)
                output = await self.run_skill("receive-shipment", {"order_id": order_id, "delivery_slot": slot,
                    "quantities": [{"request_id": req, "supplier_id": supplier, "quantity": quantity}]})
                slot_use[supplier, slot] = slot_use.get((supplier, slot), 0)+quantity
                stage_receipts.append(output["artifact_id"])
                if self.assembly_rules:
                    for row in output["data"]["quantities"]:
                        key = output["artifact_id"], row["request_id"], row["supplier_id"]
                        self.materials[key] = {"stage": stage, "quantity": row["quantity"],
                                               "grade": suppliers[row["supplier_id"]]["component_grade"]}
            else:
                raise ValueError("Public stage policy did not converge")
            kit_args = {}
            if self.assembly_rules:
                kit_args["kit_id"] = await self.assemble(stage, requests[req]["quantity"], contract["stages"])
            settled = await self.run_skill("settle-contract", {"stage": stage, "receipt_ids": stage_receipts,
                                                               "previous_settlement_id": previous, **kit_args})
            settlements[stage], claims[settled["artifact_id"]] = settled["artifact_id"], settled["data"]
            used.update(retentions)
            all_receipts.extend(stage_receipts)
        if self.assembly_rules and "reuse_delays_stages" in self.assembly_rules:
            remaining = [key for key, data in claims.items() if data["maturity_stage"] <= contract["stages"] and key not in used]
            if remaining:
                await self.run_skill("claim-retention", {"retention_ids": remaining})
        await self.run_skill("accept-deliveries", {"receipt_ids": all_receipts})

    async def assemble(self, stage, quantity, stages):
        rules = self.assembly_rules
        delays = rules.get("reuse_delays_stages", [rules.get("reuse_delay_stages")])
        required = {stage: quantity-sum(stage+delay <= stages for delay in delays)}
        for delay in delays:
            if stage > delay:
                required[stage-delay] = 1
        if "material_transfers" in rules:
            required = {stage: quantity-sum(t["quantity"] for t in rules["material_transfers"] if t["origin_stage"] == stage)}
            for transfer in rules["material_transfers"]:
                if transfer["consumer_stage"] == stage:
                    origin = transfer["origin_stage"]
                    required[origin] = required.get(origin, 0)+transfer["quantity"]
            # Public irregular joins can exceed a later stage's processing cap.
            # Process each low-grade delivery at its origin and carry the receipt
            # and certification forward; this consumes real work and cash now.
            advance = [{"receipt_id": key[0], "request_id": key[1], "supplier_id": key[2],
                        "quantity": lot["quantity"]-self.conditioned.get(key, 0)}
                       for key, lot in self.materials.items() if lot["stage"] == stage
                       and lot["grade"] < rules["minimum_grade"]
                       and lot["quantity"] > self.conditioned.get(key, 0)]
            if advance:
                await self.condition(stage, advance)
        selected, process = [], []
        for origin, count in required.items():
            for key, lot in sorted(self.materials.items(), reverse=self.alternate):
                if lot["stage"] != origin:
                    continue
                amount = min(count, lot["quantity"]-self.material_used.get(key, 0))
                if amount <= 0:
                    continue
                row = dict(receipt_id=key[0], request_id=key[1], supplier_id=key[2], quantity=amount)
                selected.append(row)
                if lot["grade"] < rules["minimum_grade"]:
                    needed = max(0, self.material_used.get(key, 0)+amount-self.conditioned.get(key, 0))
                    if needed:
                        process.append({**row, "quantity": needed})
                count -= amount
                if count == 0:
                    break
            if count:
                raise ValueError("Missing public bill-of-materials inventory")
        if process:
            await self.condition(stage, process)
        low = {(row["receipt_id"], row["request_id"], row["supplier_id"]) for row in selected
               if self.materials[row["receipt_id"], row["request_id"], row["supplier_id"]]["grade"] < rules["minimum_grade"]}
        ids = [key for key, rows in self.conditioning_records.items()
               if any((row["receipt_id"], row["request_id"], row["supplier_id"]) in low for row in rows)]
        kit = await self.run_skill("assemble-kit", {"stage": stage, "inputs": selected, "conditioning_ids": ids})
        for row in selected:
            key = row["receipt_id"], row["request_id"], row["supplier_id"]
            self.material_used[key] = self.material_used.get(key, 0)+row["quantity"]
        return kit["artifact_id"]

    async def condition(self, stage, rows):
        result = await self.run_skill("condition-components", {"stage": stage, "inputs": rows})
        self.conditioning_records[result["artifact_id"]] = rows
        for row in rows:
            key = row["receipt_id"], row["request_id"], row["supplier_id"]
            self.conditioned[key] = self.conditioned.get(key, 0)+row["quantity"]
