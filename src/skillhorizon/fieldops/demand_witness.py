"""Public, first-feasible window discovery; no initial full window-detail scan."""
import re

from .cached_witness import CachedFieldWitness


class DemandFieldWitness(CachedFieldWitness):
    async def field_windows(self):
        self.window_candidates = None
        self.window_position = 0
        return {}

    async def select_field_window(self, windows, crews, reservations, boundary, earliest, work):
        while True:
            available = self.available_field_windows(windows, crews, reservations, boundary, earliest, work)
            if available:
                start, window_id, crew = available[0]
                if self.alternate:
                    crew = next(v[2] for v in reversed(available) if v[:2] == (start, window_id))
                return start, window_id, crew
            if self.window_candidates is None:
                # Summaries expose identity, not window times. IDs determine only
                # exploration order; every feasibility decision uses read_object.
                identifiers, offset = [], 0
                while offset is not None:
                    page = await self.call("search_objects", dict(collection="windows", text="",
                        filters=[], offset=offset, limit=200))
                    identifiers.extend(item["id"] for item in page["items"])
                    offset = page["next_offset"]
                def natural_id(key):
                    return tuple((0, int(part)) if part.isdigit() else (1, part)
                                 for part in re.split(r"(\d+)", key))
                self.window_candidates = sorted(identifiers, key=natural_id)
            if self.window_position == len(self.window_candidates):
                raise ValueError("No public maintenance window remains")
            key = self.window_candidates[self.window_position]
            self.window_position += 1
            result = await self.call("read_object", dict(collection="windows", object_id=key))
            windows[key] = result["object"]
