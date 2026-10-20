"""Business collections are unordered; procedural sequences are not."""

from ..util import canonical_json


SET_FIELDS = {
    "summary_ids", "rollup_ids", "audit_ids", "device_ids", "boundary_ids",
    "upstream_restoration_ids", "test_ids", "restoration_ids", "receipt_ids",
    "allocations", "lines", "quantities", "join_keys", "group_by", "required_periods",
}


def business_arguments_equal(left, right):
    def normalize(value, field=None):
        if isinstance(value, dict):
            return {key: normalize(item, key) for key, item in value.items()}
        if isinstance(value, list):
            items = [normalize(item) for item in value]
            return sorted(items, key=canonical_json) if field in SET_FIELDS else items
        return value
    return normalize(left) == normalize(right)
