"""Cut-local source views reusing existing pure operations, not another reducer."""

from .table_ops import compute
from .verification import obligation_accounting_from_sources


class UnsupportedSource(ValueError):
    """A source shape outside the explicitly implemented claim semantics."""


class ClaimSources:
    def __init__(self, runtime):
        self.runtime = runtime
        self.prepared = {}
        self.regions = {}
        self._accounting = None

    def rows(self, entity):
        root = self.runtime.root.resolve()
        if not (root / entity["path"]).resolve().is_relative_to(root):
            raise ValueError("Claim evidence path escapes replay root")
        return self.runtime._rows(entity)

    def source(self, identifier):
        if identifier not in self.prepared:
            source = self.runtime.project["sources"][identifier]
            kind = source["kind"]
            if kind == "external_orders":
                schema = source.get("business_schema")
                if not isinstance(schema, dict) or set(schema) != {
                        "column_map", "amount_unit", "key_map_source_id", "conflict_policy"}:
                    raise UnsupportedSource("unsupported_external_schema")
                mapping = self.runtime.project["sources"][schema["key_map_source_id"]]
                if mapping["kind"] != "identity_map" or mapping["region"] != source["region"]:
                    raise UnsupportedSource("unsupported_identity_scope")
                rows = compute("harmonize-orders", [self.rows(source), self.rows(mapping)], schema)
            elif kind in {"orders", "consent", "rates"}:
                operation = {"orders": "normalize-orders", "consent": "prepare-consent", "rates": "prepare-rates"}[kind]
                rows = compute(operation, [self.rows(source)], {"deduplication": "first"} if kind == "orders" else {})
            else:
                raise UnsupportedSource("unsupported_source_kind")
            self.prepared[identifier] = rows
        return self.prepared[identifier]

    def package(self, region, period):
        matches = [p for p in self.runtime.project["work_packages"] if (p["region"], p["period"]) == (region, period)]
        if len(matches) != 1:
            raise UnsupportedSource("ambiguous_work_package")
        return matches[0]

    def regional(self, region, period):
        key = region, period
        if key not in self.regions:
            package = self.package(*key)
            orders = self.source(package["orders_source"])
            if any((row["region"], row["period"]) != key for row in orders):
                raise UnsupportedSource("source_outside_work_package")
            consent = self.source(package["consent_source"])
            rates = self.source(package["rates_source"])
            authorized = compute("authorize", [orders, consent], {"join_keys": ["customer_id", "region"]})
            joined = compute("convert", [authorized, rates], {"rounding": "half_up"})
            summary = compute("summarize-region", [joined], {"region": region, "period": period})
            self.regions[key] = {"orders": orders, "authorized": authorized, "joined": joined,
                                 "reconciliation": joined, "summary": summary}
        return self.regions[key]

    def accounting(self):
        if self._accounting is None:
            for source in self.runtime.project["sources"].values():
                root = self.runtime.root.resolve()
                if not (root / source["path"]).resolve().is_relative_to(root):
                    raise ValueError("Claim source path escapes replay root")
            self._accounting = obligation_accounting_from_sources(self.runtime.root, self.runtime.project)
        return self._accounting
