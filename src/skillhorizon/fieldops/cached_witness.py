"""FieldOps reference cache built only from public observations and action effects."""
from .horizon_witness import HorizonWitness


class CachedFieldWitness(HorizonWitness):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.resources = {}
        self.dirty = {}

    async def objects(self, collection):
        if collection not in self.resources:
            self.resources[collection] = await super().objects(collection)
        else:
            for key in sorted(self.dirty.get(collection, ())):
                result = await self.call("read_object", {"collection": collection, "object_id": key})
                self.resources[collection][key] = result["object"]
        self.dirty[collection] = set()
        return self.resources[collection]

    def invalidate(self, collection, keys=None):
        self.dirty.setdefault(collection, set()).update(
            self.resources.get(collection, {}) if keys is None else keys)

    async def run_skill(self, name, args):
        output = await super().run_skill(name, args)
        data = output.get("data", {})
        if name in {"repair-device", "test-device", "retest-device"}:
            self.invalidate("devices", [args["device_id"]])
        if name == "repair-device" and args.get("part_id"):
            self.invalidate("parts", [args["part_id"]])
        if name in {"isolate-devices", "restore-group"}:
            self.invalidate("devices", data["device_ids"])
        if name == "commission-service":
            self.invalidate("services", [str(args["stage"])])
            for stage in data.get("invalidated_service_stages", []):
                key = str(stage)
                self.invalidate("services", [key])
                self.invalidate("devices", self.resources["services"][key]["device_ids"])
        for notice in output.get("notices", []):
            if notice.get("type") == "supply_pressure_changed":
                self.invalidate("devices")
                self.invalidate("services")
            elif notice.get("type") == "local_device_fault":
                self.invalidate("devices", notice["affected_device_ids"])
                self.invalidate("services", [str(s) for s in notice["affected_service_stages"]])
            elif notice.get("type") == "inspection_update":
                self.invalidate("devices", [notice["device_id"]])
            elif notice.get('type') == 'maintenance_reopened':
                stages=[str(s) for s in notice['affected_service_stages']]
                self.invalidate('services', stages)
                self.invalidate('devices', [d for s in stages for d in self.resources['services'][s]['device_ids']])
        return output
