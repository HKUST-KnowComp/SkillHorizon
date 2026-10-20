"""Zero-prefix credit reference using only remembered public calls and responses.

The step cache contains observations, not fabricated read_object records. A
source-change notice invalidates cached freshness, which is re-observed through
the existing public current-artifact search before any output is reused.
"""

from copy import deepcopy
import json

from .public_witness import PublicCreditWitness, _PublicSourceChanged


REFERENCE_POLICY = "public_state_cache_v1"


class CachedPublicCreditWitness(PublicCreditWitness):
    def __init__(self, invoke, **options):
        super().__init__(invoke, **options)
        if self.skill_protocol != "skill_declaration_v1":
            raise ValueError("Cached reference requires explicit run-free steps")
        self.observed_steps = {}
        self.collection_cache = {}
        self.object_cache = {}
        self.table_cache = {}
        self.dirty_sources = set()
        self.freshness_unknown = False
        self.first_project = True
        self.cache_stats = {"step_reuses": 0, "object_reuses": 0, "table_reuses": 0,
                            "change_notifications": 0, "freshness_refreshes": 0,
                            "stale_steps_discarded": 0}

    def invalidate(self, notices=None):
        self.freshness_unknown = True
        self.object_cache.clear()
        if notices and all(isinstance(n, dict) and isinstance(n.get("source_id"), str)
                           for n in notices):
            self.dirty_sources.update(n["source_id"] for n in notices)
        else:
            # No reliable public scope: rediscover metadata instead of guessing.
            self.collection_cache.clear()

    async def call(self, name, **arguments):
        key = json.dumps(arguments, sort_keys=True)
        if name == "read_object" and key in self.object_cache:
            self.cache_stats["object_reuses"] += 1
            return deepcopy(self.object_cache[key])
        try:
            result = await super().call(name, **arguments)
        except _PublicSourceChanged:
            self.invalidate()
            raise
        if name == "read_project" and self.first_project:
            self.require(result["collections"]["artifacts"] == 0,
                         "Cached reference requires a zero-prefix initial world")
            self.first_project = False
        if result.get("notices"):
            self.cache_stats["change_notifications"] += len(result["notices"])
            self.invalidate(result["notices"])
        if result.get("current") is False and not result.get("notices"):
            self.invalidate()
        if name == "read_object":
            self.object_cache[key] = deepcopy(result)
        return result

    async def objects(self, collection):
        if collection == "artifacts":
            # The inherited loop imports old metadata for declaration_reusable.
            # This zero-prefix policy instead retains its own observed steps;
            # it has no imported full artifact records to expose to that loop.
            return {}
        if collection not in self.collection_cache:
            if collection == "sources":
                # Standard sources only need their public kind/identity here.
                # External order normalization additionally needs business_schema.
                summaries = await self.pages("search_objects", "items", collection=collection,
                                             text="", filters=[])
                values = {}
                for summary in summaries:
                    identifier = summary["id"]
                    self.require(identifier not in values, "Duplicate public source")
                    values[identifier] = (await self.call("read_object", collection=collection,
                        object_id=identifier))["object"] if summary["kind"] == "external_orders" else summary
                self.collection_cache[collection] = values
                self.dirty_sources.clear()
            else:
                self.collection_cache[collection] = await super().objects(collection)
        elif collection == "sources":
            for identifier in sorted(self.dirty_sources):
                self.collection_cache[collection][identifier] = (
                    await self.call("read_object", collection=collection, object_id=identifier))["object"]
            self.dirty_sources.clear()
        return deepcopy(self.collection_cache[collection])

    async def refresh_freshness(self):
        if not self.freshness_unknown:
            return
        current = {item["id"] for item in await self.pages(
            "search_objects", "items", collection="artifacts", text="",
            filters=[{"field": "status", "value": "current"}])}
        for record in self.observed_steps.values():
            usable = record["receipt"]["artifact_id"] in current
            if record["usable"] and not usable:
                self.cache_stats["stale_steps_discarded"] += 1
            record["usable"] = usable
        self.table_cache = {key: rows for key, rows in self.table_cache.items() if key in current}
        self.freshness_unknown = False
        self.cache_stats["freshness_refreshes"] += 1

    async def rows(self, identifier):
        await self.refresh_freshness()
        if identifier in self.table_cache:
            self.cache_stats["table_reuses"] += 1
            return deepcopy(self.table_cache[identifier])
        rows = await super().rows(identifier)
        self.table_cache[identifier] = deepcopy(rows)
        return rows

    async def ensure(self, name, arguments, steps=None):
        return await self.skill(name, arguments, steps)

    async def skill(self, name, arguments, steps=None):
        if name not in self.instructions:
            content = (await self.call("read_skill_file", skill_id=name, path="SKILL.md"))["content"]
            self.require(isinstance(content, str) and bool(content.strip()), "Missing public procedure")
            self.instructions[name] = content
        scripted = "Optional scripts/execute.py" in self.instructions[name]
        if scripted and name not in self.script_files:
            content = (await self.call("read_skill_file", skill_id=name, path="scripts/execute.py"))["content"]
            self.require(isinstance(content, str) and bool(content.strip()), "Missing installed script")
            self.script_files.add(name)
        await self.refresh_freshness()
        previous = None
        for step, bindings in steps or [("compute", arguments)]:
            actual = bindings(previous) if callable(bindings) else bindings
            key = json.dumps([name, step, actual], sort_keys=True)
            observed = self.observed_steps.get(key)
            if observed is not None and observed["usable"]:
                previous = deepcopy(observed["receipt"])
                self.cache_stats["step_reuses"] += 1
                continue
            tool = "run_skill_script" if scripted and (
                step == "compute" or (name == "join-orders" and step == "convert")) else "execute_step"
            previous = await self.call(tool, skill_id=name, step_id=step, arguments=actual)
            self.require(isinstance(previous.get("artifact_id"), str) and bool(previous["artifact_id"]),
                         "Missing committed artifact receipt")
            self.observed_steps[key] = {"receipt": deepcopy(previous),
                                        "usable": previous.get("current") is True}
            if previous.get("current") is True and previous.get("rows_truncated") is False:
                rows = previous.get("rows")
                if isinstance(rows, list) and len(rows) == previous.get("row_count"):
                    self.table_cache[previous["artifact_id"]] = deepcopy(rows)
            if previous.get("notices") or previous.get("current") is False:
                # Keep the actual committed receipt. The next attempt refreshes
                # currentness publicly before reusing it or its predecessors.
                raise _PublicSourceChanged()
            self.require(previous.get("current") is True, "Missing public freshness")
        return previous["artifact_id"]
