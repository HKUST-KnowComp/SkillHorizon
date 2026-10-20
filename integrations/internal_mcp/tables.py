"""Explicit-key state API for source tools, with immutable background tables."""

from copy import deepcopy


class TableState:
    def __init__(self, world, background, primary_keys):
        self.background = background
        self.primary_keys = primary_keys
        self.bind(world)

    def bind(self, world):
        self.world = world
        self.index = {name: {r[self.primary_keys[name]]: r for r in rows} for name, rows in world["tables"].items()}
        self.background_index = {name: {r[self.primary_keys[name]]: r for r in rows} for name, rows in self.background.items()}

    def _key(self, entity_type, id_field=None):
        key = self.primary_keys[entity_type]
        if id_field is not None and id_field != key:
            raise ValueError(f"Unexpected primary key for {entity_type}: {id_field}")
        return key

    def get_entity(self, entity_type, entity_id, id_field=None):
        self._key(entity_type, id_field)
        row = self.index[entity_type].get(entity_id)
        if row is None:
            row = self.background_index[entity_type].get(entity_id)
        return deepcopy(row)

    def list_entities(self, entity_type):
        return deepcopy(self.background[entity_type] + self.world["tables"][entity_type])

    def add_entity(self, entity_type, record, id_field=None):
        key = self._key(entity_type, id_field)
        identifier = record[key]
        if identifier in self.index[entity_type] or identifier in self.background_index[entity_type]:
            raise ValueError(f"Duplicate {entity_type} id: {identifier}")
        row = {**deepcopy(record), "task_id": self.world["task_id"]}
        self.world["tables"][entity_type].append(row)
        self.index[entity_type][identifier] = row
        return deepcopy(row)

    def update_entity(self, entity_type, entity_id, values, id_field=None):
        key = self._key(entity_type, id_field)
        if entity_id not in self.index[entity_type]:
            raise ValueError("Background mutation is forbidden")
        if key in values and values[key] != entity_id:
            raise ValueError("Cannot rewrite primary identity")
        if "task_id" in values and values["task_id"] != self.world["task_id"]:
            raise ValueError("Cannot rewrite task ownership")
        self.index[entity_type][entity_id].update(deepcopy(values))
        return deepcopy(self.index[entity_type][entity_id])
